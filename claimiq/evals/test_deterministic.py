"""Unit tests for the parts that must never be wrong.

Everything here runs offline in under a second — no model, no credentials.
That is the point: the layer that decides whether numbers add up and whether a
date falls inside the policy period is ordinary testable code.

Run:  python -m claimiq.evals.test_deterministic
      (or `pytest claimiq/evals/test_deterministic.py` if pytest is installed)
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

from claimiq.core.orchestrator import CheckpointStore, Context
from claimiq.core.parsing import as_confidence, as_float, parse_json
from claimiq.core.schemas import (
    Citation,
    ClaimResult,
    DocExtraction,
    DocType,
    Document,
    ExtractedValue,
    Finding,
    FindingKind,
    InvoiceData,
    LineItem,
    Page,
    PolicyData,
    Severity,
)
from claimiq.stages.rules import (
    _parse_date,
    check_arithmetic,
    check_dates,
    check_duplicates,
    check_policy_limits,
)
from claimiq.stages.verify import quote_is_present

_failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        _failures.append(name)


def _ctx(extractions: list[DocExtraction], docs: list[Document] | None = None) -> Context:
    result = ClaimResult(claim_id="TEST", extractions=extractions, documents=docs or [])
    result.total_billed = sum(
        as_float(e.invoice.total.value) or 0.0 for e in extractions if e.invoice
    )
    return Context(claim_id="TEST", result=result, workdir=Path("."))


def _val(v, page: int = 1, quote: str = "q") -> ExtractedValue:
    return ExtractedValue(
        value=v, confidence=1.0, citation=Citation(doc_id="d1", page=page, quote=quote)
    )


def _invoice(**kw) -> InvoiceData:
    inv = InvoiceData()
    for k, v in kw.items():
        if k == "line_items":
            inv.line_items = v
        else:
            setattr(inv, k, _val(v))
    return inv


# --------------------------------------------------------------------------
def test_parsing() -> None:
    print("\nparsing")
    check("plain json", parse_json('{"a": 1}')[0] == {"a": 1})
    check("fenced json", parse_json('```json\n{"a": 1}\n```')[0] == {"a": 1})
    check("prose-wrapped json", parse_json('Sure!\n{"a": 1}\nDone')[0] == {"a": 1})
    check("trailing comma repaired", parse_json('{"a": 1,}')[0] == {"a": 1})
    check("think block stripped",
          parse_json('<think>hmm</think>{"a": 1}')[0] == {"a": 1})
    check("empty is an error", parse_json("")[1] is not None)
    check("garbage is an error", parse_json("not json at all")[1] is not None)
    check("brace inside string survives",
          parse_json('{"a": "x{y"}')[0] == {"a": "x{y"})

    check("currency string", as_float("$1,650.00") == 1650.0)
    check("gbp prefix", as_float("GBP 630") == 630.0)
    check("plain number passthrough", as_float(42) == 42.0)
    check("bool is not a number", as_float(True) is None)
    check("percent confidence clamped", as_confidence(85) == 0.85)
    check("word confidence", as_confidence("high") == 0.9)
    check("out of range clamped", as_confidence(1.7) == 1.0)


def test_dates() -> None:
    print("\ndate parsing")
    check("iso", _parse_date("2024-06-30") == date(2024, 6, 30))
    check("uk slash", _parse_date("30/06/2024") == date(2024, 6, 30))
    check("long form", _parse_date("30 June 2024") == date(2024, 6, 30))
    check("embedded iso", _parse_date("dated 2024-06-30 approx") == date(2024, 6, 30))
    check("nonsense is None", _parse_date("sometime last year") is None)
    check("none is None", _parse_date(None) is None)


def test_arithmetic() -> None:
    print("\narithmetic rule")
    good = DocExtraction(
        doc_id="d1", doc_type=DocType.INVOICE,
        invoice=_invoice(
            subtotal=100.0, tax=20.0, total=120.0,
            line_items=[LineItem(description="a", amount=60.0),
                        LineItem(description="b", amount=40.0)],
        ),
    )
    check("consistent invoice is silent", check_arithmetic(_ctx([good])) == [])

    bad_sum = DocExtraction(
        doc_id="d2", doc_type=DocType.INVOICE,
        invoice=_invoice(
            subtotal=100.0, tax=0.0, total=100.0,
            line_items=[LineItem(description="a", amount=60.0),
                        LineItem(description="b", amount=55.0)],
        ),
    )
    f = check_arithmetic(_ctx([bad_sum]))
    check("line items not summing is caught", len(f) == 1, f"got {len(f)}")
    check("impact is the difference",
          f and abs(f[0].financial_impact - 15.0) < 0.01)

    bad_total = DocExtraction(
        doc_id="d3", doc_type=DocType.INVOICE,
        invoice=_invoice(
            subtotal=100.0, tax=20.0, total=999.0,
            line_items=[LineItem(description="a", amount=100.0)],
        ),
    )
    check("total not reconciling is caught", len(check_arithmetic(_ctx([bad_total]))) == 1)

    rounding = DocExtraction(
        doc_id="d4", doc_type=DocType.INVOICE,
        invoice=_invoice(
            subtotal=100.01, tax=0.0, total=100.0,
            line_items=[LineItem(description="a", amount=100.01)],
        ),
    )
    check("one-penny rounding tolerated", check_arithmetic(_ctx([rounding])) == [])


def test_duplicates() -> None:
    print("\nduplicate rule")
    a = DocExtraction(doc_id="a", doc_type=DocType.INVOICE,
                      invoice=_invoice(invoice_number="INV-1", total=100.0,
                                       provider_name="Clinic X"))
    b = DocExtraction(doc_id="b", doc_type=DocType.INVOICE,
                      invoice=_invoice(invoice_number="INV-1", total=115.0,
                                       provider_name="Clinic X"))
    f = check_duplicates(_ctx([a, b]))
    check("same invoice number flagged", len(f) >= 1)
    check("severity is critical", f and f[0].severity == Severity.CRITICAL)
    check("differing totals noted", f and "differ" in f[0].detail.lower())

    c = DocExtraction(doc_id="c", doc_type=DocType.INVOICE,
                      invoice=_invoice(invoice_number="INV-2", total=500.0,
                                       provider_name="Clinic Y"))
    d = DocExtraction(doc_id="d", doc_type=DocType.INVOICE,
                      invoice=_invoice(invoice_number="INV-3", total=9000.0,
                                       provider_name="Clinic Z"))
    check("distinct invoices are silent", check_duplicates(_ctx([c, d])) == [])

    e = DocExtraction(doc_id="e", doc_type=DocType.INVOICE,
                      invoice=_invoice(invoice_number="INV-4", total=1000.0,
                                       provider_name="Clinic Q"))
    g = DocExtraction(doc_id="g", doc_type=DocType.INVOICE,
                      invoice=_invoice(invoice_number="INV-5", total=1050.0,
                                       provider_name="Clinic Q"))
    check("near-duplicate same provider flagged", len(check_duplicates(_ctx([e, g]))) == 1)


def test_policy_dates() -> None:
    print("\npolicy period rule")
    policy = DocExtraction(
        doc_id="p", doc_type=DocType.POLICY,
        policy=PolicyData(
            policy_number=_val("POL-1"),
            effective_date=_val("2023-07-01"),
            expiry_date=_val("2024-06-30"),
        ),
    )
    inside = DocExtraction(
        doc_id="i", doc_type=DocType.INVOICE,
        invoice=_invoice(invoice_number="A", total=100.0,
                         service_date_from="2024-01-10", service_date_to="2024-02-10"),
    )
    check("service inside period is silent", check_dates(_ctx([policy, inside])) == [])

    after = DocExtraction(
        doc_id="j", doc_type=DocType.INVOICE,
        invoice=_invoice(invoice_number="B", total=10030.0,
                         service_date_from="2024-08-01", service_date_to="2024-08-05"),
    )
    f = check_dates(_ctx([policy, after]))
    check("post-expiry service is caught", len(f) == 1, f"got {len(f)}")
    check("post-expiry is critical", f and f[0].severity == Severity.CRITICAL)
    check("exposure attached", f and f[0].financial_impact == 10030.0)

    before = DocExtraction(
        doc_id="k", doc_type=DocType.INVOICE,
        invoice=_invoice(invoice_number="C", total=200.0,
                         service_date_from="2023-01-01", service_date_to="2023-01-05"),
    )
    check("pre-inception service is caught", len(check_dates(_ctx([policy, before]))) == 1)


def test_sublimits() -> None:
    print("\nsub-limit rule")
    policy = DocExtraction(
        doc_id="p", doc_type=DocType.POLICY,
        policy=PolicyData(
            policy_number=_val("POL-1"),
            coverage_limit=_val(12000.0),
            sublimits=_val({"Physiotherapy": 2000.0}),
        ),
    )
    over = DocExtraction(
        doc_id="i", doc_type=DocType.INVOICE,
        invoice=_invoice(
            invoice_number="A", total=3420.0,
            line_items=[LineItem(description="Physiotherapy - intensive", amount=3420.0)],
        ),
    )
    f = check_policy_limits(_ctx([policy, over]))
    check("sub-limit breach caught", len(f) == 1, f"got {len(f)}")
    check("excess is the impact", f and abs(f[0].financial_impact - 1420.0) < 0.01)

    under = DocExtraction(
        doc_id="u", doc_type=DocType.INVOICE,
        invoice=_invoice(
            invoice_number="B", total=500.0,
            line_items=[LineItem(description="Physiotherapy session", amount=500.0)],
        ),
    )
    check("within sub-limit is silent", check_policy_limits(_ctx([policy, under])) == [])


def test_grounding() -> None:
    print("\ngrounding verification")
    page = ("Diagnosis: Mild lumbar soft tissue strain - ICD-10 S33.5\n"
            "Disposal: Discharged home same day, ambulant. NOT ADMITTED.")
    check("exact quote found", quote_is_present("NOT ADMITTED", page)[0])
    check("case-insensitive match", quote_is_present("not admitted", page)[0])
    check("punctuation-tolerant match",
          quote_is_present("Discharged home same day ambulant", page)[0])
    check("fabricated quote rejected",
          not quote_is_present("Patient admitted to intensive care", page)[0])
    check("empty quote rejected", not quote_is_present("", page)[0])
    check("decimal preserved for money",
          quote_is_present("ICD-10 S33.5", page)[0])


def test_checkpoints() -> None:
    print("\ncheckpoint store")
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        store = CheckpointStore(Path(tmp))
        r = ClaimResult(claim_id="C1", total_billed=123.45)
        r.findings.append(
            Finding(kind=FindingKind.DUPLICATE, severity=Severity.HIGH,
                    title="t", detail="d")
        )
        store.save("C1", "extract", r)

        loaded = store.load("C1", "extract")
        check("round-trips", loaded is not None and loaded.total_billed == 123.45)
        check("findings survive", loaded and len(loaded.findings) == 1)
        check("stage recorded", store.completed_stages("C1") == {"extract"})
        check("missing stage is None", store.load("C1", "reason") is None)

        store.clear("C1")
        check("clear removes checkpoints", store.completed_stages("C1") == set())


def test_ingest_pagination() -> None:
    print("\ningestion")
    from claimiq.stages.ingest import _paginate_text

    marked = "[page 1]\nfirst\n\n[page 2]\nsecond"
    pages = _paginate_text(marked)
    check("explicit markers honoured",
          len(pages) == 2 and pages[1].number == 2 and "second" in pages[1].text)

    short = _paginate_text("just a little text")
    check("short text is one page", len(short) == 1)

    long_text = "\n\n".join(f"paragraph {i} " + "x" * 200 for i in range(40))
    many = _paginate_text(long_text, chars_per_page=1000)
    check("long text paginates", len(many) > 1)
    check("page numbers sequential",
          [p.number for p in many] == list(range(1, len(many) + 1)))


def test_degradation_is_loud() -> None:
    """Regression: a claim whose extraction failed must never look clean.

    Observed in a real batch run — the circuit breaker tripped on rate-limit
    errors, every extraction failed, and the pipeline still produced a scored
    result with billed=0 and no critical findings.
    """
    print("\nsilent-degradation guards")
    from claimiq.stages.extract import ExtractStage
    from claimiq.stages.score import recommend

    ctx = _ctx([])
    ctx.result.documents = [
        Document(doc_id="d1", filename="f.txt", doc_type=DocType.INVOICE,
                 pages=[Page(number=1, text="x")]),
    ]
    ctx.result.extractions = [
        DocExtraction(doc_id="d1", doc_type=DocType.INVOICE, error="boom"),
    ]
    ctx.result.completeness.score = 1.0

    check("all-failed extraction cannot auto-approve",
          recommend(ctx).value == "review", f"got {recommend(ctx).value}")

    ctx2 = _ctx([])
    ctx2.result.completeness.score = 1.0
    ctx2.result.errors = ["reasoner exploded"]
    check("stage errors cannot auto-approve",
          recommend(ctx2).value == "review")

    # A totally failed extract stage must raise rather than checkpoint empty.
    ctx3 = _ctx([])
    ctx3.result.documents = [
        Document(doc_id="d1", filename="a.txt", doc_type=DocType.INVOICE,
                 pages=[Page(number=1, text="x")]),
    ]
    import claimiq.stages.extract as ex

    original = ex.extract_document
    ex.extract_document = lambda d, c: DocExtraction(
        doc_id=d.doc_id, doc_type=d.doc_type, error="provider down"
    )
    try:
        ExtractStage(max_workers=1).run(ctx3)
        check("total extraction failure raises", False, "no exception raised")
    except RuntimeError:
        check("total extraction failure raises", True)
    finally:
        ex.extract_document = original


def test_rate_limit_not_a_circuit_fault() -> None:
    print("\ncircuit breaker")
    from claimiq.providers.model import CircuitBreaker

    b = CircuitBreaker(threshold=3, cooldown_s=60)
    for _ in range(5):
        b.fail()
    try:
        b.check()
        check("breaker opens on real faults", False, "did not open")
    except Exception:
        check("breaker opens on real faults", True)

    b2 = CircuitBreaker(threshold=3, cooldown_s=60)
    b2.ok()
    try:
        b2.check()
        check("healthy breaker stays closed", True)
    except Exception:
        check("healthy breaker stays closed", False)


def test_quota_fallback() -> None:
    """A spent daily quota must fall back, not stall or retry forever.

    Regression: TPD and TPM both surface as HTTP 429 but need opposite
    handling. Retrying a spent daily budget burns what little remains.
    """
    print("\nquota handling")
    import claimiq.providers.model as M

    class FakeQuotaProvider:
        """Fails the big model with a TPD error, serves the small one."""

        def __init__(self) -> None:
            self.calls: list[str] = []

        def complete(self, system, prompt, profile):
            self.calls.append(profile.model)
            if profile.model == "openai/gpt-oss-120b":
                raise RuntimeError(
                    "Error code: 429 - Rate limit reached for model "
                    "`openai/gpt-oss-120b` ... on tokens per day (TPD): "
                    "Limit 200000, Used 199549"
                )
            return M.ModelResponse(
                text='{"ok": true}', model=profile.model, in_tokens=10,
                out_tokens=5, latency_s=0.01, cost_usd=0.0,
            )

    original = M._provider
    exhausted_before = set(M._exhausted)
    fake = FakeQuotaProvider()
    try:
        M._exhausted.clear()
        M.set_provider(fake)
        r = M.invoke("test", task=M.Task.EXTRACT)
        check("falls back to the smaller model", r.model == "openai/gpt-oss-20b",
              f"got {r.model}")
        check("exhausted model is remembered",
              "openai/gpt-oss-120b" in M._exhausted)
        check("did not retry the exhausted model",
              fake.calls.count("openai/gpt-oss-120b") == 1,
              f"tried {fake.calls.count('openai/gpt-oss-120b')} times")

        # A second call should skip the dead model entirely.
        fake.calls.clear()
        M.invoke("test again", task=M.Task.EXTRACT)
        check("subsequent calls skip the exhausted model",
              "openai/gpt-oss-120b" not in fake.calls)
    finally:
        M._exhausted.clear()
        M._exhausted.update(exhausted_before)
        M.set_provider(original) if original else M.set_provider(M.MockProvider())


def main() -> int:
    print("ClaimIQ deterministic tests (no model calls)")
    print("=" * 52)
    test_parsing()
    test_dates()
    test_arithmetic()
    test_duplicates()
    test_policy_dates()
    test_sublimits()
    test_grounding()
    test_checkpoints()
    test_ingest_pagination()
    test_degradation_is_loud()
    test_rate_limit_not_a_circuit_fault()
    test_quota_fallback()
    print("=" * 52)
    if _failures:
        print(f"{len(_failures)} FAILED: {', '.join(_failures)}")
        return 1
    print("All deterministic tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
