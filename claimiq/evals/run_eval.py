"""Evaluation harness.

Scores three things that matter independently:

  field accuracy   - did extraction get the values right?
  issue recall     - did we catch the problems we know are there?
  false positives  - did we invent problems on the clean claim?

Recall and false-positive rate are reported separately on purpose. A system
that flags everything scores perfect recall and is useless; a system that flags
nothing has zero false positives and is worse. An adjuster only trusts the tool
if both hold at once.

Run:  python -m claimiq.evals.run_eval
"""
from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from claimiq.core.parsing import as_float, as_str
from claimiq.core.schemas import ClaimResult, Severity
from claimiq.evals.gold import (
    EXPECTED_RECOMMENDATION,
    GOLD_FIELDS,
    GOLD_ISSUES,
)
from claimiq.pipeline import RESULT_ROOT, process_claim

CLAIMS_ROOT = Path("claimiq/data/claims")
_SEV_RANK = {s.value: s.rank for s in Severity}


def _values_match(expected: object, actual: object) -> bool:
    if expected is None or actual is None:
        return False
    if isinstance(expected, (int, float)):
        a = as_float(actual)
        return a is not None and abs(a - float(expected)) < 0.01
    e, a = as_str(expected), as_str(actual)
    if e is None or a is None:
        return False
    e, a = e.strip().lower(), a.strip().lower()
    return e == a or e in a or a in e


@dataclass
class FieldScore:
    correct: int = 0
    wrong: int = 0
    missed: int = 0
    details: list[str] = field(default_factory=list)

    @property
    def attempted(self) -> int:
        return self.correct + self.wrong

    @property
    def precision(self) -> float:
        return self.correct / self.attempted if self.attempted else 0.0

    @property
    def recall(self) -> float:
        total = self.correct + self.wrong + self.missed
        return self.correct / total if total else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if (p + r) else 0.0


def score_fields(result: ClaimResult, gold: dict) -> FieldScore:
    s = FieldScore()
    by_name = {d.filename: d.doc_id for d in result.documents}
    ext_by_doc = {e.doc_id: e for e in result.extractions}

    for filename, fields in gold.items():
        doc_id = by_name.get(filename)
        extraction = ext_by_doc.get(doc_id) if doc_id else None
        for fname, expected in fields.items():
            ev = extraction.values().get(fname) if extraction else None
            if ev is None or not ev.is_present:
                s.missed += 1
                s.details.append(f"MISSED {filename}.{fname} (expected {expected!r})")
            elif _values_match(expected, ev.value):
                s.correct += 1
            else:
                s.wrong += 1
                s.details.append(
                    f"WRONG  {filename}.{fname}: got {ev.value!r}, expected {expected!r}"
                )
    return s


def _finding_matches(finding, spec: dict) -> bool:
    """Every keyword group must be represented somewhere in the finding."""
    text = f"{finding.title} {finding.detail}".lower()
    if _SEV_RANK[finding.severity.value] > _SEV_RANK[spec["min_severity"]]:
        return False
    return all(any(kw in text for kw in group) for group in spec["must_match"])


@dataclass
class IssueScore:
    found: list[str] = field(default_factory=list)
    missed: list[str] = field(default_factory=list)
    false_positives: int = 0

    @property
    def recall(self) -> float:
        total = len(self.found) + len(self.missed)
        return len(self.found) / total if total else 1.0


def score_issues(result: ClaimResult, specs: list[dict]) -> IssueScore:
    s = IssueScore()
    matched_findings: set[int] = set()

    for spec in specs:
        hit = next(
            (
                i for i, f in enumerate(result.findings)
                if i not in matched_findings and _finding_matches(f, spec)
            ),
            None,
        )
        if hit is not None:
            s.found.append(spec["id"])
            matched_findings.add(hit)
        else:
            s.missed.append(spec["id"])

    # On a claim with no expected issues, any serious finding is a false alarm.
    if not specs:
        s.false_positives = sum(
            1 for f in result.findings
            if f.severity in (Severity.CRITICAL, Severity.HIGH)
        )
    return s


