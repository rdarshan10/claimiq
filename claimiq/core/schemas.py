"""Typed contracts for the pipeline.

Everything crossing a stage boundary is a Pydantic model. Two reasons that
matters here: malformed model output fails loudly at the boundary rather than
silently corrupting a downstream flag, and Citation is mandatory on every
extracted value — an extraction with no source cannot be represented.
"""
from __future__ import annotations

from datetime import date
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator


# --------------------------------------------------------------------------
# Provenance
# --------------------------------------------------------------------------
class Citation(BaseModel):
    """Where a value came from. Required — this is the hallucination defence."""

    doc_id: str
    page: int = Field(ge=1)
    quote: str = Field(min_length=1, max_length=400, description="Verbatim source span")

    def label(self) -> str:
        return f"{self.doc_id} p.{self.page}"


class ExtractedValue(BaseModel):
    """A single extracted datum with provenance and self-reported confidence."""

    value: Any = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    citation: Citation | None = None
    grounded: bool | None = Field(
        default=None, description="Set by the grounding verifier, not the model"
    )

    @property
    def is_present(self) -> bool:
        return self.value not in (None, "", [], {})


# --------------------------------------------------------------------------
# Documents
# --------------------------------------------------------------------------
class DocType(str, Enum):
    MEDICAL_REPORT = "medical_report"
    INVOICE = "invoice"
    POLICE_REPORT = "police_report"
    POLICY = "policy"
    DISCHARGE_SUMMARY = "discharge_summary"
    PRESCRIPTION = "prescription"
    CLAIM_FORM = "claim_form"
    CORRESPONDENCE = "correspondence"
    PHOTO_EVIDENCE = "photo_evidence"
    UNKNOWN = "unknown"


class Page(BaseModel):
    number: int = Field(ge=1)
    text: str = ""


class Document(BaseModel):
    doc_id: str
    filename: str
    pages: list[Page] = Field(default_factory=list)
    doc_type: DocType = DocType.UNKNOWN
    type_confidence: float = 0.0

    @property
    def text(self) -> str:
        return "\n\n".join(f"[page {p.number}]\n{p.text}" for p in self.pages)

    @property
    def page_count(self) -> int:
        return len(self.pages)

    def char_count(self) -> int:
        return sum(len(p.text) for p in self.pages)


# --------------------------------------------------------------------------
# Extracted payloads, per document type
# --------------------------------------------------------------------------
class LineItem(BaseModel):
    description: str = ""
    amount: float = 0.0
    quantity: float = 1.0
    code: str | None = Field(default=None, description="CPT/HCPCS/procedure code")


class InvoiceData(BaseModel):
    invoice_number: ExtractedValue = Field(default_factory=ExtractedValue)
    invoice_date: ExtractedValue = Field(default_factory=ExtractedValue)
    provider_name: ExtractedValue = Field(default_factory=ExtractedValue)
    provider_npi: ExtractedValue = Field(default_factory=ExtractedValue)
    patient_name: ExtractedValue = Field(default_factory=ExtractedValue)
    subtotal: ExtractedValue = Field(default_factory=ExtractedValue)
    tax: ExtractedValue = Field(default_factory=ExtractedValue)
    total: ExtractedValue = Field(default_factory=ExtractedValue)
    currency: ExtractedValue = Field(default_factory=ExtractedValue)
    line_items: list[LineItem] = Field(default_factory=list)
    service_date_from: ExtractedValue = Field(default_factory=ExtractedValue)
    service_date_to: ExtractedValue = Field(default_factory=ExtractedValue)


class MedicalData(BaseModel):
    patient_name: ExtractedValue = Field(default_factory=ExtractedValue)
    patient_dob: ExtractedValue = Field(default_factory=ExtractedValue)
    provider_name: ExtractedValue = Field(default_factory=ExtractedValue)
    diagnosis: ExtractedValue = Field(default_factory=ExtractedValue)
    icd_codes: ExtractedValue = Field(default_factory=ExtractedValue)
    procedures: ExtractedValue = Field(default_factory=ExtractedValue)
    treatment_plan: ExtractedValue = Field(default_factory=ExtractedValue)
    admission_date: ExtractedValue = Field(default_factory=ExtractedValue)
    discharge_date: ExtractedValue = Field(default_factory=ExtractedValue)
    incident_date: ExtractedValue = Field(default_factory=ExtractedValue)
    body_parts: ExtractedValue = Field(default_factory=ExtractedValue)
    prior_conditions: ExtractedValue = Field(default_factory=ExtractedValue)


