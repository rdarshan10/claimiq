# ClaimIQ

Insurance claim document intelligence: extract key information from claim packs,
summarise them for an adjuster, and flag missing data and inconsistencies —
with every finding traced back to a verified quote in a source document.

> Proof of concept. Demo data is synthetic and contains no real personal or
> health information.

---

## Quick start

```bash
pip install -r requirements.txt

cp .env.example .env          # then add your GROQ_API_KEY
python -m claimiq.data.generate   # write the synthetic claim packs

streamlit run claimiq/ui/app.py   # demo UI
```

Batch and evaluation:

```bash
python -m claimiq.batch claimiq/data/claims     # process every claim, resumable
python -m claimiq.evals.run_eval                # accuracy report
```

Run with no credentials at all:

```bash
CLAIMIQ_PROVIDER=mock python -m claimiq.batch claimiq/data/claims
```

---

## What it does

Eight stages, each checkpointed so any run resumes where it stopped:

| Stage | What happens |
|---|---|
| `ingest` | PDF/text → normalised documents with a page map |
| `classify` | document type — keyword rules first, model only when ambiguous |
| `extract` | typed fields with a **mandatory verbatim quote + page** |
| `rules` | deterministic checks: arithmetic, dates, duplicates, policy limits |
| `reason` | three specialised reasoners: clinical, narrative, financial |
| `verify` | confirms every cited quote really exists in the source |
| `summarize` | adjuster briefing |
| `score` | completeness %, risk score, and an `auto_approve / review / investigate` routing decision |

---

## The three demo claims

| Claim | Character | Demonstrates |
|---|---|---|
| `CLM-2024-0917` | clean | straight-through processing |
| `CLM-2024-1043` | incomplete | missing radiology report, unfilled fields |
| `CLM-2024-1188` | inconsistent | **the one to demo live** |

`CLM-2024-1188` contains, by construction: a duplicate invoice number with a
silently increased total, physiotherapy and surgery dated after the policy
lapsed, a £10,030 ACL reconstruction billed against a documented **normal** knee
examination, an "admitted overnight" claim contradicted by a same-day discharge,
and a police report recording a sub-5mph impact with no injuries reported.

---

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `GROQ_API_KEY` | — | provider credential (read from `.env`) |
| `CLAIMIQ_PROVIDER` | `groq` | `groq` or `mock` |
| `CLAIMIQ_TPM` | `8000` | provider tokens-per-minute budget |
| `CLAIMIQ_RPM` | `28` | provider requests-per-minute budget |

**`CLAIMIQ_TPM` is the throughput dial.** The demo account is limited to 8,000
tokens/minute, which dominates wall-clock time: a 7-document claim uses ~32s of
actual model time but ~250s end to end. The pipeline derives evidence size,
token budgets and concurrency from this number, so raising it on a paid tier
widens everything automatically.

### Free-tier quotas — read this before demo day

The Groq free tier also caps **200,000 tokens per day, per model**. A full day
of development exhausts it, and the symptom is easy to misread as a code fault:

```
Rate limit reached ... on tokens per day (TPD): Limit 200000, Used 199549
```

The pipeline handles this rather than stalling — it marks the model exhausted
and falls back to `gpt-oss-20b`, which has its own separate daily quota. You
will see `by_model` in the run detail change accordingly.

Practical guidance:

- Budget roughly **8–10 full claim runs per day** on the free tier.
- **Pre-run the claims before a demo.** Results load instantly from checkpoints,
  so the demo never waits on a model or a quota.
- Use `CLAIMIQ_PROVIDER=mock` for any UI or layout work — it is instant and
  costs nothing.
- On a paid tier, raise `CLAIMIQ_TPM` and the pipeline widens automatically.

---

## Using your own model

`claimiq/providers/model.py` is the only file that imports a provider SDK.
Implement `complete()`, register the class in `PROVIDERS`, set
`CLAIMIQ_PROVIDER`. Retry, circuit breaking, rate governing, task routing and
cost accounting are applied around it — see `ARCHITECTURE.md` §5.

---

## Layout

```
claimiq/
  providers/model.py     ← the single model seam
  core/                  schemas · parsing · orchestrator
  stages/                the eight pipeline stages
  data/generate.py       synthetic claim packs
  evals/                 gold labels + scoring harness
  ui/app.py              Streamlit demo
  pipeline.py            stage assembly
  batch.py               SQLite queue + resumable batch runner
```

- `ARCHITECTURE.md` — design decisions, and the ones deliberately not taken
- `BUSINESS_CASE.md` — cost, ROI and what to say in the meeting
