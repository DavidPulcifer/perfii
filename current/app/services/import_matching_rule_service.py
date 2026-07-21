from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
from typing import Any, Callable

from app.repositories import accounts_repo, envelopes_repo
from app.repositories import import_matching_rules_repo
from app.utils import parse_money_to_cents


TEXT_FIELDS = {"payee", "memo", "text"}
TEXT_OPERATORS = {"contains", "equals", "starts_with"}
DIRECTIONS = {"any", "expense", "income"}
ACTION_TYPES = {"", "expense", "income"}
SPLIT_AMOUNT_MODES = {"signed", "absolute"}


@dataclass
class RuleSelection:
    row_index: int
    rules: list[dict[str, Any]]
    actions: dict[str, Any]
    conflict: bool = False
    conflict_reason: str | None = None


def _clean_str(value: Any) -> str:
    return str(value or "").strip()


def _truthy_form_value(value: Any) -> bool:
    return _clean_str(value).lower() not in {"", "0", "false", "no", "off"}


def _normalized_text(value: Any) -> str:
    return " ".join(_clean_str(value).lower().split())


def _amount_cents(row: dict[str, Any]) -> int:
    raw = row.get("amount_cents")
    if raw is None:
        raw = row.get("amount")
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def _direction_for_amount(amount_cents: int) -> str:
    if amount_cents < 0:
        return "expense"
    if amount_cents > 0:
        return "income"
    return "any"


def _field_text(row: dict[str, Any], field: str) -> str:
    payee = _clean_str(row.get("payee") or row.get("name"))
    memo = _clean_str(row.get("memo"))
    if field == "payee":
        return payee
    if field == "memo":
        return memo
    return " ".join(part for part in (payee, memo) if part)


def _rule_matches(row: dict[str, Any], account_id: int | None, rule: dict[str, Any]) -> bool:
    if not int(rule.get("enabled") or 0):
        return False
    rule_account_id = rule.get("account_id")
    if rule_account_id is not None and account_id is not None and int(rule_account_id) != int(account_id):
        return False

    condition = rule.get("condition_json") or {}
    amount_cents = _amount_cents(row)
    direction = _direction_for_amount(amount_cents)
    wanted_direction = _clean_str(condition.get("direction") or "any").lower()
    if wanted_direction not in DIRECTIONS:
        wanted_direction = "any"
    if wanted_direction != "any" and wanted_direction != direction:
        return False

    min_cents = condition.get("amount_min_cents")
    max_cents = condition.get("amount_max_cents")
    abs_amount = abs(amount_cents)
    try:
        if min_cents is not None and abs_amount < int(min_cents):
            return False
        if max_cents is not None and abs_amount > int(max_cents):
            return False
    except (TypeError, ValueError):
        return False

    value = _normalized_text(condition.get("value"))
    if value:
        field = _clean_str(condition.get("field") or "text").lower()
        operator = _clean_str(condition.get("operator") or "contains").lower()
        if field not in TEXT_FIELDS:
            field = "text"
        if operator not in TEXT_OPERATORS:
            operator = "contains"
        target = _normalized_text(_field_text(row, field))
        if operator == "equals":
            return target == value
        if operator == "starts_with":
            return target.startswith(value)
        return value in target

    return bool(min_cents is not None or max_cents is not None or wanted_direction != "any")


