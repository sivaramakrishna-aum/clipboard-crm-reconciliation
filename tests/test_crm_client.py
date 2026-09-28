"""CRM client and apply tests against an in-memory fake of the documented endpoints.

The fake only models what the docs define (GET/POST /accounts, GET/PATCH
/accounts/{id}, page/page_size, zip/parent_id filters). It lets us test write
guards, idempotency and interruption recovery without touching the real CRM.
"""

import itertools

import pytest
import requests

from matcher.billing import NEW_ACCOUNT_PLACEHOLDER
from matcher.reconcile import DecisionStore, reconcile
from review_app.crm_client import (
    CrmClient,
    CrmSchemaError,
    CrmVerificationError,
    CrmWriteBlockedError,
    apply_proposal,
    extract_items,
    verify_applied,
)

PARENT = "P-BELLHAVEN"


def raw(id, **kw):
    base = {"id": id, "name": "Bellhaven Oaks", "parent_id": PARENT, "street": "100 Main St", "city": "San Francisco",
            "state": "CA", "zip": "94105", "status": "Active", "lifetime_revenue": 0, "outstanding_ar": 0,
            "chow_current_account": None, "duplicate_of_account": None}
    base.update(kw)
    return base


class FakeApi(CrmClient):
    def __init__(self, records, write_enabled=True, envelope="items", fail_after_post=False, ignore_patch=False):
        super().__init__(base_url="http://fake", token="SECRET-TOKEN-123", write_enabled=write_enabled, page_size=2)
        self.db = {r["id"]: dict(r) for r in records}
        self.envelope, self.fail_after_post, self.ignore_patch = envelope, fail_after_post, ignore_patch
        self.posts, self.patches, self.ids = 0, 0, itertools.count(1)

    def _request(self, method, path, *, params=None, json_body=None, retries=3):
        parts = path.strip("/").split("/")
        if method == "GET" and parts == ["accounts"]:
            rows = [r for r in self.db.values()
                    if all(str(r.get(k)) == v for k, v in (params or {}).items() if k in ("zip", "parent_id") and v)]
            page, size = params["page"], params["page_size"]
            chunk = rows[(page - 1) * size: page * size]
            return chunk if self.envelope is None else {self.envelope: chunk, "page": page, "total": len(rows)}
        if method == "GET" and parts[0] == "accounts":
            return dict(self.db[parts[1]])
        if method == "POST":
            new = dict(json_body, id=f"NEW-{next(self.ids)}")
            self.db[new["id"]] = new
            self.posts += 1
            if self.fail_after_post:
                self.fail_after_post = False
                raise requests.ConnectionError("response lost")
            return new
        if method == "PATCH":
            self.patches += 1
            if not self.ignore_patch:
                self.db[parts[1]].update(json_body)
            return dict(self.db[parts[1]])
        raise AssertionError(f"unexpected {method} {path}")


def website(**kw):
    loc = {"name": "Bellhaven Oaks", "address": "100 Main Street", "city": "San Francisco", "state": "CA",
           "zip_code": "94105", "care_offerings": ["Assisted Living"], "source_url": "https://site.test/x"}
    loc.update(kw)
    return loc


def approved(store, client, locations, parent=PARENT):
    proposals, _ = reconcile(locations, client.list_accounts(), parent)
    store.upsert_proposals(proposals)
    items = store.list_proposals("pending")
    for item in items:
        store.record_decision(item["key"], "approved", "tester")
    return store.list_proposals("approved")


def test_pagination_and_envelopes():
    for env in ("items", "data", None):
        api = FakeApi([raw(f"A{i}") for i in range(5)], envelope=env)
        assert [a["id"] for a in api.list_accounts()] == [f"A{i}" for i in range(5)]
    with pytest.raises(CrmSchemaError):
        extract_items({"a": [{"x": 1}], "b": [{"y": 2}]})


def test_pagination_loop_guard():
    api = FakeApi([raw("A1"), raw("A2")])
    api._request = lambda *a, **k: [raw("A1"), raw("A2")]  # server ignoring the page param
    with pytest.raises(CrmSchemaError):
        api.list_accounts()


def test_writes_blocked_without_flag_and_in_ci(monkeypatch):
    with pytest.raises(CrmWriteBlockedError):
        FakeApi([raw("A1")], write_enabled=False).update_account("A1", {"name": "x"})
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    with pytest.raises(CrmWriteBlockedError):
        FakeApi([raw("A1")]).update_account("A1", {"name": "x"})


def test_writes_blocked_when_live_schema_lacks_mapped_fields():
    rec = raw("A1")
    del rec["chow_current_account"]
    with pytest.raises(CrmSchemaError):
        FakeApi([rec]).update_account("A1", {"name": "x"})


