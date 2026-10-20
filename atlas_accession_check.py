#!/usr/bin/env python3
"""Does the Atlas split leak?

Measures whether the Atlas pretrain/benchmark split is grouped by study, and
whether near-identical profiles sit on both sides of it.

  1. Study Accession overlap between the two splits
  2. Fraction of benchmark ROWS whose study also appears in pretrain (headline)
  3. Exact-duplicate profiles across splits (taxa + abundances hashed)
  4. Near-duplicate profiles: identical top-K taxa in rank order
  5. Whether provenance strata (data type / sequencing method / pipeline
     version) are shared across the split

and writes a study-grouped split assignment (grouped_split.csv) so downstream
benchmarks can be re-run without study straddling.

Shards are read one record batch at a time. Checks 1, 2 and 5 read only the
metadata columns; checks 3 and 4 also stream the taxa/abundance columns but keep
only an 8-byte hash per row. --skip-profiles limits the run to metadata.

The script fails loudly on a schema mismatch instead of guessing column names.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import random
import struct
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

import pyarrow as pa
import pyarrow.parquet as pq

REPO_ID = "outpost-bio/Atlas"
# Commit the check was written against. Pass --revision main to check a newer release.
DEFAULT_REVISION = "9dad400edb9e4f1e483a2f9021208c16f694aec2"
SPLITS = ("pretrain", "benchmark")

TAXA = "Taxa"
ABUND = "Relative Abundances"
RUN = "Run Accession"
STUDY = "Study Accession"
STRATA = ("Data Type", "Sequencing Method", "Pipeline Version")
META_COLS = (RUN, STUDY, *STRATA)
PROFILE_COLS = (TAXA, ABUND)

TOP_K = 20
GROUPED_BENCHMARK_FRAC = 0.10
SEED = 42
BATCH_SIZE = 4096


class SchemaError(Exception):
    pass


# --------------------------------------------------------------------------- schema


def _is_str(t: pa.DataType) -> bool:
    return pa.types.is_string(t) or pa.types.is_large_string(t)


def _list_of(pred: Callable[[pa.DataType], bool]) -> Callable[[pa.DataType], bool]:
    def check(t: pa.DataType) -> bool:
        return (pa.types.is_list(t) or pa.types.is_large_list(t)) and pred(t.value_type)

    return check


EXPECTED_SCHEMA: dict[str, tuple[str, Callable[[pa.DataType], bool]]] = {
    TAXA: ("list<string>", _list_of(_is_str)),
    ABUND: ("list<float64>", _list_of(pa.types.is_float64)),
    RUN: ("string", _is_str),
    STUDY: ("string", _is_str),
    **{c: ("string", _is_str) for c in STRATA},
}


def check_schema(schema: pa.Schema, source: str, columns: Iterable[str]) -> None:
    problems = []
    for col in columns:
        expected, ok = EXPECTED_SCHEMA[col]
        if col not in schema.names:
            problems.append(f"  missing column {col!r} (expected {expected})")
        elif not ok(schema.field(col).type):
            problems.append(f"  column {col!r} is {schema.field(col).type}, expected {expected}")
    if problems:
        raise SchemaError(
            f"schema mismatch in {source}:\n" + "\n".join(problems) + f"\nactual schema:\n{schema}"
        )


# --------------------------------------------------------------------------- sources

Shard = tuple[str, Callable[[], object]]


def hf_shards(split: str, revision: str) -> list[Shard]:
    from huggingface_hub import HfFileSystem

    fs = HfFileSystem()
    pattern = f"datasets/{REPO_ID}@{revision}/data/{split}-*.parquet"
    try:
        paths = sorted(fs.glob(pattern))
        if paths:  # listing is public; reading is gated, so probe one byte range now
            with fs.open(paths[0], "rb") as f:
                f.read(4)
    except Exception as e:  # gated repo, no login, network
        raise SystemExit(
            f"could not list {pattern}: {e}\n"
            f"Atlas is gated: accept the terms at https://huggingface.co/datasets/{REPO_ID} "
            "and run `hf auth login` first."
        ) from e
    return [(p, lambda p=p: fs.open(p, "rb")) for p in paths]


def local_shards(split: str, root: Path) -> list[Shard]:
    paths = sorted(root.glob(f"{split}-*.parquet")) or sorted(root.glob(f"data/{split}-*.parquet"))
    return [(str(p), lambda p=p: open(p, "rb")) for p in paths]


# --------------------------------------------------------------------------- reading


def profile_keys(taxa: list[str], abund: list[float], top_k: int) -> tuple[int | None, int | None]:
    """Exact and near-duplicate keys for one profile.

    Exact: every (taxon, abundance) pair, order-independent, zeros dropped.
    Near: the top_k taxa by abundance in rank order (ties broken by name);
    None when the profile has fewer than top_k non-zero taxa, because short
    profiles would collide trivially.
    """
    if len(taxa) != len(abund):
        raise SchemaError(f"Taxa / Relative Abundances length mismatch ({len(taxa)} vs {len(abund)})")
    pairs = [(t, a) for t, a in zip(taxa, abund) if a]
    if not pairs:
        return None, None
    h = hashlib.blake2b(digest_size=8)
    for t, a in sorted(pairs):
        h.update(t.encode())
        h.update(struct.pack("<d", a))
        h.update(b"\x1e")
    exact = int.from_bytes(h.digest(), "little")
    if len(pairs) < top_k:
        return exact, None
    top = sorted(pairs, key=lambda p: (-p[1], p[0]))[:top_k]
    near = int.from_bytes(hashlib.blake2b("\x1e".join(t for t, _ in top).encode(), digest_size=8).digest(), "little")
    return exact, near


@dataclass
class SplitData:
    run: list[str | None] = field(default_factory=list)
    study: list[str | None] = field(default_factory=list)
    strata: list[tuple] = field(default_factory=list)
    exact: list[int | None] = field(default_factory=list)
    near: list[int | None] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.run)


def load_split(name: str, shards: list[Shard], with_profiles: bool, top_k: int) -> SplitData:
    if not shards:
        raise SchemaError(f"no parquet shards found for split {name!r}")
    cols = META_COLS + (PROFILE_COLS if with_profiles else ())
    data = SplitData()
    for i, (label, opener) in enumerate(shards, 1):
        print(f"[{name}] shard {i}/{len(shards)}: {label}", file=sys.stderr)
        with opener() as f:
            pf = pq.ParquetFile(f)
            check_schema(pf.schema_arrow, label, cols)
            for batch in pf.iter_batches(batch_size=BATCH_SIZE, columns=list(cols)):
                data.run += batch.column(RUN).to_pylist()
                data.study += batch.column(STUDY).to_pylist()
                data.strata += zip(*(batch.column(c).to_pylist() for c in STRATA))
                if with_profiles:
                    for taxa, abund in zip(batch.column(TAXA).to_pylist(), batch.column(ABUND).to_pylist()):
                        e, n = profile_keys(taxa or [], abund or [], top_k)
                        data.exact.append(e)
                        data.near.append(n)
    return data


# --------------------------------------------------------------------------- analysis


def pct(n: int, d: int) -> str:
    return f"{n:,} / {d:,} ({100 * n / d:.1f}%)" if d else f"{n:,} / 0"


def study_overlap(pre: SplitData, bench: SplitData) -> dict:
    pre_n = Counter(s for s in pre.study if s is not None)
    bench_n = Counter(s for s in bench.study if s is not None)
    shared = pre_n.keys() & bench_n.keys()
    return {
        "pretrain_counts": pre_n,
        "benchmark_counts": bench_n,
        "shared": shared,
        "benchmark_rows_in_shared": sum(bench_n[s] for s in shared),
        "pretrain_rows_in_shared": sum(pre_n[s] for s in shared),
        "benchmark_rows_no_study": sum(s is None for s in bench.study),
        "pretrain_rows_no_study": sum(s is None for s in pre.study),
    }


def profile_matches(pre: SplitData, bench: SplitData, attr: str) -> list[tuple[int, int]]:
    """(benchmark_idx, first matching pretrain_idx) for every benchmark row whose key is in pretrain."""
    first: dict[int, int] = {}
    for i, k in enumerate(getattr(pre, attr)):
        if k is not None:
            first.setdefault(k, i)
    return [(j, first[k]) for j, k in enumerate(getattr(bench, attr)) if k is not None and k in first]


def grouped_split(pre: SplitData, bench: SplitData, frac: float, seed: int) -> list[tuple[str, str, str, str]]:
    """Assign whole studies to benchmark until ~frac of rows; rows with no study are their own group."""
    rows = [(r, s, "pretrain") for r, s in zip(pre.run, pre.study)]
    rows += [(r, s, "benchmark") for r, s in zip(bench.run, bench.study)]
    group = [s if s is not None else f"__no_study__:{r}:{i}" for i, (r, s, _) in enumerate(rows)]
    sizes = Counter(group)
    order = sorted(sizes)
    random.Random(seed).shuffle(order)
    target = round(frac * len(rows))
    chosen, n = set(), 0
    for g in order:
        if n + sizes[g] <= target:
            chosen.add(g)
            n += sizes[g]
    return [(r or "", s or "", orig, "benchmark" if g in chosen else "pretrain") for (r, s, orig), g in zip(rows, group)]


# --------------------------------------------------------------------------- output


def write_csv(path: Path, header: list[str], rows: Iterable[Iterable]) -> None:
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


def run(args: argparse.Namespace) -> int:
    with_profiles = not args.skip_profiles
    if args.local_dir:
        source = f"local:{args.local_dir}"
        shards = {s: local_shards(s, Path(args.local_dir)) for s in SPLITS}
    else:
        source = f"hf:{REPO_ID}@{args.revision}"
        shards = {s: hf_shards(s, args.revision) for s in SPLITS}

    pre = load_split("pretrain", shards["pretrain"], with_profiles, args.top_k)
    bench = load_split("benchmark", shards["benchmark"], with_profiles, args.top_k)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    nb, npre = len(bench), len(pre)
    summary: dict = {"source": source, "utc": stamp, "pretrain_rows": npre, "benchmark_rows": nb}
    lines = [
        "Atlas split-integrity check",
        f"source: {source}",
        f"run:    {stamp}",
        f"rows:   pretrain {npre:,} · benchmark {nb:,}",
        "",
    ]

    # 1-2. study overlap
    so = study_overlap(pre, bench)
    shared, bench_n, pre_n = so["shared"], so["benchmark_counts"], so["pretrain_counts"]
    headline = so["benchmark_rows_in_shared"]
    summary.update(
        studies_pretrain=len(pre_n),
        studies_benchmark=len(bench_n),
        studies_shared=len(shared),
        benchmark_rows_in_shared_study=headline,
        benchmark_rows_in_shared_study_frac=headline / nb if nb else None,
        pretrain_rows_in_shared_study=so["pretrain_rows_in_shared"],
        benchmark_rows_no_study=so["benchmark_rows_no_study"],
        pretrain_rows_no_study=so["pretrain_rows_no_study"],
    )
    lines += [
        "== 1. Study Accession overlap",
        f"studies: pretrain {len(pre_n):,} · benchmark {len(bench_n):,} · in both {len(shared):,}",
        f"benchmark studies also in pretrain: {pct(len(shared), len(bench_n))}",
        "",
        "== 2. HEADLINE",
        f"benchmark rows in a study also present in pretrain: {pct(headline, nb)}",
        f"pretrain rows in a study also present in benchmark: {pct(so['pretrain_rows_in_shared'], npre)}",
        f"rows with no Study Accession: benchmark {so['benchmark_rows_no_study']:,} · pretrain {so['pretrain_rows_no_study']:,}",
        "largest shared studies by benchmark rows:",
    ]
    top_shared = sorted(shared, key=lambda s: (-bench_n[s], s))[:10]
    lines += [f"  {s:<14} benchmark {bench_n[s]:>7,} · pretrain {pre_n[s]:>8,}" for s in top_shared]
    study_rows = sorted(pre_n.keys() | bench_n.keys(), key=lambda s: (-bench_n[s], -pre_n[s], s))
    write_csv(
        out / "study_overlap.csv",
        ["study_accession", "pretrain_rows", "benchmark_rows", "in_both"],
        ((s, pre_n[s], bench_n[s], int(s in shared)) for s in study_rows),
    )

    runs_shared = {r for r in pre.run if r} & {r for r in bench.run if r}
    summary["run_accessions_in_both"] = len(runs_shared)
    lines += ["", f"Run Accessions present in both splits: {len(runs_shared):,}"]

    # 3-4. profile duplicates
    if with_profiles:
        match_rows = []
        for label, attr, n_none in (
            ("3. Exact-duplicate profiles", "exact", sum(k is None for k in bench.exact)),
            (f"4. Near-duplicate profiles (identical top-{args.top_k} taxa, rank order)", "near", sum(k is None for k in bench.near)),
        ):
            m = profile_matches(pre, bench, attr)
            same = sum(pre.study[i] is not None and pre.study[i] == bench.study[j] for j, i in m)
            summary[f"{attr}_dup_benchmark_rows"] = len(m)
            summary[f"{attr}_dup_same_study"] = same
            summary[f"{attr}_dup_benchmark_rows_unkeyed"] = n_none
            lines += [
                "",
                f"== {label}",
                f"benchmark rows with a match in pretrain: {pct(len(m), nb)}",
                f"  of which same study: {same:,} · different study: {len(m) - same:,}",
            ]
            if attr == "near":
                lines.append(f"  benchmark rows not checked (<{args.top_k} non-zero taxa): {n_none:,}")
            match_rows += [
                (attr, bench.run[j], bench.study[j], pre.run[i], pre.study[i], int(pre.study[i] == bench.study[j]))
                for j, i in m
            ]
        write_csv(
            out / "profile_matches.csv",
            ["kind", "benchmark_run", "benchmark_study", "pretrain_run", "pretrain_study", "same_study"],
            match_rows,
        )
    else:
        lines += ["", "== 3-4. Profile duplicates: skipped (--skip-profiles)"]

    # 5. provenance strata
    pre_s, bench_s = Counter(pre.strata), Counter(bench.strata)
    bench_only = sorted(bench_s.keys() - pre_s.keys(), key=str)
    covered = sum(n for s, n in bench_s.items() if s in pre_s)
    summary.update(strata_pretrain=len(pre_s), strata_benchmark=len(bench_s), strata_benchmark_only=len(bench_only))
    summary["benchmark_rows_in_shared_stratum"] = covered
    lines += [
        "",
        "== 5. Provenance strata (" + " × ".join(STRATA) + ")",
        f"strata: pretrain {len(pre_s):,} · benchmark {len(bench_s):,} · benchmark-only {len(bench_only):,}",
        f"benchmark rows whose stratum also appears in pretrain: {pct(covered, nb)}",
    ]
    write_csv(
        out / "strata.csv",
        [*(c.lower().replace(" ", "_") for c in STRATA), "pretrain_rows", "benchmark_rows"],
        ((*s, pre_s[s], bench_s[s]) for s in sorted(pre_s.keys() | bench_s.keys(), key=lambda s: (-bench_s[s], str(s)))),
    )

    # grouped split
    g = grouped_split(pre, bench, GROUPED_BENCHMARK_FRAC, SEED)
    write_csv(out / "grouped_split.csv", ["run_accession", "study_accession", "original_split", "grouped_split"], g)
    gb = sum(row[3] == "benchmark" for row in g)
    summary["grouped_benchmark_rows"] = gb
    lines += [
        "",
        "== grouped_split.csv",
        f"study-grouped split (seed={SEED}, target {GROUPED_BENCHMARK_FRAC:.0%}): benchmark {pct(gb, len(g))}",
    ]

    gate_failed = False
    if args.max_leak_frac is not None and nb:
        gate_failed = headline / nb > args.max_leak_frac
        lines += ["", f"GATE: headline {headline / nb:.3f} vs max {args.max_leak_frac:.3f} -> {'FAIL' if gate_failed else 'PASS'}"]

    text = "\n".join(lines) + "\n"
    (out / f"run_{stamp}.txt").write_text(text)
    (out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(text)
    return 1 if gate_failed else 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--revision", default=DEFAULT_REVISION, help="Atlas dataset revision (default: pinned commit)")
    p.add_argument("--local-dir", help="read <split>-*.parquet from here instead of Hugging Face")
    p.add_argument("--out", default="out", help="output directory (default: out/)")
    p.add_argument("--skip-profiles", action="store_true", help="metadata-only: skip checks 3 and 4")
    p.add_argument("--top-k", type=int, default=TOP_K, help=f"taxa compared for near-duplicates (default {TOP_K})")
    p.add_argument("--max-leak-frac", type=float, help="exit 1 if the headline fraction exceeds this (regression gate)")
    args = p.parse_args(argv)
    try:
        return run(args)
    except SchemaError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
