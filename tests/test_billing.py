"""Tests for billing/CHOW rules and duplicate handling."""

from decimal import Decimal

import pytest

from matcher.billing import (
    INACTIVE_STATUS,
    NEW_ACCOUNT_PLACEHOLDER,
    BillingAction,
    OpType,
    choose_survivor,
    decide_parent_change,
    parse_amount,
    plan_duplicate_resolution,
)

PARENT = "P-BELLHAVEN"


def acct(**kw):
    base = {"id": "A1", "name": "Bellhaven Oaks", "parent_account": "P-OLD",
            "lifetime_revenue": 0, "outstanding_ar": 0, "status": "Active"}
    base.update(kw)
    return base


@pytest.mark.parametrize("raw,expected", [
    (100, Decimal(100)), ("$1,250.50", Decimal("1250.50")), ("(20)", Decimal(-20)),
    ("0", Decimal(0)), (0.0, Decimal("0.0")),
    (None, None), ("", None), ("N/A", None), ("unknown", None), (float("nan"), None), (True, None),
])
def test_parse_amount(raw, expected):
    assert parse_amount(raw) == expected


def test_chow_when_revenue_history_and_open_ar():
    d = decide_parent_change(acct(lifetime_revenue="5000", outstanding_ar="120"), PARENT, {"name": "Bellhaven Oaks"})
    assert d.action == BillingAction.CHOW_NEW_ACCOUNT
    create, link = d.operations
    assert create.op == OpType.CREATE and create.fields["parent_account"] == PARENT
    # Old account keeps every field except the CHOW link.
    assert link.op == OpType.UPDATE and link.account_id == "A1"
    assert link.fields == {"chow_current_account": NEW_ACCOUNT_PLACEHOLDER}


@pytest.mark.parametrize("revenue,ar", [(5000, 0), (0, 0), (0, 50)])
def test_reparent_otherwise(revenue, ar):
    d = decide_parent_change(acct(lifetime_revenue=revenue, outstanding_ar=ar), PARENT)
    assert d.action == BillingAction.REPARENT
    assert len(d.operations) == 1
    assert d.operations[0].fields == {"parent_account": PARENT}


@pytest.mark.parametrize("revenue,ar", [(None, 10), (5000, None), ("", ""), ("N/A", 10), (5000, "pending"), (-1, 10)])
def test_missing_or_ambiguous_financials_need_manual_review(revenue, ar):
    d = decide_parent_change(acct(lifetime_revenue=revenue, outstanding_ar=ar), PARENT)
    assert d.action == BillingAction.MANUAL_REVIEW
    assert d.operations == ()


def test_no_delete_operation_exists():
    assert {o.value for o in OpType} == {"create", "update"}


def test_duplicate_losers_point_to_survivor_and_become_inactive():
    group = [acct(id="A2", lifetime_revenue=10), acct(id="A1", lifetime_revenue=900), acct(id="A3", lifetime_revenue=None)]
    survivor, ops = plan_duplicate_resolution(group)
    assert survivor == "A1"
    assert {o.account_id for o in ops} == {"A2", "A3"}
    for o in ops:
        assert o.op == OpType.UPDATE
        assert o.fields == {"duplicate_of_account": "A1", "status": INACTIVE_STATUS}


def test_duplicate_resolution_is_idempotent():
    group = [acct(id="A1", lifetime_revenue=900),
             acct(id="A2", duplicate_of_account="A1", status=INACTIVE_STATUS)]
    survivor, ops = plan_duplicate_resolution(group)
    assert survivor == "A1" and ops == []


def test_survivor_prefers_active_then_revenue_then_id():
    assert choose_survivor([acct(id="B", lifetime_revenue=5), acct(id="A", lifetime_revenue=5)])["id"] == "A"
    assert choose_survivor([acct(id="A", lifetime_revenue=999, status=INACTIVE_STATUS), acct(id="B", lifetime_revenue=1)])["id"] == "B"


def test_single_account_is_not_a_duplicate_group():
    assert plan_duplicate_resolution([acct()]) == (None, [])
