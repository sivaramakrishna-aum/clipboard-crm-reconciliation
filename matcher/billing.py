"""Billing / change of ownership (CHOW) rules and duplicate resolution.

These functions are pure: they take account data and return planned changes.
They never talk to the CRM. The review app turns approved plans into API calls.

Rules implemented:
* CHOW: if the account has revenue history (lifetime_revenue > 0) AND
  outstanding_ar > 0, keep the old account untouched, create a new account under
  the correct parent, and point ``chow_current_account`` on the old account at
  the new account ID.
* Otherwise the existing account is re-parented.
* Missing or ambiguous financial values require manual review.
* Duplicates: the losing account gets ``duplicate_of_account`` = survivor ID and
  status ``Inactive``. Nothing is ever deleted.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Optional, Sequence

INACTIVE_STATUS = "Inactive"  # TODO(assessment): confirm the exact status value the CRM expects.

# Placeholder used in a plan until the CRM returns the real ID of a created account.
NEW_ACCOUNT_PLACEHOLDER = "{new_account_id}"


class BillingAction(str, Enum):
    CHOW_NEW_ACCOUNT = "chow_new_account"
    REPARENT = "reparent"
    MANUAL_REVIEW = "manual_review"


class OpType(str, Enum):
    CREATE = "create"
    UPDATE = "update"
    # There is intentionally no DELETE operation.


@dataclass(frozen=True)
class ChangeOp:
    """A single planned CRM write."""

    op: OpType
    account_id: Optional[str]  # None for CREATE
    fields: dict[str, Any]
    note: str = ""


@dataclass(frozen=True)
class BillingDecision:
    action: BillingAction
    reason: str
    operations: tuple[ChangeOp, ...] = field(default_factory=tuple)


def parse_amount(value: Any) -> Optional[Decimal]:
    """Parse a currency-like value.

    Returns None when the value is missing or ambiguous (blank, "N/A", text,
    NaN, several numbers, etc.). Accepts ints, floats, Decimals and strings
    such as "$1,234.50" or "(100.00)" for negatives.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, Decimal)):
        return Decimal(value)
    if isinstance(value, float):
        return None if math.isnan(value) or math.isinf(value) else Decimal(str(value))
    if isinstance(value, str):
        text = value.strip().replace("$", "").replace(",", "").replace(" ", "")
        if not text:
            return None
        negative = text.startswith("(") and text.endswith(")")
        if negative:
            text = text[1:-1]
        try:
            amount = Decimal(text)
        except InvalidOperation:
            return None
        if amount.is_nan() or amount.is_infinite():
            return None
        return -amount if negative else amount
    return None


def decide_parent_change(account: dict[str, Any], correct_parent_id: str,
                         new_account_fields: Optional[dict[str, Any]] = None) -> BillingDecision:
    """Decide how to move ``account`` under ``correct_parent_id``.

    Args:
        account: CRM account dict. Needs ``id``, ``lifetime_revenue``, ``outstanding_ar``.
        correct_parent_id: ID of the parent the location should belong to.
        new_account_fields: fields for the replacement account in the CHOW case
            (name, address, etc.). ``parent_account`` is always set here.
    """
    account_id = str(account.get("id", "")).strip()
    if not account_id:
        return BillingDecision(BillingAction.MANUAL_REVIEW, "Account has no ID.")

    revenue = parse_amount(account.get("lifetime_revenue"))
    ar = parse_amount(account.get("outstanding_ar"))

    if revenue is None or ar is None:
        missing = [n for n, v in (("lifetime_revenue", revenue), ("outstanding_ar", ar)) if v is None]
        return BillingDecision(
            BillingAction.MANUAL_REVIEW,
            f"Financial values missing or ambiguous: {', '.join(missing)} "
            f"(raw lifetime_revenue={account.get('lifetime_revenue')!r}, outstanding_ar={account.get('outstanding_ar')!r}).",
        )
    if revenue < 0 or ar < 0:
        return BillingDecision(
            BillingAction.MANUAL_REVIEW,
            f"Negative financial values need a human check (lifetime_revenue={revenue}, outstanding_ar={ar}).",
        )

    if revenue > 0 and ar > 0:
        create_fields = dict(new_account_fields or {})
        create_fields["parent_account"] = correct_parent_id
        return BillingDecision(
            BillingAction.CHOW_NEW_ACCOUNT,
            f"Revenue history ({revenue}) and open AR ({ar}): preserve old account, create new one under {correct_parent_id}.",
            (
                ChangeOp(OpType.CREATE, None, create_fields, "New account under correct parent (CHOW)."),
                ChangeOp(OpType.UPDATE, account_id, {"chow_current_account": NEW_ACCOUNT_PLACEHOLDER},
                         "Link old account to its replacement; no other fields change."),
            ),
        )

    return BillingDecision(
        BillingAction.REPARENT,
        f"No open AR with revenue history (lifetime_revenue={revenue}, outstanding_ar={ar}): re-parent existing account.",
        (ChangeOp(OpType.UPDATE, account_id, {"parent_account": correct_parent_id}, "Re-parent."),),
    )


def choose_survivor(accounts: Sequence[dict[str, Any]], preferred_parent: Optional[str] = None) -> dict[str, Any]:
    """Pick the account that survives a duplicate group.

    Preference order (documented so reviewers can challenge it):
    1. Accounts not already marked as a duplicate / inactive.
    2. Accounts already under ``preferred_parent`` (the correct parent).
    3. Higher lifetime_revenue (unknown counts as 0), keeping billing history.
    4. Lowest account ID as a deterministic tie breaker.
    """
    if not accounts:
        raise ValueError("choose_survivor needs at least one account")

    def sort_key(acc: dict[str, Any]) -> tuple:
        already_losing = bool(acc.get("duplicate_of_account")) or str(acc.get("status", "")).lower() == INACTIVE_STATUS.lower()
        revenue = parse_amount(acc.get("lifetime_revenue")) or Decimal(0)
        wrong_parent = bool(preferred_parent) and str(acc.get("parent_account") or "") != preferred_parent
        return (already_losing, wrong_parent, -revenue, str(acc.get("id")))

    return sorted(accounts, key=sort_key)[0]


def plan_duplicate_resolution(accounts: Sequence[dict[str, Any]],
                              preferred_parent: Optional[str] = None) -> tuple[Optional[str], list[ChangeOp]]:
    """Plan updates that mark all but one account in a group as duplicates.

    Losing accounts that already point at the survivor and are Inactive are
    skipped, so re-running produces no repeated writes.

    Returns:
        (survivor ID or None when the group has fewer than two accounts, operations)
    """
    if len(accounts) < 2:
        return None, []
    survivor = choose_survivor(accounts, preferred_parent)
    survivor_id = str(survivor["id"])
    ops: list[ChangeOp] = []
    for acc in accounts:
        acc_id = str(acc["id"])
        if acc_id == survivor_id:
            continue
        wanted = {"duplicate_of_account": survivor_id, "status": INACTIVE_STATUS}
        pending = {k: v for k, v in wanted.items() if str(acc.get(k) or "") != v}
        if pending:
            ops.append(ChangeOp(OpType.UPDATE, acc_id, pending, f"Duplicate of {survivor_id}."))
    return survivor_id, ops
