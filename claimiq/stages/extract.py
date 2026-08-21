"""Structured extraction with mandatory provenance.

Every field the model returns must carry a verbatim quote and page number.
That constraint does real work: it is the input to the grounding verifier, and
a value the model cannot cite is a value we treat as absent rather than fact.

Documents are extracted concurrently — this is the latency win worth showing.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from claimiq.core.orchestrator import Context
from claimiq.core.parsing import (
    as_confidence,
    as_float,
    as_list,
    as_str,
    parse_json,
)
from claimiq.core.schemas import (
    Citation,
    DocExtraction,
    DocType,
    Document,
    ExtractedValue,
    IncidentData,
    InvoiceData,
    LineItem,
    MedicalData,
    PolicyData,
)
from claimiq.providers.model import Task, invoke

SYSTEM = (
    "You are a precise insurance claim data extractor. You never infer or "
    "invent values. If a field is not stated in the document, return null for "
    "it. Every value you do return must be supported by a verbatim quote "
    "copied exactly from the document text."
)

_FIELD_SPECS: dict[DocType, tuple[type, list[str], str]] = {
    DocType.INVOICE: (
        InvoiceData,
        ["invoice_number", "invoice_date", "provider_name", "provider_npi",
         "patient_name", "subtotal", "tax", "total", "currency",
         "service_date_from", "service_date_to"],
        'Also return "line_items": [{"description": str, "amount": number, '
        '"quantity": number, "code": str|null}] copied from the itemised table.',
    ),
    DocType.MEDICAL_REPORT: (
        MedicalData,
        ["patient_name", "patient_dob", "provider_name", "diagnosis",
         "icd_codes", "procedures", "treatment_plan", "admission_date",
         "discharge_date", "incident_date", "body_parts", "prior_conditions"],
        "",
    ),
    DocType.DISCHARGE_SUMMARY: (
        MedicalData,
        ["patient_name", "patient_dob", "provider_name", "diagnosis",
         "treatment_plan", "admission_date", "discharge_date", "procedures"],
        "",
    ),
    DocType.POLICY: (
        PolicyData,
        ["policy_number", "policyholder", "effective_date", "expiry_date",
         "coverage_limit", "deductible", "sublimits", "exclusions"],
        'For "sublimits" return an object mapping category to amount. '
        'For "exclusions" return an array of strings.',
    ),
    DocType.POLICE_REPORT: (
        IncidentData,
        ["incident_date", "incident_location", "description", "parties",
         "report_number", "claimant_statement"],
        "",
    ),
    DocType.CLAIM_FORM: (
        IncidentData,
        ["incident_date", "incident_location", "description", "parties",
         "report_number", "claimant_statement"],
        "",
    ),
}

_PROMPT = """Extract the listed fields from this {doc_type} document.

For EVERY field return an object:
  {{"value": <value or null>, "confidence": <0.0-1.0>, "page": <page number>,
    "quote": "<verbatim text from the document proving this value>"}}

Rules:
- If the field is not present, return {{"value": null, "confidence": 0, "page": null, "quote": null}}
- "quote" must be copied character-for-character from the document. Never paraphrase.
- Dates as ISO YYYY-MM-DD. Money as plain numbers, no currency symbols.
- Do not infer values from context. Only extract what is explicitly written.

Fields: {fields}
{extra}

DOCUMENT ({filename}):
---
{content}
---

