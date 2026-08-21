# ClaimIQ — Business Case

Read-from document for the business review. Every figure marked **measured** was
produced by running the system; every figure marked *assumption* is a slider in
the demo UI that you should replace with your own numbers.

---

## The problem, in one line

Adjusters spend most of their time reading documents to find the few facts that
matter, and the inconsistencies that matter are the easiest ones to miss.

## The proposition, in one line

> We are not replacing the adjuster. We are handing them a pre-read file with
> the exceptions already circled and sourced, so they spend their time on
> judgment instead of page-turning.

---

## What the system produces per claim

1. **A one-minute briefing** — what happened, treatment course, documentation position.
2. **A completeness score** with a named list of what is missing.
3. **A findings list**, ranked by severity, each with a **verbatim quote and page
   reference** in the source document.
4. **A routing decision** — `auto-approve`, `review`, or `investigate`.
5. **A cost figure** for having done all of it.

---

## Measured results

From the three synthetic demo claims. Reproduce with
`python -m claimiq.evals.run_eval`.

| Metric | Measured |
|---|---|
| Model cost per claim | **~$0.009** (under one penny) |
| Model time per claim | **~32 seconds** |
| Wall-clock per claim | ~250s — *rate-limit constrained, see below* |
| Citation grounding rate | **100%** of citations verified against source |
| Findings on the complex claim | 18, spanning 5 distinct problem classes |

**On wall-clock time.** The demo runs on a free provider tier capped at 8,000
tokens per minute. Actual computation is 32 seconds; the remaining ~220 seconds
is waiting for the rate limit. This is a billing-tier constraint, not an
engineering one — the pipeline reads its concurrency from that budget and widens
automatically on a paid tier. Worth stating plainly rather than letting someone
extrapolate 4 minutes/claim into a capacity plan.

---

## What it caught on the demo claim

`CLM-2024-1188` — a claim engineered to contain realistic problems:

| Finding | Detected by | Exposure |
|---|---|---|
| Duplicate invoice number, total silently raised £5,568 → £5,682 | rule | £5,682 |
| Physiotherapy and surgery dated **after the policy lapsed** | rule | £15,598 |
| Physiotherapy charges exceed the £2,000 sub-limit | rule | £1,420 |
| **£10,030 ACL reconstruction billed against a documented normal knee exam** | reasoning | £10,030 |
| "Admitted overnight" contradicted by same-day discharge record | reasoning | £850 |
| Injuries claimed that were not reported at the scene or at first presentation | reasoning | — |

The fourth line is the one to dwell on. No rule engine finds it: it requires
reading a clinical examination, understanding that "no effusion, ligaments
stable, Lachman negative" means *no ACL injury*, and connecting that to a
surgical invoice raised two months later. That is the class of finding that
justifies the system.

---

## The economics

Using the demo defaults — **replace these with your figures**:

| Input | Assumption |
|---|---|
| Claims per year | 50,000 |
| Loaded adjuster cost | £45/hour |
| Manual review time | 30 min/claim |
| Review time saved on assisted claims | 55% |
| Straight-through rate | 20% |
| Leakage prevented | £25,000 per 1,000 claims |

| Line | Annual |
|---|---|
| Current manual review cost | £1,125,000 |
| Labour saved | ~£720,000 |
| Leakage prevented | ~£1,250,000 |
| **Model spend** | **~£350** |
| **Net benefit** | **~£1,970,000** |

The asymmetry is the point: **model spend is ~£350 against a seven-figure
benefit.** One duplicate invoice caught — a single £5,682 finding — pays for
processing the entire annual book roughly sixteen times over.

Treat the leakage number as the uncertain one. It is also the one your claims
data can settle: the honest version of this slide uses your historical leakage
rate, not a placeholder.

---

## Questions you will be asked

**"Can it hallucinate a finding?"**
Every value and finding carries a verbatim quote and page number, and a
verification pass confirms the quote genuinely exists in that document. Failures
are demoted and flagged, not hidden. Measured grounding on the demo set: 100%.
Separately, all arithmetic and date logic is computed in Python — the model is
never asked whether numbers add up.

**"What if it's wrong?"**
It will sometimes be. Three defences: nothing auto-approves that has any serious
finding; every field carries a confidence score, and low-confidence extractions
are surfaced for manual check; and the adjuster sees the source quote next to
every claim the system makes, so verification takes seconds rather than a
re-read.

**"Is this replacing adjusters?"**
No. It changes what the first twenty minutes look like. The routing decision is
a recommendation, and every auto-approve candidate is one that raised no serious
finding at all.

**"What about PHI and GDPR?"**
The demo uses synthetic data. Before production, PII detection and masking sits
in front of the provider call — designed for, not built (`ARCHITECTURE.md` §6).
Full audit trails are written per claim today: exact prompts, responses, tokens
and rule outcomes.

**"Why not use ChatGPT/Copilot for this?"**
A general assistant will summarise a document. It will not enforce a citation
contract, verify quotes against sources, compute policy sub-limit breaches
deterministically, resume a 500-claim batch after a failure, or report a
per-claim cost. That machinery is the product.

**"How do we know it works?"**
There is an eval harness with hand-written gold labels
(`python -m claimiq.evals.run_eval`) that scores field accuracy, known-issue
recall, and false positives on the clean claim separately. Most POCs cannot
answer this question at all.

---

## What this POC is not

Stated up front, because credibility is worth more than a clean sweep:

- No PII masking yet — required before real claim data.
- No cross-claim fraud detection — that needs a claim corpus (Phase 2 headline).
- No feedback loop yet — the schema supports it, the UI does not.
- SQLite, not Postgres — correct at this scale, swaps cleanly.
- Three synthetic claims, not a validation set of hundreds.

## Recommended next step

A **bounded pilot on 500 real historical claims** with known outcomes. That
answers the only question that actually matters: on *your* claims, how much did
it catch that was missed, and how much adjuster time did it save? Everything
needed to run that pilot — batch processing, resume, audit trails, cost
tracking, accuracy scoring — is already built.
