"""Gold-standard labels for the synthetic claim packs.

Hand-written from the source documents. These are what "correct" means, and
they are what makes an accuracy claim defensible rather than anecdotal.
"""
from __future__ import annotations

# Expected field values, per claim, per document type.
GOLD_FIELDS: dict[str, dict[str, dict[str, object]]] = {
    "CLM-2024-0917": {
        "03_physio_invoice.txt": {
            "invoice_number": "OPA-2024-3387",
            "total": 630.00,
            "subtotal": 630.00,
        },
        "02_medical_report.txt": {
            "patient_name": "Margaret A. Whitfield",
            "admission_date": "2024-03-09",
        },
        "05_policy_schedule.txt": {
            "policy_number": "POL-88213-A",
            "effective_date": "2024-01-01",
            "expiry_date": "2024-12-31",
        },
    },
    "CLM-2024-1043": {
        "03_invoice.txt": {"invoice_number": "ROC-5512", "total": 505.00},
        "04_policy_schedule.txt": {
            "policy_number": "POL-91556-C",
            "effective_date": "2024-02-15",
            "expiry_date": "2025-02-14",
        },
    },
    "CLM-2024-1188": {
        "03_invoice_physio.txt": {
            "invoice_number": "NSS-2024-0881",
            "total": 5568.00,
            "subtotal": 4640.00,
        },
        "05_invoice_surgical.txt": {
            "invoice_number": "PPO-19947",
            "total": 10030.00,
        },
        "06_policy_schedule.txt": {
            "policy_number": "POL-77401-B",
            "effective_date": "2023-07-01",
            "expiry_date": "2024-06-30",
        },
    },
}

# Issues the system must detect, described by the concepts that must appear in
# a finding. Matching is keyword-based rather than exact-string, because the
# wording is model-generated and only the substance is being asserted.
GOLD_ISSUES: dict[str, list[dict]] = {
    "CLM-2024-0917": [],  # clean claim — any HIGH/CRITICAL finding is a false positive
    "CLM-2024-1043": [
        {
            "id": "missing_radiology",
            "must_match": [["radiology", "x-ray", "imaging", "discharge", "missing",
                            "pending", "incomplete", "fracture clinic"]],
            "min_severity": "medium",
        },
    ],
    "CLM-2024-1188": [
        {
            "id": "duplicate_invoice",
            "must_match": [["duplicate", "same invoice", "identical"],
                           ["nss-2024-0881", "invoice number", "invoice"]],
            "min_severity": "high",
        },
        {
            "id": "post_expiry_surgery",
            "must_match": [["expiry", "expired", "lapse", "lapsed", "outside",
                            "period of insurance", "after policy"],
                           ["policy", "cover", "insurance"]],
            "min_severity": "high",
        },
        {
            "id": "acl_not_indicated",
            "must_match": [["acl", "knee", "arthroscop", "reconstruction"],
                           ["no", "not", "normal", "stable", "without", "despite",
                            "unsupported", "lack"]],
            "min_severity": "high",
        },
        {
            "id": "physio_sublimit",
            "must_match": [["sub-limit", "sublimit", "limit", "exceed"],
                           ["physiotherapy", "physio"]],
            "min_severity": "medium",
        },
        {
            "id": "admission_contradiction",
            "must_match": [["admitted", "admission", "overnight", "inpatient",
                            "ambulance"],
                           ["discharged same day", "not admitted", "same day",
                            "contradic", "conflict", "self-presented", "no ambulance",
                            "not supported"]],
            "min_severity": "medium",
        },
    ],
}

EXPECTED_RECOMMENDATION = {
    "CLM-2024-0917": "auto_approve",
    "CLM-2024-1043": "review",
    "CLM-2024-1188": "investigate",
}