Return a single JSON object keyed by field name."""


def _to_value(raw: object, doc_id: str, numeric: bool = False) -> ExtractedValue:
    """Normalise one model-returned field object into an ExtractedValue."""
    if not isinstance(raw, dict):
        # Model returned a bare scalar — accept the value, no citation.
        v = as_float(raw) if numeric else as_str(raw)
        return ExtractedValue(value=v, confidence=0.4 if v is not None else 0.0)

    val = raw.get("value")
    value = as_float(val) if numeric else (
        as_str(val) if not isinstance(val, (list, dict)) else val
    )

    citation = None
    quote = as_str(raw.get("quote"))
    page = raw.get("page")
    if quote and page is not None:
        try:
            citation = Citation(doc_id=doc_id, page=int(page), quote=quote[:400])
        except (ValueError, TypeError):
            citation = None

    return ExtractedValue(
        value=value,
        confidence=as_confidence(raw.get("confidence")),
        citation=citation,
    )


def _parse_line_items(raw: object) -> list[LineItem]:
    items: list[LineItem] = []
    for entry in as_list(raw):
        if not isinstance(entry, dict):
            continue
        amount = as_float(entry.get("amount"))
        if amount is None:
            continue
        items.append(
            LineItem(
                description=as_str(entry.get("description")) or "",
                amount=amount,
                quantity=as_float(entry.get("quantity")) or 1.0,
                code=as_str(entry.get("code")),
            )
        )
    return items


_NUMERIC_FIELDS = {
    "subtotal", "tax", "total", "coverage_limit", "deductible",
}


def extract_document(doc: Document, ctx: Context) -> DocExtraction:
    spec = _FIELD_SPECS.get(doc.doc_type)
    if spec is None:
        return DocExtraction(doc_id=doc.doc_id, doc_type=doc.doc_type)

    model_cls, fields, extra = spec

    # Size the document excerpt to what fits alongside the output budget in one
    # TPM window. Oversized prompts do not degrade gracefully — they 413.
    from claimiq.providers.model import LIMITER, effective_max_tokens

    room = LIMITER.budget - effective_max_tokens(Task.EXTRACT) - 700
    content_chars = max(2000, int(room * 3.2))

    prompt = _PROMPT.format(
        doc_type=doc.doc_type.value.replace("_", " "),
        fields=", ".join(fields),
        extra=extra,
        filename=doc.filename,
        content=doc.text[:content_chars],
    )

    try:
        r = invoke(prompt, task=Task.EXTRACT, system=SYSTEM)
    except Exception as e:  # noqa: BLE001 - one dead doc != dead claim
        return DocExtraction(
            doc_id=doc.doc_id, doc_type=doc.doc_type, error=f"{type(e).__name__}: {e}"
        )

    ctx.log(
        "extract", "model_call", doc=doc.doc_id, model=r.model, tokens=r.tokens,
        cost=r.cost_usd, latency=r.latency_s, attempts=r.attempts,
    )

    data, err = parse_json(r.text)
    if err or not isinstance(data, dict):
        return DocExtraction(doc_id=doc.doc_id, doc_type=doc.doc_type, error=err)

    payload = model_cls()
    for fname in fields:
        if not hasattr(payload, fname):
            continue
        setattr(
            payload,
            fname,
            _to_value(data.get(fname), doc.doc_id, numeric=fname in _NUMERIC_FIELDS),
        )

    if isinstance(payload, InvoiceData):
        payload.line_items = _parse_line_items(data.get("line_items"))

    out = DocExtraction(doc_id=doc.doc_id, doc_type=doc.doc_type)
    if isinstance(payload, InvoiceData):
        out.invoice = payload
    elif isinstance(payload, MedicalData):
        out.medical = payload
    elif isinstance(payload, PolicyData):
        out.policy = payload
    elif isinstance(payload, IncidentData):
        out.incident = payload
    return out


_RETRY_PROMPT = """You previously extracted these fields from this document but
reported low confidence in them. Look again, carefully, at ONLY these fields.

Fields to re-examine: {fields}

For each, return:
  {{"value": <value or null>, "confidence": <0.0-1.0>, "page": <n>,
    "quote": "<verbatim text proving this value>"}}

If the field genuinely is not stated in the document, return null with
confidence 0 — that is a correct answer, not a failure. Do not guess.

DOCUMENT ({filename}):
---
{content}
---

