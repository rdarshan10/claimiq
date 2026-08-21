"""Deterministic validation floor.

Arithmetic, date logic, duplicate detection and policy limits are computed in
Python, never by a model. This is the compliance answer: the numbers that drive
a finding are calculated by code that is inspectable and testable, and the model
is only ever asked to explain or to reason semantically on top.

It is also free and instant, which matters at 50k claims/year.
"""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import date, datetime

from claimiq.core.orchestrator import Context
from claimiq.core.parsing import as_float, as_list, as_str
from claimiq.core.schemas import (
    Citation,
    DocExtraction,
    DocType,
    Finding,
    FindingKind,
    Severity,
)

# Documents a complete claim pack is expected to contain.
REQUIRED_DOCS: dict[DocType, Severity] = {
    DocType.CLAIM_FORM: Severity.HIGH,
    DocType.MEDICAL_REPORT: Severity.HIGH,
    DocType.INVOICE: Severity.HIGH,
    DocType.POLICY: Severity.CRITICAL,
}

# Fields that must be present for a claim to be actionable.
REQUIRED_FIELDS: dict[DocType, list[str]] = {
    DocType.INVOICE: ["invoice_number", "invoice_date", "total", "provider_name"],
    DocType.MEDICAL_REPORT: ["patient_name", "diagnosis", "provider_name"],
    DocType.POLICY: ["policy_number", "effective_date", "expiry_date"],
    DocType.CLAIM_FORM: ["incident_date", "description"],
}

TOLERANCE = 0.02  # currency rounding


def _parse_date(v: object) -> date | None:
    s = as_str(v)
    if not s:
        return None
    s = s.strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d %B %Y", "%d %b %Y",
                "%B %d, %Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    if m := re.search(r"(\d{4})-(\d{2})-(\d{2})", s):
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    return None


def _cite(e: DocExtraction, field: str) -> list[Citation]:
    payload = e.payload()
    if payload is None:
        return []
    ev = getattr(payload, field, None)
    return [ev.citation] if ev is not None and ev.citation else []


# --------------------------------------------------------------------------
# Individual rules
# --------------------------------------------------------------------------
def check_completeness(ctx: Context) -> list[Finding]:
    findings: list[Finding] = []
    present_types = {d.doc_type for d in ctx.result.documents}

    for dtype, sev in REQUIRED_DOCS.items():
        if dtype not in present_types:
            findings.append(
                Finding(
                    kind=FindingKind.MISSING_DOCUMENT,
                    severity=sev,
                    title=f"Missing document: {dtype.value.replace('_', ' ')}",
                    detail=(
                        f"No {dtype.value.replace('_', ' ')} was found in the claim "
                        f"pack. This is required before the claim can be assessed."
                    ),
                    source="rule",
                )
            )

    # A discharge summary is expected whenever admission is documented.
    for e in ctx.result.extractions:
        if e.medical and e.medical.admission_date.is_present:
            if DocType.DISCHARGE_SUMMARY not in present_types:
                findings.append(
                    Finding(
                        kind=FindingKind.MISSING_DOCUMENT,
                        severity=Severity.MEDIUM,
                        title="Missing discharge summary",
                        detail=(
                            "An admission date is documented but no discharge "
                            "summary is present in the pack."
                        ),
                        citations=_cite(e, "admission_date"),
                        source="rule",
                    )
                )
            break

    for e in ctx.result.extractions:
        for field in REQUIRED_FIELDS.get(e.doc_type, []):
            payload = e.payload()
            ev = getattr(payload, field, None) if payload else None
            if ev is None or not ev.is_present:
                findings.append(
                    Finding(
                        kind=FindingKind.MISSING_FIELD,
                        severity=Severity.MEDIUM,
                        title=f"Missing field: {field.replace('_', ' ')}",
                        detail=(
                            f"Required field '{field}' could not be found in "
                            f"{e.doc_id} ({e.doc_type.value})."
                        ),
                        source="rule",
                    )
                )
    return findings


def check_arithmetic(ctx: Context) -> list[Finding]:
    findings: list[Finding] = []
    for e in ctx.result.extractions:
        inv = e.invoice
        if not inv or not inv.line_items:
            continue

        line_sum = round(sum(li.amount for li in inv.line_items), 2)
        subtotal = as_float(inv.subtotal.value)
        tax = as_float(inv.tax.value) or 0.0
        total = as_float(inv.total.value)

        if subtotal is not None and abs(line_sum - subtotal) > TOLERANCE:
            findings.append(
                Finding(
                    kind=FindingKind.ARITHMETIC,
                    severity=Severity.HIGH,
                    title="Line items do not sum to subtotal",
                    detail=(
                        f"Line items total {line_sum:,.2f} but the stated subtotal "
                        f"is {subtotal:,.2f} (difference {abs(line_sum - subtotal):,.2f})."
                    ),
                    citations=_cite(e, "subtotal"),
                    financial_impact=abs(line_sum - subtotal),
                    source="rule",
                )
            )

        if total is not None:
            base = subtotal if subtotal is not None else line_sum
            expected = round(base + tax, 2)
            if abs(expected - total) > TOLERANCE:
                findings.append(
                    Finding(
                        kind=FindingKind.ARITHMETIC,
                        severity=Severity.HIGH,
                        title="Invoice total does not reconcile",
                        detail=(
                            f"Subtotal {base:,.2f} plus tax {tax:,.2f} = "
                            f"{expected:,.2f}, but the invoice states {total:,.2f}."
                        ),
                        citations=_cite(e, "total"),
                        financial_impact=abs(expected - total),
                        source="rule",
                    )
                )
    return findings


