#!/usr/bin/env python3
"""
channel_b_check.py  --  are the Compass source studies inside Atlas?

Compass tasks 3-8 come from three external papers. For each accession in
atlas_accession_check.SUSPECT (taken from the papers' Data availability
sections), this:

  1. confirms the project on ENA and records its secondary (ERP/SRP) ID
  2. looks it up in MGnify, by search and by direct study lookup
  3. pulls every ENA run (and its sample IDs) under the project
  4. scans Atlas's `Run Accession` column, both splits, for any of them

Step 2 is only trusted if a positive control (a study known to be in MGnify)
resolves first; otherwise the script stops rather than report a false negative.

Absence here is not evidence of absence from the corpus: Compass may use data
deposited under accessions not listed in SUSPECT.

Run:
    python channel_b_check.py > out/run_channel_b.txt
"""

from __future__ import annotations
import json
import sys
import urllib.error
import urllib.request
from collections import Counter, defaultdict

import pyarrow.parquet as pq
from huggingface_hub import HfFileSystem

from atlas_accession_check import SUSPECT

REVISION = "9dad400edb9e4f1e483a2f9021208c16f694aec2"
ATLAS = f"datasets/outpost-bio/Atlas@{REVISION}/data"
ENA = "https://www.ebi.ac.uk/ena/portal/api"
MGNIFY = "https://www.ebi.ac.uk/metagenomics/api/v1/studies"
CONTROL = ("PRJEB6456", "MGYS00001799")  # Baeckhed 2015 infant gut, known to be in MGnify


def get(url: str) -> str | None:
    try:
        with urllib.request.urlopen(url, timeout=60) as r:
            return r.read().decode()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def mgnify_ids(acc: str) -> set[str]:
    found = {s["id"] for s in json.loads(get(f"{MGNIFY}?search={acc}"))["data"]}
    direct = get(f"{MGNIFY}/{acc}")
    if direct:
        found.add(json.loads(direct)["data"]["id"])
    return found


def ena_tsv(url: str) -> list[list[str]]:
    lines = (get(url) or "").strip().splitlines()
    return [l.split("\t") for l in lines[1:]]


def main() -> None:
    ids = mgnify_ids(CONTROL[0])
    if CONTROL[1] not in ids:
        sys.exit(f"Positive control failed: {CONTROL[0]} -> {ids or 'nothing'}, expected {CONTROL[1]}. "
                 "MGnify lookup is not trustworthy; refusing to report negatives.")
    print(f"positive control: {CONTROL[0]} -> {CONTROL[1]}  OK\n")

    projects = [a for a in SUSPECT if a.startswith("PRJ")]
    want: dict[str, set[str]] = defaultdict(set)
    print(f"{'project':<14} {'secondary':<11} {'MGYS (project)':<16} {'MGYS (secondary)':<17} runs  title")
    for p in projects:
        study = ena_tsv(f"{ENA}/search?result=study&query=study_accession=%22{p}%22"
                        "&fields=study_accession,secondary_study_accession,study_title&format=tsv")
        if not study:
            sys.exit(f"{p} not found on ENA -- unconfirmed, remove it from SUSPECT")
        _, secondary, title = study[0]
        runs = ena_tsv(f"{ENA}/filereport?accession={p}&result=read_run"
                       "&fields=run_accession,sample_accession,secondary_sample_accession&format=tsv")
        for row in runs:
            for x in row:
                if x:
                    want[x].add(p)
        m1 = ",".join(sorted(mgnify_ids(p))) or "none"
        m2 = ",".join(sorted(mgnify_ids(secondary))) or "none"
        print(f"{p:<14} {secondary:<11} {m1:<16} {m2:<17} {len(runs):>5}  {title[:60]}")

    n_runs = sum(1 for x in want if x[:3] in ("SRR", "ERR", "DRR"))
    print(f"\nENA identifiers checked: {len(want):,} ({n_runs:,} runs + their sample accessions)")

    fs = HfFileSystem()
    hits: Counter = Counter()
    prefixes: Counter = Counter()
    rows = 0
    for path in sorted(fs.glob(f"{ATLAS}/*.parquet")):
        split = path.rsplit("/", 1)[1].split("-")[0]
        with fs.open(path, "rb") as f:
            for r in pq.read_table(f, columns=["Run Accession"]).column(0).to_pylist():
                rows += 1
                prefixes[(r or "<none>")[:3]] += 1
                for p in want.get(r, ()):
                    hits[(p, split)] += 1

    print(f"Atlas rows scanned (Run Accession, revision {REVISION[:7]}): {rows:,}")
    insdc = ("SRR", "ERR", "DRR", "ERZ", "SRS", "ERS")
    other = sum(v for k, v in prefixes.items() if k not in insdc)
    print("Run Accession prefixes:", ", ".join(f"{k} {prefixes[k]:,}" for k in insdc), f", other {other:,}")
    print("\nmatches in Atlas:")
    if not hits:
        print("  none -- no run or sample accession from these projects appears in either split")
    for (p, split), n in sorted(hits.items()):
        print(f"  {p} in {split}: {n:,} rows")


if __name__ == "__main__":
    main()