Return a single JSON object keyed by field name."""


def reextract_low_confidence(
    doc: Document, extraction: DocExtraction, ctx: Context, floor: float = 0.5
) -> int:
    """Targeted second pass over fields the model was unsure about.

    Cheap and bounded: only uncertain fields, only one retry, and a result is
    kept only if it is more confident than what it replaces. This is the
    mechanism behind the confidence numbers shown in the UI — the system knows
    which parts of its own output are weak and does something about it.
    """
    payload = extraction.payload()
    if payload is None:
        return 0

    weak = [
        name for name, ev in extraction.values().items()
        if ev.is_present and ev.confidence < floor
    ]
    if not weak:
        return 0

    from claimiq.providers.model import LIMITER, effective_max_tokens

    room = LIMITER.budget - effective_max_tokens(Task.EXTRACT) - 700
    prompt = _RETRY_PROMPT.format(
        fields=", ".join(weak),
        filename=doc.filename,
        content=doc.text[: max(2000, int(room * 3.2))],
    )

    try:
        r = invoke(prompt, task=Task.EXTRACT, system=SYSTEM)
    except Exception:  # noqa: BLE001 - the first-pass value stands
        return 0

    ctx.log(
        "extract", "model_call", doc=doc.doc_id, pass_="reextract",
        fields=weak, model=r.model, tokens=r.tokens, cost=r.cost_usd,
    )

    data, err = parse_json(r.text)
    if err or not isinstance(data, dict):
        return 0

    improved = 0
    for name in weak:
        if name not in data:
            continue
        candidate = _to_value(
            data.get(name), doc.doc_id, numeric=name in _NUMERIC_FIELDS
        )
        current = getattr(payload, name)
        if candidate.confidence > current.confidence:
            setattr(payload, name, candidate)
            improved += 1
    return improved


class ExtractStage:
    name = "extract"

    def __init__(
        self, max_workers: int | None = None, reextract: bool = True
    ) -> None:
        self.max_workers = max_workers
        self.reextract = reextract

    def _workers(self, doc_count: int) -> int:
        """Derive concurrency from the TPM budget rather than hard-coding it.

        On a constrained tier, fanning out wider than the budget allows does
        not speed anything up — every extra thread simply blocks in the rate
        limiter, and starving this stage silently produces empty extractions.
        """
        if self.max_workers is not None:
            return self.max_workers
        from claimiq.providers.model import LIMITER, effective_max_tokens

        per_call = effective_max_tokens(Task.EXTRACT) + 2000
        return max(1, min(6, doc_count, int(LIMITER.tpm // max(1, per_call))))

    def run(self, ctx: Context) -> None:
        docs = [d for d in ctx.result.documents if d.doc_type != DocType.UNKNOWN]
        if not docs:
            ctx.progress(self.name, "no classified documents")
            return

        workers = self._workers(len(docs))
        if workers == 1:
            extractions = [extract_document(d, ctx) for d in docs]
        else:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                extractions = list(pool.map(lambda d: extract_document(d, ctx), docs))

        # Second pass over uncertain fields only. Bounded by design: one retry,
        # weak fields only, and the result is kept only if more confident.
        improved = 0
        if self.reextract:
            by_id = {d.doc_id: d for d in docs}
            for e in extractions:
                if e.error:
                    continue
                doc = by_id.get(e.doc_id)
                if doc is not None:
                    improved += reextract_low_confidence(doc, e, ctx)

        ctx.result.extractions = extractions
        failed = [e.doc_id for e in extractions if e.error]

        # A claim with no extracted data is not a low-risk claim, it is an
        # unprocessed one. Downstream stages would happily score it as complete
        # and clean. Fail loudly instead: the checkpoint is not written, so a
        # resumed run retries this stage rather than inheriting the emptiness.
        if failed and len(failed) == len(extractions):
            raise RuntimeError(
                f"Extraction failed for all {len(failed)} documents. "
                f"First error: {extractions[0].error}"
            )

        # Total billed drives the ROI and exposure figures downstream.
        total = 0.0
        for e in extractions:
            if e.invoice and e.invoice.total.is_present:
                v = as_float(e.invoice.total.value)
                if v:
                    total += v
        ctx.result.total_billed = round(total, 2)

        ctx.log(
            self.name, "event",
            documents=len(docs), failed=failed, reextracted=improved,
            total_billed=ctx.result.total_billed,
        )
        ctx.progress(
            self.name,
            f"{len(docs) - len(failed)}/{len(docs)} extracted, "
            f"billed {ctx.result.total_billed:,.2f}"
            + (f", {improved} field(s) improved on retry" if improved else ""),
        )
