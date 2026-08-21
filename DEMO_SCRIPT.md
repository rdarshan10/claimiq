# Demo Script

A 12-minute run-through for a mixed audience (business + architect + CTO).

---

## Before the meeting

**The day before, not the morning of:**

```bash
python prepare_demo.py
```

This checks provider quota, processes every claim so results load instantly
from checkpoints, and tells you plainly whether the demo is ready. It exists
because the free tier allows only ~8–10 full claim runs per day — see the
quota section in `README.md`.

Then:

```bash
streamlit run claimiq/ui/app.py
```

Have open in tabs: the UI, `claimiq/providers/model.py`, and this file.

**If the quota is spent and you cannot wait:** run with
`CLAIMIQ_PROVIDER=mock` and say so plainly — "I'm running offline so we don't
wait on rate limits; here are the results from the real run." Never pretend a
mock run is a live one.

---

## The 12 minutes

### 1 · Frame it (1 min) — before touching the screen

> "Adjusters spend most of their time reading documents to find the few facts
> that matter. The inconsistencies that matter are the easiest ones to miss.
> This doesn't replace the adjuster — it hands them a pre-read file with the
> exceptions already circled and sourced."

Say up front: **the data is synthetic, no real PHI.** Compliance people relax
immediately and stop half-listening.

### 2 · The clean claim (1 min) — establish the baseline

**Review a claim → `CLM-2024-0917`**

> "Complete documentation, nothing inconsistent. Auto-approve — this one never
> needs a human. That percentage is the straight-through-processing rate, and
> it's the single biggest lever in the business case."

Why show the boring one first: it proves the system isn't just flagging
everything. Recall means nothing without this.

### 3 · The incomplete claim (1.5 min)

**`CLM-2024-1043` → Completeness tab**

> "Radiology report is pending, fields are missing. The value here is timing —
> you chase the missing document on day one instead of day fourteen."

### 4 · The main event (5 min) — `CLM-2024-1188`

**Findings tab.** Walk down the list in severity order.

Duplicate invoice first:

> "Same invoice number, submitted twice, second one £114 higher. That's not a
> filing error — that's a resubmission with an added line."

Then the policy dates:

> "Physiotherapy and surgery both dated after the policy lapsed on 30 June."

Then **stop and slow down on the ACL finding** — this is the moment:

> "£10,030 for an ACL reconstruction. Now look at the emergency department
> examination: no effusion, ligaments stable, Lachman negative — that is a
> documented *normal* knee. The police report says a sub-5mph reverse impact
> with no injuries reported at the scene.
>
> No rule engine finds this. It requires reading a clinical examination,
> understanding what those findings mean, and connecting them to an invoice
> raised two months later."

**Click into the citation.** Show the verbatim quote and page.

> "Every finding carries the source quote. The adjuster verifies in seconds
> rather than re-reading the file."

### 5 · The cost (1.5 min)

**Run detail tab**, then **Business case**.

> "That whole analysis cost under a penny. At 50,000 claims a year, model spend
> is roughly £350 — against a seven-figure benefit. One duplicate invoice
> caught pays for processing the entire annual book many times over."

Move the sliders to *their* numbers. The panel recalculates live. **Ask them
for their real leakage rate** — it turns your slide into their slide.

### 6 · Close (1 min)

> "The recommended next step is a bounded pilot on 500 real historical claims
> with known outcomes. That answers the only question that matters: on your
> claims, what did it catch that was missed, and how much adjuster time did it
> save? Everything needed to run that pilot is already built."

---

## For the architect and CTO

Keep these in your pocket. Do not volunteer them to the business audience.

**"Can it hallucinate?"**
Every value and finding carries a verbatim quote plus page. A verification pass
string-matches each quote against the source; failures are demoted and flagged,
not hidden. 100% grounding measured on the demo set. Separately, all arithmetic
and date logic runs in Python — the model is never asked whether numbers add up.

**"Why not LangChain?"**
Checkpoint/resume needs control of every stage boundary, which is exactly what
LCEL hides. The audit trail must record the exact prompt and raw response.
Groq is OpenAI-compatible, so the provider layer is ~40 lines. `ARCHITECTURE.md`
§2.1. *This is a decision they'll respect if you own it — don't hedge.*

**"Why no vector database / RAG?"**
A claim pack fits in context. The highest-value findings are cross-document
contradictions, and chunk retrieval systematically hides those — contradictory
documents don't resemble each other. RAG belongs on cross-*claim* fraud search,
which is Phase 2 and genuinely needs it. §2.2.

**"Is it multi-agent?"**
Three specialised reasoners run in parallel with a deterministic merge. Fixed
topology, every call logged, reproducible call graph. Call it what it is —
parallel specialised reasoning, not autonomous agents. Deliberate: an adjuster
decision needs an audit trail, and "the agent decided to look at this" isn't one.

**"How do you know it's accurate?"**
`python -m claimiq.evals.run_eval` — hand-written gold labels, scoring field
accuracy, known-issue recall, and false positives on the clean claim
*separately*. Plus 61 offline unit tests: `python -m claimiq.evals.test_deterministic`.

**"What happens when it fails?"**
Per-stage checkpoints, so a 500-claim batch that dies at 340 resumes at 340.
Circuit breaker for a dead endpoint. Daily-quota exhaustion falls back to a
second model automatically. And two guards against the worst failure mode:
extraction raises if every document fails rather than checkpointing empty data,
and nothing auto-approves while carrying stage errors.

**"How long per claim?"**
~32 seconds of actual model time. The ~250s wall clock is free-tier rate
limiting — a billing decision, not an engineering one. Be precise about this
distinction or they'll extrapolate 4 min/claim into a capacity plan.

---

## If something breaks

- **A claim is slow or stalls** → quota. Switch to a pre-processed claim; say
  "these results are from last night's run."
- **UI throws** → `data/runs/*.json` holds every result; open the JSON.
- **Asked something you don't know** → "I'd have to check" costs you nothing.
  Inventing an answer in front of a CTO costs you the room.

## Don't oversell

The credibility of this demo rests on the ACL finding and the citations. If you
claim it replaces adjusters, or that it's production-ready, or that accuracy is
proven on three synthetic claims, a sharp CTO will press and the whole thing
deflates. `BUSINESS_CASE.md` has a "What this POC is not" section — read it
before the meeting and be the one who raises the limitations first.
