"""Tests for matching classifications, proposals and decision persistence."""

import pytest

from matcher.billing import NEW_ACCOUNT_PLACEHOLDER
from matcher.reconcile import Classification, DecisionStore, normalize_address, normalize_zip, reconcile, resolve_parent_id

PARENT = "P-BELLHAVEN"


def loc(name="Bellhaven Oaks", address="100 Main Street", zip_code="94105", city="San Francisco", state="CA"):
    return {"name": name, "address": address, "city": city, "state": state, "zip_code": zip_code,
            "care_offerings": ["Assisted Living", "Memory Care"], "source_url": "https://example.test/locations"}


def acct(id="A1", name="Bellhaven Oaks", address="100 Main St", zip="94105", parent=PARENT, **kw):
    base = {"id": id, "name": name, "address": address, "city": "San Francisco", "state": "CA", "zip": zip,
            "parent_account": parent, "status": "Active", "lifetime_revenue": "0", "outstanding_ar": "0",
            "duplicate_of_account": "", "chow_current_account": ""}
    base.update(kw)
    return base


def kinds(proposals):
    return sorted(p.kind.value for p in proposals)


def test_normalizers():
    assert normalize_address("100 Main Street, Suite 2") == normalize_address("100 main st ste 2")
    assert normalize_zip("94105-1234") == "94105"
    assert normalize_zip("2134") == "02134"


def test_confident_match_produces_no_proposal():
    proposals, classes = reconcile([loc()], [acct()], PARENT)
    assert proposals == []
    assert list(classes.values()) == [Classification.CONFIDENT_MATCH.value]


def test_name_mismatch():
    proposals, _ = reconcile([loc(name="Bellhaven Gardens")], [acct(name="Sunrise Meadows")], PARENT)
    assert kinds(proposals) == ["name_mismatch"]
    assert proposals[0].operations[0].fields == {"name": "Bellhaven Gardens"}
    assert proposals[0].evidence, "every proposal must carry evidence"


def test_parent_mismatch_uses_billing_rule():
    chow = acct(parent="P-OTHER", lifetime_revenue="1000", outstanding_ar="50")
    proposals, _ = reconcile([loc()], [chow], PARENT)
    assert kinds(proposals) == ["parent_mismatch"]
    p = proposals[0]
    assert p.billing_action == "chow_new_account"
    assert p.operations[1].fields == {"chow_current_account": NEW_ACCOUNT_PLACEHOLDER}

    manual, _ = reconcile([loc()], [acct(parent="P-OTHER", lifetime_revenue="", outstanding_ar="50")], PARENT)
    assert manual[0].requires_manual_review and manual[0].operations == []


def test_missing_in_crm():
    proposals, _ = reconcile([loc(address="9 Elm Road", zip_code="10001", city="New York", state="NY",
                                  name="Bellhaven Harbor")], [acct()], PARENT)
    assert "missing_in_crm" in kinds(proposals)
    create = next(p for p in proposals if p.kind == Classification.MISSING_IN_CRM)
    assert create.operations[0].fields["parent_account"] == PARENT


def test_duplicates_detected():
    proposals, classes = reconcile([loc()], [acct(id="A1", lifetime_revenue="500"), acct(id="A2")], PARENT)
    dup = next(p for p in proposals if p.kind == Classification.DUPLICATE)
    assert dup.operations[0].account_id == "A2"
    assert dup.operations[0].fields["duplicate_of_account"] == "A1"
    assert Classification.DUPLICATE.value in classes.values()


def test_not_on_website():
    proposals, _ = reconcile([loc()], [acct(), acct(id="A9", name="Bellhaven Closed", address="5 Gone Ave", zip="90001")], PARENT)
    gone = [p for p in proposals if p.kind == Classification.NOT_ON_WEBSITE]
    assert [p.account_ids for p in gone] == [["A9"]]
    assert gone[0].operations == []


def test_retired_accounts_are_ignored_after_resolution():
    accounts = [acct(id="A1"), acct(id="A2", duplicate_of_account="A1", status="Inactive")]
    proposals, _ = reconcile([loc()], accounts, PARENT)
    assert proposals == []


