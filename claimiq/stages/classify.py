"""Document classification.

Cheap-model task by design, with a deterministic keyword pre-pass that resolves
the easy majority for free. Only ambiguous documents reach a model — this is
the routing story in miniature and it is measurable in the cost panel.
"""
from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor

from claimiq.core.orchestrator import Context
from claimiq.core.parsing import as_confidence, as_str, parse_json
from claimiq.core.schemas import DocType, Document
from claimiq.providers.model import Task, invoke

# Ordered: first rule with a decisive score wins.
_SIGNALS: list[tuple[DocType, list[str], list[str]]] = [
    (DocType.POLICY, ["policy schedule", "period of insurance", "cover summary",
                      "sub-limit", "general exclusions", "premium paid"], []),
    (DocType.POLICE_REPORT, ["police", "collision report", "reporting officer",
                             "constabulary", "traffic collision"], []),
    (DocType.DISCHARGE_SUMMARY, ["discharge summary", "summary of care",
                                 "discharge medications", "fit for discharge"], []),
    # Invoices vary far more in layout than clinical documents do: some carry
    # no subtotal, no VAT line and no payment terms. Match the structural
    # signals that are actually invariant.
    (DocType.INVOICE, ["invoice number", "invoice date", "subtotal", "total gbp",
                       "payment terms", "amount due", "invoice", "total:",
                       "description", "amount", "qty", "unit"], []),
    (DocType.CLAIM_FORM, ["claim notification", "claim reference",
                          "date of notification", "policyholder"], []),
    (DocType.PRESCRIPTION, ["prescription", "dispense", "sig:", "refills"], []),
    (DocType.MEDICAL_REPORT, ["presenting complaint", "examination", "diagnosis",
                              "treatment plan", "icd-10", "clinician", "gmc"], []),
]

_PROMPT = """Classify this insurance claim document into exactly one type.

Types: medical_report, invoice, police_report, policy, discharge_summary,
prescription, claim_form, correspondence, photo_evidence, unknown

Document filename: {filename}
Content (first 1200 chars):
---
{excerpt}
---

Return JSON: {{"doc_type": "<type>", "confidence": <0.0-1.0>, "reason": "<10 words>"}}"""


def _rule_classify(doc: Document) -> tuple[DocType, float]:
    text = (doc.filename + "\n" + doc.text[:2500]).lower()
    best, best_score = DocType.UNKNOWN, 0
    for dtype, keywords, _ in _SIGNALS:
        score = sum(1 for k in keywords if k in text)
        if score > best_score:
            best, best_score = dtype, score

    if best_score >= 3:
        return best, min(0.95, 0.6 + 0.1 * best_score)
    if best_score == 2:
        return best, 0.55
    return DocType.UNKNOWN, 0.0


def _model_classify(doc: Document, ctx: Context) -> tuple[DocType, float]:
    prompt = _PROMPT.format(filename=doc.filename, excerpt=doc.text[:1200])
    r = invoke(prompt, task=Task.CLASSIFY)
    ctx.log(
        "classify", "model_call", doc=doc.doc_id, model=r.model,
        tokens=r.tokens, cost=r.cost_usd, latency=r.latency_s,
    )
    data, err = parse_json(r.text)
    if err or not isinstance(data, dict):
        return DocType.UNKNOWN, 0.0
    raw = (as_str(data.get("doc_type")) or "").strip().lower()
    try:
        return DocType(raw), as_confidence(data.get("confidence"))
    except ValueError:
        return DocType.UNKNOWN, 0.0


class ClassifyStage:
    name = "classify"

    # Below this, ask a model rather than trusting the keyword pass.
    RULE_CONFIDENCE_FLOOR = 0.7

    def run(self, ctx: Context) -> None:
        docs = ctx.result.documents
        needs_model: list[Document] = []

        for doc in docs:
            dtype, conf = _rule_classify(doc)
            if conf >= self.RULE_CONFIDENCE_FLOOR:
                doc.doc_type, doc.type_confidence = dtype, conf
            else:
                needs_model.append(doc)

        if needs_model:
            with ThreadPoolExecutor(max_workers=6) as pool:
                results = list(
                    pool.map(lambda d: _model_classify(d, ctx), needs_model)
                )
            for doc, (dtype, conf) in zip(needs_model, results):
                doc.doc_type, doc.type_confidence = dtype, conf

        by_rule = len(docs) - len(needs_model)
        ctx.log(
            self.name, "event",
            total=len(docs), by_rule=by_rule, by_model=len(needs_model),
            types={d.filename: d.doc_type.value for d in docs},
        )
        ctx.progress(
            self.name,
            f"{len(docs)} classified ({by_rule} by rule, {len(needs_model)} by model)",
        )
