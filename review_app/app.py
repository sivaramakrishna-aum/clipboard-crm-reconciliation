"""Streamlit review app for proposed CRM changes.

Run from the project root:
    streamlit run review_app/app.py

Flow:
1. ``python -m matcher.reconcile --dry-run`` stores proposals (read only against the CRM).
2. A named reviewer approves or rejects each proposal here. Decisions go to SQLite
   and to the git tracked ledger (decisions/ledger.jsonl), and cannot be changed.
3. Approved proposals are written to the CRM only when the reviewer ticks the
   confirmation box and clicks "Apply", and only if ``CRM_WRITE_ENABLED=true``.
   Every write is verified by reading the account back.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd  # noqa: E402
import requests  # noqa: E402
import streamlit as st  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

from matcher.reconcile import DecisionStore  # noqa: E402
from review_app.crm_client import (  # noqa: E402
    CrmClient,
    CrmSchemaError,
    CrmVerificationError,
    CrmWriteBlockedError,
    apply_proposal,
)

load_dotenv()
DB_PATH = os.getenv("DECISIONS_DB_PATH", "data/decisions.db")
LEDGER_PATH = os.getenv("DECISIONS_LEDGER_PATH", "decisions/ledger.jsonl")
WRITE_ENABLED = os.getenv("CRM_WRITE_ENABLED", "false").strip().lower() == "true"

st.set_page_config(page_title="Bellhaven CRM Review", layout="wide")


@st.cache_resource
def get_store() -> DecisionStore:
    return DecisionStore(DB_PATH, ledger_path=LEDGER_PATH)


store = get_store()
st.title("Bellhaven CRM reconciliation review")
st.caption(f"Decisions: {DB_PATH} + {LEDGER_PATH} | CRM writes: "
           f"{'ENABLED (approved items only, after confirmation)' if WRITE_ENABLED else 'disabled (read only)'}")

reviewer = st.sidebar.text_input("Reviewer name (required)", value=os.getenv("REVIEWER_NAME", "")).strip()
view = st.sidebar.radio("Show", ["pending", "approved", "rejected", "all"])
proposals = store.list_proposals(view)
kinds = sorted({p["kind"] for p in proposals})
chosen = st.sidebar.multiselect("Type", kinds, default=kinds)
proposals = [p for p in proposals if p["kind"] in chosen]

all_items = store.list_proposals("all")
c1, c2, c3, c4 = st.columns(4)
c1.metric("Pending", sum(1 for p in all_items if p["decision"] is None))
c2.metric("Approved", sum(1 for p in all_items if p["decision"] == "approved"))
c3.metric("Rejected", sum(1 for p in all_items if p["decision"] == "rejected"))
c4.metric("Approved, not yet applied",
          sum(1 for p in all_items if p["decision"] == "approved" and p["operations"] and None in p["applied_ops"]))

st.subheader(f"{len(proposals)} {view} proposal(s)")
if not proposals:
    st.info("Nothing to show. Run `python -m matcher.reconcile --dry-run` to generate proposals.")

for p in proposals:
    flag = " ⚠ manual review" if p.get("requires_manual_review") else ""
    with st.expander(f"[{p['kind']}] {p['title']}{flag}", expanded=view == "pending"):
        st.markdown("**Evidence**")
        for line in p["evidence"]:
            st.markdown(f"- {line}")
        if p.get("billing_action"):
            st.markdown(f"**Billing rule outcome:** `{p['billing_action']}`")
        st.markdown("**Proposed CRM operations** (no deletes are ever proposed)")
        if p["operations"]:
            df = pd.DataFrame(p["operations"])
            df["applied"] = [r if r is not None else "" for r in p["applied_ops"]]
            st.dataframe(df, width="stretch")
        else:
            st.write("No CRM write. Approving records the reviewer's acknowledgement only.")

        if p["decision"] is None:
            a, r = st.columns(2)
            if a.button("Approve", key=f"a_{p['key']}", disabled=not reviewer):
                store.record_decision(p["key"], "approved", reviewer)
                st.rerun()
            if r.button("Reject", key=f"r_{p['key']}", disabled=not reviewer):
                store.record_decision(p["key"], "rejected", reviewer)
                st.rerun()
            if not reviewer:
                st.caption("Enter a reviewer name in the sidebar to enable decisions.")
            continue

        st.write(f"Decision: **{p['decision']}** by {p.get('decided_by') or 'unknown'} at {p['decided_at']}")
        pending_ops = p["decision"] == "approved" and p["operations"] and None in p["applied_ops"]
        if not pending_ops:
            continue
        if not WRITE_ENABLED:
            st.caption("Set CRM_WRITE_ENABLED=true in .env and restart the app to apply approved changes.")
            continue
        confirm = st.checkbox("I have reviewed these operations and want them written to the CRM now",
                              key=f"c_{p['key']}")
        if st.button("Apply to CRM", key=f"x_{p['key']}", disabled=not confirm):
            try:
                for line in apply_proposal(p, CrmClient.from_env(), store):
                    st.write(line)
                st.success("Applied and verified by reading the account back.")
            except (CrmWriteBlockedError, CrmSchemaError, CrmVerificationError, PermissionError,
                    ValueError, RuntimeError, requests.RequestException) as exc:
                st.error(f"Not applied: {exc}. Safe to retry; completed steps are skipped.")