def test_proposal_keys_are_stable_across_runs():
    a, _ = reconcile([loc(name="Bellhaven Gardens")], [acct(name="Sunrise Meadows")], PARENT)
    b, _ = reconcile([loc(name="Bellhaven Gardens")], [acct(name="Sunrise Meadows")], PARENT)
    assert a[0].key == b[0].key


def test_decided_proposals_do_not_reappear():
    store = DecisionStore(":memory:")
    proposals, _ = reconcile([loc(name="Bellhaven Gardens")], [acct(name="Sunrise Meadows"), acct(id="A9", address="1 X", zip="1")], PARENT)
    first = store.upsert_proposals(proposals)
    assert len(first) == 2
    store.record_decision(first[0].key, "approved", "tester")
    store.record_decision(first[1].key, "rejected", "tester")
    rerun, _ = reconcile([loc(name="Bellhaven Gardens")], [acct(name="Sunrise Meadows"), acct(id="A9", address="1 X", zip="1")], PARENT)
    assert store.upsert_proposals(rerun) == []
    assert store.list_proposals("pending") == []


def test_decisions_survive_a_lost_database_via_ledger(tmp_path):
    ledger = tmp_path / "ledger.jsonl"
    args = ([loc(name="Bellhaven Gardens")], [acct(name="Sunrise Meadows")], PARENT)
    store = DecisionStore(tmp_path / "a.db", ledger_path=ledger)
    p = store.upsert_proposals(reconcile(*args)[0])[0]
    assert store.record_decision(p.key, "rejected", "tester") is True
    assert store.record_decision(p.key, "approved", "tester") is False  # decided once only
    store.mark_applied(p.key, 0, "A1")
    store.close()

    # Fresh DB (e.g. a new machine or the stateless CI run) rebuilt from the ledger.
    fresh = DecisionStore(":memory:", ledger_path=ledger)
    assert fresh.upsert_proposals(reconcile(*args)[0]) == []
    assert fresh.get_decision(p.key) == "rejected"
    assert fresh.applied_result(p.key, 0) == "A1"
    lines = ledger.read_text().splitlines()
    assert len(lines) == 2 and all("lifetime_revenue" not in l for l in lines)


def test_decision_requires_reviewer_name():
    store = DecisionStore(":memory:")
    p = store.upsert_proposals(reconcile([loc(name="Bellhaven Gardens")], [acct(name="Sunrise Meadows")], PARENT)[0])[0]
    with pytest.raises(ValueError):
        store.record_decision(p.key, "approved", "  ")


def test_resolve_parent_id():
    accounts = [acct(id="P-BELLHAVEN", name="Bellhaven Senior Living", parent=""), acct()]
    assert resolve_parent_id(accounts) == "P-BELLHAVEN"
    assert resolve_parent_id(accounts, "A1") == "A1"
    with pytest.raises(ValueError):
        resolve_parent_id(accounts, "NOPE")
    with pytest.raises(ValueError):
        resolve_parent_id([acct()])


def test_duplicate_survivor_prefers_correct_parent():
    accounts = [acct(id="A1", parent="P-OTHER", lifetime_revenue="900"), acct(id="A2", lifetime_revenue="10")]
    proposals, _ = reconcile([loc()], accounts, PARENT)
    dup = next(p for p in proposals if p.kind == Classification.DUPLICATE)
    assert dup.operations[0].account_id == "A1"
    assert dup.operations[0].fields["duplicate_of_account"] == "A2"
    # The survivor is already under the right parent, so no parent proposal is needed.
    assert not any(p.kind == Classification.PARENT_MISMATCH for p in proposals)


def test_offdirectory_location_is_flagged_in_evidence():
    proposals, _ = reconcile([dict(loc(address="1800 N Blanchard St", zip_code="45840", city="Findlay", state="OH",
                                        name="Bellhaven Meadows of Findlay"), listed_in_directory=False)], [acct()], PARENT)
    missing = next(p for p in proposals if p.kind == Classification.MISSING_IN_CRM)
    assert "not in the paginated directory" in missing.evidence[0]
