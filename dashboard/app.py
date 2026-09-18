"""
Compliance Audit Dashboard (brief Section 2, BI/Dashboards layer)

Run with:
    streamlit run dashboard/app.py

Every number on this page is sourced directly from one of the SQL-01..
SQL-12 queries or the views built in sql/00_schema_ddl.sql - nothing
here is computed independently in Python, so the dashboard always
agrees with what an auditor running the raw SQL would see.
"""
import sys
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from db import get_engine  # noqa: E402
from sqlalchemy import text  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parent.parent

st.set_page_config(page_title="Compliance Audit & Intent Classification Engine", layout="wide")


@st.cache_resource
def _engine():
    return get_engine()


def q(sql: str) -> pd.DataFrame:
    with _engine().connect() as conn:
        result = conn.execute(text(sql))
        return pd.DataFrame(result.fetchall(), columns=list(result.keys()))


st.title("🛡️ Automated Compliance Audit & Intent Classification Engine")
st.caption(
    "Deterministic, 100% rule-based (zero ML) - every number below traces back to a named "
    "rule_id or a numbered SQL-01..SQL-12 query. UK GDPR / FCA Consumer Duty compliance monitoring."
)

tab_exec, tab_queue, tab_rules, tab_quality = st.tabs(
    ["📊 Executive Summary", "🚨 Auditor Work Queue", "⚙️ Rule Effectiveness", "🧹 Data Quality"]
)

# ---------------------------------------------------------------------
# Executive Summary
# ---------------------------------------------------------------------
with tab_exec:
    totals = q(
        """
        SELECT
            COUNT(*)                                                    AS total_interactions,
            SUM(CASE WHEN risk_tier = 'High' THEN 1 ELSE 0 END)         AS high_risk,
            SUM(CASE WHEN risk_tier = 'Medium' THEN 1 ELSE 0 END)       AS medium_risk,
            SUM(CASE WHEN risk_tier = 'Low' THEN 1 ELSE 0 END)          AS low_risk,
            ROUND(AVG(total_score), 1)                                  AS avg_score
        FROM risk_scores
        """
    ).iloc[0]

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total Interactions Audited", f"{int(totals['total_interactions']):,}")
    c2.metric(
        "High Risk (Priority 1)",
        f"{int(totals['high_risk']):,}",
        help="Escalated instantly to Senior Compliance Manager queue (<2hr SLA)",
    )
    c3.metric("Medium Risk (Priority 2)", f"{int(totals['medium_risk']):,}")
    c4.metric("Average Risk Score", f"{totals['avg_score']:.1f} / 100+")

    st.caption(
        "Coverage: 100% of interactions automatically inspected, vs the 2-5% manual sampling "
        "baseline this system replaces (brief Section 1.1)."
    )

    st.divider()
    left, right = st.columns([3, 2])

    with left:
        st.subheader("Vulnerability Trend (7-day rolling average)")
        st.caption("Source: SQL-07")
        vuln = q((PROJECT_ROOT / "sql" / "07_vulnerability_trend.sql").read_text())
        vuln["audit_date"] = pd.to_datetime(vuln["audit_date"])
        st.line_chart(vuln.set_index("audit_date")[["rolling_7d_avg"]])

    with right:
        st.subheader("Intent Category Mix")
        st.caption("Source: risk_scores.primary_category")
        cat_mix = q(
            "SELECT primary_category, COUNT(*) AS n FROM risk_scores GROUP BY primary_category ORDER BY n DESC"
        )
        st.bar_chart(cat_mix.set_index("primary_category"))

    st.divider()
    st.subheader("Daily Compliance Breach Summary")
    st.caption("Source: v_daily_compliance_summary (backs SQL-01)")
    daily = q("SELECT * FROM v_daily_compliance_summary ORDER BY audit_date DESC, channel")
    st.dataframe(daily, use_container_width=True, hide_index=True)

# ---------------------------------------------------------------------
# Auditor Work Queue
# ---------------------------------------------------------------------
with tab_queue:
    st.subheader("Operational Auditor Work Queue")
    st.caption("Source: v_auditor_work_queue (backs SQL-05) - unreviewed flags only")

    tier_filter = st.multiselect("Risk tier", ["High", "Medium"], default=["High", "Medium"])
    queue = q("SELECT * FROM v_auditor_work_queue")
    if tier_filter:
        queue = queue[queue["risk_tier"].isin(tier_filter)]
    queue = queue.sort_values(
        by=["risk_tier", "total_score"], ascending=[True, False], key=lambda s: s.map({"High": 0, "Medium": 1}) if s.name == "risk_tier" else s
    )

    st.metric("Items awaiting review", len(queue))
    st.dataframe(queue, use_container_width=True, hide_index=True)

    st.divider()
    st.subheader("SLA Compliance Tracking")
    st.caption("Source: SQL-04")
    sla = q((PROJECT_ROOT / "sql" / "04_sla_compliance_tracking.sql").read_text())
    st.dataframe(sla, use_container_width=True, hide_index=True)

# ---------------------------------------------------------------------
# Rule Effectiveness
# ---------------------------------------------------------------------
with tab_rules:
    st.subheader("Rule Effectiveness Report")
    st.caption("Source: v_rule_effectiveness (backs SQL-06)")
    rules = q(
        """
        SELECT r.rule_id, r.rule_type, r.category_or_name, r.weight, r.compliance_mandate,
               v.times_fired, v.total_points_generated
        FROM v_rule_effectiveness v
        JOIN rules r ON r.rule_id = v.rule_id
        ORDER BY v.times_fired DESC
        """
    )
    st.dataframe(rules, use_container_width=True, hide_index=True)

    never_fired = rules[rules["times_fired"] == 0]
    if not never_fired.empty:
        st.warning(
            f"{len(never_fired)} rule(s) have never fired: "
            + ", ".join(never_fired["rule_id"].tolist())
            + ". Worth a governance review - see SQL-06 header comment for the CAT-GENERAL fallback caveat."
        )

    st.subheader("Agent Exposure")
    st.caption("Source: SQL-09")
    agents = q((PROJECT_ROOT / "sql" / "09_agent_exposure_report.sql").read_text())
    st.dataframe(agents, use_container_width=True, hide_index=True)

# ---------------------------------------------------------------------
# Data Quality
# ---------------------------------------------------------------------
with tab_quality:
    st.subheader("Source Data Quality Report")
    st.caption("Source: SQL-10 - channel variant mapping, blanks, invalid timestamps, duplicates")
    dq = q((PROJECT_ROOT / "sql" / "10_data_quality_report.sql").read_text())
    st.dataframe(dq, use_container_width=True, hide_index=True)

    st.subheader("PII Assurance Check")
    st.caption("Source: SQL-11 - expected result is ZERO rows")
    pii = q((PROJECT_ROOT / "sql" / "11_pii_assurance_check.sql").read_text())
    if pii.empty:
        st.success("✅ Zero PII leakage detected across all persisted records.")
    else:
        st.error(f"⚠️ {len(pii)} record(s) failed the PII assurance check.")
        st.dataframe(pii, use_container_width=True, hide_index=True)
