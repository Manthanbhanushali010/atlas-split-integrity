# Atlas split-integrity check

An independent check on how the [Atlas](https://huggingface.co/datasets/outpost-bio/Atlas)
pretraining corpus is split, and whether that split leaks study-level structure into the
[Compass](https://github.com/Outpost-Bio/waypoint) benchmark.

Nobody asked for this. I ran it because "how much of this number is real" is a question
I find hard to leave alone.

## The question

The Atlas dataset card states the corpus was "randomly split into **pretrain** (90%) and
**benchmark** (10%) with `seed=42`". Every row also carries a `Study Accession`.

Microbiome samples within a single study share a DNA extraction kit, a primer set, a
sequencing run and often the same subjects. If the split is row-level rather than grouped
by study, benchmark samples have siblings in the pretraining corpus — and a pretrained
model can recognise protocol rather than biology. Some portion of a reported pretraining
gain would then be an artifact of the split.

There are two distinct channels worth separating:

**A. Within Atlas.** Compass tasks 1–2 (biome classification) use held-out MGnify samples,
i.e. Atlas's own `benchmark` split. If studies straddle the split, those tasks are affected
directly.

**B. Across corpora.** Compass tasks 3–8 come from separate published studies — Shi et al.
(2025), Mastrorilli et al. (2026), Roswall et al. (2021). Atlas was scraped from MGnify.
If those studies were also deposited in MGnify, they may have been swept into the
pretraining corpus, meaning the model was pretrained on the benchmark's own samples.

Channel B is the sharper claim and requires confirming real accessions against the papers.

## What this repo does

`atlas_accession_check.py` streams **only** the `Study Accession` column from the Atlas
parquet files over the network — not the 5.55 GB of taxa lists — and reports:

- study counts per split, and how many studies appear in both
- the fraction of **benchmark rows** whose study also appears in pretrain (the number that
  matters; a few large shared studies can cover most of the split)
- the largest straddling studies
- whether configured external accessions appear in the pretrain split (channel B)

## Usage

```bash
pip install -r requirements.txt
hf auth login          # Atlas is gated; access is auto-approved
python atlas_accession_check.py
```

To check channel B, fill the `SUSPECT` dict at the top of the script with the real
ENA/SRA accessions from the three source papers' Data Availability sections. Atlas may
store MGnify IDs (`MGYS…`) rather than ENA project IDs (`PRJEB…`), in which case map
between them first:

```
https://www.ebi.ac.uk/metagenomics/api/v1/studies?search=PRJEB12345
```

## Results

**Status:** Stage 1 complete, metadata only, 20 Oct 2026. Profile-hash checks and grouped
re-run not yet done. Raw output: [`out/run_meta.txt`](out/run_meta.txt).

**The split is row-level, not study-grouped.** 53,884 of 53,931 benchmark rows (99.9%)
belong to a study that also appears in pretrain. Of 2,603 studies with benchmark rows,
2,562 also have pretrain rows; only 41 studies (47 rows) are benchmark-only.

| | rows | studies |
|---|---:|---:|
| pretrain | 485,377 | 3,903 |
| benchmark | 53,931 | 2,603 |
| studies in both splits | | 2,562 |

The ten largest straddling studies show how the split was drawn:

| study | benchmark rows | pretrain rows | benchmark : pretrain |
|---|---:|---:|---:|
| MGYS00003194 | 6,219 | 56,035 | 11.1% |
| MGYS00000596 | 2,799 | 24,911 | 11.2% |
| MGYS00004712 | 749 | 6,615 | 11.3% |
| MGYS00005143 | 713 | 6,661 | 10.7% |
| MGYS00000633 | 671 | 6,228 | 10.8% |
| MGYS00003733 | 441 | 3,937 | 11.2% |
| MGYS00000604 | 427 | 3,722 | 11.5% |
| MGYS00005495 | 394 | 3,754 | 10.5% |
| MGYS00005200 | 389 | 3,689 | 10.5% |
| MGYS00001075 | 338 | 2,918 | 11.6% |

Every ratio sits close to 1:9 (11.1%). That is the signature of row-level random
assignment at 90/10: each study is divided at the global ratio rather than assigned
whole to one side. It is consistent with the dataset card's description of a random split
with `seed=42`, which is what this check set out to confirm or rule out.

`Study Accession` holds MGnify study IDs (`MGYS…`), and the schema carries a separate
`Run Accession` column, so the field is study-level, as the analysis assumes.

The row total, 485,377 + 53,931 = 539,308, matches the split sizes on the dataset card.

### What this does not show

It does not show that any published result is inflated. A shared study means benchmark
samples have technical siblings in pretraining; whether the model actually exploited that
structure is an empirical question. Answering it requires re-running the benchmark under a
study-grouped split and comparing the pretraining gain. That has not been done yet.

**Scope.** Compass tasks 1–2 use held-out MGnify samples from Atlas's own benchmark split,
so they are the tasks this result bears on directly. Tasks 3–8 come from external studies
(Shi, Mastrorilli, Roswall); whether those appear in pretraining is a separate question,
and this result does not answer it.

## Limitations — read this before quoting anything

- This measures **metadata overlap**, not model behaviour. A shared study accession is
  evidence that a split is not grouped. It is not, by itself, evidence that any reported
  score is wrong.
- Quantifying the actual effect requires re-running the Compass tasks under a
  study-grouped split and comparing the pretraining gain. That is the next step, not
  something this repo has done.
- Channel B depends entirely on accession matching being correct. An unconfirmed
  accession is a lead, not a finding.
- A clean result is a real result. If studies do not straddle the split, that is worth
  reporting as such, and the script still has standing value as a regression check on
  future Atlas releases.

## Why it is public

Outpost Bio open-sourced the model, the corpus and the benchmark. That is unusual and it
is the reason this check was possible at all. Independent verification is the thing open
releases are for.

Issues and corrections welcome — particularly if I have the split semantics wrong.

---

Manthan Bhanushali · [github.com/Manthanbhanushali010](https://github.com/Manthanbhanushali010)
