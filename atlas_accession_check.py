#!/usr/bin/env python3
"""
atlas_accession_check.py  --  the 20-minute version.

Pulls ONLY the `Study Accession` column from Atlas over the network (not the
5.55 GB of taxa lists) and answers two questions:

  A) Do studies straddle the pretrain/benchmark split?  (random row-level split)
  B) Do the external Compass benchmark studies appear in the PRETRAIN corpus?

(B) is the sharper one. Atlas was scraped from MGnify; the Compass tasks 3-8 come
from separate published studies (Shi, Mastrorilli, Roswall). If those studies were
also deposited in MGnify, they may have been swept into pretraining -- meaning the
model saw the benchmark's own samples during pretraining.

This does NOT prove anything on its own. Accessions must be confirmed against the
papers. Treat the output as a lead, not a result.

Setup (2 min):
    pip install "huggingface_hub>=0.23" "pyarrow>=14"
    hf auth login            # or: export HF_TOKEN=hf_...
    # Request access at https://huggingface.co/datasets/outpost-bio/Atlas
    # (gated, auto-approved, takes seconds)

Run:
    python atlas_accession_check.py
"""

from __future__ import annotations
import sys
from collections import Counter

try:
    import pyarrow.parquet as pq
    from huggingface_hub import HfFileSystem
except ImportError as e:
    sys.exit(f"pip install huggingface_hub pyarrow   ({e})")

REPO = "datasets/outpost-bio/Atlas"
COL = "Study Accession"

# Accessions to look for in the PRETRAIN split, taken from each paper's Data and
# code availability section and confirmed on ENA (portal API, 20 Oct 2026).
#
# MGnify form: NONE of these resolve to an MGYS study. Both
#   /api/v1/studies?search=<acc> and /api/v1/studies/<acc>
# return nothing for the PRJ* and ERP/SRP forms, while a positive control
# (PRJEB6456 -> MGYS00001799) resolves. Atlas stores MGYS IDs, so these ENA keys
# cannot match `Study Accession` by construction -- a zero here is uninformative.
# Separately, none of the 6,998 ENA runs/samples under these projects appears in
# Atlas's `Run Accession` column (either split).
#
# Absence from SUSPECT is not evidence of absence from the corpus: Compass may use
# data deposited elsewhere (e.g. prior SIC datasets the Shi paper builds on).
SUSPECT = {
    "PRJNA1297205": "Shi 2025 (Cell) - tasks 3-5 [ENA]",
    "SRP604364":    "Shi 2025 (Cell) - tasks 3-5 [SRA]",
    "PRJEB37062":   "Mastrorilli 2026 (bioRxiv) - task 6 [ENA]",
    "ERP120353":    "Mastrorilli 2026 (bioRxiv) - task 6 [ENA]",
    "PRJEB38986":   "Roswall 2021 (CHM) children - tasks 7-8 [ENA]",
    "ERP122450":    "Roswall 2021 (CHM) children - tasks 7-8 [ENA]",
    # Adult cohort (Tebani 2020) also deposited by Roswall; likely not used by Compass.
    "PRJEB38984":   "Roswall 2021 adult cohort - tasks 7-8? [ENA]",
    "ERP122448":    "Roswall 2021 adult cohort - tasks 7-8? [ENA]",
}


def main() -> None:
    fs = HfFileSystem()
    files = [f for f in fs.glob(f"{REPO}/**/*.parquet")]
    if not files:
        sys.exit(f"No parquet found under {REPO}. Accepted the gated terms? Logged in?")

    splits: dict[str, Counter] = {}
    for f in files:
        split = "benchmark" if "benchmark" in f.lower() else "pretrain"
        print(f"  reading {COL!r} from {f.split('/')[-1]} -> {split}")
        with fs.open(f, "rb") as fh:
            schema = pq.ParquetFile(fh).schema_arrow.names
            if COL not in schema:
                sys.exit(f"Column {COL!r} not in schema. Found: {schema}")
        with fs.open(f, "rb") as fh:
            tbl = pq.read_table(fh, columns=[COL])
        c = splits.setdefault(split, Counter())
        for v in tbl.column(COL).to_pylist():
            c[v if v is not None else "<missing>"] += 1

    pre = splits.get("pretrain", Counter())
    ben = splits.get("benchmark", Counter())
    shared = set(pre) & set(ben)
    ben_rows_shared = sum(ben[s] for s in shared)
    total_ben = max(sum(ben.values()), 1)

    print("\n" + "=" * 62)
    print("A)  SPLIT INTEGRITY WITHIN ATLAS")
    print("=" * 62)
    print(f"  pretrain rows / studies : {sum(pre.values()):,} / {len(pre):,}")
    print(f"  benchmark rows / studies: {sum(ben.values()):,} / {len(ben):,}")
    print(f"  studies in BOTH splits  : {len(shared):,}")
    print(f"  benchmark rows whose study is also in pretrain: "
          f"{ben_rows_shared:,} ({ben_rows_shared/total_ben:.1%})")
    if shared:
        print("\n  largest straddling studies (benchmark rows / pretrain rows):")
        for s in sorted(shared, key=lambda s: -ben[s])[:10]:
            print(f"    {s:<20} {ben[s]:>7,} / {pre[s]:>7,}")

    print("\n" + "=" * 62)
    print("B)  EXTERNAL BENCHMARK STUDIES PRESENT IN PRETRAIN?")
    print("=" * 62)
    if not SUSPECT:
        print("  No accessions configured. Fill SUSPECT{} with the real accessions")
        print("  from the Shi / Mastrorilli / Roswall papers, then re-run.")
    else:
        for acc, label in SUSPECT.items():
            n = pre.get(acc, 0)
            print(f"  {acc:<16} {label:<38} pretrain rows: {n:,}"
                  + ("   <-- PRESENT" if n else ""))

    print("\n" + "=" * 62)
    if ben_rows_shared / total_ben > 0.5:
        print("  Most benchmark rows sit in a study that is also in pretrain.")
        print("  Split is row-level, not study-grouped.")
    elif ben_rows_shared:
        print("  Partial overlap. Quantify per-task before claiming anything.")
    else:
        print("  Clean on axis A. Report it as such -- that is a real result too.")
    print("=" * 62)


if __name__ == "__main__":
    main()
