# ClaimIQ — Architecture

Technical companion to `BUSINESS_CASE.md`. This document records the design
decisions and, more importantly, the ones deliberately **not** taken.

---

## 1. Shape of the system

```
Entry points        Streamlit UI        Batch CLI          (FastAPI: Phase 2)
                          │                  │
                          └────────┬─────────┘
                                   ▼
                        ┌──────────────────────┐
                        │  SQLite job queue    │  durable, resumable
                        └──────────┬───────────┘
                                   ▼
   ┌───────────────────────────────────────────────────────────────┐
   │  Pipeline — 8 stages, each checkpointed after completion       │
   │                                                                │
   │  ingest → classify → extract → rules → reason → verify         │
   │                                        → summarize → score     │
   └───────────────────────────────┬───────────────────────────────┘
                                   ▼
                     claimiq/providers/model.py
              (routing · retry · circuit breaker · TPM governor)
                                   ▼
                        Groq  ·  or your endpoint
```

Every stage takes a `Context`, mutates `ctx.result`, and returns nothing. After
each stage the whole `ClaimResult` is written to
`data/checkpoints/<claim_id>/<stage>.json`. That single convention is what
makes resume, replay and inspection work.

---

## 2. Decisions worth defending

### 2.1 No LangChain

Used only for text splitting, and not currently even that.

The pipeline needs control of every stage boundary for checkpointing — which is
precisely what LCEL exists to hide. The audit trail must record the exact prompt
and raw response; through a framework you are reconstructing what it *probably*
sent. And Groq is OpenAI-compatible, so the provider layer is about forty lines.
A dependency that abstracts a call we write once, while removing the boundaries
we depend on, is negative value.

This is reversible. If your team standardises on LangChain, the stage protocol
(`name` + `run(ctx)`) is small enough to reimplement over LCEL without touching
the schemas.

### 2.2 No vector database

A claim pack is 20–80 pages and fits in a context window.

More importantly, the highest-value findings are **cross-document
contradictions** — an invoice on page 4 against a medical report on page 31.
Chunk retrieval selects passages that resemble the query; two documents that
*contradict* each other frequently do not resemble each other. RAG here would
systematically hide the thing the system exists to find.

Where a vector store does earn its place is cross-**claim** search — the same
provider NPI across 40 unrelated claims, near-identical accident narratives
filed by different claimants. That corpus does not fit in context. It is
Phase 2, and it is a genuinely different problem.

### 2.3 Hybrid validation, not "AI validation"

Two layers, and the split is deliberate:

| Layer | Implementation | Handles |
|---|---|---|
| Deterministic floor | `stages/rules.py`, pure Python | arithmetic, date logic, duplicates, policy limits, completeness |
| Semantic reasoning | `stages/reason.py`, model calls | coding vs. diagnosis, narrative conflicts, clinical plausibility |

Arithmetic and date comparison never touch a model: slower, costlier, and
occasionally wrong at exactly what code is exactly right at. When an auditor
asks *"can the AI invent a number?"* the answer is that the numbers driving a
finding are computed by inspectable, unit-tested code, and the model's role is
to explain and to reason semantically on top.

The semantic layer is not decoration — it produced findings the rules cannot
reach, such as a £10,030 ACL reconstruction billed against a documented **normal**
knee examination.

### 2.4 Parallel specialised reasoners — not autonomous agents

Three reasoners (`clinical`, `narrative`, `financial`) run over the same
evidence with different prompts, and their findings are merged and deduplicated.

Called accurately: **parallel specialised reasoning with a deterministic merge.**
The topology is fixed, every call is logged, and the same inputs produce the
same call graph. Nothing chooses its own control flow.

Why not one prompt: three different failure modes need three different kinds of
attention, and one omnibus prompt does all three badly. Why not autonomous
agents: an adjuster decision needs an audit trail, and *"the agent decided to
look at this"* is not one. Corroboration is treated as signal — when two
reasoners independently raise the same issue, confidence increases rather than
one being dropped.

### 2.5 Citations are mandatory, and verified

Every extracted value and every finding carries `{doc_id, page, quote}`. The
`verify` stage then checks, by string matching against the source page, that the
quote actually appears there.

- A value whose quote cannot be located is marked ungrounded, its confidence is
  capped at 0.3, and an explicit finding is raised.
- A *finding* whose quotes cannot be located is stripped of citations,
  demoted from CRITICAL/HIGH to MEDIUM, and annotated.

This costs nothing — pure string matching, no model call — and it is the most
direct available answer to *"can it make things up?"*. Measured grounding rate
on the demo set is reported by the eval harness.

---

## 3. Reliability

**Checkpoint and resume.** Per-stage snapshots mean a 500-claim batch that dies
at claim 340 resumes at 340, and a claim that failed in `reason` re-runs from
`reason` rather than from ingestion.

**Circuit breaker.** After 8 consecutive provider failures the breaker opens for
30s. Without it, a dead endpoint burns an entire batch through retry storms.

**Two different quota failures, handled differently.** The demo account enforces
both **8,000 tokens/minute** and **200,000 tokens/day**, per model. They look
identical on the wire (HTTP 429) and require opposite responses:

| | TPM (per minute) | TPD (per day) |
|---|---|---|
| Meaning | backpressure from a healthy endpoint | budget spent |
| Right response | wait and retry | stop retrying; switch model |
| Handled by | `RateLimiter` + backoff | `QuotaExhausted` + `FALLBACK_MODEL` |

