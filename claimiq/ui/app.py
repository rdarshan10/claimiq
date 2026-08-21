"""ClaimIQ demo UI.

Streamlit is the demo surface, not the system: the pipeline, queue and
checkpoints all run outside it, and this reads results. That matters because
Streamlit re-runs the whole script on every interaction — a design that did
real work in here would be unusable mid-batch.

Run:  streamlit run claimiq/ui/app.py
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

# Allow `streamlit run claimiq/ui/app.py` from the repo root.
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd
import streamlit as st

from claimiq.batch import DB_PATH, JobQueue, run_batch
from claimiq.core.schemas import ClaimResult, Recommendation, Severity
from claimiq.pipeline import RESULT_ROOT, process_claim
from claimiq.storage.feedback import FeedbackStore

CLAIMS_ROOT = ROOT / "claimiq" / "data" / "claims"
UPLOAD_ROOT = ROOT / "data" / "uploads"

st.set_page_config(page_title="ClaimIQ", page_icon="🛡️", layout="wide")

SEV_COLOR = {
    Severity.CRITICAL: "#b3261e",
    Severity.HIGH: "#c8641b",
    Severity.MEDIUM: "#a8860a",
    Severity.LOW: "#4a6572",
    Severity.INFO: "#5f6368",
}
REC_STYLE = {
    Recommendation.AUTO_APPROVE: ("✅", "#1e7a3c", "Straight-through"),
    Recommendation.REVIEW: ("⚠️", "#c8641b", "Adjuster review"),
    Recommendation.INVESTIGATE: ("🚩", "#b3261e", "Investigate"),
}

st.markdown(
    """<style>
    .metric-big { font-size: 2.1rem; font-weight: 650; line-height: 1.1; }
    .metric-cap { font-size: .74rem; text-transform: uppercase;
                  letter-spacing: .06em; opacity: .65; }
    .finding { border-left: 4px solid var(--c); padding: .55rem .8rem;
               margin: .45rem 0; background: rgba(128,128,128,.07);
               border-radius: 0 5px 5px 0; }
    .finding-title { font-weight: 600; }
    .finding-meta { font-size: .76rem; opacity: .7; margin-top: .25rem; }
    .quote { font-family: ui-monospace, monospace; font-size: .78rem;
             background: rgba(128,128,128,.10); padding: .3rem .5rem;
             border-radius: 3px; margin-top: .35rem; }
    </style>""",
    unsafe_allow_html=True,
)


# --------------------------------------------------------------------------
def load_result(claim_id: str) -> ClaimResult | None:
    p = RESULT_ROOT / f"{claim_id}.json"
    if not p.exists():
        return None
    try:
        return ClaimResult.model_validate_json(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def metric(col, caption: str, value: str, color: str | None = None) -> None:
    style = f' style="color:{color}"' if color else ""
    col.markdown(
        f'<div class="metric-cap">{caption}</div>'
        f'<div class="metric-big"{style}>{value}</div>',
        unsafe_allow_html=True,
    )


def render_findings(result: ClaimResult) -> None:
    findings = result.sorted_findings()
    if not findings:
        st.success("No inconsistencies detected. Claim is documentation-complete.")
        return

    counts = {s: len(result.by_severity(s)) for s in Severity}
    chips = "  ".join(
        f"<span style='color:{SEV_COLOR[s]};font-weight:600'>{counts[s]} {s.value}</span>"
        for s in Severity if counts[s]
    )
    st.markdown(chips, unsafe_allow_html=True)

    show = st.multiselect(
        "Filter by severity",
        [s.value for s in Severity if counts[s]],
        default=[s.value for s in Severity if counts[s] and s.rank <= 1],
        key=f"sev_{result.claim_id}",
    )
    docs = {d.doc_id: d for d in result.documents}

    store = FeedbackStore()
    for idx, f in enumerate(findings):
        if show and f.severity.value not in show:
            continue
        impact = (
            f" · exposure £{f.financial_impact:,.0f}" if f.financial_impact else ""
        )
        st.markdown(
            f'<div class="finding" style="--c:{SEV_COLOR[f.severity]}">'
            f'<div class="finding-title">{f.severity.value.upper()} — {f.title}</div>'
            f"<div>{f.detail}</div>"
            f'<div class="finding-meta">source: {f.source} · '
            f"confidence {f.confidence:.0%}{impact}</div>"
            "</div>",
            unsafe_allow_html=True,
        )
        for c in f.citations:
            doc = docs.get(c.doc_id)
            name = doc.filename if doc else c.doc_id
            st.markdown(
                f'<div class="quote">📄 <b>{name}</b> p.{c.page} — "{c.quote}"</div>',
                unsafe_allow_html=True,
            )

        # Adjuster verdict. Every judgement is ground truth arriving for free
        # during normal work — it is what turns "we think it's accurate" into a
        # measured precision figure per reasoner.
        v1, v2, _ = st.columns([1, 1, 6])
        key = f"{result.claim_id}_{idx}"
        if v1.button("Confirm", key=f"ok_{key}", help="This finding is correct"):
            store.record_finding_verdict(
                result.claim_id, f.title, "confirmed", f.kind.value,
                f.severity.value, f.source,
            )
            st.toast("Recorded: confirmed")
        if v2.button("Reject", key=f"no_{key}", help="This is a false positive"):
            store.record_finding_verdict(
                result.claim_id, f.title, "rejected", f.kind.value,
                f.severity.value, f.source,
            )
            st.toast("Recorded: false positive")


def render_claim(result: ClaimResult) -> None:
    icon, color, label = REC_STYLE[result.recommendation]

    c1, c2, c3, c4, c5 = st.columns(5)
    metric(c1, "Recommendation", f"{icon} {label}", color)
    metric(c2, "Risk score", f"{result.risk_score:.2f}")
    metric(c3, "Completeness", f"{result.completeness.pct}%")
    metric(c4, "Total billed", f"£{result.total_billed:,.0f}")
    metric(
        c5, "Exposure flagged",
        f"£{result.exposure:,.0f}" if result.exposure else "—",
    )

    st.divider()
    tabs = st.tabs(
        ["Briefing", f"Findings ({len(result.findings)})", "Extracted data",
         "Completeness", "Run detail"]
    )

    with tabs[0]:
        if result.summary.headline:
            st.markdown(f"### {result.summary.headline}")
        if result.summary.narrative:
            st.write(result.summary.narrative)
        left, right = st.columns(2)
        if result.summary.key_facts:
            left.markdown("**Key facts**")
            left.table(
                pd.DataFrame(
                    result.summary.key_facts.items(), columns=["Field", "Value"]
                ).set_index("Field")
            )
        if result.summary.timeline:
            right.markdown("**Timeline**")
            for item in result.summary.timeline:
                right.markdown(f"- {item}")

    with tabs[1]:
        render_findings(result)

    with tabs[2]:
        for e in result.extractions:
            vals = e.values()
            if not vals:
                continue
            doc = next((d for d in result.documents if d.doc_id == e.doc_id), None)
            with st.expander(
                f"{doc.filename if doc else e.doc_id} — {e.doc_type.value}",
                expanded=False,
            ):
                if e.error:
                    st.error(e.error)
                rows = []
                for name, ev in vals.items():
                    if not ev.is_present:
                        continue
                    grounded = (
                        "✅" if ev.grounded else ("❌" if ev.grounded is False else "—")
                    )
                    rows.append(
                        {
                            "Field": name.replace("_", " "),
                            "Value": str(ev.value)[:90],
                            "Conf": f"{ev.confidence:.0%}",
                            "Grounded": grounded,
                            "Source": ev.citation.label() if ev.citation else "—",
                        }
                    )
                if rows:
                    st.dataframe(
                        pd.DataFrame(rows), width="stretch", hide_index=True
                    )
                if e.invoice and e.invoice.line_items:
                    st.markdown("**Line items**")
                    st.dataframe(
                        pd.DataFrame([li.model_dump() for li in e.invoice.line_items]),
                        width="stretch", hide_index=True,
                    )

    with tabs[3]:
        left, right = st.columns(2)
        left.markdown(f"**Present ({len(result.completeness.present)})**")
        for item in result.completeness.present:
            left.markdown(f"- ✅ {item}")
        right.markdown(f"**Missing ({len(result.completeness.missing)})**")
        if not result.completeness.missing:
            right.success("Nothing missing.")
        for item in result.completeness.missing:
            right.markdown(f"- ❌ {item}")

    with tabs[4]:
        u = result.usage
        a, b, c, d = st.columns(4)
        metric(a, "Model cost", f"${u.get('cost_usd', 0):.4f}")
        metric(b, "Model calls", str(u.get("calls", 0)))
        metric(c, "Tokens", f"{u.get('total_tokens', 0):,}")
        metric(
            d, "Grounding",
            f"{u.get('grounding_rate', 0):.0%}"
            f" ({u.get('citations_checked', 0)} cites)",
        )
        st.markdown("**Stage timings (seconds)**")
        st.dataframe(
            pd.DataFrame(
                [{"Stage": k, "Seconds": v} for k, v in result.stage_timings.items()]
            ),
            width="stretch", hide_index=True,
        )
        if u.get("by_model"):
            st.markdown("**Calls by model** — routing sends easy work to the small model")
            st.dataframe(
                pd.DataFrame(u["by_model"].items(), columns=["Model", "Calls"]),
                width="stretch", hide_index=True,
            )
        if result.errors:
            st.warning("Non-fatal errors during this run:")
            for e in result.errors:
                st.code(e, language="text")


# --------------------------------------------------------------------------
st.title("🛡️ ClaimIQ")
st.caption(
    "Claim document intelligence — extraction, validation and triage "
    "with source-cited findings"
)

page = st.sidebar.radio(
    "View",
    ["Review a claim", "Batch operations", "Cross-claim signals", "Business case"],
    index=0,
)
st.sidebar.divider()
st.sidebar.caption("Synthetic demo data — contains no real personal or health information.")


# --------------------------------------------------------------------------
if page == "Review a claim":
    available = sorted(p.name for p in CLAIMS_ROOT.iterdir() if p.is_dir()) \
        if CLAIMS_ROOT.exists() else []

    col_a, col_b = st.columns([3, 1])
    claim_id = col_a.selectbox("Claim", available) if available else None
    col_b.markdown("<br>", unsafe_allow_html=True)
    rerun = col_b.button("Process claim", type="primary", width="stretch")

    if claim_id:
        cached = load_result(claim_id)
        if cached and not rerun:
            st.caption(
                f"Showing saved result · {cached.stage_timings.get('_wall', 0):.0f}s "
                f"· ${cached.usage.get('cost_usd', 0):.4f}"
            )
            render_claim(cached)
        elif rerun:
            status = st.status("Processing…", expanded=True)
            log: list[str] = []

            def on_progress(stage: str, msg: str) -> None:
                log.append(f"**{stage}** — {msg}")
                status.write(f"**{stage}** — {msg}")

            t0 = time.time()
            try:
                result = process_claim(
                    CLAIMS_ROOT / claim_id, claim_id,
                    resume=False, progress=on_progress,
                )
                status.update(
                    label=f"Done in {time.time() - t0:.0f}s", state="complete",
                    expanded=False,
                )
                render_claim(result)
            except Exception as e:  # noqa: BLE001
                status.update(label="Failed", state="error")
                st.exception(e)
        else:
            st.info("No saved result for this claim. Press **Process claim** to run it.")


# --------------------------------------------------------------------------
elif page == "Batch operations":
    st.subheader("Batch processing")
    st.caption(
        "Durable SQLite queue with per-stage checkpoints. A batch survives "
        "restarts and rate-limit failures, and resumes mid-claim rather than "
        "reprocessing completed work."
    )

    q = JobQueue(DB_PATH)
    stats = q.stats()

    a, b, c, d = st.columns(4)
    metric(a, "Processed", str(stats["processed"]))
    metric(b, "STP rate", f"{stats['stp_rate']:.0%}")
    metric(c, "Cost / claim", f"${stats['cost_per_claim']:.4f}")
    metric(d, "Exposure found", f"£{stats['total_exposure']:,.0f}")

    left, right = st.columns([1, 3])
    if left.button("Run batch", type="primary", width="stretch"):
        status = st.status("Running batch…", expanded=True)
        report = run_batch(
            CLAIMS_ROOT, workers=1,
            on_event=lambda kind, msg: status.write(f"`{kind}` {msg}"),
        )
        status.update(
            label=f"Batch complete — {report.processed} processed, "
                  f"{report.failed} failed, {report.wall_s:.0f}s",
            state="complete",
        )
        st.rerun()
    right.caption(
        "Concurrency is derived from the provider's token-per-minute budget. "
        "Raise CLAIMIQ_TPM on a higher tier and the pipeline widens automatically."
    )

    rows = q.rows()
    if rows:
        df = pd.DataFrame(rows)[
            ["claim_id", "status", "recommendation", "risk", "findings",
             "critical", "billed", "exposure", "wall_s", "cost_usd", "grounding"]
        ]
        st.dataframe(df, width="stretch", hide_index=True)
    else:
        st.info("Queue is empty. Press **Run batch** to process the demo claims.")

    # --- Operations -------------------------------------------------------
    done = [r for r in rows if r["status"] == "done"]
    if done:
        st.divider()
        st.markdown("#### Operations")
        st.caption(
            "What a run costs, how long it takes, and whether quality is "
            "holding — the numbers you would alert on in production."
        )

        o1, o2, o3, o4 = st.columns(4)
        metric(o1, "Avg wall / claim", f"{stats['avg_wall_s']:.0f}s")
        metric(o2, "Avg risk", f"{stats['avg_risk']:.2f}")
        metric(o3, "Avg grounding", f"{stats['avg_grounding']:.0%}")
        failed = stats["counts"].get("failed", 0)
        metric(o4, "Failed", str(failed), "#b3261e" if failed else None)

        chart_df = pd.DataFrame(
            [
                {
                    "claim": r["claim_id"],
                    "seconds": r["wall_s"] or 0,
                    "findings": r["findings"] or 0,
                    "exposure": r["exposure"] or 0,
                }
                for r in done
            ]
        ).set_index("claim")

        c1, c2 = st.columns(2)
        c1.markdown("**Processing time (s)**")
        c1.bar_chart(chart_df[["seconds"]], height=220)
        c2.markdown("**Exposure identified (£)**")
        c2.bar_chart(chart_df[["exposure"]], height=220)

        st.markdown("**Routing mix**")
        rec_counts = stats["recommendations"]
        if rec_counts:
            st.bar_chart(
                pd.DataFrame(
                    {"claims": list(rec_counts.values())},
                    index=list(rec_counts.keys()),
                ),
                height=200,
            )

        errored = [r for r in rows if r["error"]]
        if errored:
            st.warning(f"{len(errored)} claim(s) recorded errors:")
            for r in errored:
                st.code(f"{r['claim_id']}: {r['error']}", language="text")


# --------------------------------------------------------------------------
elif page == "Cross-claim signals":
    from claimiq.stages.crossclaim import load_results
    from claimiq.stages.crossclaim import run as run_crossclaim

    st.subheader("Cross-claim intelligence")
    st.caption(
        "Patterns visible across a book of claims but invisible inside any "
        "single one. This analysis only becomes possible once batch processing "
        "exists — it is the argument for processing the whole book, not just "
        "the suspicious ones."
    )

    results = load_results()
    if len(results) < 2:
        st.info("Process at least two claims to enable cross-claim analysis.")
    else:
        signals = run_crossclaim()
        a, b = st.columns(2)
        metric(a, "Claims analysed", str(len(results)))
        metric(b, "Signals raised", str(len(signals)))

        if not signals:
            st.success(
                "No cross-claim patterns detected across the processed claims."
            )
        for s in signals:
            sev = Severity(s.severity)
            impact = (
                f" · exposure £{s.financial_impact:,.0f}" if s.financial_impact else ""
            )
            st.markdown(
                f'<div class="finding" style="--c:{SEV_COLOR[sev]}">'
                f'<div class="finding-title">{sev.value.upper()} — {s.title}</div>'
                f"<div>{s.detail}</div>"
                f'<div class="finding-meta">claims: {", ".join(s.claims)}{impact}</div>'
                "</div>",
                unsafe_allow_html=True,
            )

        st.divider()
        st.caption(
            "Implemented as exact and near-exact matching (inverted index + "
            "shingled similarity) rather than embedding search: at this scale "
            "the signals that matter are identity matches, which are faster to "
            "compute and explainable to an investigator. Semantic narrative "
            "clustering is the Phase 2 upgrade — see ARCHITECTURE.md."
        )


# --------------------------------------------------------------------------
else:
    st.subheader("Business case")
    st.caption("Figures below are computed live from measured pipeline runs.")

    q = JobQueue(DB_PATH)
    stats = q.stats()
    measured_cost = stats["cost_per_claim"] or 0.0087

    st.markdown("#### Your assumptions")
    a, b, c = st.columns(3)
    volume = a.number_input("Claims per year", 1000, 5_000_000, 50_000, step=5_000)
    hourly = b.number_input("Loaded adjuster cost (£/hour)", 10.0, 200.0, 45.0, step=5.0)
    minutes = c.number_input("Manual review (minutes/claim)", 1, 240, 30, step=5)

    d, e, f = st.columns(3)
    reduction = d.slider("Review time saved on assisted claims", 0, 90, 55, step=5) / 100
    stp = e.slider("Straight-through rate", 0, 60, 20, step=5) / 100
    leakage = f.number_input("Leakage prevented per 1,000 claims (£)", 0, 500_000, 25_000, step=5_000)

    manual_hours = volume * minutes / 60
    manual_cost = manual_hours * hourly

    stp_claims = volume * stp
    assisted_claims = volume - stp_claims
    new_hours = assisted_claims * (minutes * (1 - reduction)) / 60
    new_labour = new_hours * hourly

    model_cost = volume * measured_cost * 0.79  # USD -> GBP, indicative
    labour_saving = manual_cost - new_labour
    leakage_saving = volume / 1000 * leakage
    net = labour_saving + leakage_saving - model_cost

    st.divider()
    m1, m2, m3, m4 = st.columns(4)
    metric(m1, "Current annual cost", f"£{manual_cost:,.0f}")
    metric(m2, "Labour saved", f"£{labour_saving:,.0f}", "#1e7a3c")
    metric(m3, "Leakage prevented", f"£{leakage_saving:,.0f}", "#1e7a3c")
    metric(m4, "Net annual benefit", f"£{net:,.0f}", "#1e7a3c")

    st.markdown(
        f"""