def _int_or_none(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed else None


def _signed_amount_for_type(amount_cents: int, transaction_type: str) -> int:
    amount = abs(int(amount_cents))
    return -amount if transaction_type == "expense" else amount


def _clean_split_remainder_action(action: Any) -> dict[str, Any]:
    if not isinstance(action, dict):
        return {}
    transaction_type = _clean_str(action.get("transaction_type")).lower()
    if transaction_type not in {"expense", "income"}:
        return {}

    splits: list[dict[str, Any]] = []
    seen_envelope_ids: set[int] = set()
    expected_sign = -1 if transaction_type == "expense" else 1
    for item in action.get("splits") or []:
        if not isinstance(item, dict):
            return {}
        envelope_id = _int_or_none(item.get("envelope_id"))
        amount_mode = _clean_str(item.get("amount_mode") or "signed").lower()
        try:
            amount_cents = int(item.get("amount_cents") or 0)
        except (TypeError, ValueError):
            return {}
        if not envelope_id or amount_mode not in SPLIT_AMOUNT_MODES or amount_cents == 0:
            return {}
        if amount_mode == "absolute":
            amount_cents = _signed_amount_for_type(amount_cents, transaction_type)
        if (int(amount_cents) < 0) != (expected_sign < 0):
            return {}
        if envelope_id in seen_envelope_ids:
            return {}
        seen_envelope_ids.add(envelope_id)
        splits.append({
            "envelope_id": envelope_id,
            "amount_cents": amount_cents,
            "amount_mode": amount_mode,
        })

    remainder_envelope_id = _int_or_none(action.get("remainder_envelope_id"))
    if remainder_envelope_id and remainder_envelope_id in seen_envelope_ids:
        return {}
    if not splits and not remainder_envelope_id:
        return {}
    output: dict[str, Any] = {
        "transaction_type": transaction_type,
        "splits": splits,
    }
    if remainder_envelope_id:
        output["remainder_envelope_id"] = remainder_envelope_id
    target_amount_cents = _int_or_none(action.get("target_amount_cents") or action.get("total_amount_cents"))
    if target_amount_cents:
        normalized_target = _signed_amount_for_type(target_amount_cents, transaction_type)
        fixed_total = sum(int(split["amount_cents"]) for split in splits)
        remainder_delta = normalized_target - fixed_total
        if remainder_envelope_id:
            if remainder_delta == 0 or (remainder_delta < 0) != (expected_sign < 0):
                return {}
        elif fixed_total != normalized_target:
            return {}
        output["target_amount_cents"] = normalized_target
    return output


def _clean_transfer_leg_splits(splits_value: Any) -> list[dict[str, Any]] | None:
    splits: list[dict[str, Any]] = []
    seen_envelope_ids: set[int] = set()
    for item in splits_value or []:
        if not isinstance(item, dict):
            return None
        envelope_id = _int_or_none(item.get("envelope_id"))
        try:
            amount_cents = abs(int(item.get("amount_cents") or 0))
        except (TypeError, ValueError):
            return None
        if not envelope_id or amount_cents == 0 or envelope_id in seen_envelope_ids:
            return None
        seen_envelope_ids.add(envelope_id)
        splits.append({"envelope_id": envelope_id, "amount_cents": amount_cents})
    return splits


def _clean_transfer_action(action: Any) -> dict[str, Any]:
    if not isinstance(action, dict):
        return {}
    transaction_type = _clean_str(action.get("transaction_type")).lower()
    if transaction_type not in {"expense", "income"}:
        return {}
    other_account_id = _int_or_none(action.get("other_account_id"))
    if not other_account_id:
        return {}
    current_splits = _clean_transfer_leg_splits(action.get("current_account_splits"))
    other_splits = _clean_transfer_leg_splits(action.get("other_account_splits"))
    if current_splits is None or other_splits is None:
        return {}

    current_remainder_id = _int_or_none(action.get("current_account_remainder_envelope_id"))
    other_remainder_id = _int_or_none(action.get("other_account_remainder_envelope_id"))
    current_ids = {split["envelope_id"] for split in current_splits}
    other_ids = {split["envelope_id"] for split in other_splits}
    if current_remainder_id and current_remainder_id in current_ids:
        return {}
    if other_remainder_id and other_remainder_id in other_ids:
        return {}
    if not current_splits and not current_remainder_id:
        return {}
    if not other_splits and not other_remainder_id:
        return {}

    output: dict[str, Any] = {
        "transaction_type": transaction_type,
        "other_account_id": other_account_id,
        "current_account_splits": current_splits,
        "other_account_splits": other_splits,
    }
    if current_remainder_id:
        output["current_account_remainder_envelope_id"] = current_remainder_id
    if other_remainder_id:
        output["other_account_remainder_envelope_id"] = other_remainder_id
    target_amount_cents = _int_or_none(action.get("target_amount_cents") or action.get("total_amount_cents"))
    if target_amount_cents:
        output["target_amount_cents"] = _signed_amount_for_type(target_amount_cents, transaction_type)
    return output


def _clean_manual_match_action(action: Any) -> dict[str, Any]:
    if not isinstance(action, dict):
        return {}
    transaction_id = _int_or_none(action.get("transaction_id") or action.get("tx_id") or action.get("manual_transaction_id"))
    return {"transaction_id": transaction_id} if transaction_id else {}


def _clean_action(action: dict[str, Any] | None) -> dict[str, Any]:
    action = dict(action or {})
    output: dict[str, Any] = {}
    payee = _clean_str(action.get("payee"))
    if payee:
        output["payee"] = payee
    if "memo" in action:
        memo = _clean_str(action.get("memo"))
        if memo:
            output["memo"] = memo
    transaction_type = _clean_str(action.get("transaction_type")).lower()
    if transaction_type in {"expense", "income"}:
        output["transaction_type"] = transaction_type
    try:
        envelope_id = int(action.get("single_envelope_id") or 0)
    except (TypeError, ValueError):
        envelope_id = 0
    if envelope_id:
        output["single_envelope_id"] = envelope_id
    split_remainder = _clean_split_remainder_action(action.get("split_remainder"))
    if split_remainder:
        output["split_remainder"] = split_remainder
    transfer = _clean_transfer_action(action.get("transfer"))
    if transfer:
        output["transfer"] = transfer
    manual_match = _clean_manual_match_action(action.get("manual_match"))
    if manual_match:
        output["manual_match"] = manual_match
    return output


def _envelope_lookup(list_envelopes_func: Callable[..., list[dict[str, Any]]] | None) -> dict[int, dict[str, Any]]:
    func = list_envelopes_func or envelopes_repo.list_envelopes
    try:
        rows = func(include_archived=True)
    except TypeError:
        rows = func()
    lookup: dict[int, dict[str, Any]] = {}
    for row in rows or []:
        envelope_id = _int_or_none(row.get("id"))
        if envelope_id:
            lookup[envelope_id] = row
    return lookup


def _account_lookup(list_accounts_func: Callable[..., list[dict[str, Any]]] | None = None) -> dict[int, dict[str, Any]]:
    func = list_accounts_func or accounts_repo.list_accounts
    rows = func()
    lookup: dict[int, dict[str, Any]] = {}
    for row in rows or []:
        account_id = _int_or_none(row.get("id"))
        if account_id:
            lookup[account_id] = row
    return lookup


def _envelope_allowed(envelope: dict[str, Any] | None, account_id: int | None) -> bool:
    if not envelope or envelope.get("archived_at") or envelope.get("deleted_at"):
        return False
    locked_account_id = _int_or_none(envelope.get("locked_account_id"))
    if locked_account_id is None:
        return True
    return account_id is not None and locked_account_id == int(account_id)


def _transfer_withheld(row_index: int, selection: RuleSelection, reason: str) -> dict[str, Any]:
    debug = _debug_payload(selection, decision="withheld")
    debug["reason_codes"] = [reason]
    return {
        "row_index": row_index,
        "prefill": False,
        "debug_reason_codes": [reason],
        "prediction_debug": debug,
    }


def _transfer_leg_balance(
    *,
    splits: list[dict[str, Any]],
    remainder_envelope_id: int | None,
    target_abs_cents: int,
) -> tuple[bool, int]:
    fixed_total = sum(abs(int(split.get("amount_cents") or 0)) for split in splits)
    remainder_amount = target_abs_cents - fixed_total
    if remainder_envelope_id:
        return remainder_amount > 0, remainder_amount
    return fixed_total == target_abs_cents, 0


def _transfer_prefill(
    row: dict[str, Any],
    selection: RuleSelection,
    account_id: int | None,
    account_lookup: dict[int, dict[str, Any]],
    envelope_lookup: dict[int, dict[str, Any]],
) -> dict[str, Any] | None:
    action = selection.actions.get("transfer")
    if not isinstance(action, dict):
        return None
    row_index = int(selection.row_index)
    amount_cents = _amount_cents(row)
    if amount_cents == 0:
        return _transfer_withheld(row_index, selection, "manual_rule_transfer_zero_amount")
    transaction_type = _direction_for_amount(amount_cents)
    if action.get("transaction_type") != transaction_type:
        return _transfer_withheld(row_index, selection, "manual_rule_transfer_type_mismatch")
    other_account_id = _int_or_none(action.get("other_account_id"))
    if not account_id or not other_account_id or other_account_id == int(account_id) or other_account_id not in account_lookup:
        return _transfer_withheld(row_index, selection, "manual_rule_transfer_invalid_account")
    target_amount = _int_or_none(action.get("target_amount_cents"))
    if target_amount and target_amount != amount_cents:
        return _transfer_withheld(row_index, selection, "manual_rule_transfer_amount_mismatch")

    current_splits = list(action.get("current_account_splits") or [])
    other_splits = list(action.get("other_account_splits") or [])
    current_remainder_id = _int_or_none(action.get("current_account_remainder_envelope_id"))
    other_remainder_id = _int_or_none(action.get("other_account_remainder_envelope_id"))
    for split in current_splits:
        if not _envelope_allowed(envelope_lookup.get(int(split.get("envelope_id") or 0)), account_id):
            return _transfer_withheld(row_index, selection, "manual_rule_transfer_unavailable_current_envelope")
    for split in other_splits:
        if not _envelope_allowed(envelope_lookup.get(int(split.get("envelope_id") or 0)), other_account_id):
            return _transfer_withheld(row_index, selection, "manual_rule_transfer_unavailable_other_envelope")
    if current_remainder_id and not _envelope_allowed(envelope_lookup.get(current_remainder_id), account_id):
        return _transfer_withheld(row_index, selection, "manual_rule_transfer_unavailable_current_envelope")
    if other_remainder_id and not _envelope_allowed(envelope_lookup.get(other_remainder_id), other_account_id):
        return _transfer_withheld(row_index, selection, "manual_rule_transfer_unavailable_other_envelope")

    target_abs = abs(amount_cents)
    current_ok, current_remainder_amount = _transfer_leg_balance(
        splits=current_splits,
        remainder_envelope_id=current_remainder_id,
        target_abs_cents=target_abs,
    )
    if not current_ok:
        return _transfer_withheld(row_index, selection, "manual_rule_transfer_current_unbalanced")
    other_ok, other_remainder_amount = _transfer_leg_balance(
        splits=other_splits,
        remainder_envelope_id=other_remainder_id,
        target_abs_cents=target_abs,
    )
    if not other_ok:
        return _transfer_withheld(row_index, selection, "manual_rule_transfer_other_unbalanced")

    rule_ids = [int(rule.get("id") or 0) for rule in selection.rules if rule.get("id")]
    transfer = {
        "other_account_id": other_account_id,
        "other_account_name": _clean_str(account_lookup.get(other_account_id, {}).get("name")),
        "current_account_splits": current_splits,
        "other_account_splits": other_splits,
    }
    if current_remainder_id:
        transfer["current_account_remainder_envelope_id"] = current_remainder_id
        transfer["current_account_remainder_amount_cents"] = current_remainder_amount
    if other_remainder_id:
        transfer["other_account_remainder_envelope_id"] = other_remainder_id
        transfer["other_account_remainder_amount_cents"] = other_remainder_amount
    return {
        "row_index": row_index,
        "prefill": True,
        "prediction_type": "manual_rule",
        "prediction_id": _prediction_id(account_id, row_index, rule_ids),
        "transaction_type": "transfer_out" if amount_cents < 0 else "transfer_in",
        "transfer": transfer,
        "debug_reason_codes": ["manual_rule_match"],
        "prediction_debug": _debug_payload(selection, decision="prefill"),
    }


def _manual_match_withheld(row_index: int, selection: RuleSelection, reason: str) -> dict[str, Any]:
    debug = _debug_payload(selection, decision="withheld")
    debug["reason_codes"] = [reason]
    return {
        "row_index": row_index,
        "prefill": False,
        "debug_reason_codes": [reason],
        "prediction_debug": debug,
    }


def _date_distance_days(left: Any, right: Any) -> int | None:
    try:
        left_date = datetime.strptime(str(left or "").strip(), "%Y-%m-%d").date()
        right_date = datetime.strptime(str(right or "").strip(), "%Y-%m-%d").date()
    except Exception:
        return None
    return abs((left_date - right_date).days)


def _manual_match_prefill(
    row: dict[str, Any],
    selection: RuleSelection,
    account_id: int | None,
    *,
    get_transaction_func: Callable[[int], dict[str, Any] | None] | None = None,
    existing_fitids: set[str] | None = None,
    row_states_by_index: dict[int, dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    action = selection.actions.get("manual_match")
    if not isinstance(action, dict):
        return None
    row_index = int(selection.row_index)
    state = (row_states_by_index or {}).get(row_index, {})
    if state and not state.get("manual_match_eligible", True):
        return _manual_match_withheld(row_index, selection, "manual_rule_manual_match_row_ineligible")
    transaction_id = _int_or_none(action.get("transaction_id"))
    if not transaction_id:
        return _manual_match_withheld(row_index, selection, "manual_rule_manual_match_missing_target")
    if not get_transaction_func:
        return _manual_match_withheld(row_index, selection, "manual_rule_manual_match_unvalidated")
    target = get_transaction_func(transaction_id)
    if not target:
        return _manual_match_withheld(row_index, selection, "manual_rule_manual_match_missing_target")
    if int(target.get("account_id") or 0) != int(account_id or 0):
        return _manual_match_withheld(row_index, selection, "manual_rule_manual_match_wrong_account")
    if target.get("ignore_match"):
        return _manual_match_withheld(row_index, selection, "manual_rule_manual_match_ignored_target")
    row_amount = _amount_cents(row)
    target_amount = int(target.get("amount_cents") or 0)
    if row_amount == 0 or row_amount != target_amount:
        return _manual_match_withheld(row_index, selection, "manual_rule_manual_match_amount_mismatch")
    distance = _date_distance_days(row.get("posted_at"), target.get("posted_at"))
    if distance is None or distance > 7:
        return _manual_match_withheld(row_index, selection, "manual_rule_manual_match_date_mismatch")
    target_fitid = _clean_str(target.get("fitid"))
    if target_fitid and target_fitid in (existing_fitids or set()):
        return _manual_match_withheld(row_index, selection, "manual_rule_manual_match_duplicate_fitid")

    rule_ids = [int(rule.get("id") or 0) for rule in selection.rules if rule.get("id")]
    return {
        "row_index": row_index,
        "prefill": True,
        "prediction_type": "manual_rule",
        "prediction_id": _prediction_id(account_id, row_index, rule_ids),
        "manual_match": {"transaction_id": transaction_id},
        "debug_reason_codes": ["manual_rule_match"],
        "prediction_debug": _debug_payload(selection, decision="prefill"),
    }


def _split_remainder_withheld(row_index: int, selection: RuleSelection, reason: str) -> dict[str, Any]:
    debug = _debug_payload(selection, decision="withheld")
    debug["reason_codes"] = [reason]
    return {
        "row_index": row_index,
        "prefill": False,
        "debug_reason_codes": [reason],
        "prediction_debug": debug,
    }


def _split_remainder_prefill(
    row: dict[str, Any],
    selection: RuleSelection,
    account_id: int | None,
    envelope_lookup: dict[int, dict[str, Any]],
) -> dict[str, Any] | None:
    action = selection.actions.get("split_remainder")
    if not isinstance(action, dict):
        return None
    row_index = int(selection.row_index)
    amount_cents = _amount_cents(row)
    if amount_cents == 0:
        return _split_remainder_withheld(row_index, selection, "manual_rule_split_remainder_zero_amount")
    transaction_type = _direction_for_amount(amount_cents)
    if action.get("transaction_type") != transaction_type:
        return _split_remainder_withheld(row_index, selection, "manual_rule_split_remainder_type_mismatch")

    splits = []
    envelope_ids: list[int] = []
    expected_sign = -1 if transaction_type == "expense" else 1
    for split in action.get("splits") or []:
        try:
            envelope_id = int(split.get("envelope_id") or 0)
            split_amount = int(split.get("amount_cents") or 0)
        except (TypeError, ValueError):
            return _split_remainder_withheld(row_index, selection, "manual_rule_split_remainder_invalid_split")
        if not envelope_id or split_amount == 0 or (split_amount < 0) != (expected_sign < 0):
            return _split_remainder_withheld(row_index, selection, "manual_rule_split_remainder_invalid_split")
        envelope_ids.append(envelope_id)
        splits.append({"envelope_id": envelope_id, "amount_cents": split_amount})

    remainder_envelope_id = _int_or_none(action.get("remainder_envelope_id"))
    if remainder_envelope_id:
        envelope_ids.append(remainder_envelope_id)
    if len(envelope_ids) != len(set(envelope_ids)):
        return _split_remainder_withheld(row_index, selection, "manual_rule_split_remainder_duplicate_envelope")
    for envelope_id in envelope_ids:
        if not _envelope_allowed(envelope_lookup.get(envelope_id), account_id):
            return _split_remainder_withheld(row_index, selection, "manual_rule_split_remainder_unavailable_envelope")

    target_amount = _int_or_none(action.get("target_amount_cents"))
    if target_amount and target_amount != amount_cents:
        return _split_remainder_withheld(row_index, selection, "manual_rule_split_remainder_amount_mismatch")
    fixed_total = sum(split["amount_cents"] for split in splits)
    remainder_amount = amount_cents - fixed_total
    if remainder_envelope_id:
        if remainder_amount == 0 or (remainder_amount < 0) != (expected_sign < 0):
            return _split_remainder_withheld(row_index, selection, "manual_rule_split_remainder_invalid_remainder")
    elif fixed_total != amount_cents:
        return _split_remainder_withheld(row_index, selection, "manual_rule_split_remainder_unbalanced")

    rule_ids = [int(rule.get("id") or 0) for rule in selection.rules if rule.get("id")]
    prefill = {
        "row_index": row_index,
        "prefill": True,
        "prediction_type": "manual_rule",
        "prediction_id": _prediction_id(account_id, row_index, rule_ids),
        "transaction_type": transaction_type,
        "splits": splits,
        "debug_reason_codes": ["manual_rule_match"],
        "prediction_debug": _debug_payload(selection, decision="prefill"),
    }
    if remainder_envelope_id:
        prefill["remainder_envelope_id"] = remainder_envelope_id
        prefill["remainder_amount_cents"] = remainder_amount
    return prefill


def _parse_split_remainder_payload(
    raw_payload: Any,
    *,
    account_id: int | None,
    action: dict[str, Any],
    errors: list[str],
    list_envelopes_func: Callable[..., list[dict[str, Any]]] | None = None,
) -> None:
    raw_payload = _clean_str(raw_payload)
    if not raw_payload:
        return
    try:
        payload = json.loads(raw_payload)
    except json.JSONDecodeError:
        errors.append("Split/remainder action payload must be valid JSON.")
        return
    split_action = _clean_split_remainder_action(payload)
    if not split_action:
        errors.append("Split/remainder action payload is incomplete or unsupported.")
        return
    if action.get("single_envelope_id"):
        errors.append("Choose either a single-envelope action or a split/remainder action.")
        return
    if action.get("transaction_type") and action.get("transaction_type") != split_action["transaction_type"]:
        errors.append("Split/remainder action type conflicts with the rule transaction type action.")
        return

    envelope_ids = [int(split["envelope_id"]) for split in split_action["splits"]]
    remainder_envelope_id = split_action.get("remainder_envelope_id")
    if remainder_envelope_id:
        envelope_ids.append(int(remainder_envelope_id))
    if len(envelope_ids) != len(set(envelope_ids)):
        errors.append("Split/remainder action cannot use the same envelope more than once.")
        return

    lookup = _envelope_lookup(list_envelopes_func)
    invalid_ids = [
        envelope_id
        for envelope_id in envelope_ids
        if not _envelope_allowed(lookup.get(envelope_id), account_id)
    ]
    if invalid_ids:
        errors.append("Split/remainder action references an unavailable envelope.")
        return

    expected_sign = -1 if split_action["transaction_type"] == "expense" else 1
    for split in split_action["splits"]:
        if (int(split["amount_cents"]) < 0) != (expected_sign < 0):
            errors.append("Split/remainder action amounts must match the transaction type sign.")
            return

    total = split_action.get("target_amount_cents")
    if total:
        if (int(total) < 0) != (expected_sign < 0):
            errors.append("Split/remainder action target amount must match the transaction type sign.")
            return
        fixed_total = sum(int(split["amount_cents"]) for split in split_action["splits"])
        remainder_delta = int(total) - fixed_total
        if split_action.get("remainder_envelope_id"):
            if remainder_delta == 0 or (remainder_delta < 0) != (expected_sign < 0):
                errors.append("Split/remainder action remainder would be zero or signed incorrectly.")
                return
        elif fixed_total != int(total):
            errors.append("Split/remainder action fixed amounts must balance without a remainder envelope.")
            return

    action["split_remainder"] = split_action


def _parse_transfer_payload(
    raw_payload: Any,
    *,
    account_id: int | None,
    action: dict[str, Any],
    errors: list[str],
    list_envelopes_func: Callable[..., list[dict[str, Any]]] | None = None,
    list_accounts_func: Callable[..., list[dict[str, Any]]] | None = None,
) -> None:
    raw_payload = _clean_str(raw_payload)
    if not raw_payload:
        return
    try:
        payload = json.loads(raw_payload)
    except json.JSONDecodeError:
        errors.append("Transfer action payload must be valid JSON.")
        return
    transfer = _clean_transfer_action(payload)
    if not transfer:
        errors.append("Transfer action payload is incomplete or unsupported.")
        return
    if action.get("single_envelope_id") or action.get("split_remainder"):
        errors.append("Choose either a simple envelope/split action or a transfer action.")
        return
    if action.get("transaction_type") and action.get("transaction_type") != transfer["transaction_type"]:
        errors.append("Transfer action type conflicts with the rule transaction type action.")
        return
    other_account_id = int(transfer["other_account_id"])
    account_lookup = _account_lookup(list_accounts_func)
    if account_id is None or other_account_id == int(account_id) or other_account_id not in account_lookup:
        errors.append("Transfer action references an unavailable account.")
        return

    envelope_lookup = _envelope_lookup(list_envelopes_func)
    current_ids = [int(split["envelope_id"]) for split in transfer["current_account_splits"]]
    other_ids = [int(split["envelope_id"]) for split in transfer["other_account_splits"]]
    current_remainder_id = _int_or_none(transfer.get("current_account_remainder_envelope_id"))
    other_remainder_id = _int_or_none(transfer.get("other_account_remainder_envelope_id"))
    if current_remainder_id:
        current_ids.append(current_remainder_id)
    if other_remainder_id:
        other_ids.append(other_remainder_id)
    if any(not _envelope_allowed(envelope_lookup.get(envelope_id), account_id) for envelope_id in current_ids):
        errors.append("Transfer action references an unavailable current-account envelope.")
        return
    if any(not _envelope_allowed(envelope_lookup.get(envelope_id), other_account_id) for envelope_id in other_ids):
        errors.append("Transfer action references an unavailable other-account envelope.")
        return
    target_abs = abs(int(transfer.get("target_amount_cents") or 0))
    if target_abs:
        current_ok, _ = _transfer_leg_balance(
            splits=transfer["current_account_splits"],
            remainder_envelope_id=current_remainder_id,
            target_abs_cents=target_abs,
        )
        other_ok, _ = _transfer_leg_balance(
            splits=transfer["other_account_splits"],
            remainder_envelope_id=other_remainder_id,
            target_abs_cents=target_abs,
        )
        if not current_ok or not other_ok:
            errors.append("Transfer action split plans must balance to the rule amount.")
            return
    action["transfer"] = transfer


def _parse_manual_match_payload(raw_payload: Any, *, action: dict[str, Any], errors: list[str]) -> None:
    raw_payload = _clean_str(raw_payload)
    if not raw_payload:
        return
    try:
        payload = json.loads(raw_payload)
    except json.JSONDecodeError:
        errors.append("Manual-match action payload must be valid JSON.")
        return
    manual_match = _clean_manual_match_action(payload)
    if not manual_match:
        errors.append("Manual-match action payload is incomplete or unsupported.")
        return
    if action.get("single_envelope_id") or action.get("split_remainder") or action.get("transfer"):
        errors.append("Choose either an assignment/transfer action or a manual-match action.")
        return
    action["manual_match"] = manual_match


def _merged_selection(row_index: int, matches: list[dict[str, Any]]) -> RuleSelection:
    actions: dict[str, Any] = {}
    selected: list[dict[str, Any]] = []
    for rule in matches:
        rule_actions = _clean_action(rule.get("action_json") or {})
        if not rule_actions:
            continue
        for key, value in rule_actions.items():
            if key in actions and actions[key] != value:
                return RuleSelection(
                    row_index=row_index,
                    rules=selected + [rule],
                    actions={},
                    conflict=True,
                    conflict_reason=f"conflicting_{key}",
                )
        actions.update(rule_actions)
        selected.append(rule)
    return RuleSelection(row_index=row_index, rules=selected, actions=actions)


def select_import_matching_rules(
    row: dict[str, Any],
    row_index: int,
    account_id: int | None,
    rules: list[dict[str, Any]],
) -> RuleSelection:
    matches = [
        rule for rule in sorted(rules or [], key=lambda item: (int(item.get("priority") or 100), int(item.get("id") or 0)))
        if _rule_matches(row, account_id, rule)
    ]
    return _merged_selection(int(row_index), matches)


def _prediction_id(account_id: int | None, row_index: int, rule_ids: list[int]) -> str:
    digest = hashlib.sha1(",".join(str(rule_id) for rule_id in rule_ids).encode("utf-8")).hexdigest()[:12]
    return f"manual-rule:{account_id or 0}:{row_index}:{digest}"


def _debug_payload(selection: RuleSelection, *, decision: str) -> dict[str, Any]:
    rule_ids = [int(rule.get("id") or 0) for rule in selection.rules if rule.get("id")]
    return {
        "engine": "manual_rule",
        "decision": decision,
        "prediction_type": "manual_rule",
        "reason_codes": [selection.conflict_reason or "manual_rule_match"],
        "confidence": "explicit" if not selection.conflict else "none",
        "evidence": {
            "rule_ids": rule_ids,
            "rule_names": [_clean_str(rule.get("name")) for rule in selection.rules],
        },
    }


def build_import_matching_rule_prefills(
    transactions: list[dict[str, Any]],
    account_id: int | None,
    *,
    list_rules_func: Callable[..., list[dict[str, Any]]] = import_matching_rules_repo.list_import_matching_rules,
    list_envelopes_func: Callable[..., list[dict[str, Any]]] | None = None,
    list_accounts_func: Callable[..., list[dict[str, Any]]] | None = None,
    get_transaction_func: Callable[[int], dict[str, Any] | None] | None = None,
    existing_fitids: set[str] | None = None,
    row_states: list[dict[str, Any]] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    if account_id is None:
        return {"import_prefills": [], "payee_prefills": []}

    rules = list_rules_func(account_id=account_id, include_disabled=False)
    envelope_lookup: dict[int, dict[str, Any]] | None = None
    account_lookup: dict[int, dict[str, Any]] | None = None
    import_prefills: list[dict[str, Any]] = []
    payee_prefills: list[dict[str, Any]] = []
    row_states_by_index = {
        int(state.get("row_index")): state
        for state in row_states or []
        if state.get("row_index") is not None
    }

    for row_index, row in enumerate(transactions or []):
        selection = select_import_matching_rules(row, row_index, account_id, rules)
        if selection.conflict:
            import_prefills.append({
                "row_index": row_index,
                "prefill": False,
                "debug_reason_codes": [selection.conflict_reason or "manual_rule_conflict"],
                "prediction_debug": _debug_payload(selection, decision="withheld"),
            })
            continue
        if not selection.actions:
            continue

        if "transfer" in selection.actions:
            if envelope_lookup is None:
                envelope_lookup = _envelope_lookup(list_envelopes_func)
            if account_lookup is None:
                account_lookup = _account_lookup(list_accounts_func)
            transfer_prefill = _transfer_prefill(row, selection, account_id, account_lookup, envelope_lookup)
            if transfer_prefill is not None:
                import_prefills.append(transfer_prefill)
                if not transfer_prefill.get("prefill"):
                    continue
        else:
            transfer_prefill = None

        if "manual_match" in selection.actions:
            manual_match_prefill = _manual_match_prefill(
                row,
                selection,
                account_id,
                get_transaction_func=get_transaction_func,
                existing_fitids=existing_fitids,
                row_states_by_index=row_states_by_index,
            )
            if manual_match_prefill is not None:
                import_prefills.append(manual_match_prefill)
                if not manual_match_prefill.get("prefill"):
                    continue
        else:
            manual_match_prefill = None

        if "split_remainder" in selection.actions and envelope_lookup is None:
            envelope_lookup = _envelope_lookup(list_envelopes_func)
        split_prefill = _split_remainder_prefill(row, selection, account_id, envelope_lookup or {})
        if split_prefill is not None:
            import_prefills.append(split_prefill)
            if not split_prefill.get("prefill"):
                continue

        rule_ids = [int(rule.get("id") or 0) for rule in selection.rules if rule.get("id")]
        debug = _debug_payload(selection, decision="prefill")
        if (
            split_prefill is None
            and transfer_prefill is None
            and manual_match_prefill is None
            and ("transaction_type" in selection.actions or "single_envelope_id" in selection.actions)
        ):
            import_prefills.append({
                "row_index": row_index,
                "prefill": True,
                "prediction_type": "manual_rule",
                "prediction_id": _prediction_id(account_id, row_index, rule_ids),
                "transaction_type": selection.actions.get("transaction_type") or _direction_for_amount(_amount_cents(row)),
                "single_envelope_id": selection.actions.get("single_envelope_id"),
                "debug_reason_codes": ["manual_rule_match"],
                "prediction_debug": debug,
            })
        if "payee" in selection.actions or "memo" in selection.actions:
            payee_prefills.append({
                "row_index": row_index,
                "payee_prefill": "payee" in selection.actions,
                "memo_prefill": "memo" in selection.actions,
                "canonical_payee": selection.actions.get("payee") or "",
                "canonical_memo": selection.actions.get("memo") or "",
                "rule_ids": rule_ids,
                "prediction_debug": debug,
            })
    return {"import_prefills": import_prefills, "payee_prefills": payee_prefills}


def parse_rule_form(
    form,
    *,
    list_envelopes_func: Callable[..., list[dict[str, Any]]] | None = None,
    list_accounts_func: Callable[..., list[dict[str, Any]]] | None = None,
) -> tuple[dict[str, Any] | None, list[str]]:
    errors: list[str] = []
    name = _clean_str(form.get("name"))
    if not name:
        errors.append("Rule name is required.")

    account_id = form.get("account_id", type=int)
    account_scope = _clean_str(form.get("account_scope") or "account")
    if account_scope == "global":
        account_id = None
    elif not account_id:
        errors.append("Choose an account or make the rule global.")

    direction = _clean_str(form.get("direction") or "any").lower()
    if direction not in DIRECTIONS:
        direction = "any"
    field = _clean_str(form.get("match_field") or "text").lower()
    if field not in TEXT_FIELDS:
        field = "text"
    operator = _clean_str(form.get("match_operator") or "contains").lower()
    if operator not in TEXT_OPERATORS:
        operator = "contains"
    value = _clean_str(form.get("match_value"))

    raw_amount_min = _clean_str(form.get("amount_min"))
    raw_amount_max = _clean_str(form.get("amount_max"))
    amount_min = parse_money_to_cents(raw_amount_min) if raw_amount_min else None
    amount_max = parse_money_to_cents(raw_amount_max) if raw_amount_max else None
    condition: dict[str, Any] = {"direction": direction}
    if value:
        condition.update({"field": field, "operator": operator, "value": value})
    if amount_min is not None:
        condition["amount_min_cents"] = abs(int(amount_min))
    if amount_max is not None:
        condition["amount_max_cents"] = abs(int(amount_max))
    if (
        condition.get("direction") == "any"
        and not condition.get("value")
        and "amount_min_cents" not in condition
        and "amount_max_cents" not in condition
    ):
        errors.append("Add at least one match condition.")

    action: dict[str, Any] = {}
    payee = _clean_str(form.get("action_payee"))
    memo = _clean_str(form.get("action_memo"))
    transaction_type = _clean_str(form.get("action_transaction_type")).lower()
    envelope_id = form.get("action_envelope_id", type=int)
    if payee:
        action["payee"] = payee
    if memo:
        action["memo"] = memo
    if transaction_type in {"expense", "income"}:
        action["transaction_type"] = transaction_type
    if envelope_id:
        action["single_envelope_id"] = int(envelope_id)
    _parse_split_remainder_payload(
        form.get("action_split_remainder_json"),
        account_id=account_id,
        action=action,
        errors=errors,
        list_envelopes_func=list_envelopes_func,
    )
    _parse_transfer_payload(
        form.get("action_transfer_json"),
        account_id=account_id,
        action=action,
        errors=errors,
        list_envelopes_func=list_envelopes_func,
        list_accounts_func=list_accounts_func,
    )
    _parse_manual_match_payload(
        form.get("action_manual_match_json"),
        action=action,
        errors=errors,
    )
    if not action:
        errors.append("Add at least one rule action.")

    data = {
        "account_id": account_id,
        "name": name,
        "enabled": _truthy_form_value(form.get("enabled")),
        "priority": form.get("priority", type=int) or 100,
        "condition_json": condition,
        "action_json": action,
    }
    return (None if errors else data), errors
