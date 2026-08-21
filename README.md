# ClaimIQ

Insurance claim document intelligence: extract key information from claim packs,
summarise them for an adjuster, and flag missing data and inconsistencies —
with every finding traced back to a verified quote in a source document.

> Proof of concept. Demo data is synthetic and contains no real personal or
> health information.

---

## Install

Requires **Python 3.11+**.

```bash
git clone https://github.com/rdarshan10/claimiq.git
cd claimiq
pip install -r requirements.txt
```

Add your API key — the app reads it from `.env`, never from source:

```bash
cp .env.example .env
```

Then edit `.env` and set `GROQ_API_KEY=gsk_...`
(free key from [console.groq.com](https://console.groq.com)).

> The three synthetic claim packs are already in the repo. No generation step
> is needed — `python -m claimiq.data.generate` only *re*writes them.

## Run

```bash
python -m streamlit run claimiq/ui/app.py
```

Opens at http://localhost:8501.

> Use `python -m streamlit`, not bare `streamlit` — pip installs the launcher
> outside PATH on many Windows setups.

**Try it without an API key.** The mock provider runs the whole pipeline
offline in about a second — useful for exploring the UI:

```bash
# macOS / Linux
CLAIMIQ_PROVIDER=mock python -m streamlit run claimiq/ui/app.py

# Windows PowerShell
$env:CLAIMIQ_PROVIDER="mock"; python -m streamlit run claimiq/ui/app.py
```

### Command line

```bash
python prepare_demo.py                       # check quota, pre-process every claim
python -m claimiq.batch claimiq/data/claims  # batch run, resumable
python -m claimiq.evals.run_eval             # accuracy report vs. gold labels
python -m claimiq.evals.test_deterministic   # 61 offline tests, no API key needed
python -m claimiq.stages.crossclaim          # cross-claim fraud signals
```

### First run

Start with **Review a claim → `CLM-2024-1188`** — the claim built to contain
realistic problems. Press **Process claim**, then open the **Findings** tab.

On a free-tier key the first run takes a few minutes (see quotas below).
Results are checkpointed, so afterwards they load instantly.

### Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `streamlit: command not found` | Use `python -m streamlit run ...` |
| `GROQ_API_KEY not set` | Create `.env` from `.env.example` and add the key |
| Run stalls for minutes | Free-tier rate limit — expected; see quotas below |
| `tokens per day (TPD)` error | Daily quota spent. It falls back to a smaller model automatically; otherwise wait for reset or use `CLAIMIQ_PROVIDER=mock` |
| `ModuleNotFoundError: claimiq` | Run commands from the repo root |

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