**How this reads.** Processing {volume:,} claims costs about
**£{model_cost:,.0f}/year in model spend** — measured at
**${measured_cost:.4f} per claim** on real runs, not estimated. Against that,
{stp:.0%} of claims clear without human review and the remainder are read
{reduction:.0%} faster because the adjuster opens a pre-read file with the
exceptions already cited.

The leakage figure is the one to press on. A single duplicate invoice or a
post-expiry surgical charge caught on one claim can exceed the model cost of
processing the entire annual book.
"""
    )

    st.divider()
    st.markdown("#### Adjuster feedback")
    acc = FeedbackStore().accuracy()
    if acc["findings_judged"]:
        f1, f2, f3 = st.columns(3)
        metric(f1, "Findings judged", str(acc["findings_judged"]))
        metric(
            f2, "Measured precision",
            f"{acc['precision']:.0%}" if acc["precision"] is not None else "—",
        )
        metric(f3, "Field corrections", str(acc["field_corrections"]))
        if acc["by_source"]:
            st.markdown("**Precision by finding source**")
            st.dataframe(
                pd.DataFrame(acc["by_source"]), width="stretch", hide_index=True
            )
        st.caption(
            "Collected from Confirm/Reject on the findings list. This is how "
            "an accuracy claim becomes defensible: adjusters generate ground "
            "truth during normal work, and corrections feed back as few-shot "
            "examples for the document layouts this insurer actually receives."
        )
    else:
        st.info(
            "No feedback recorded yet. Use **Confirm** / **Reject** on the "
            "findings list to start measuring precision per reasoner."
        )

    st.divider()
    st.markdown("#### Measured on the demo claims")
    if stats["processed"]:
        g1, g2, g3 = st.columns(3)
        metric(g1, "Grounding rate", f"{stats['avg_grounding']:.0%}")
        metric(g2, "Avg processing", f"{stats['avg_wall_s']:.0f}s")
        metric(g3, "Exposure identified", f"£{stats['total_exposure']:,.0f}")
        st.caption(
            "Grounding rate is the share of cited quotes verified to exist "
            "verbatim in the source document — the system's own check against "
            "fabricated evidence."
        )
    else:
        st.info("Run a batch to populate measured figures.")
