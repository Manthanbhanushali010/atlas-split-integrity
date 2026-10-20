"""Harness tests on synthetic shards with known answers."""

import csv
import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

import atlas_accession_check as chk

SCHEMA = pa.schema(
    [
        ("Taxa", pa.list_(pa.string())),
        ("Relative Abundances", pa.list_(pa.float64())),
        ("Run Accession", pa.string()),
        ("Data Type", pa.large_string()),
        ("Sequencing Method", pa.large_string()),
        ("Pipeline Version", pa.large_string()),
        ("Study Accession", pa.large_string()),
    ]
)


def profile(seed: int, n: int = 25) -> tuple[list[str], list[float]]:
    taxa = [f"g__t{seed}_{i}" for i in range(n)]
    ab = [(n - i) / 1000 + seed * 1e-7 for i in range(n)]
    return taxa, ab


def row(run, study, prof, strata=("amplicon", "ILLUMINA", "v5.0")):
    taxa, ab = prof
    return {
        "Taxa": taxa,
        "Relative Abundances": ab,
        "Run Accession": run,
        "Data Type": strata[0],
        "Sequencing Method": strata[1],
        "Pipeline Version": strata[2],
        "Study Accession": study,
    }


def write(path: Path, rows, schema=SCHEMA):
    pq.write_table(pa.Table.from_pylist(rows, schema=schema), path)


@pytest.fixture
def atlas(tmp_path):
    # Study A straddles (4 pretrain, 2 benchmark); B pretrain-only; C benchmark-only.
    pre = [row(f"P{i}", "A", profile(i)) for i in range(4)]
    pre += [row(f"PB{i}", "B", profile(100 + i)) for i in range(5)]
    taxa, ab = profile(1)
    bench = [
        row("X0", "A", profile(1)),  # exact duplicate of P1, same study
        row("X1", "A", (taxa, [a * 1.01 for a in ab])),  # near duplicate of P1 (same ranking)
        row("Y0", "C", profile(101), ("metagenomic", "NOVASEQ", "v6.0")),  # exact dup of PB1, other study, new stratum
        row("Y1", "C", profile(200, n=5)),  # too short for near-dup key
    ]
    write(tmp_path / "pretrain-00000-of-00002.parquet", pre[:5])
    write(tmp_path / "pretrain-00001-of-00002.parquet", pre[5:])
    write(tmp_path / "benchmark-00000-of-00001.parquet", bench)
    return tmp_path


def test_end_to_end(atlas, tmp_path):
    out = tmp_path / "out"
    assert chk.main(["--local-dir", str(atlas), "--out", str(out)]) == 0
    s = json.loads((out / "summary.json").read_text())

    assert (s["pretrain_rows"], s["benchmark_rows"]) == (9, 4)
    assert (s["studies_pretrain"], s["studies_benchmark"], s["studies_shared"]) == (2, 2, 1)
    assert s["benchmark_rows_in_shared_study"] == 2
    assert s["benchmark_rows_in_shared_study_frac"] == 0.5
    assert s["pretrain_rows_in_shared_study"] == 4

    assert s["exact_dup_benchmark_rows"] == 2
    assert s["exact_dup_same_study"] == 1
    assert s["near_dup_benchmark_rows"] == 3  # X0, X1, Y0
    assert s["near_dup_same_study"] == 2
    assert s["near_dup_benchmark_rows_unkeyed"] == 1

    assert s["strata_benchmark_only"] == 1
    assert s["benchmark_rows_in_shared_stratum"] == 3

    with (out / "grouped_split.csv").open() as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 13
    by_study: dict[str, set[str]] = {}
    for r in rows:
        by_study.setdefault(r["study_accession"], set()).add(r["grouped_split"])
    assert all(len(v) == 1 for v in by_study.values()), "a study straddles the grouped split"

    assert list(out.glob("run_*.txt"))


def test_gate(atlas, tmp_path):
    assert chk.main(["--local-dir", str(atlas), "--out", str(tmp_path / "o1"), "--max-leak-frac", "0.6"]) == 0
    assert chk.main(["--local-dir", str(atlas), "--out", str(tmp_path / "o2"), "--max-leak-frac", "0.4"]) == 1


def test_skip_profiles(atlas, tmp_path):
    out = tmp_path / "out"
    assert chk.main(["--local-dir", str(atlas), "--out", str(out), "--skip-profiles"]) == 0
    s = json.loads((out / "summary.json").read_text())
    assert "exact_dup_benchmark_rows" not in s
    assert s["benchmark_rows_in_shared_study"] == 2


def test_schema_mismatch_fails_loudly(tmp_path, capsys):
    bad = SCHEMA.set(SCHEMA.get_field_index("Study Accession"), pa.field("Study", pa.string()))
    r = row("P0", "A", profile(0))
    r["Study"] = r.pop("Study Accession")
    write(tmp_path / "pretrain-00000-of-00001.parquet", [r], bad)
    write(tmp_path / "benchmark-00000-of-00001.parquet", [r], bad)
    assert chk.main(["--local-dir", str(tmp_path), "--out", str(tmp_path / "out")]) == 2
    assert "missing column 'Study Accession'" in capsys.readouterr().err


def test_near_key_ignores_order_and_small_abundance_noise():
    taxa, ab = profile(7)
    e1, n1 = chk.profile_keys(taxa, ab, 20)
    e2, n2 = chk.profile_keys(taxa[::-1], [a * 1.001 for a in ab[::-1]], 20)
    assert n1 == n2 and e1 != e2
    assert chk.profile_keys(taxa[::-1], ab[::-1], 20)[0] == e1
