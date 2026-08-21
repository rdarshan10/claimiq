"""Parallel specialised reasoners over the assembled claim.

Three reasoners, each with its own prompt, run concurrently over the same
evidence and their findings are merged and deduplicated:

  clinical    - does the billed treatment match the documented injury?
  narrative   - do the accounts across documents agree with each other?
  financial   - are the charges plausible for what was documented?

Deliberate framing for the architecture review: this is parallel specialised
reasoning with a deterministic merge, NOT autonomous agents. The topology is
fixed, every call is logged, and a re-run with the same inputs produces the
same call graph. One omnibus prompt does all three of these jobs badly —
different failure modes need different attention — but nothing here chooses
its own control flow, which is what keeps it auditable.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from claimiq.core.orchestrator import Context
from claimiq.core.parsing import as_confidence, as_float, as_list, as_str, parse_json
from claimiq.core.schemas import (
    Citation,
    Finding,
    FindingKind,
    Severity,
)
from claimiq.providers.model import Task, invoke

SYSTEM = (
    "You are a senior insurance claims investigator. You identify genuine "
    "inconsistencies supported by evidence in the documents. You do not "
    "speculate, and you do not report a finding you cannot quote support for. "
    "Reporting a false inconsistency wastes an adjuster's time and is a "
    "serious error; reporting nothing when the claim is clean is correct."
)

# Flat schema, deliberately. An earlier version nested an "evidence" array of
# citation objects inside each finding; gpt-oss rejected that shape under
# response_format=json_object (json_validate_failed) even with budget to spare.
# Flat string fields validate reliably, and quote/doc/page are recombined into
# a Citation on our side.
_ENVELOPE = """
Return JSON: {{"findings": [
  {{"title": "<short>",
    "detail": "<2-3 sentences, specific, cite figures and dates>",
    "severity": "critical|high|medium|low",
    "kind": "{kinds}",
    "confidence": <0.0-1.0>,
    "financial_impact": <number or null>,
    "quote": "<single verbatim sentence from a document proving this finding>",
    "source_document": "<the filename that quote came from>",
    "page": <page number>}}
]}}

If you find nothing, return {{"findings": []}}. Do not invent findings.
Every finding MUST have a quote copied verbatim from a document.
"""


@dataclass(frozen=True)
class Reasoner:
    name: str
    kinds: str
    instruction: str


REASONERS = [
    Reasoner(
        name="clinical",
        kinds="coding_mismatch|narrative_conflict|cross_doc_conflict",
        instruction="""Assess whether the billed treatment is consistent with the
documented clinical findings.

Look specifically for:
- Procedures billed for a body part or condition the medical examination found
  to be normal or did not mention at all
- Treatment intensity disproportionate to the documented diagnosis severity
- Procedure/diagnosis code mismatches (e.g. a surgical code against a soft
  tissue strain diagnosis)
- Inpatient charges where the record documents same-day discharge
- Treatment that continued after the documented recovery or discharge advice""",
    ),
    Reasoner(
        name="narrative",
        kinds="narrative_conflict|cross_doc_conflict|fraud_signal",
        instruction="""Compare the accounts of the incident across all documents.

Look specifically for:
- Contradictions between the claimant's stated account and independent reports
  (police, ambulance, witness)
- Injury severity claimed versus mechanism of injury documented
- Contradictions about how the claimant travelled to hospital, or whether an
  ambulance attended
- Claimed injuries not reported at the scene or at first presentation
- Timeline inconsistencies between the incident, first treatment and later claims""",
    ),
    Reasoner(
        name="financial",
        kinds="cross_doc_conflict|fraud_signal|coding_mismatch",
        instruction="""Assess whether the charges are plausible and internally
consistent.

