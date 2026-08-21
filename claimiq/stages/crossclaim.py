"""Cross-claim intelligence.

This is the analysis that only becomes possible once batch processing exists:
patterns visible across a book of claims but invisible inside any single one.

Deliberately not a vector database. At POC scale the signals that matter are
exact and near-exact identity matches — the same provider across unrelated
claimants, the same invoice number in two different claims, narratives that
are textually near-identical. Those are computed exactly with an inverted index
and shingled similarity, which is faster than embedding search, has no
false-neighbour problem, and is explainable to an investigator.

An embedding index earns its place at a scale this POC does not have, and for
the softer question of *semantically* similar (not textually similar)
narratives. That is the Phase 2 upgrade path, and the interface here does not
change when it happens.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from claimiq.core.parsing import as_float, as_str
from claimiq.core.schemas import ClaimResult, Severity

RESULT_ROOT = Path("data/runs")


@dataclass
class CrossClaimSignal:
    kind: str
    severity: Severity
    title: str
    detail: str
    claims: list[str] = field(default_factory=list)
    financial_impact: float | None = None

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "severity": self.severity.value,
            "title": self.title,
            "detail": self.detail,
            "claims": self.claims,
            "financial_impact": self.financial_impact,
        }


def _shingles(text: str, n: int = 5) -> set[str]:
    words = re.findall(r"[a-z]+", text.lower())
    if len(words) < n:
        return {" ".join(words)} if words else set()
    return {" ".join(words[i : i + n]) for i in range(len(words) - n + 1)}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def load_results(root: Path = RESULT_ROOT) -> list[ClaimResult]:
    out: list[ClaimResult] = []
    if not root.exists():
        return out
    for p in sorted(root.glob("*.json")):
        try:
            out.append(ClaimResult.model_validate_json(p.read_text(encoding="utf-8")))
        except Exception:  # noqa: BLE001 - skip unreadable artefacts
            continue
    return out


def analyse(results: list[ClaimResult]) -> list[CrossClaimSignal]:
    signals: list[CrossClaimSignal] = []
    if len(results) < 2:
        return signals

    providers: dict[str, set[str]] = defaultdict(set)
    invoice_numbers: dict[str, list[tuple[str, float]]] = defaultdict(list)
    claimants: dict[str, set[str]] = defaultdict(set)
    narratives: dict[str, set[str]] = {}

    for r in results:
        narrative_parts: list[str] = []
        for e in r.extractions:
            payload = e.payload()
            if payload is None:
                continue

            if e.invoice:
                prov = as_str(e.invoice.provider_name.value)
                if prov:
                    providers[prov.strip().lower()].add(r.claim_id)
                num = as_str(e.invoice.invoice_number.value)
                if num:
                    invoice_numbers[num.strip().upper()].append(
                        (r.claim_id, as_float(e.invoice.total.value) or 0.0)
                    )
                name = as_str(e.invoice.patient_name.value)
                if name:
                    claimants[name.strip().lower()].add(r.claim_id)

            if e.medical:
                name = as_str(e.medical.patient_name.value)
                if name:
                    claimants[name.strip().lower()].add(r.claim_id)

            if e.incident:
                desc = as_str(e.incident.description.value)
                if desc:
                    narrative_parts.append(desc)

        if narrative_parts:
            narratives[r.claim_id] = _shingles(" ".join(narrative_parts))

    # 1. Same invoice number appearing in two different claims.
    for num, entries in invoice_numbers.items():
        claims = {c for c, _ in entries}
        if len(claims) > 1:
            signals.append(
                CrossClaimSignal(
                    kind="cross_claim_duplicate_invoice",
                    severity=Severity.CRITICAL,
                    title=f"Invoice {num} billed against {len(claims)} separate claims",
                    detail=(
                        f"The same invoice number appears in claims "
                        f"{', '.join(sorted(claims))}. A single invoice billed to "
                        f"more than one claim is a direct duplicate-payment risk."
                    ),
                    claims=sorted(claims),
                    financial_impact=max(v for _, v in entries),
                )
            )

    # 2. One provider recurring across unrelated claimants.
    claim_to_claimants = defaultdict(set)
    for name, cids in claimants.items():
        for cid in cids:
            claim_to_claimants[cid].add(name)

    for prov, cids in providers.items():
        if len(cids) < 2:
            continue
        people = {n for c in cids for n in claim_to_claimants.get(c, set())}
        if len(people) > 1:
            signals.append(
                CrossClaimSignal(
                    kind="provider_concentration",
                    severity=Severity.MEDIUM,
                    title=f"Provider appears across {len(cids)} claims",
                    detail=(
                        f"'{prov}' bills on claims {', '.join(sorted(cids))} for "
                        f"{len(people)} different claimants. Legitimate for a large "
                        f"practice; worth review if the volume is disproportionate."
                    ),
                    claims=sorted(cids),
                )
            )

    # 3. Near-identical incident narratives across different claims.
    ids = sorted(narratives)
    for i, a in enumerate(ids):
        for b in ids[i + 1 :]:
            sim = _jaccard(narratives[a], narratives[b])
            if sim >= 0.55:
                signals.append(
                    CrossClaimSignal(
                        kind="narrative_similarity",
                        severity=Severity.HIGH,
                        title=f"Near-identical incident narratives: {a} and {b}",
                        detail=(
                            f"Incident descriptions are {sim:.0%} textually similar. "
                            f"Recycled narratives across claims are a common "
                            f"organised-fraud signal."
                        ),
                        claims=[a, b],
                    )
                )

    # 4. Same claimant across multiple claims.
    for name, cids in claimants.items():
        if len(cids) > 1:
            signals.append(
                CrossClaimSignal(
                    kind="repeat_claimant",
                    severity=Severity.MEDIUM,
                    title=f"Claimant appears on {len(cids)} claims",
                    detail=(
                        f"'{name.title()}' is named on claims "
                        f"{', '.join(sorted(cids))}. Verify these are distinct "
                        f"incidents rather than a resubmission."
                    ),
                    claims=sorted(cids),
                )
            )

    signals.sort(key=lambda s: s.severity.rank)
    return signals


def run(root: Path = RESULT_ROOT, write: bool = True) -> list[CrossClaimSignal]:
    results = load_results(root)
    signals = analyse(results)
    if write:
        out = Path("data/cross_claim.json")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(
                {
                    "claims_analysed": len(results),
                    "signals": [s.to_dict() for s in signals],
                },
                indent=2,
            ),
            encoding="utf-8",
        )
    return signals


if __name__ == "__main__":
    found = run()
    print(f"Cross-claim analysis over {len(load_results())} claims")
    print("=" * 60)
    if not found:
        print("No cross-claim signals detected.")
    for s in found:
        print(f"\n[{s.severity.value.upper()}] {s.title}")
        print(f"  {s.detail}")
        print(f"  claims: {', '.join(s.claims)}")