def test_token_not_in_repr_and_no_delete():
    api = FakeApi([raw("A1")])
    assert "SECRET-TOKEN-123" not in repr(api)
    assert not any("delete" in name.lower() for name in dir(api))


def test_apply_requires_approval():
    api = FakeApi([raw("A1", name="Old Name")])
    store = DecisionStore(":memory:")
    store.upsert_proposals(reconcile([website()], api.list_accounts(), PARENT)[0])
    item = store.list_proposals("pending")[0]
    with pytest.raises(PermissionError):
        apply_proposal(item, api, store)
    assert api.patches == 0


def test_chow_apply_is_idempotent_and_verified():
    api = FakeApi([raw("A1", parent_id="P-OLD", lifetime_revenue=5000, outstanding_ar=120)])
    store = DecisionStore(":memory:")
    [item] = approved(store, api, [website()])
    assert item["billing_action"] == "chow_new_account"
    apply_proposal(item, api, store)
    apply_proposal(item, api, store)
    assert api.posts == 1
    old = api.db["A1"]
    assert old["chow_current_account"] == "NEW-1"
    assert old["parent_id"] == "P-OLD" and old["name"] == "Bellhaven Oaks"  # old account otherwise untouched
    assert api.db["NEW-1"]["parent_id"] == PARENT
    assert all(line.startswith("OK") for line in verify_applied(api, store))


def test_interrupted_create_is_adopted_not_duplicated():
    api = FakeApi([raw("A1", parent_id="P-OLD", lifetime_revenue=5000, outstanding_ar=120)], fail_after_post=True)
    store = DecisionStore(":memory:")
    [item] = approved(store, api, [website()])
    with pytest.raises(requests.ConnectionError):
        apply_proposal(item, api, store)  # POST succeeded server side, response lost
    log = apply_proposal(item, api, store)  # rerun
    assert api.posts == 1
    assert "already exists" in log[0]
    assert api.db["A1"]["chow_current_account"] == "NEW-1"


def test_missing_account_created_once_even_with_fresh_database():
    api = FakeApi([raw("A9", name="Bellhaven Other", street="1 Else Rd", zip="10001")])
    location = website(name="Bellhaven Harbor", address="9 Elm Road", zip_code="10001", city="New York", state="NY")
    s1 = DecisionStore(":memory:")
    item = next(i for i in approved(s1, api, [location]) if i["kind"] == "missing_in_crm")
    apply_proposal(item, api, s1)
    s2 = DecisionStore(":memory:")  # local state lost entirely
    s2.upsert_proposals([])
    s2._conn.execute("INSERT INTO proposals VALUES (?,?,?,?,?,?)", (item["key"], item["kind"], item["title"], "{}", "t", "t"))
    s2.record_decision(item["key"], "approved", "tester")
    apply_proposal(item, api, s2)
    assert api.posts == 1


def test_reparent_and_duplicate_apply():
    api = FakeApi([raw("A1", parent_id="P-OLD", lifetime_revenue=5000, outstanding_ar=0),
                   raw("A2", street="500 Pine St", zip="94110", name="Bellhaven Pine"),
                   raw("A3", street="500 Pine Street", zip="94110", name="Bellhaven Pine", lifetime_revenue=10)])
    store = DecisionStore(":memory:")
    items = approved(store, api, [website(), website(name="Bellhaven Pine", address="500 Pine St", zip_code="94110")])
    for item in items:
        apply_proposal(item, api, store)
    assert api.db["A1"]["parent_id"] == PARENT and api.posts == 0
    assert api.db["A2"]["duplicate_of_account"] == "A3" and api.db["A2"]["status"] == "Inactive"
    assert set(api.db) == {"A1", "A2", "A3"}  # nothing deleted
    # A rerun of the whole pipeline produces no new work.
    proposals, _ = reconcile([website(), website(name="Bellhaven Pine", address="500 Pine St", zip_code="94110")],
                             api.list_accounts(), PARENT)
    assert proposals == []


def test_write_that_does_not_stick_is_reported():
    api = FakeApi([raw("A1", name="Old Name")], ignore_patch=True)
    store = DecisionStore(":memory:")
    [item] = approved(store, api, [website()])
    with pytest.raises(CrmVerificationError):
        apply_proposal(item, api, store)
    assert store.applied_result(item["key"], 0) is None


def test_manual_review_placeholder_never_leaks():
    api = FakeApi([raw("A1", parent_id="P-OLD", lifetime_revenue=None, outstanding_ar=5)])
    store = DecisionStore(":memory:")
    [item] = approved(store, api, [website()])
    assert item["operations"] == [] and item["requires_manual_review"]
    assert apply_proposal(item, api, store) == ["No CRM write needed for this proposal."]
    assert NEW_ACCOUNT_PLACEHOLDER not in str(api.db)
