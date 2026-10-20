# Does the Atlas split leak?

**A 90-second read.** Independent check on the Waypoint / Atlas / Compass release.
Nobody asked me to do this.

**Status:** Stage 1 complete, metadata only, 20 Oct 2026. Profile-hash checks and grouped
re-run not yet done. · Raw output in [`out/run_meta.txt`](out/run_meta.txt) and
[`out/run_channel_b.txt`](out/run_channel_b.txt)

**In short:** within Atlas, the split is row-level (99.9% of benchmark rows share a study
with pretrain). For the external benchmark studies, the result is negative: none is in
MGnify, and none of their runs is in Atlas.

---

## Why I built this

I read the Senior ML Research Engineer posting and stopped on one line:

> *Set rigorous evaluation standards, accounting for batch effects, leakage,
> compositional data and correlated samples, to distinguish real results from artifacts.*

That is the job I already do, in other domains — eval harnesses with acceptance
thresholds, regression gates, deterministic scoring separated from stochastic output. I
don't come from omics and I'm not going to pretend otherwise. But "is this number
measuring biology or measuring the lab that produced it" is a data-hygiene question, not
a microbiology one.

So rather than write that I'd be good at it, I went and did it on the only system of
Outpost's I can see from outside.

---

## The question

The [Atlas dataset card](https://huggingface.co/datasets/outpost-bio/Atlas) says the
corpus was:

> *"randomly split into **pretrain** (90%) and **benchmark** (10%) with `seed=42`"*

Random at **row** level. But every row also carries a `Study Accession`.

Samples within one study share a DNA extraction kit, a primer set, a sequencing run, a
pipeline version, and often the same subjects. So if studies straddle the split, every
benchmark sample has technical siblings sitting in the pretraining corpus.

A model can then score well by recognising *protocol* rather than *biology* — and some
share of the reported pretraining gain would be an artifact of how the split was drawn,
not a property of the representation.

**I don't know that this is happening. That's the point — it's measurable, and nobody
has measured it publicly.**

---

## Three channels, different strength

**A — within Atlas.** Compass tasks 1–2 (biome classification) use held-out MGnify
samples, i.e. Atlas's own benchmark split. Directly affected if studies straddle.

**B — across corpora.** Compass tasks 3–8 come from separate published studies (Shi 2025,
Mastrorilli 2026, Roswall 2021). Atlas was scraped from MGnify. If those studies were
*also* deposited in MGnify, they may have been swept into pretraining — meaning the model
was pretrained on the benchmark's own samples. Six of eight tasks. Stronger claim,
needs accession confirmation against each paper.

**C — in the tooling.** `waypoint finetune` defaults to a random 80/10/10. One Compass
task is "identify which donor this perturbed community came from", so each donor recurs
across drug conditions — a random split puts the same donor on both sides. `split_column`
exists, so this is about what the default encourages.

---

## What I measured

Stage 1 reads single metadata columns out of the parquet shards — never the taxa lists,
which are most of the 5.55 GB.

1. `Study Accession` overlap between the two splits
2. **Fraction of benchmark ROWS whose study also appears in pretrain** ← the headline
   number. Study *count* is misleading: three large shared studies can be 2% of studies
   and 60% of rows.
3. **Channel B:** each source paper's accessions resolved against ENA and MGnify, and
   every one of their runs looked up in Atlas's `Run Accession` column.

Planned for Stage 2, not yet run:

- Exact-duplicate profiles across splits (taxa + abundances hashed)
- Near-duplicate profiles — identical top-20 taxa in rank order. Technical replicates
  rarely match to six decimals but almost always agree on their dominant taxa. This is
  the check that catches real duplication.
- Whether provenance strata (data type / sequencing method / pipeline version) are
  shared across the split
- A study-grouped split assignment, so Compass can be re-run cleanly whatever the result

The scripts **fail loudly** rather than guess: a missing column stops the run, and the
Channel B check refuses to report a negative unless a study known to be in MGnify
resolves first. If I'm going to question someone's methodology, mine shouldn't silently
compute the wrong thing.

---

## Result

### Channel A — within Atlas

**The split is row-level, not study-grouped.**

```
benchmark rows whose study is also in pretrain: 53,884 / 53,931 (99.9%)
studies: pretrain 3,903 · benchmark 2,603 · in both 2,562
```

Only 41 benchmark studies (47 rows) have no pretrain counterpart.

The tell is in the largest straddling studies. Their benchmark-to-pretrain ratios are
11.1%, 11.2%, 11.3%, 10.7%, 10.8%, 11.2%, 11.5%, 10.5%, 10.5% and 11.6% — all ≈1:9. That
is what row-level random assignment at 90/10 looks like: each study is diced at the
global ratio rather than assigned to one side. It matches the card's own description of
the split; the check confirms it rather than contradicting it.

`Study Accession` holds MGnify study IDs (`MGYS…`), with runs in a separate
`Run Accession` column, so the field is study-level.

Rows total 539,308 (485,377 + 53,931), matching the card's split sizes.

### Channel B — external benchmark studies: negative result

Accessions are from each paper's Data availability section, confirmed on ENA:

| Paper | Tasks | ENA project | ERP/SRP | MGnify | Runs |
|---|---|---|---|---|---:|
| Shi 2025, *Cell* | 3–5 | `PRJNA1297205` | `SRP604364` | none | 4,098 |
| Mastrorilli 2026, bioRxiv | 6 | `PRJEB37062` | `ERP120353` | none | 87 |
| Roswall 2021, children and mothers | 7–8 | `PRJEB38986` | `ERP122450` | none | 2,036 |
| Roswall 2021, adult cohort (Tebani 2020) | 7–8? | `PRJEB38984` | `ERP122448` | none | 777 |

- **No MGYS IDs exist for any of them**, by MGnify search or direct lookup.
- Before trusting that negative, I validated the lookup method on a study known to be in
  MGnify: `PRJEB6456` → `MGYS00001799`.
- **None of the 6,998 runs, or their sample accessions (20,226 identifiers), appears in
  Atlas's `Run Accession` column**, in either split.

Caveats:

- Absence from `SUSPECT` is not evidence of absence from the corpus.
- Shi et al. build on earlier datasets of the same stool-derived communities; Compass may
  actually use one of those deposits, which this check does not cover.
- The adult cohort's role in tasks 7–8 is unconfirmed.
- `Run Accession` is not purely runs: it also contains ~18k `ERZ` analysis IDs and ~400
  sample IDs, plus ~2k identifiers that are not ENA accessions.

### What this does not show

It does **not** show that any published result is inflated. Shared studies mean the
benchmark has technical siblings in pretraining; whether the model exploited that is
empirical, and answering it requires re-running the benchmark under a grouped split.
That has not been done yet.

**Scope.** Compass tasks 1–2 use held-out MGnify samples and are directly affected.
Tasks 3–8 come from external studies (Shi, Mastrorilli, Roswall) and are a separate
question; Channel B above answers it in the negative at the accession level only.

---

## The actual deliverable (next)

Re-run the Compass tasks under the grouped split and report one honest number:

> pretraining is worth **+X** under the published split and **+Y** when grouped by study.

If X ≈ Y, the published result is robust to this and I'll say so plainly. That outcome is
just as publishable as the other one, and the harness still has standing value as a
regression check on future Atlas releases.

---

## Limitations

- Measures metadata, not model behaviour.
- Channel B depends entirely on accession matching being correct. An unconfirmed
  accession is a lead, not a finding.
- I have not re-run Compass yet. Any claim about the size of the effect would be
  premature.
- I came at this as an outside eye on evaluation methodology, not as a microbiologist.

---

## Possible next steps, not yet done

- Linear probes on Waypoint embeddings predicting `Sequencing Method` / `Pipeline Version`
  / `Study Accession`, benchmarked against probes predicting biology. If protocol is more
  decodable than biome, the representation is partly a batch encoder — which bears on the
  cross-biome transfer result.
- A fairer classical baseline: the published comparison used raw bag-of-taxa abundances.
  Relative abundances are compositional, so CLR-transformed features with gradient
  boosting is the honest version of that comparison. Outpost flagged this gap themselves.

---

## Why it's public

Outpost open-sourced the model, the corpus and the benchmark. That's unusual, and it's
the only reason this check was possible. Independent verification is what open releases
are for.

**Corrections very welcome — especially if I've got the split semantics wrong.**

Run it yourself:

```bash
pip install -r requirements.txt
hf auth login
python atlas_accession_check.py   # Channel A
python channel_b_check.py         # Channel B
```

---

Manthan Bhanushali · [github.com/Manthanbhanushali010](https://github.com/Manthanbhanushali010)