class PolicyData(BaseModel):
    policy_number: ExtractedValue = Field(default_factory=ExtractedValue)
    policyholder: ExtractedValue = Field(default_factory=ExtractedValue)
    effective_date: ExtractedValue = Field(default_factory=ExtractedValue)
    expiry_date: ExtractedValue = Field(default_factory=ExtractedValue)
    coverage_limit: ExtractedValue = Field(default_factory=ExtractedValue)
    deductible: ExtractedValue = Field(default_factory=ExtractedValue)
    sublimits: ExtractedValue = Field(default_factory=ExtractedValue)
    exclusions: ExtractedValue = Field(default_factory=ExtractedValue)


class IncidentData(BaseModel):
    incident_date: ExtractedValue = Field(default_factory=ExtractedValue)
    incident_location: ExtractedValue = Field(default_factory=ExtractedValue)
    description: ExtractedValue = Field(default_factory=ExtractedValue)
    parties: ExtractedValue = Field(default_factory=ExtractedValue)
    report_number: ExtractedValue = Field(default_factory=ExtractedValue)
    claimant_statement: ExtractedValue = Field(default_factory=ExtractedValue)


class DocExtraction(BaseModel):
    """Per-document extraction result, tagged by type."""

    doc_id: str
    doc_type: DocType
    invoice: InvoiceData | None = None
    medical: MedicalData | None = None
    policy: PolicyData | None = None
    incident: IncidentData | None = None
    error: str | None = None

    def payload(self) -> BaseModel | None:
        return self.invoice or self.medical or self.policy or self.incident

    def values(self) -> dict[str, ExtractedValue]:
        """Flatten to {field_name: ExtractedValue} for verification and scoring."""
        p = self.payload()
        if p is None:
            return {}
        return {
            k: v for k, v in p.__dict__.items() if isinstance(v, ExtractedValue)
        }


# --------------------------------------------------------------------------
# Findings
# --------------------------------------------------------------------------
class Severity(str, Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"

    @property
    def rank(self) -> int:
        return {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}[self.value]


class FindingKind(str, Enum):
    MISSING_DOCUMENT = "missing_document"
    MISSING_FIELD = "missing_field"
    ARITHMETIC = "arithmetic"
    DATE_LOGIC = "date_logic"
    DUPLICATE = "duplicate"
    CROSS_DOC_CONFLICT = "cross_doc_conflict"
    CODING_MISMATCH = "coding_mismatch"
    POLICY_BREACH = "policy_breach"
    NARRATIVE_CONFLICT = "narrative_conflict"
    FRAUD_SIGNAL = "fraud_signal"
    UNGROUNDED = "ungrounded"
    LOW_CONFIDENCE = "low_confidence"


class Finding(BaseModel):
    kind: FindingKind
    severity: Severity
    title: str
    detail: str
    citations: list[Citation] = Field(default_factory=list)
    source: str = Field(default="rule", description="'rule' or a reasoner name")
    financial_impact: float | None = None
    confidence: float = 1.0

    def sort_key(self) -> tuple:
        return (self.severity.rank, -(self.financial_impact or 0.0))


# --------------------------------------------------------------------------
# Claim-level result
# --------------------------------------------------------------------------
class Recommendation(str, Enum):
    AUTO_APPROVE = "auto_approve"
    REVIEW = "review"
    INVESTIGATE = "investigate"


class Completeness(BaseModel):
    score: float = Field(ge=0.0, le=1.0, default=0.0)
    present: list[str] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)

    @property
    def pct(self) -> int:
        return round(self.score * 100)


class ClaimSummary(BaseModel):
    headline: str = ""
    narrative: str = ""
    key_facts: dict[str, str] = Field(default_factory=dict)
    timeline: list[str] = Field(default_factory=list)


class ClaimResult(BaseModel):
    claim_id: str
    documents: list[Document] = Field(default_factory=list)
    extractions: list[DocExtraction] = Field(default_factory=list)
    summary: ClaimSummary = Field(default_factory=ClaimSummary)
    completeness: Completeness = Field(default_factory=Completeness)
    findings: list[Finding] = Field(default_factory=list)
    recommendation: Recommendation = Recommendation.REVIEW
    risk_score: float = 0.0
    total_billed: float = 0.0
    exposure: float | None = None
    usage: dict[str, Any] = Field(default_factory=dict)
    stage_timings: dict[str, float] = Field(default_factory=dict)
    errors: list[str] = Field(default_factory=list)

    def sorted_findings(self) -> list[Finding]:
        return sorted(self.findings, key=lambda f: f.sort_key())

    def by_severity(self, s: Severity) -> list[Finding]:
        return [f for f in self.findings if f.severity == s]

    @property
    def is_clean(self) -> bool:
        return not any(
            f.severity in (Severity.CRITICAL, Severity.HIGH) for f in self.findings
        )
