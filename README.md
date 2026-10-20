# Does the Atlas split leak?

**A 90-second read.** Independent check on the Waypoint / Atlas / Compass release.
Nobody asked me to do this.

**Status:** `STAGE 1 COMPLETE / IN PROGRESS — fill this in` · Raw output in [`out/`](out/)

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

Streams the parquet shards one record batch at a time — nothing close to the 5.55 GB
is ever held in memory. Checks 1, 2 and 5 read only the metadata columns; checks 3 and 4
also stream the taxa/abundance columns but keep only an 8-byte hash per row
(`--skip-profiles` gives a metadata-only run).

1. `Study Accession` overlap between the two splits
2. **Fraction of benchmark ROWS whose study also appears in pretrain** ← the headline
   number. Study *count* is misleading: three large shared studies can be 2% of studies
   and 60% of rows.
3. Exact-duplicate profiles across splits (taxa + abundances hashed)
4. Near-duplicate profiles — identical top-20 taxa in rank order. Technical replicates
   rarely match to six decimals but almost always agree on their dominant taxa. This is
   the check that catches real duplication.
5. Whether provenance strata (data type / sequencing method / pipeline version) are
   shared across the split

It also emits `grouped_split.csv` — a study-grouped assignment, so Compass can be re-run
cleanly whatever the result.

The script **fails loudly** on a schema mismatch rather than guessing at column names.
If I'm going to question someone's methodology, mine shouldn't silently compute the
wrong thing.

---

## Result

> **← FILL THIS IN. Paste the summary block from `out/run_*.txt`.**
>
> Example of the shape:
> `benchmark rows in a study also present in pretrain: 48,112 / 53,931 (89.2%)`

**What it does and does not mean**

Metadata overlap is evidence that a split is **not grouped**. It is *not*, on its own,
evidence that any published score is wrong. The number that would speak to that is the
next step.

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

Run it yourself (Atlas is gated — accept the terms on the dataset page first):

```bash
pip install -r requirements.txt
hf auth login
python atlas_accession_check.py
```

| Flag | Effect |
| --- | --- |
| `--revision <sha>` | Dataset revision. Defaults to the commit this check was written against (`9dad400`); `main` checks the latest release. |
| `--skip-profiles` | Metadata only (checks 1, 2, 5). Much faster. |
| `--top-k 20` | Taxa compared for the near-duplicate check. |
| `--max-leak-frac 0.05` | Exit 1 if the headline fraction exceeds this — for use as a regression gate. |
| `--local-dir <path>` | Read already-downloaded `<split>-*.parquet` shards. |

Writes to `out/`: `run_<utc>.txt` (the summary block), `summary.json`, `study_overlap.csv`,
`profile_matches.csv`, `strata.csv`, `grouped_split.csv`.

The harness itself is tested on synthetic shards with known answers:
`pip install -r requirements-dev.txt && pytest`.

---

Manthan Bhanushali · [github.com/Manthanbhanushali010](https://github.com/Manthanbhanushali010)