def main(rerun: bool = False) -> int:
    claim_ids = sorted(GOLD_ISSUES)
    print(f"ClaimIQ evaluation — {len(claim_ids)} claims\n" + "=" * 62)

    totals = FieldScore()
    all_found = all_missed = 0
    false_positives = 0
    rec_correct = 0
    wall_total = cost_total = 0.0
    rows = []

    for cid in claim_ids:
        cached = RESULT_ROOT / f"{cid}.json"
        if cached.exists() and not rerun:
            result = ClaimResult.model_validate_json(cached.read_text(encoding="utf-8"))
            source = "cached"
        else:
            t0 = time.time()
            result = process_claim(CLAIMS_ROOT / cid, cid, resume=False)
            source = f"{time.time() - t0:.0f}s"

        fs = score_fields(result, GOLD_FIELDS.get(cid, {}))
        iss = score_issues(result, GOLD_ISSUES.get(cid, []))

        totals.correct += fs.correct
        totals.wrong += fs.wrong
        totals.missed += fs.missed
        totals.details.extend(f"[{cid}] {d}" for d in fs.details)
        all_found += len(iss.found)
        all_missed += len(iss.missed)
        false_positives += iss.false_positives

        expected_rec = EXPECTED_RECOMMENDATION.get(cid)
        rec_ok = result.recommendation.value == expected_rec
        rec_correct += int(rec_ok)

        wall_total += result.stage_timings.get("_wall", 0.0)
        cost_total += result.usage.get("cost_usd", 0.0)

        print(f"\n{cid}  ({source})")
        print(f"  fields      {fs.correct}/{fs.correct + fs.wrong + fs.missed} correct"
              f"  (P {fs.precision:.2f} · R {fs.recall:.2f} · F1 {fs.f1:.2f})")
        print(f"  issues      {len(iss.found)}/{len(iss.found) + len(iss.missed)} detected"
              + (f"  · missed: {', '.join(iss.missed)}" if iss.missed else ""))
        if iss.false_positives:
            print(f"  FALSE POS   {iss.false_positives} serious finding(s) on a clean claim")
        print(f"  routing     {result.recommendation.value}"
              f" (expected {expected_rec}) {'✓' if rec_ok else '✗'}")
        print(f"  grounding   {result.usage.get('grounding_rate', 0):.0%}"
              f" of {result.usage.get('citations_checked', 0)} citations")

        rows.append({
            "claim_id": cid,
            "field_f1": round(fs.f1, 3),
            "issue_recall": round(iss.recall, 3),
            "false_positives": iss.false_positives,
            "recommendation_correct": rec_ok,
            "grounding": result.usage.get("grounding_rate", 0),
            "findings": len(result.findings),
        })

    issue_recall = all_found / (all_found + all_missed) if (all_found + all_missed) else 1.0

    print("\n" + "=" * 62)
    print("OVERALL")
    print(f"  Field extraction F1      {totals.f1:.3f}"
          f"   (P {totals.precision:.3f} · R {totals.recall:.3f})")
    print(f"  Known-issue recall       {issue_recall:.3f}"
          f"   ({all_found}/{all_found + all_missed} detected)")
    print(f"  False positives (clean)  {false_positives}")
    print(f"  Routing accuracy         {rec_correct}/{len(claim_ids)}")
    print(f"  Avg cost per claim       ${cost_total / len(claim_ids):.4f}")
    print(f"  Avg wall per claim       {wall_total / len(claim_ids):.0f}s")

    if totals.details:
        print("\nField-level misses:")
        for d in totals.details[:20]:
            print("  " + d)

    out = Path("data/eval_report.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {
                "field_f1": round(totals.f1, 4),
                "field_precision": round(totals.precision, 4),
                "field_recall": round(totals.recall, 4),
                "issue_recall": round(issue_recall, 4),
                "false_positives": false_positives,
                "routing_accuracy": f"{rec_correct}/{len(claim_ids)}",
                "avg_cost_usd": round(cost_total / len(claim_ids), 5),
                "per_claim": rows,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\nWrote {out}")

    # Non-zero exit on a quality regression makes this CI-usable.
    return 0 if (totals.f1 >= 0.85 and issue_recall >= 0.8 and false_positives == 0) else 1


if __name__ == "__main__":
    sys.exit(main(rerun="--rerun" in sys.argv))
