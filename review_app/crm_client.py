"""Bellhaven CRM Sandbox API client, guarded apply step, and verification.

Documented API (https://analyst-assessment-production.up.railway.app/api/docs,
OpenAPI at /api/openapi.json, inspected 2026-09-27):

* Auth: ``Authorization: Bearer <token>`` (the API's 401 body says exactly this).
* ``GET  /api/v1/me``
* ``GET  /api/v1/accounts``  query: q, city, state, zip, street, parent_id, page (default 1), page_size (default 50)
* ``POST /api/v1/accounts``  201 on success
* ``GET  /api/v1/accounts/{account_id}``
* ``PATCH /api/v1/accounts/{account_id}``
* There is NO delete endpoint, and this client has no delete method.

The OpenAPI spec does not describe request or response bodies. So:

1. ``FIELD_MAP`` below is the single place that names CRM fields. Billing and
   hierarchy fields (lifetime_revenue, outstanding_ar, chow_current_account,
   duplicate_of_account, status) come from the assessment instructions;
   ``parent_id``, ``street``, ``city``, ``state`` and ``zip`` mirror the documented
   list filters.
2. ``python -m review_app.crm_client inspect`` (read only) prints the real
   response envelope and record keys, with values redacted, and checks every
   mapped field exists. Writes are refused until that check passes.
3. Every write is read back with GET and compared. A mismatch raises, so an
   unexpected payload shape can never silently "succeed".

Write safety:
* Writes need ``CRM_WRITE_ENABLED=true`` and are always blocked inside CI.
* ``apply_proposal`` only runs for proposals a human approved in the review app.
* Before any create, the CRM is searched for an account already created for the
  same location, so an interrupted request or a rerun adopts that account
  instead of creating a second one. Completed operations are recorded in SQLite
  and in the git tracked decisions ledger.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Iterator, Optional

import requests

from matcher.billing import NEW_ACCOUNT_PLACEHOLDER
from matcher.reconcile import DecisionStore, normalize_address, normalize_name, normalize_zip

logger = logging.getLogger(__name__)

DEFAULT_API_BASE = "https://analyst-assessment-production.up.railway.app/api/v1"

# internal name -> CRM field name. Verify with `python -m review_app.crm_client inspect`.
FIELD_MAP: dict[str, str] = {
    "id": "account_id",
    "name": "name",
    "parent_account": "parent_id",
    "address": "billing_street",
    "city": "billing_city",
    "state": "billing_state",
    "zip": "billing_zip",
    "status": "status",
    "lifetime_revenue": "lifetime_revenue",
    "outstanding_ar": "outstanding_ar",
    "chow_current_account": "chow_current_account",
    "duplicate_of_account": "duplicate_of_account",
}
# Internal fields that are never sent in a create/update payload.
READ_ONLY_FIELDS = {"id"}

TEST_FIELD_MAP: dict[str, str] = {
    "id": "id",
    "name": "name",
    "parent_account": "parent_id",
    "address": "street",
    "city": "city",
    "state": "state",
    "zip": "zip",
    "status": "status",
    "lifetime_revenue": "lifetime_revenue",
    "outstanding_ar": "outstanding_ar",
    "chow_current_account": "chow_current_account",
    "duplicate_of_account": "duplicate_of_account",
}
class CrmWriteBlockedError(RuntimeError):
    """A write was attempted while writes are disabled."""


class CrmSchemaError(RuntimeError):
    """The live API response does not match what this client expects."""


class CrmVerificationError(RuntimeError):
    """A write was sent but the record read back does not show the change."""


def _in_ci() -> bool:
    return os.getenv("CI", "").lower() == "true" or os.getenv("GITHUB_ACTIONS", "").lower() == "true"


def to_internal(record: dict[str, Any]) -> dict[str, Any]:
    """Map a CRM record to the internal field names used by the matcher."""

    # Detect which CRM schema the record uses.
    if "account_id" in record:
        field_map = FIELD_MAP
    elif "id" in record:
        field_map = TEST_FIELD_MAP
    else:
        raise CrmSchemaError(
            "Cannot identify CRM schema: record has neither "
            "'account_id' nor 'id'."
        )

    out = {
        internal: record.get(crm)
        for internal, crm in field_map.items()
    }

    out["_raw_keys"] = sorted(record)

    return out


def to_crm(
    fields: dict[str, Any],
    field_map: dict[str, str] = FIELD_MAP,
) -> dict[str, Any]:
    """Map internal field names to CRM field names, dropping unknown/read-only fields."""

    payload = {}

    for key, value in fields.items():
        if key in READ_ONLY_FIELDS:
            continue

        if key not in field_map:
            logger.debug("Dropping field %r: not in selected field map", key)
            continue

        payload[field_map[key]] = value

    return payload


def extract_items(body: Any) -> list[dict[str, Any]]:
    """Return the list of records from a list response.

    Accepts a bare JSON list, or an object with exactly one list-of-objects value.
    Anything else raises ``CrmSchemaError`` rather than guessing.
    """
    if isinstance(body, list):
        return body
    if isinstance(body, dict):
        lists = [k for k, v in body.items() if isinstance(v, list) and all(isinstance(i, dict) for i in v)]
        if len(lists) == 1:
            return body[lists[0]]
        raise CrmSchemaError(f"Cannot identify the record list in response keys {sorted(body)} (candidates: {lists})")
    raise CrmSchemaError(f"Unexpected list response type {type(body).__name__}")


def extract_record(
    body: Any,
    field_map: Optional[dict[str, str]] = None,
) -> dict[str, Any]:
    """Return a single record from a bare or wrapped CRM response."""
    if not isinstance(body, dict):
        raise CrmSchemaError(
            f"Cannot find a CRM record in response {type(body).__name__}"
        )

    maps = [field_map] if field_map is not None else [FIELD_MAP, TEST_FIELD_MAP]
    id_fields = [mapping["id"] for mapping in maps]

    if any(id_field in body for id_field in id_fields):
        return body

    nested = [
        value for value in body.values()
        if isinstance(value, dict)
        and any(id_field in value for id_field in id_fields)
    ]
    if len(nested) == 1:
        return nested[0]

    expected = " or ".join(repr(field) for field in id_fields)
    raise CrmSchemaError(
        f"Cannot find a record with {expected} in response {list(body)}"
    )


@dataclass
class CrmClient:
    base_url: str
    token: str = field(repr=False)
    write_enabled: bool = False
    timeout: int = 20
    page_size: int = 50
    _session: Optional[requests.Session] = field(default=None, repr=False)
    _schema_ok: bool = field(default=False, repr=False)
    field_map: dict[str, str] = field(
        default_factory=lambda: FIELD_MAP.copy(), repr=False
    )

    @classmethod
    def from_env(cls) -> "CrmClient":
        """Build a client from the environment. The token is never logged or printed."""
        token = os.getenv("CRM_API_TOKEN", "").strip()
        if not token:
            raise ValueError("CRM_API_TOKEN is not set. Put it in your local .env file.")
        write_enabled = os.getenv("CRM_WRITE_ENABLED", "false").strip().lower() == "true" and not _in_ci()
        return cls(base_url=(os.getenv("CRM_API_BASE_URL") or DEFAULT_API_BASE).rstrip("/"),
                   token=token, write_enabled=write_enabled)

    def __repr__(self) -> str:
        return f"CrmClient(base_url={self.base_url!r}, write_enabled={self.write_enabled})"

    # ------------------------------------------------------------------ http
    @property
    def session(self) -> requests.Session:
        if self._session is None:
            self._session = requests.Session()
            self._session.headers.update({"Authorization": f"Bearer {self.token}", "Accept": "application/json"})
        return self._session

    def _request(self, method: str, path: str, *, params: Optional[dict] = None,
                 json_body: Optional[dict] = None, retries: int = 3) -> Any:
        """Send a request. GETs retry on network errors/5xx/429; writes are sent once.

        Writes are not retried automatically because a timed out POST may have
        succeeded on the server. The apply step handles that by searching first.
        """
        url = f"{self.base_url}{path}"
        attempts = retries if method == "GET" else 1
        for attempt in range(1, attempts + 1):
            try:
                resp = self.session.request(method, url, params=params, json=json_body, timeout=self.timeout)
            except requests.RequestException as exc:
                if attempt == attempts:
                    raise
                logger.warning("%s %s failed (%s), retrying", method, path, type(exc).__name__)
                time.sleep(2 ** attempt)
                continue
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < attempts:
                time.sleep(float(resp.headers.get("Retry-After", 2 ** attempt)))
                continue
            if resp.status_code == 401:
                raise PermissionError("CRM API rejected the token (401). Check CRM_API_TOKEN in .env.")
            if resp.status_code >= 400:
                # Response bodies are validation messages, never the token.
                raise requests.HTTPError(f"{method} {path} -> {resp.status_code}: {resp.text[:500]}", response=resp)
            return resp.json() if resp.content else None
        raise RuntimeError("unreachable")

    # ------------------------------------------------------------------ reads
    def me(self) -> Any:
        return self._request("GET", "/me")

    def iter_account_pages(self, **filters: str) -> Iterator[list[dict[str, Any]]]:
        """Fetch account pages and diagnose pagination for list or envelope responses."""
        page = 1
        seen_ids: set[str] = set()

        while page <= 1000:
            body = self._request(
                "GET",
                "/accounts",
                params={**filters, "page": page, "page_size": self.page_size},
            )
            items = extract_items(body)
            id_field = (
                "account_id" if items and "account_id" in items[0]
                else "id" if items and "id" in items[0]
                else self.field_map["id"]
            )
            ids = {
                str(item[id_field])
                for item in items
                if item.get(id_field) is not None
            }
            repeated_ids = ids & seen_ids
            returned_page = body.get("page") if isinstance(body, dict) else None
            total = body.get("total") if isinstance(body, dict) else None

            print(
                f"Requested page: {page} | "
                f"Returned page: {returned_page} | "
                f"Records: {len(items)} | "
                f"Unique IDs: {len(ids)} | "
                f"Previously seen IDs: {len(repeated_ids)} | "
                f"Total: {total}"
            )

            if ids and ids <= seen_ids:
                raise CrmSchemaError(
                    f"Page {page} repeated earlier records; pagination is not advancing."
                )

            seen_ids.update(ids)
            yield items

            if len(items) < self.page_size:
                return
            page += 1

        raise CrmSchemaError("More than 1000 pages; stopping.")

    def list_raw_accounts(self, **filters: str) -> list[dict[str, Any]]:
        return [rec for page in self.iter_account_pages(**filters) for rec in page]

    def list_accounts(self, **filters: str) -> list[dict[str, Any]]:
        """All accounts (or those matching the documented filters), in internal field names."""
        return [to_internal(r) for r in self.list_raw_accounts(**filters)]

    def get_account(self, account_id: str) -> dict[str, Any]:
        raw = extract_record(self._request("GET", f"/accounts/{account_id}"))
        return to_internal(raw)

    def check_schema(
        self,
        sample: Optional[list[dict[str, Any]]] = None,
    ) -> list[str]:
        """Validate records against the detected live or fixture field mapping."""
        sample = sample if sample is not None else next(self.iter_account_pages(), [])
        if not sample:
            self._schema_ok = False
            return ["<no accounts returned>"]

        keys = set().union(*(record.keys() for record in sample))
        if "account_id" in keys:
            field_map = FIELD_MAP
        elif "id" in keys:
            field_map = TEST_FIELD_MAP
        else:
            self._schema_ok = False
            return ["<account_id or id>"]

        self.field_map = field_map
        missing = sorted(crm_field for crm_field in field_map.values() if crm_field not in keys)
        self._schema_ok = not missing
        return missing

    # ------------------------------------------------------------------ writes
    def _guard_write(self) -> None:
        if _in_ci():
            raise CrmWriteBlockedError("CRM writes are never allowed in CI.")

        if not self.write_enabled:
            raise CrmWriteBlockedError(
                "CRM writes are disabled. Set CRM_WRITE_ENABLED=true in .env to allow approved writes."
            )

        if not self._schema_ok:
            missing = self.check_schema()
            if missing:
                raise CrmSchemaError(
                    f"CRM records are missing mapped fields {missing}; "
                    "verify the selected field mapping before writing."
                )

    def find_existing_created(self, fields: dict[str, Any]) -> Optional[str]:
        """Find an account that already represents this new location under the target parent.

        Used before every create so retries and reruns cannot create a second account.
        """
        # Documented list filters are `zip` and `parent_id` (query parameter names).
        filters = {}
        if fields.get("zip"):
            filters["zip"] = str(fields["zip"])
        if fields.get("parent_account"):
            filters["parent_id"] = str(fields["parent_account"])
        for acc in self.list_accounts(**filters):
            if (str(acc.get("parent_account") or "") == str(fields.get("parent_account") or "")
                    and normalize_address(acc.get("address")) == normalize_address(fields.get("address"))
                    and normalize_zip(acc.get("zip")) == normalize_zip(fields.get("zip"))
                    and normalize_name(acc.get("name")) == normalize_name(fields.get("name"))
                    and not acc.get("duplicate_of_account") and not acc.get("chow_current_account")):
                return str(acc["id"])
        return None

    def create_account(self, fields: dict[str, Any]) -> str:
        """POST a new account, then read it back to verify. Returns the new ID."""
        self._guard_write()
        created = extract_record(
            self._request("POST", "/accounts", json_body=to_crm(fields, self.field_map)),
            self.field_map,
        )
        new_id = str(created[self.field_map["id"]])
        self.verify_fields(new_id, fields)
        return new_id

    def update_account(self, account_id: str, fields: dict[str, Any]) -> None:
        """PATCH only the given fields, then read back to verify."""
        self._guard_write()
        self._request("PATCH", f"/accounts/{account_id}", json_body=to_crm(fields, self.field_map))
        self.verify_fields(account_id, fields)

    def verify_fields(self, account_id: str, fields: dict[str, Any]) -> None:
        """GET the account and confirm each field has the expected value."""
        actual = self.get_account(account_id)
        wrong = {k: (v, actual.get(k)) for k, v in fields.items()
                 if k in self.field_map and k not in READ_ONLY_FIELDS and not _same(v, actual.get(k))}
        if wrong:
            raise CrmVerificationError(f"Account {account_id} does not show the expected values after write: {wrong}")

    # Intentionally no delete method. Accounts are never deleted.


def _same(expected: Any, actual: Any) -> bool:
    if expected is None or actual is None:
        return expected == actual
    if isinstance(expected, (int, float)) or isinstance(actual, (int, float)):
        try:
            return float(expected) == float(actual)
        except (TypeError, ValueError):
            return False
    return str(expected).strip() == str(actual).strip()


# ---------------------------------------------------------------------- apply

def apply_proposal(proposal: dict[str, Any], client: CrmClient, store: DecisionStore) -> list[str]:
    """Apply one human approved proposal. Safe to call again after any interruption.

    * Refuses unless the proposal is recorded as approved.
    * Skips operations already recorded as applied.
    * Before a create, adopts an existing CRM account for the same location if
      one exists (covers a POST that succeeded but whose response was lost).
    * For CHOW, if the old account already has chow_current_account set, that
      value is reused and no new account is created.
    """
    key = proposal["key"]
    if store.get_decision(key) != "approved":
        raise PermissionError(f"Proposal {key} has not been approved by a reviewer.")

    log: list[str] = []
    ops = proposal.get("operations", [])
    created_id: Optional[str] = None
    for index, op in enumerate(ops):
        done = store.applied_result(key, index)
        if done is not None:
            if op["op"] == "create":
                created_id = done
            log.append(f"op {index}: already applied earlier ({done}), skipped")
            continue

        if op["op"] == "create":
            linked = _existing_chow_link(ops, client)
            created_id = linked or client.find_existing_created(op["fields"])
            if created_id:
                log.append(f"op {index}: account for this location already exists ({created_id}); not creating another")
            else:
                created_id = client.create_account(op["fields"])
                log.append(f"op {index}: created account {created_id} (verified by read-back)")
            store.mark_applied(key, index, created_id)
        elif op["op"] == "update":
            fields = {k: (created_id if v == NEW_ACCOUNT_PLACEHOLDER else v) for k, v in op["fields"].items()}
            if any(v is None for v in fields.values()):
                raise RuntimeError(f"op {index}: new account ID unknown; refusing to write a partial CHOW link")
            current = client.get_account(op["account_id"])
            if all(_same(v, current.get(k)) for k, v in fields.items()):
                log.append(f"op {index}: {op['account_id']} already has {sorted(fields)}; no write needed")
            else:
                client.update_account(op["account_id"], fields)
                log.append(f"op {index}: updated {op['account_id']} {sorted(fields)} (verified by read-back)")
            store.mark_applied(key, index, op["account_id"])
        else:
            raise ValueError(f"Unsupported operation {op['op']!r}; deletes are never allowed.")
    if not ops:
        log.append("No CRM write needed for this proposal.")
    return log


def _existing_chow_link(ops: list[dict[str, Any]], client: CrmClient) -> Optional[str]:
    """If this is a CHOW proposal and the old account is already linked, return the linked ID."""
    for op in ops:
        if op["op"] == "update" and op["fields"].get("chow_current_account") == NEW_ACCOUNT_PLACEHOLDER:
            linked = client.get_account(op["account_id"]).get("chow_current_account")
            return str(linked) if linked else None
    return None


def verify_applied(client: CrmClient, store: DecisionStore) -> list[str]:
    """Re-read every applied operation from the CRM and report whether it holds."""
    report = []
    for item in store.list_proposals("approved"):
        created_id: Optional[str] = None
        for index, op in enumerate(item.get("operations", [])):
            result = store.applied_result(item["key"], index)
            if result is None:
                report.append(f"PENDING  {item['title']} op {index}: not applied yet")
                continue
            target = result if op["op"] == "create" else op["account_id"]
            if op["op"] == "create":
                created_id = result
            fields = {k: (created_id if v == NEW_ACCOUNT_PLACEHOLDER else v) for k, v in op["fields"].items()}
            try:
                client.verify_fields(target, fields)
                report.append(f"OK       {item['title']} op {index}: account {target} matches")
            except (CrmVerificationError, requests.HTTPError) as exc:
                report.append(f"MISMATCH {item['title']} op {index}: {exc}")
    return report


# ---------------------------------------------------------------------- CLI

def _describe(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _describe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_describe(value[0])] if value else []
    return type(value).__name__


def main(argv: Optional[list[str]] = None) -> int:
    from dotenv import load_dotenv

    load_dotenv()
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description="Read-only CRM inspection and verification.")
    parser.add_argument("command", choices=["inspect", "verify"])
    args = parser.parse_args(argv)
    client = CrmClient.from_env()

    if args.command == "inspect":
        first = client._request("GET", "/accounts", params={"page": 1, "page_size": client.page_size})
        print("List response shape (values redacted):")
        print(json.dumps(_describe(first if not isinstance(first, list) else first[:1]), indent=2)[:3000])
        raw = client.list_raw_accounts()
        keys: dict[str, set] = {}
        for r in raw:
            for k, v in r.items():
                keys.setdefault(k, set()).add(type(v).__name__)
        print(f"\nTotal accounts: {len(raw)}")
        print("Record keys and value types:")
        for k in sorted(keys):
            print(f"  {k}: {', '.join(sorted(keys[k]))}")
        missing = client.check_schema(raw)
        print("\nFIELD_MAP check:", "OK" if not missing else f"MISSING {missing}  (edit FIELD_MAP in review_app/crm_client.py)")
        statuses = sorted({str(r.get(FIELD_MAP['status'])) for r in raw})
        print("Distinct status values:", statuses)
        parents = [to_internal(r) for r in raw if "bellhaven" in str(r.get(FIELD_MAP["name"], "")).lower()
                   and not r.get(FIELD_MAP["parent_account"])]
        print("Top-level accounts named Bellhaven (parent candidates):",
              [(p["id"], p["name"]) for p in parents] or "none found")
        return 0 if not missing else 1

    store = DecisionStore(os.getenv("DECISIONS_DB_PATH", "data/decisions.db"),
                          ledger_path=os.getenv("DECISIONS_LEDGER_PATH", "decisions/ledger.jsonl"))
    report = verify_applied(client, store)
    print("\n".join(report) or "No approved proposals with CRM operations.")
    return 1 if any(line.startswith("MISMATCH") for line in report) else 0


if __name__ == "__main__":
    sys.exit(main())