Look specifically for:
- Charges for services on dates where no corresponding clinical record exists
- Unit prices materially above normal market rates for the service
- Session counts or quantities implausible for the documented condition
- Charges appearing on one invoice but absent from an otherwise identical one
- Services billed by a provider with no referral documented anywhere in the pack""",
    ),
]


def _build_evidence(ctx: Context, max_chars: int | None = None) -> str:
    """Assemble the claim into one evidence block, sized to the TPM window.

    Full document text, not retrieved chunks — cross-document contradiction is
    exactly what chunk retrieval hides, and a claim pack fits in context.

    The size cap is derived from the provider's TPM limit rather than being a
    fixed constant: on a constrained tier the whole request (evidence +
    reasoning trace + answer) must fit inside one minute's budget.
    """
    from claimiq.providers.model import LIMITER, Task, effective_max_tokens

    if max_chars is None:
        # Size against the budget of the model that will actually serve this
        # call. When the primary model's daily quota is spent, invoke() falls
        # back to a smaller one whose output budget is capped lower — sizing
        # from the configured value produced a 413 that silently dropped a
        # whole reasoner from the analysis.
        out_budget = effective_max_tokens(Task.REASON)
        input_tokens = max(1200, LIMITER.budget - out_budget - 900)
        max_chars = int(input_tokens * 3.2)

    docs = [d for d in ctx.result.documents if d.text.strip()]
    if not docs:
        return ""

    # Fair share per document, then redistribute what short documents leave.
    share = max_chars // len(docs)
    spare = sum(max(0, share - len(d.text)) for d in docs)
    long_docs = [d for d in docs if len(d.text) > share] or docs
    bonus = spare // len(long_docs)

    blocks: list[str] = []
    for doc in docs:
        budget = share + (bonus if len(doc.text) > share else 0)
        text = doc.text[:budget]
        truncated = len(doc.text) > budget
        blocks.append(
            f"=== DOCUMENT: {doc.filename} (type: {doc.doc_type.value}) ===\n"
            f"{text}" + ("\n[... truncated ...]" if truncated else "")
        )
    return "\n\n".join(blocks)


_KIND_MAP = {
    "coding_mismatch": FindingKind.CODING_MISMATCH,
    "narrative_conflict": FindingKind.NARRATIVE_CONFLICT,
    "cross_doc_conflict": FindingKind.CROSS_DOC_CONFLICT,
    "fraud_signal": FindingKind.FRAUD_SIGNAL,
    "policy_breach": FindingKind.POLICY_BREACH,
    "duplicate": FindingKind.DUPLICATE,
}

_DOC_BY_NAME: dict[str, str] = {}


def _resolve_citation(item: dict) -> list[Citation]:
    """Rebuild a Citation from the flat quote/source_document/page fields."""
    quote = as_str(item.get("quote"))
    if not quote:
        return []

    name = (as_str(item.get("source_document")) or "").strip()
    doc_id = _DOC_BY_NAME.get(name)
    if not doc_id:
        # Models cite by partial or reformatted filename; match leniently.
        lowered = name.lower()
        for fname, did in _DOC_BY_NAME.items():
            fl = fname.lower()
            if lowered and (lowered in fl or fl in lowered):
                doc_id = did
                break
    if not doc_id:
        return []

    try:
        page = int(item.get("page") or 1)
    except (TypeError, ValueError):
        page = 1
    return [Citation(doc_id=doc_id, page=max(1, page), quote=quote[:400])]


def run_reasoner(r: Reasoner, evidence: str, ctx: Context) -> list[Finding]:
    prompt = (
        f"{r.instruction}\n\n"
        f"{_ENVELOPE.format(kinds=r.kinds)}\n\n"
        f"CLAIM EVIDENCE:\n{evidence}"
    )
    try:
        resp = invoke(prompt, task=Task.REASON, system=SYSTEM)
    except Exception as e:  # noqa: BLE001
        ctx.result.errors.append(f"reasoner {r.name}: {e}")
        return []

    ctx.log(
        "reason", "model_call", reasoner=r.name, model=resp.model,
        tokens=resp.tokens, cost=resp.cost_usd, latency=resp.latency_s,
    )

    data, err = parse_json(resp.text)
    if err or not isinstance(data, dict):
        ctx.result.errors.append(f"reasoner {r.name}: {err}")
        return []

    out: list[Finding] = []
    for item in as_list(data.get("findings")):
        if not isinstance(item, dict):
            continue
        title = as_str(item.get("title"))
        detail = as_str(item.get("detail"))
        if not title or not detail:
            continue
        try:
            sev = Severity(str(item.get("severity", "medium")).lower())
        except ValueError:
            sev = Severity.MEDIUM
        kind = _KIND_MAP.get(
            str(item.get("kind", "")).lower(), FindingKind.CROSS_DOC_CONFLICT
        )
        cites = _resolve_citation(item)

        # An uncited semantic finding is a claim we cannot show the adjuster.
        # Keep it, but demote it — never present it as established.
        if not cites:
            sev = Severity.LOW if sev.rank < Severity.LOW.rank else sev

        out.append(
            Finding(
                kind=kind,
                severity=sev,
                title=title[:160],
                detail=detail,
                citations=cites,
                financial_impact=as_float(item.get("financial_impact")),
                confidence=as_confidence(item.get("confidence")) or 0.7,
                source=r.name,
            )
        )
    return out


def _dedupe(findings: list[Finding]) -> list[Finding]:
    """Merge near-identical findings raised by more than one reasoner.

    Agreement between independent reasoners is signal, so a corroborated
    finding keeps the highest severity and gains confidence rather than being
    silently dropped.
    """
    kept: list[Finding] = []
    for f in findings:
        words = set(f.title.lower().split())
        match = None
        for k in kept:
            kw = set(k.title.lower().split())
            overlap = len(words & kw) / max(1, min(len(words), len(kw)))
            if k.kind == f.kind and overlap > 0.6:
                match = k
                break
        if match:
            if f.severity.rank < match.severity.rank:
                match.severity = f.severity
            match.confidence = min(1.0, match.confidence + 0.15)
            match.source = f"{match.source}+{f.source}"
            seen = {(c.doc_id, c.quote) for c in match.citations}
            match.citations.extend(
                c for c in f.citations if (c.doc_id, c.quote) not in seen
            )
            if f.financial_impact and not match.financial_impact:
                match.financial_impact = f.financial_impact
        else:
            kept.append(f)
    return kept


class ReasonStage:
    name = "reason"

    def run(self, ctx: Context) -> None:
        _DOC_BY_NAME.clear()
        _DOC_BY_NAME.update({d.filename: d.doc_id for d in ctx.result.documents})

        evidence = _build_evidence(ctx)
        if not evidence.strip():
            ctx.progress(self.name, "no text to reason over")
            return

        # Concurrency here is governed by the TPM budget, not by CPU. Each
        # reasoner request is a large fraction of a constrained window, so
        # fanning out 3-wide just forces all three to queue inside the limiter.
        # On a higher tier, raise CLAIMIQ_TPM and this widens automatically.
        from claimiq.providers.model import LIMITER, effective_max_tokens

        per_call = effective_max_tokens(Task.REASON) + len(evidence) // 3.5
        workers = max(1, min(len(REASONERS), int(LIMITER.tpm // max(1, per_call))))

        if workers == 1:
            batches = [run_reasoner(r, evidence, ctx) for r in REASONERS]
        else:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                batches = list(
                    pool.map(lambda r: run_reasoner(r, evidence, ctx), REASONERS)
                )

        raw = [f for batch in batches for f in batch]
        merged = _dedupe(raw)
        ctx.result.findings.extend(merged)

        ctx.log(
            self.name, "event",
            reasoners=[r.name for r in REASONERS],
            raw_findings=len(raw), merged_findings=len(merged),
        )
        ctx.progress(
            self.name, f"{len(merged)} semantic findings from {len(REASONERS)} reasoners"
        )
