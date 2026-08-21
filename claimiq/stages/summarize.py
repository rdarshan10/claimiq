"""Adjuster-facing summary.

The audience is a professional who will read this in under a minute and decide
where to spend their attention. Prose, no bullets-of-bullets, no restating the
findings list that sits next to it on screen.
"""
from __future__ import annotations

from claimiq.core.orchestrator import Context
from claimiq.core.parsing import as_str, parse_json
from claimiq.core.schemas import ClaimSummary, Severity
from claimiq.providers.model import Task, invoke

SYSTEM = (
    "You write claim briefings for experienced insurance adjusters. You are "
    "factual and concise. You never invent detail that is not in the source "
    "documents. You write for a reader who will spend sixty seconds on this."
)

_PROMPT = """Write an adjuster briefing for this claim.

Return JSON:
{{"headline": "<one sentence, max 25 words, stating what happened and the key concern>",
  "narrative": "<2-3 short paragraphs of plain prose: the incident, the treatment
     course, and the documentation position. Reference specific dates and amounts.
     Do not use bullet points. Do not list the flags - they appear separately.>",
  "key_facts": {{"Claimant": "...", "Incident date": "...", "Incident type": "...",
     "Injuries claimed": "...", "Total billed": "...", "Policy number": "...",
     "Policy period": "..."}},
  "timeline": ["<YYYY-MM-DD - event>", "..."]}}

Use "not documented" for any key fact the documents do not state.
Order the timeline chronologically.

{flags_note}

CLAIM DOCUMENTS:
{evidence}"""


class SummarizeStage:
    name = "summarize"

    def run(self, ctx: Context) -> None:
        docs = [d for d in ctx.result.documents if d.text.strip()]
        if not docs:
            ctx.progress(self.name, "no text to summarise")
            return

        from claimiq.providers.model import LIMITER, ROUTING

        room = LIMITER.tpm - ROUTING[Task.SUMMARIZE].max_tokens - 800
        budget = max(600, int(room * 3.2) // max(1, len(docs)))
        evidence = "\n\n".join(
            f"=== {d.filename} ({d.doc_type.value}) ===\n{d.text[:budget]}" for d in docs
        )

        serious = [
            f for f in ctx.result.findings
            if f.severity in (Severity.CRITICAL, Severity.HIGH)
        ]
        flags_note = (
            "Context: validation has already flagged "
            f"{len(serious)} serious issue(s) on this claim. Your headline should "
            "reflect the overall documentation position without enumerating them."
            if serious
            else "Context: validation found no serious issues on this claim."
        )

        try:
            r = invoke(
                _PROMPT.format(flags_note=flags_note, evidence=evidence),
                task=Task.SUMMARIZE,
                system=SYSTEM,
            )
        except Exception as e:  # noqa: BLE001
            ctx.result.errors.append(f"summarize: {e}")
            return

        ctx.log(
            self.name, "model_call", model=r.model, tokens=r.tokens,
            cost=r.cost_usd, latency=r.latency_s,
        )

        data, err = parse_json(r.text)
        if err or not isinstance(data, dict):
            # Summary is presentation, not decision input — degrade, don't fail.
            ctx.result.summary = ClaimSummary(
                headline="Summary unavailable (model output could not be parsed)",
                narrative=r.text[:1500],
            )
            ctx.progress(self.name, "summary degraded")
            return

        facts = data.get("key_facts")
        timeline = data.get("timeline")
        ctx.result.summary = ClaimSummary(
            headline=as_str(data.get("headline")) or "",
            narrative=as_str(data.get("narrative")) or "",
            key_facts={
                str(k): as_str(v) or "not documented"
                for k, v in (facts or {}).items()
            } if isinstance(facts, dict) else {},
            timeline=[as_str(t) or "" for t in (timeline or []) if as_str(t)]
            if isinstance(timeline, list) else [],
        )
        ctx.progress(self.name, "briefing written")
