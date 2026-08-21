"""Grounding verification.

Every citation claims a verbatim quote from a specific page. This stage checks
that the quote actually appears there. It is pure string matching — no model
call, no cost, no latency — and it is the most direct answer available to
"can the system make things up?".

A citation that fails verification does not silently disappear: the value is
marked ungrounded and an explicit finding is raised, because a confident
extraction with a fabricated source is worse than a missing one.
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher

from claimiq.core.orchestrator import Context
from claimiq.core.schemas import Citation, Finding, FindingKind, Severity

# Fuzzy threshold: models normalise whitespace, casing and punctuation when
# copying. Below this we treat the quote as not present.
SIMILARITY_FLOOR = 0.82


def _normalise(s: str) -> str:
    s = s.lower()
    s = re.sub(r"[^\w\s.]", " ", s)   # keep decimal points; money matters
    return re.sub(r"\s+", " ", s).strip()


def quote_is_present(quote: str, page_text: str) -> tuple[bool, float]:
    """Is this quote genuinely in this page? Exact, then substring, then fuzzy."""
    if not quote or not page_text:
        return False, 0.0

    if quote in page_text:
        return True, 1.0

    nq, np_ = _normalise(quote), _normalise(page_text)
    if not nq:
        return False, 0.0
    if nq in np_:
        return True, 1.0

    # Slide a window the size of the quote across the page.
    window = len(nq)
    if window > len(np_):
        ratio = SequenceMatcher(None, nq, np_).ratio()
        return ratio >= SIMILARITY_FLOOR, ratio

    best = 0.0
    step = max(1, window // 4)
    for i in range(0, len(np_) - window + 1, step):
        ratio = SequenceMatcher(None, nq, np_[i : i + window]).quick_ratio()
        if ratio > best:
            best = ratio
            if best >= 0.98:
                break
    return best >= SIMILARITY_FLOOR, best


class VerifyStage:
    name = "verify"

    def run(self, ctx: Context) -> None:
        pages: dict[tuple[str, int], str] = {}
        doc_text: dict[str, str] = {}
        for doc in ctx.result.documents:
            doc_text[doc.doc_id] = doc.text
            for p in doc.pages:
                pages[(doc.doc_id, p.number)] = p.text

        checked = grounded = 0
        ungrounded_fields: list[str] = []

        def verify_citation(c: Citation) -> bool:
            """Check the cited page; fall back to the whole document.

            Page drift is a citation-precision problem, not a fabrication
            problem — we count it as grounded but it is visible in the audit log.
            """
            ok, _ = quote_is_present(c.quote, pages.get((c.doc_id, c.page), ""))
            if ok:
                return True
            ok_doc, _ = quote_is_present(c.quote, doc_text.get(c.doc_id, ""))
            if ok_doc:
                ctx.log(self.name, "event", page_drift=c.doc_id, page=c.page)
            return ok_doc

        # 1. Extracted values
        for e in ctx.result.extractions:
            for fname, ev in e.values().items():
                if not ev.is_present or ev.citation is None:
                    continue
                checked += 1
                ok = verify_citation(ev.citation)
                ev.grounded = ok
                if ok:
                    grounded += 1
                else:
                    ungrounded_fields.append(f"{e.doc_id}.{fname}")
                    # Confidence must reflect that the source did not check out.
                    ev.confidence = min(ev.confidence, 0.3)

        # 2. Findings raised by the reasoners
        bad_findings: list[str] = []
        for f in ctx.result.findings:
            if not f.citations:
                continue
            verified = [c for c in f.citations if verify_citation(c)]
            checked += len(f.citations)
            grounded += len(verified)
            if not verified and f.citations:
                bad_findings.append(f.title)
                f.citations = []
                f.confidence = min(f.confidence, 0.4)
                if f.severity in (Severity.CRITICAL, Severity.HIGH):
                    f.severity = Severity.MEDIUM
                f.detail += (
                    " [Note: supporting quotes could not be located in the source "
                    "documents; severity reduced pending manual verification.]"
                )
            else:
                f.citations = verified

        if ungrounded_fields:
            ctx.result.findings.append(
                Finding(
                    kind=FindingKind.UNGROUNDED,
                    severity=Severity.MEDIUM,
                    title=f"{len(ungrounded_fields)} extracted value(s) failed grounding",
                    detail=(
                        "These values were extracted with a source quote that could "
                        "not be located in the document: "
                        + ", ".join(ungrounded_fields[:8])
                        + ". They have been down-weighted and require manual check."
                    ),
                    source="rule",
                )
            )

        rate = grounded / checked if checked else 1.0
        ctx.result.usage["grounding_rate"] = round(rate, 4)
        ctx.result.usage["citations_checked"] = checked

        ctx.log(
            self.name, "event",
            checked=checked, grounded=grounded, rate=round(rate, 4),
            ungrounded_fields=ungrounded_fields, ungrounded_findings=bad_findings,
        )
        ctx.progress(
            self.name,
            f"{grounded}/{checked} citations verified ({rate:.0%} grounded)",
        )