def check_duplicates(ctx: Context) -> list[Finding]:
    """Exact and near-duplicate invoices — a classic leakage vector."""
    findings: list[Finding] = []
    by_number: dict[str, list[DocExtraction]] = defaultdict(list)
    invoices = [e for e in ctx.result.extractions if e.invoice]

    for e in invoices:
        num = as_str(e.invoice.invoice_number.value)
        if num:
            by_number[num.strip().upper()].append(e)

    for num, group in by_number.items():
        if len(group) < 2:
            continue
        totals = [as_float(g.invoice.total.value) or 0.0 for g in group]
        impact = max(totals) if totals else 0.0
        differing = len(set(totals)) > 1
        findings.append(
            Finding(
                kind=FindingKind.DUPLICATE,
                severity=Severity.CRITICAL,
                title=f"Duplicate invoice number: {num}",
                detail=(
                    f"Invoice number {num} appears on {len(group)} separate documents "
                    f"({', '.join(g.doc_id for g in group)}) with totals "
                    f"{', '.join(f'{t:,.2f}' for t in totals)}. "
                    + (
                        "The amounts differ, which suggests a resubmission with "
                        "added charges rather than a filing duplicate."
                        if differing
                        else "Amounts are identical — likely a duplicate submission."
                    )
                ),
                citations=[c for g in group for c in _cite(g, "invoice_number")],
                financial_impact=impact,
                source="rule",
            )
        )

    # Near-duplicates: same provider, same service window, similar totals.
    seen: set[tuple] = set()
    for i, a in enumerate(invoices):
        for b in invoices[i + 1 :]:
            na = as_str(a.invoice.invoice_number.value)
            nb = as_str(b.invoice.invoice_number.value)
            if na and nb and na.strip().upper() == nb.strip().upper():
                continue  # already reported above
            pa = (as_str(a.invoice.provider_name.value) or "").lower()
            pb = (as_str(b.invoice.provider_name.value) or "").lower()
            ta = as_float(a.invoice.total.value) or 0.0
            tb = as_float(b.invoice.total.value) or 0.0
            if not pa or pa != pb or ta == 0 or tb == 0:
                continue
            if abs(ta - tb) / max(ta, tb) < 0.10:
                key = tuple(sorted([a.doc_id, b.doc_id]))
                if key in seen:
                    continue
                seen.add(key)
                findings.append(
                    Finding(
                        kind=FindingKind.DUPLICATE,
                        severity=Severity.HIGH,
                        title="Near-duplicate invoices from same provider",
                        detail=(
                            f"{a.doc_id} ({ta:,.2f}) and {b.doc_id} ({tb:,.2f}) are "
                            f"from the same provider with totals within 10%. "
                            f"Verify these are distinct services."
                        ),
                        citations=_cite(a, "total") + _cite(b, "total"),
                        financial_impact=min(ta, tb),
                        source="rule",
                    )
                )
    return findings