Conflating them is expensive in both directions. Treating TPD as retryable
burns the remaining budget on calls that cannot succeed; treating TPM as fatal
kills a batch that would have completed. `invoke()` distinguishes them by
message and, on TPD, marks the model exhausted and transparently falls back to
`gpt-oss-20b` — which carries its own separate daily quota. Quality drops and
the run says so, but a degraded claim beats a dead batch.

Neither counts toward the circuit breaker. An early version let rate limits trip
it, and the observed failure was the worst kind: the breaker opened mid-batch,
every subsequent extraction failed, and the pipeline still reported success with
`billed=0` and no critical findings. Two guards now prevent that class of bug —
`ExtractStage` raises if *every* document fails rather than checkpointing empty
data, and `recommend()` refuses to auto-approve any claim carrying stage errors.

**TPM governor.** Counting input and output together. This is architectural, not
cosmetic:

- A request larger than the window can never succeed — it returns HTTP 413. So
  evidence size and `max_tokens` are *derived* from `LIMITER.tpm`, never
  hard-coded.
- Concurrency is derived the same way. Fanning out wider than the budget allows
  does not speed anything up; threads simply queue inside the limiter. Raise
  `CLAIMIQ_TPM` on a paid tier and extraction and reasoning widen automatically.
- Reservations are pessimistic but **reconciled** after each call. An early
  version did not refund unused reservation and ran at 40% of achievable
  throughput (605s/claim vs. 251s after the fix).

**Truncated-JSON handling.** With `response_format=json_object`, a response that
exhausts `max_tokens` mid-object is rejected by the API as HTTP 400
(`json_validate_failed`) rather than returned as partial text. Since
`reasoning_effort=high` can consume several thousand tokens before the answer
begins, `invoke()` detects this specific failure, retries once with a doubled
budget, and then raises a message naming the real cause. Silently returning zero
findings would be the worst possible outcome for a validation system.

---

## 4. Cost engineering

Routing is by **task**, not by model name — stages request `Task.EXTRACT`, and
`ROUTING` in `providers/model.py` decides what that costs.

| Task | Model | Rationale |
|---|---|---|
| classify | gpt-oss-20b | high volume, easy; most documents never reach a model at all |
| extract | gpt-oss-120b | errors here cascade into every downstream finding |
| reason | gpt-oss-120b, effort=high | the genuinely hard semantic work |
| summarize | gpt-oss-120b | user-facing prose |
| verify / cheap | gpt-oss-20b | cheap checks on expensive output |

Classification additionally runs a **deterministic keyword pre-pass**; on the
demo packs all documents classify by rule with **zero** model calls.

`qwen/qwen3.6-27b` was evaluated and dropped: it rejects `response_format:
json_object` outright (HTTP 400) and emits `<think>` traces that consumed the
entire token budget before answering. Two predictable models beat three.

Every call records tokens, latency and cost into a thread-safe ledger, so the
per-claim figure in the UI is **measured, not estimated**.

---

## 5. Swapping in your own model

`claimiq/providers/model.py` is the only file that imports a provider SDK.

```python
class MyProvider:
    def complete(self, system, prompt, profile) -> ModelResponse:
        ...  # your endpoint here
        return ModelResponse(text=..., model=..., in_tokens=..., out_tokens=...,
                             latency_s=..., cost_usd=...)

PROVIDERS["mine"] = MyProvider   # then set CLAIMIQ_PROVIDER=mine
```

Retry, circuit breaking, rate governing, routing and cost accounting are applied
by `invoke()` around whatever `complete()` does, so a new provider inherits all
of it. A `MockProvider` ships for credential-free offline runs and tests.

---

## 6. What is not built

Stated plainly, because a POC that pretends to be production invites the wrong
questions.

| Not built | Why | Effort to add |
|---|---|---|
| PII detection / masking | Demo data is synthetic. Real deployment needs it before any PHI reaches a provider. | ~1 day (presidio) |
| FastAPI service | Streamlit + batch CLI cover both demo audiences. | ~0.5 day |
| Cross-claim fraud index | Needs a claim corpus; this is the Phase 2 headline. | ~3 days |
| Human-in-the-loop feedback capture | Schema is designed for it; no UI yet. | ~1 day |
| Postgres / real queue | SQLite is correct at POC scale and swaps cleanly. | ~0.5 day |
| Auth, RBAC, audit retention | Deployment concerns, not architecture. | — |
| Bounding-box citations | Text-span citations work today; pixel highlights need OCR geometry. | ~2 days |

---

## 7. Phase 2

1. **Cross-claim intelligence.** Provider NPIs across claims, near-duplicate
   invoices between claimants, narrative clustering to surface fraud rings.
   This is where a vector store belongs.
2. **Feedback loop.** Capture adjuster corrections, measure field accuracy over
   time, and promote corrections into few-shot examples.
3. **Policy-aware payability.** Structured policy ingestion so the system checks
   what is *payable*, not merely what is complete.
4. **Confidence-based escalation.** Route only low-confidence claims to senior
   adjusters; the confidence data is already captured per field.
5. **Throughput.** The pipeline is not the bottleneck — 32s of model time
   against 251s wall clock on a constrained tier. A paid tier is a
   configuration change.
