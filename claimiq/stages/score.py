"""Completeness, risk score and routing recommendation.

Fully deterministic. The recommendation is the number the business case rests
on — the straight-through-processing rate — so it must be explainable in one
sentence to an adjuster and reproducible for an auditor. No model call.
"""
from __future__ import annotations

from claimiq.core.orchestrator import Context
from claimiq.core.parsing import as_float
from claimiq.core.schemas import (
    Completeness,
    DocType,
    Recommendation,
    Severity,
)
from claimiq.stages.rules import REQUIRED_DOCS, REQUIRED_FIELDS

# Contribution of one finding at each severity to the 0-1 risk score.
_RISK_WEIGHT = {
    Severity.CRITICAL: 0.45,
    Severity.HIGH: 0.22,
    Severity.MEDIUM: 0.07,
    Severity.LOW: 0.02,
    Severity.INFO: 0.0,
}

AUTO_APPROVE_CEILING = 0.15
INVESTIGATE_FLOOR = 0.50


def compute_completeness(ctx: Context) -> Completeness:
    present: list[str] = []
    missing: list[str] = []

    have = {d.doc_type for d in ctx.result.documents}
    for dtype in REQUIRED_DOCS:
        label = dtype.value.replace("_", " ").title()
        (present if dtype in have else missing).append(f"Document: {label}")

    for e in ctx.result.extractions:
        payload = e.payload()
        if payload is None:
            continue
        for field in REQUIRED_FIELDS.get(e.doc_type, []):
            ev = getattr(payload, field, None)
            label = f"{e.doc_type.value.replace('_', ' ').title()}: {field.replace('_', ' ')}"
            if ev is not None and ev.is_present:
                present.append(label)
            else:
                missing.append(label)

    total = len(present) + len(missing)
    return Completeness(
        score=len(present) / total if total else 0.0,
        present=present,
        missing=missing,
    )


def compute_risk(ctx: Context) -> float:
    """Weighted finding severity, damped so many minor flags never dominate."""
    score = 0.0
    for f in ctx.result.findings:
        weight = _RISK_WEIGHT.get(f.severity, 0.0)
        # A low-confidence finding contributes proportionally less.
        score += weight * max(0.35, f.confidence)

    # Missing documentation is itself risk, independent of findings raised.
    score += (1.0 - ctx.result.completeness.score) * 0.25
    return round(min(1.0, score), 3)


def compute_exposure(ctx: Context) -> float | None:
    """Money at issue: the sum of financial impacts, capped at total billed."""
    impacts = [f.financial_impact for f in ctx.result.findings if f.financial_impact]
    if not impacts:
        return None
    return round(min(sum(impacts), ctx.result.total_billed or sum(impacts)), 2)


def recommend(ctx: Context) -> Recommendation:
    r = ctx.result

    # Never route on incomplete evidence. A claim whose extraction partly
    # failed can look clean simply because nothing was read — that must land in
    # human hands, not in auto-approve.
    if r.errors or any(e.error for e in r.extractions):
        return Recommendation.REVIEW

    has_critical = any(f.severity == Severity.CRITICAL for f in r.findings)
    has_high = any(f.severity == Severity.HIGH for f in r.findings)

    if has_critical or r.risk_score >= INVESTIGATE_FLOOR:
        return Recommendation.INVESTIGATE
    if has_high or r.risk_score > AUTO_APPROVE_CEILING or r.completeness.score < 0.95:
        return Recommendation.REVIEW
    return Recommendation.AUTO_APPROVE


class ScoreStage:
    name = "score"

    def run(self, ctx: Context) -> None:
        from claimiq.providers.model import LEDGER

        ctx.result.completeness = compute_completeness(ctx)
        ctx.result.risk_score = compute_risk(ctx)
        ctx.result.exposure = compute_exposure(ctx)
        ctx.result.recommendation = recommend(ctx)
        ctx.result.usage.update(LEDGER.snapshot())

        ctx.log(
            self.name, "rule",
            completeness=ctx.result.completeness.pct,
            risk=ctx.result.risk_score,
            exposure=ctx.result.exposure,
            recommendation=ctx.result.recommendation.value,
            findings=len(ctx.result.findings),
        )
        ctx.progress(
            self.name,
            f"{ctx.result.recommendation.value.upper()} · risk {ctx.result.risk_score:.2f} "
            f"· complete {ctx.result.completeness.pct}%",
        )