def check_dates(ctx: Context) -> list[Finding]:
    findings: list[Finding] = []
    policy = next((e for e in ctx.result.extractions if e.policy), None)

    eff = exp = None
    if policy:
        eff = _parse_date(policy.policy.effective_date.value)
        exp = _parse_date(policy.policy.expiry_date.value)

    for e in ctx.result.extractions:
        # Admission must precede discharge.
        if e.medical:
            adm = _parse_date(e.medical.admission_date.value)
            dis = _parse_date(e.medical.discharge_date.value)
            if adm and dis and dis < adm:
                findings.append(
                    Finding(
                        kind=FindingKind.DATE_LOGIC,
                        severity=Severity.HIGH,
                        title="Discharge precedes admission",
                        detail=f"Admission {adm} is after discharge {dis}.",
                        citations=_cite(e, "admission_date") + _cite(e, "discharge_date"),
                        source="rule",
                    )
                )

        # Treatment dates must fall inside the period of insurance.
        if e.invoice and (eff or exp):
            for field in ("service_date_from", "service_date_to", "invoice_date"):
                d = _parse_date(getattr(e.invoice, field).value)
                if not d:
                    continue
                if exp and d > exp:
                    findings.append(
                        Finding(
                            kind=FindingKind.POLICY_BREACH,
                            severity=Severity.CRITICAL,
                            title="Service date falls outside the period of insurance",
                            detail=(
                                f"{field.replace('_', ' ').title()} of {d} on "
                                f"{e.doc_id} is after policy expiry {exp}. "
                                f"Charges after expiry are not covered."
                            ),
                            citations=_cite(e, field) + _cite(policy, "expiry_date"),
                            financial_impact=as_float(e.invoice.total.value),
                            source="rule",
                        )
                    )
                    break
                if eff and d < eff:
                    findings.append(
                        Finding(
                            kind=FindingKind.POLICY_BREACH,
                            severity=Severity.CRITICAL,
                            title="Service date precedes policy inception",
                            detail=(
                                f"{field.replace('_', ' ').title()} of {d} on "
                                f"{e.doc_id} is before policy inception {eff}."
                            ),
                            citations=_cite(e, field) + _cite(policy, "effective_date"),
                            financial_impact=as_float(e.invoice.total.value),
                            source="rule",
                        )
                    )
                    break
    return findings


def check_policy_limits(ctx: Context) -> list[Finding]:
    """Not just complete — payable. This is leakage prevention with a number."""
    findings: list[Finding] = []
    policy = next((e for e in ctx.result.extractions if e.policy), None)
    if not policy:
        return findings

    billed = ctx.result.total_billed
    limit = as_float(policy.policy.coverage_limit.value)
    if limit and billed > limit:
        findings.append(
            Finding(
                kind=FindingKind.POLICY_BREACH,
                severity=Severity.HIGH,
                title="Billed amount exceeds coverage limit",
                detail=(
                    f"Total billed {billed:,.2f} exceeds the policy medical expenses "
                    f"limit of {limit:,.2f}. Excess of {billed - limit:,.2f} is not "
                    f"recoverable."
                ),
                citations=_cite(policy, "coverage_limit"),
                financial_impact=billed - limit,
                source="rule",
            )
        )

    # Sub-limits, matched by category keyword against invoice line items.
    sublimits = policy.policy.sublimits.value
    if isinstance(sublimits, dict):
        for category, cap in sublimits.items():
            cap_v = as_float(cap)
            if not cap_v:
                continue
            key = category.lower().split()[0] if category.split() else ""
            if not key:
                continue
            spent = 0.0
            cited: list[Citation] = []
            for e in ctx.result.extractions:
                if not e.invoice:
                    continue
                for li in e.invoice.line_items:
                    if key in li.description.lower():
                        spent += li.amount
                if spent and e.invoice.total.citation:
                    cited.append(e.invoice.total.citation)
            if spent > cap_v:
                findings.append(
                    Finding(
                        kind=FindingKind.POLICY_BREACH,
                        severity=Severity.HIGH,
                        title=f"Sub-limit exceeded: {category}",
                        detail=(
                            f"Charges matching '{category}' total {spent:,.2f} against "
                            f"a sub-limit of {cap_v:,.2f}. Excess {spent - cap_v:,.2f} "
                            f"falls outside cover."
                        ),
                        citations=cited[:2] + _cite(policy, "sublimits"),
                        financial_impact=spent - cap_v,
                        source="rule",
                    )
                )
    return findings


def check_confidence(ctx: Context) -> list[Finding]:
    """Surface fields the model itself was unsure about."""
    findings: list[Finding] = []
    LOW = 0.5
    for e in ctx.result.extractions:
        weak = [
            name
            for name, ev in e.values().items()
            if ev.is_present and ev.confidence < LOW
        ]
        if weak:
            findings.append(
                Finding(
                    kind=FindingKind.LOW_CONFIDENCE,
                    severity=Severity.LOW,
                    title=f"Low-confidence extraction in {e.doc_id}",
                    detail=(
                        f"The extractor reported low confidence for: "
                        f"{', '.join(weak)}. Recommend manual verification."
                    ),
                    source="rule",
                    confidence=0.5,
                )
            )
    return findings


ALL_RULES = [
    check_completeness,
    check_arithmetic,
    check_duplicates,
    check_dates,
    check_policy_limits,
    check_confidence,
]


class RulesStage:
    name = "rules"

    def run(self, ctx: Context) -> None:
        found: list[Finding] = []
        for rule in ALL_RULES:
            try:
                produced = rule(ctx)
                found.extend(produced)
                ctx.log(self.name, "rule", rule=rule.__name__, findings=len(produced))
            except Exception as e:  # noqa: BLE001 - one bad rule != dead claim
                ctx.result.errors.append(f"rule {rule.__name__}: {e}")

        ctx.result.findings.extend(found)
        ctx.progress(self.name, f"{len(found)} deterministic findings")
