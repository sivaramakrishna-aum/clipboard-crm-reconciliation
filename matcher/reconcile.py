"""Reconcile scraped Bellhaven locations against CRM accounts.

Pipeline (``python -m matcher.reconcile --dry-run``):
    1. Load website locations (scraper, or a CSV snapshot).
    2. Load CRM accounts (API client, or a CSV export).
    3. Classify every location and every in-scope CRM account.
    4. Turn discrepancies into proposals with evidence.
    5. Store proposals in SQLite. Proposals that were already approved or
       rejected are not re-queued.

This module never writes to the CRM. Writes happen only from the review app,
after a human approves a proposal.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from difflib import SequenceMatcher
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional, Sequence

import pandas as pd
from dotenv import load_dotenv

from matcher.billing import (
    BillingAction,
    ChangeOp,
    OpType,
    decide_parent_change,
    plan_duplicate_resolution,
)

logger = logging.getLogger(__name__)

ADDRESS_MATCH_THRESHOLD = 0.90
NAME_MATCH_THRESHOLD = 0.90
NAME_ONLY_MATCH_THRESHOLD = 0.95

_ABBREVIATIONS = {
    "street": "st",
    "avenue": "ave",
    "road": "rd",
    "boulevard": "blvd",
    "drive": "dr",
    "lane": "ln",
    "court": "ct",
    "place": "pl",
    "parkway": "pkwy",
    "highway": "hwy",
    "suite": "ste",
    "north": "n",
    "south": "s",
    "east": "e",
    "west": "w",
    "northwest": "nw",
    "northeast": "ne",
    "southwest": "sw",
    "southeast": "se",
}
_NAME_NOISE = {"the", "of", "at", "llc", "inc"}


class Classification(str, Enum):
    CONFIDENT_MATCH = "confident_match"
    NAME_MISMATCH = "name_mismatch"
    PARENT_MISMATCH = "parent_mismatch"
    MISSING_IN_CRM = "missing_in_crm"
    DUPLICATE = "duplicate"
    NOT_ON_WEBSITE = "not_on_website"


@dataclass
class Proposal:
    """A proposed change plus the evidence a reviewer needs to judge it."""

    kind: Classification
    title: str
    evidence: list[str]
    operations: list[ChangeOp]
    account_ids: list[str] = field(default_factory=list)
    location: Optional[dict[str, Any]] = None
    billing_action: Optional[str] = None
    requires_manual_review: bool = False

    @property
    def key(self) -> str:
        """Stable identity: same situation and same target values give the same key."""
        basis = {
            "kind": self.kind.value,
            "accounts": sorted(self.account_ids),
            "location": _location_key(self.location) if self.location else None,
            "ops": [(o.op.value, o.account_id, sorted(o.fields.items())) for o in self.operations],
        }
        return hashlib.sha256(json.dumps(basis, sort_keys=True, default=str).encode()).hexdigest()[:20]

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["kind"] = self.kind.value
        data["key"] = self.key
        data["operations"] = [
            {"op": o.op.value, "account_id": o.account_id, "fields": o.fields, "note": o.note}
            for o in self.operations
        ]
        return data


# --------------------------------------------------------------------------- normalization

def normalize_text(value: Any) -> str:
    text = re.sub(r"[^a-z0-9 ]", " ", str(value or "").lower())
    return " ".join(text.split())


def normalize_address(value: Any) -> str:
    return " ".join(_ABBREVIATIONS.get(tok, tok) for tok in normalize_text(value).split())


def normalize_name(value: Any) -> str:
    return " ".join(tok for tok in normalize_text(value).split() if tok not in _NAME_NOISE)


def normalize_zip(value: Any) -> str:
    digits = re.sub(r"\D", "", str(value or ""))
    return digits[:5].zfill(5) if digits else ""


def similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def _location_key(loc: dict[str, Any]) -> str:
    return f"{normalize_address(loc.get('address'))}|{normalize_zip(loc.get('zip_code'))}"


def _acc_zip(acc: dict[str, Any]) -> str:
    return normalize_zip(acc.get("zip") or acc.get("zip_code"))


def _is_retired(acc: dict[str, Any]) -> bool:
    """Accounts already marked as duplicates or superseded by CHOW are ignored for matching."""
    return bool(acc.get("duplicate_of_account")) or bool(acc.get("chow_current_account"))


# --------------------------------------------------------------------------- matching

def find_candidates(location: dict[str, Any], accounts: Sequence[dict[str, Any]]) -> list[tuple[dict[str, Any], str, float]]:
    """Return CRM accounts that plausibly represent ``location``.

    Primary signal is address + ZIP. When no address match exists, fall back to a
    very close name match in the same city and state.

    Returns:
        List of (account, match_basis, score).
    """
    loc_addr = normalize_address(location.get("address"))
    loc_zip = normalize_zip(location.get("zip_code"))
    loc_name = normalize_name(location.get("name"))

    by_address = []
    for acc in accounts:
        if _is_retired(acc) or _acc_zip(acc) != loc_zip:
            continue
        score = similarity(loc_addr, normalize_address(acc.get("address")))
        if score >= ADDRESS_MATCH_THRESHOLD:
            by_address.append((acc, "address+zip", round(score, 3)))
    if by_address:
        return by_address

    by_name = []
    for acc in accounts:
        if _is_retired(acc):
            continue
        same_place = (normalize_text(acc.get("city")) == normalize_text(location.get("city"))
                      and normalize_text(acc.get("state")) == normalize_text(location.get("state")))
        score = similarity(loc_name, normalize_name(acc.get("name")))
        if same_place and score >= NAME_ONLY_MATCH_THRESHOLD:
            by_name.append((acc, "name+city+state", round(score, 3)))
    return by_name


def _location_evidence(loc: dict[str, Any]) -> str:
    return (f"Website: '{loc.get('name')}', {loc.get('address')}, {loc.get('city')}, {loc.get('state')} "
            f"{loc.get('zip_code')}; care: {', '.join(loc.get('care_offerings') or []) or 'n/a'} "
            f"(source: {loc.get('source_url') or 'n/a'}"
            f"{'; linked from the home page, not in the paginated directory' if str(loc.get('listed_in_directory', True)).lower() in ('false', '0') else ''})")


def _account_evidence(acc: dict[str, Any]) -> str:
    return (f"CRM {acc.get('id')}: '{acc.get('name')}', {acc.get('address')}, {acc.get('city')}, "
            f"{acc.get('state')} {acc.get('zip') or acc.get('zip_code')}; parent={acc.get('parent_account')}; "
            f"status={acc.get('status')}; lifetime_revenue={acc.get('lifetime_revenue')}; "
            f"outstanding_ar={acc.get('outstanding_ar')}")


def _new_account_fields(loc: dict[str, Any], parent_id: str) -> dict[str, Any]:
    """Fields for a new account, in internal names (mapped by crm_client.FIELD_MAP)."""
    return {
        "name": loc.get("name"),
        "address": loc.get("address"),
        "city": loc.get("city"),
        "state": loc.get("state"),
        "zip": normalize_zip(loc.get("zip_code")),
        "parent_account": parent_id,
    }


def reconcile(locations: Sequence[dict[str, Any]], accounts: Sequence[dict[str, Any]],
              parent_id: str) -> tuple[list[Proposal], dict[str, str]]:
    """Classify locations and accounts and build proposals.

    Args:
        locations: scraped website locations (``Location.to_dict()`` shape).
        accounts: CRM accounts.
        parent_id: CRM ID of the Bellhaven Senior Living parent account.

    Returns:
        (proposals, classification per location key / account ID). Confident
        matches appear in the classification map but produce no proposal.
    """
    proposals: list[Proposal] = []
    classes: dict[str, str] = {}
    matched_ids: set[str] = set()

    for loc in locations:
        loc_key = _location_key(loc)
        candidates = find_candidates(loc, accounts)

        if not candidates:
            classes[loc_key] = Classification.MISSING_IN_CRM.value
            proposals.append(Proposal(
                kind=Classification.MISSING_IN_CRM,
                title=f"Create account for '{loc.get('name')}'",
                evidence=[_location_evidence(loc), "No CRM account matched on address+ZIP or on name+city+state."],
                operations=[ChangeOp(OpType.CREATE, None, _new_account_fields(loc, parent_id), "New Bellhaven location.")],
                location=loc,
            ))
            continue

        cand_accounts = [c[0] for c in candidates]
        matched_ids.update(str(a["id"]) for a in cand_accounts)

        if len(cand_accounts) > 1:
            classes[loc_key] = Classification.DUPLICATE.value
            survivor_id, ops = plan_duplicate_resolution(cand_accounts, preferred_parent=parent_id)
            if ops:
                proposals.append(Proposal(
                    kind=Classification.DUPLICATE,
                    title=f"Resolve {len(cand_accounts)} duplicate accounts for '{loc.get('name')}' (keep {survivor_id})",
                    evidence=[_location_evidence(loc)]
                    + [f"{_account_evidence(a)} [matched on {basis}, score {score}]"
                       for a, basis, score in candidates]
                    + [f"Survivor {survivor_id} chosen using the documented priority: "
                       f"not already retired, preferred parent when present, highest lifetime revenue, "
                       f"then lowest account ID."],
                    operations=ops,
                    account_ids=[str(a["id"]) for a in cand_accounts],
                    location=loc,
                ))
            # Continue checks on the survivor only.
            cand_accounts = [a for a in cand_accounts if str(a["id"]) == survivor_id]
            candidates = [c for c in candidates if str(c[0]["id"]) == survivor_id]

        acc, basis, score = candidates[0]
        acc_id = str(acc["id"])
        found_issue = classes.get(loc_key) == Classification.DUPLICATE.value
        match_line = f"Matched on {basis} (score {score})."

        name_score = similarity(normalize_name(loc.get("name")), normalize_name(acc.get("name")))
        if name_score < NAME_MATCH_THRESHOLD:
            found_issue = True
            classes.setdefault(loc_key, Classification.NAME_MISMATCH.value)
            proposals.append(Proposal(
                kind=Classification.NAME_MISMATCH,
                title=f"Rename {acc_id} '{acc.get('name')}' to '{loc.get('name')}'",
                evidence=[_location_evidence(loc), _account_evidence(acc), match_line,
                          f"Name similarity {name_score:.2f} is below {NAME_MATCH_THRESHOLD}."],
                operations=[ChangeOp(OpType.UPDATE, acc_id, {"name": loc.get("name")}, "Correct account name.")],
                account_ids=[acc_id],
                location=loc,
            ))

        if str(acc.get("parent_account") or "") != parent_id:
            found_issue = True
            classes.setdefault(loc_key, Classification.PARENT_MISMATCH.value)
            decision = decide_parent_change(acc, parent_id, _new_account_fields(loc, parent_id))
            proposals.append(Proposal(
                kind=Classification.PARENT_MISMATCH,
                title=f"Fix parent of {acc_id} '{acc.get('name')}' ({decision.action.value})",
                evidence=[_location_evidence(loc), _account_evidence(acc), match_line,
                          f"Current parent {acc.get('parent_account')!r} differs from Bellhaven parent {parent_id!r}.",
                          f"Billing rule: {decision.reason}"],
                operations=list(decision.operations),
                account_ids=[acc_id],
                location=loc,
                billing_action=decision.action.value,
                requires_manual_review=decision.action == BillingAction.MANUAL_REVIEW,
            ))

        if not found_issue:
            classes[loc_key] = Classification.CONFIDENT_MATCH.value

    # CRM accounts under the Bellhaven parent that no website location matched.
    for acc in accounts:
        acc_id = str(acc.get("id"))
        if acc_id in matched_ids or _is_retired(acc) or str(acc.get("parent_account") or "") != parent_id:
            continue
        classes[acc_id] = Classification.NOT_ON_WEBSITE.value
        # TODO(assessment): confirm the desired action (e.g. mark Inactive). Until then this
        # is flagged for review only and approving it performs no CRM write.
        proposals.append(Proposal(
            kind=Classification.NOT_ON_WEBSITE,
            title=f"{acc_id} '{acc.get('name')}' is no longer listed on the Bellhaven website",
            evidence=[_account_evidence(acc), f"No scraped location matched this account ({len(locations)} locations checked)."],
            operations=[],
            account_ids=[acc_id],
            requires_manual_review=True,
        ))

    return proposals, classes


# --------------------------------------------------------------------------- parent account

def resolve_parent_id(accounts: Sequence[dict[str, Any]], configured: str = "") -> str:
    """Return the Bellhaven parent account ID.

    Uses ``BELLHAVEN_PARENT_ACCOUNT_ID`` when set (and checks it exists). Otherwise
    looks for exactly one top-level account named "Bellhaven Senior Living".
    Raises ``ValueError`` when it cannot be determined unambiguously.
    """
    ids = {str(a.get("id")) for a in accounts}
    if configured:
        if configured not in ids:
            raise ValueError(f"BELLHAVEN_PARENT_ACCOUNT_ID={configured!r} is not an existing CRM account.")
        return configured
    candidates = [a for a in accounts
                  if normalize_name(a.get("name")) == "bellhaven senior living" and not a.get("parent_account")]
    if len(candidates) == 1:
        return str(candidates[0]["id"])
    raise ValueError(
        "Could not identify a single top-level 'Bellhaven Senior Living' account "
        f"(found {[(a.get('id'), a.get('name')) for a in candidates]}). Set BELLHAVEN_PARENT_ACCOUNT_ID in .env."
    )


# --------------------------------------------------------------------------- decision store

class DecisionStore:
    """Proposals, human decisions and applied writes.

    Two layers:
    * SQLite (``data/decisions.db``, git ignored): fast local working state for
      the review app, including proposal payloads.
    * Ledger (``decisions/ledger.jsonl``, meant to be committed to a PRIVATE repo):
      an append-only record of decisions and applied operations only. It holds
      proposal keys, reviewer name, timestamps and CRM account IDs. No tokens, no
      financials, no addresses. On startup the ledger is replayed into SQLite, so
      decisions survive a lost/recreated database and are visible to the
      read-only scheduled run, which never needs a database of its own.
    """

    def __init__(self, path: str | Path, ledger_path: Optional[str | Path] = None):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.ledger_path = Path(ledger_path) if ledger_path else None
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init()
        self._replay_ledger()

    def _init(self) -> None:
        with self._tx() as cur:
            cur.executescript(
                """
                CREATE TABLE IF NOT EXISTS proposals (
                    key TEXT PRIMARY KEY, kind TEXT NOT NULL, title TEXT NOT NULL,
                    payload TEXT NOT NULL, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS decisions (
                    key TEXT PRIMARY KEY,
                    decision TEXT NOT NULL CHECK (decision IN ('approved', 'rejected')),
                    decided_by TEXT, decided_at TEXT NOT NULL, note TEXT
                );
                CREATE TABLE IF NOT EXISTS applied_ops (
                    key TEXT NOT NULL, op_index INTEGER NOT NULL, result_id TEXT, applied_at TEXT NOT NULL,
                    PRIMARY KEY (key, op_index)
                );
                """
            )

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Cursor]:
        cur = self._conn.cursor()
        try:
            yield cur
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        finally:
            cur.close()

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    # ---------------------------------------------------------------- ledger
    def _append_ledger(self, entry: dict[str, Any]) -> None:
        if not self.ledger_path:
            return
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        with self.ledger_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, sort_keys=True) + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def _replay_ledger(self) -> None:
        if not self.ledger_path or not self.ledger_path.exists():
            return
        with self._tx() as cur, self.ledger_path.open(encoding="utf-8") as fh:
            for line_no, line in enumerate(fh, 1):
                if not line.strip():
                    continue
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    logger.error("Ignoring malformed ledger line %d in %s", line_no, self.ledger_path)
                    continue
                if e.get("type") == "decision":
                    cur.execute("INSERT OR IGNORE INTO decisions (key, decision, decided_by, decided_at, note) "
                                "VALUES (?, ?, ?, ?, ?)",
                                (e["key"], e["decision"], e.get("decided_by", ""), e["at"], e.get("note", "")))
                elif e.get("type") == "applied":
                    cur.execute("INSERT OR IGNORE INTO applied_ops (key, op_index, result_id, applied_at) "
                                "VALUES (?, ?, ?, ?)", (e["key"], e["op_index"], e.get("result_id"), e["at"]))

    # ---------------------------------------------------------------- proposals
    def upsert_proposals(self, proposals: Iterable[Proposal]) -> list[Proposal]:
        """Save proposals and return only those that still need a decision."""
        now = self._now()
        new: list[Proposal] = []
        with self._tx() as cur:
            for p in proposals:
                cur.execute(
                    """INSERT INTO proposals (key, kind, title, payload, first_seen, last_seen)
                       VALUES (?, ?, ?, ?, ?, ?)
                       ON CONFLICT(key) DO UPDATE SET last_seen = excluded.last_seen, payload = excluded.payload""",
                    (p.key, p.kind.value, p.title, json.dumps(p.to_dict(), default=str), now, now),
                )
                if cur.execute("SELECT 1 FROM decisions WHERE key = ?", (p.key,)).fetchone() is None:
                    new.append(p)
        return new

    def is_decided(self, key: str) -> bool:
        return self.get_decision(key) is not None

    def record_decision(self, key: str, decision: str, decided_by: str = "", note: str = "") -> bool:
        """Record approve/reject once. Returns False if the proposal was already decided."""
        if decision not in ("approved", "rejected"):
            raise ValueError("decision must be 'approved' or 'rejected'")
        if not decided_by.strip():
            raise ValueError("A reviewer name is required to record a decision.")
        at = self._now()
        with self._tx() as cur:
            if cur.execute("SELECT 1 FROM proposals WHERE key = ?", (key,)).fetchone() is None:
                raise KeyError(f"Unknown proposal {key}")
            cur.execute("INSERT OR IGNORE INTO decisions (key, decision, decided_by, decided_at, note) "
                        "VALUES (?, ?, ?, ?, ?)", (key, decision, decided_by, at, note))
            inserted = cur.rowcount == 1
        if inserted:
            self._append_ledger({"type": "decision", "key": key, "decision": decision,
                                 "decided_by": decided_by, "at": at, "note": note})
        return inserted

    def get_decision(self, key: str) -> Optional[str]:
        row = self._conn.execute("SELECT decision FROM decisions WHERE key = ?", (key,)).fetchone()
        return row["decision"] if row else None

    def list_proposals(self, status: str = "pending") -> list[dict[str, Any]]:
        """status: 'pending', 'approved', 'rejected' or 'all'."""
        sql = ("SELECT p.payload, d.decision, d.decided_at, d.decided_by FROM proposals p "
               "LEFT JOIN decisions d ON d.key = p.key")
        params: tuple = ()
        if status == "pending":
            sql += " WHERE d.key IS NULL"
        elif status in ("approved", "rejected"):
            sql += " WHERE d.decision = ?"
            params = (status,)
        rows = self._conn.execute(sql + " ORDER BY p.first_seen, p.key", params).fetchall()
        out = []
        for r in rows:
            item = json.loads(r["payload"])
            item.update(decision=r["decision"], decided_at=r["decided_at"], decided_by=r["decided_by"])
            item["applied_ops"] = [self.applied_result(item["key"], i) for i in range(len(item.get("operations", [])))]
            out.append(item)
        return out

    # ---------------------------------------------------------------- applied writes
    def applied_result(self, key: str, op_index: int) -> Optional[str]:
        row = self._conn.execute(
            "SELECT result_id FROM applied_ops WHERE key = ? AND op_index = ?", (key, op_index)
        ).fetchone()
        return None if row is None else (row["result_id"] or "")

    def mark_applied(self, key: str, op_index: int, result_id: Optional[str]) -> None:
        at = self._now()
        with self._tx() as cur:
            cur.execute("INSERT OR IGNORE INTO applied_ops (key, op_index, result_id, applied_at) VALUES (?, ?, ?, ?)",
                        (key, op_index, result_id, at))
            inserted = cur.rowcount == 1
        if inserted:
            self._append_ledger({"type": "applied", "key": key, "op_index": op_index, "result_id": result_id, "at": at})

    def backup(self, backup_dir: str | Path = "data/backups", keep: int = 14) -> Optional[Path]:
        """Copy the SQLite DB with the online backup API and keep the newest ``keep`` copies."""
        if self.path == ":memory:":
            return None
        backup_dir = Path(backup_dir)
        backup_dir.mkdir(parents=True, exist_ok=True)
        target = backup_dir / f"decisions-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.db"
        dest = sqlite3.connect(target)
        with dest:
            self._conn.backup(dest)
        dest.close()
        for old in sorted(backup_dir.glob("decisions-*.db"))[:-keep]:
            old.unlink()
        return target

    def close(self) -> None:
        self._conn.close()


# --------------------------------------------------------------------------- CLI

def _load_locations(csv_path: Optional[str]) -> list[dict[str, Any]]:
    if csv_path:
        rows = pd.read_csv(csv_path, dtype=str).fillna("").to_dict(orient="records")
        for r in rows:
            r["care_offerings"] = [c.strip() for c in str(r.get("care_offerings", "")).split(";") if c.strip()]
        return rows
    from scraper.bellhaven import scrape_locations
    return [loc.to_dict() for loc in scrape_locations()]


def _load_accounts(csv_path: Optional[str]) -> list[dict[str, Any]]:
    if csv_path:
        return pd.read_csv(csv_path, dtype=str).fillna("").to_dict(orient="records")
    from review_app.crm_client import CrmClient
    return CrmClient.from_env().list_accounts()


def main(argv: Optional[list[str]] = None) -> int:
    load_dotenv()
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    parser = argparse.ArgumentParser(description="Reconcile Bellhaven locations with CRM accounts (read only).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Accepted for clarity. This command never writes to the CRM in any mode.")
    parser.add_argument("--locations-csv", help="Use a saved locations CSV instead of scraping (offline testing).")
    parser.add_argument("--crm-csv", help="Use a CRM export CSV instead of the API (offline testing).")
    parser.add_argument("--db", default=os.getenv("DECISIONS_DB_PATH", "data/decisions.db"),
                        help="SQLite path. Use ':memory:' for a stateless run (CI).")
    parser.add_argument("--ledger", default=os.getenv("DECISIONS_LEDGER_PATH", "decisions/ledger.jsonl"))
    parser.add_argument("--out", default="data/proposals.csv", help="Summary CSV of proposals awaiting review.")
    parser.add_argument("--export-json", help="Also write proposals awaiting review as JSON (used by CI).")
    args = parser.parse_args(argv)

    locations = _load_locations(args.locations_csv)
    accounts = _load_accounts(args.crm_csv)
    logger.info("Loaded %d website locations and %d CRM accounts", len(locations), len(accounts))

    try:
        parent_id = resolve_parent_id(accounts, os.getenv("BELLHAVEN_PARENT_ACCOUNT_ID", "").strip())
    except ValueError as exc:
        logger.error("%s", exc)
        return 2
    logger.info("Bellhaven parent account: %s", parent_id)

    proposals, classes = reconcile(locations, accounts, parent_id)
    store = DecisionStore(args.db, ledger_path=args.ledger)
    try:
        store.backup()
        new = store.upsert_proposals(proposals)
    finally:
        store.close()

    counts = pd.Series(list(classes.values()), dtype=str).value_counts().to_dict()
    logger.info("Classification counts: %s", counts)
    logger.info("%d proposals total, %d awaiting review (decided ones are not re-queued)", len(proposals), len(new))

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame([{"key": p.key, "kind": p.kind.value, "title": p.title, "manual_review": p.requires_manual_review,
                   "evidence": " | ".join(p.evidence)} for p in new],
                 columns=["key", "kind", "title", "manual_review", "evidence"]).to_csv(args.out, index=False)
    if args.export_json:
        Path(args.export_json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.export_json).write_text(json.dumps({"parent_id": parent_id, "counts": counts,
                                                      "proposals": [p.to_dict() for p in new]}, indent=2, default=str))
    for p in new:
        logger.info("PROPOSAL [%s] %s", p.kind.value, p.title)
    logger.info("Wrote %s. Review with: streamlit run review_app/app.py", args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
