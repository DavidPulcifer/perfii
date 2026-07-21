from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Callable, Iterable

from app.db import get_db, table_exists
from app.repositories import payee_normalization_repo
from app.services.import_prefill_service import prediction_debug_payload
from app.services.transaction_text_profile_service import (
    build_transaction_text_profile_from_row,
    merchant_cluster_signature,
)


@dataclass(frozen=True)
class _ProfileRuleSelection:
    rule: dict[str, Any] | None = None
    withheld_reason: str | None = None
    evidence: dict[str, Any] | None = None


def normalize_import_identity_part(value: Any) -> str:
    """Conservative key for exact-ish imported payee/memo identity.

    Keep digits because bank-provided account/card/vendor metadata can be the
    useful distinction. Normalize case, punctuation, and whitespace only.
    """
    text = str(value or "").strip().lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def import_identity_keys(payee: Any, memo: Any = None) -> tuple[str, str]:
    return normalize_import_identity_part(payee), normalize_import_identity_part(memo)


def payee_differs_meaningfully(raw_payee: Any, canonical_payee: Any) -> bool:
    raw_key = normalize_import_identity_part(raw_payee)
    canonical_key = normalize_import_identity_part(canonical_payee)
    return bool(raw_key and canonical_key and raw_key != canonical_key)


def cleanup_part_differs_meaningfully(raw_value: Any, canonical_value: Any) -> bool:
    raw_key = normalize_import_identity_part(raw_value)
    canonical_key = normalize_import_identity_part(canonical_value)
    return bool(raw_key != canonical_key and (raw_key or canonical_key))


def build_payee_normalization_prefills(
    transactions: list[dict[str, Any]],
    account_id: int | None,
    *,
    list_rules_func: Callable[..., list[dict[str, Any]]] = payee_normalization_repo.list_payee_normalization_rules,
    list_learning_examples_func: Callable[..., list[dict[str, Any]]] | None = None,
) -> list[dict[str, Any]]:
    if account_id is None:
        return []
    if list_learning_examples_func is None:
        list_learning_examples_func = list_cleanup_learning_examples

    rows = list(transactions or [])
    keys_by_index: dict[int, tuple[str, str]] = {}
    for idx, row in enumerate(rows):
        key = import_identity_keys(row.get("payee") or row.get("name"), row.get("memo"))
        if key[0] or key[1]:
            keys_by_index[idx] = key

    if not keys_by_index:
        return [{"row_index": idx, "payee_prefill": False} for idx in range(len(rows))]

    rules = list_rules_func(account_id=int(account_id), keys=keys_by_index.values())
    learned_rules = list_rules_func(account_id=int(account_id), keys=[], min_use_count=1)
    learning_rules = list_learning_examples_func(account_id=int(account_id), keys=keys_by_index.values())
    learned_learning_rules = list_learning_examples_func(account_id=int(account_id), keys=[], min_use_count=1)
    rule_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for rule in rules or []:
        key = (str(rule.get("raw_payee_key") or ""), str(rule.get("raw_memo_key") or ""))
        if key not in rule_by_key:
            rule_by_key[key] = rule
    learning_rule_by_key: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for rule in learning_rules or []:
        key = (str(rule.get("raw_payee_key") or ""), str(rule.get("raw_memo_key") or ""))
        if key[0] or key[1]:
            learning_rule_by_key.setdefault(key, []).append(rule)

    output: list[dict[str, Any]] = []
    for idx in range(len(rows)):
        key = keys_by_index.get(idx)
        rule = rule_by_key.get(key) if key else None
        learned_rule = None
        if rule is None and key:
            learned_rule = _select_exact_rule_candidate(learning_rule_by_key.get(key) or [])
        withheld_profile_selection: _ProfileRuleSelection | None = None
        if rule is None and learned_rule is None:
            profile_selection = _profile_rule_candidate(
                rows[idx],
                list(learned_rules or []) + list(learned_learning_rules or []),
            )
            learned_rule = profile_selection.rule
            if profile_selection.withheld_reason:
                withheld_profile_selection = profile_selection
        rule = rule or learned_rule
        prefill = _prefill_from_rule(idx, rule, learned_rule=bool(learned_rule))
        if prefill:
            output.append(prefill)
        elif withheld_profile_selection:
            output.append(_withheld_profile_cluster_prefill(idx, withheld_profile_selection))
        else:
            output.append({"row_index": idx, "payee_prefill": False})
    return output


def _prefill_from_rule(
    row_index: int,
    rule: dict[str, Any] | None,
    *,
    learned_rule: bool,
) -> dict[str, Any] | None:
    if not rule:
        return None

    canonical_payee = str(rule.get("canonical_payee") or "").strip()
    canonical_memo_value = rule.get("canonical_memo")
    canonical_memo = canonical_memo_value.strip() if isinstance(canonical_memo_value, str) else canonical_memo_value
    payee_changed = _truthy_flag(rule.get("payee_changed"), default=True)
    memo_changed = _truthy_flag(rule.get("memo_changed"), default=False)
    payee_prefill = bool(canonical_payee and payee_changed)
    memo_prefill = bool(canonical_memo is not None and memo_changed)
    if not payee_prefill and not memo_prefill:
        return None

    reason = str(rule.get("_payee_cleanup_reason_code") or (
        "payee_learned_from_prior_rows" if learned_rule else "matched_raw_text_profile"
    ))
    evidence = {"rule_id": rule.get("id")}
    evidence.update(rule.get("_payee_cleanup_evidence") or {})
    prefill = {
        "row_index": row_index,
        "payee_prefill": payee_prefill,
        "memo_prefill": memo_prefill,
        "rule_id": rule.get("id"),
        "debug_reason_codes": [reason],
        "prediction_debug": prediction_debug_payload(
            engine="payee_cleanup",
            decision="prefill",
            prediction_type="payee_memo_cleanup",
            reason_codes=[reason],
            confidence="high",
            evidence=evidence,
        ),
    }
    if payee_prefill:
        prefill["canonical_payee"] = canonical_payee
    if memo_prefill:
        prefill["canonical_memo"] = canonical_memo or ""
    return prefill


def _truthy_flag(value: Any, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    try:
        return bool(int(value))
    except (TypeError, ValueError):
        return bool(value)


def _select_exact_rule_candidate(candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    candidates = [candidate for candidate in candidates if _rule_has_cleanup(candidate)]
    if not candidates:
        return None
    candidates.sort(
        key=lambda rule: (
            int(rule.get("use_count") or 0),
            str(rule.get("last_used_at") or ""),
            str(rule.get("id") or ""),
        ),
        reverse=True,
    )
    top = candidates[0]
    top_signature = _display_signature(top)
    top_count = int(top.get("use_count") or 0)
    for candidate in candidates[1:]:
        if _display_signature(candidate) != top_signature and int(candidate.get("use_count") or 0) == top_count:
            return None
    return top


def _rule_has_cleanup(rule: dict[str, Any]) -> bool:
    canonical_payee = str(rule.get("canonical_payee") or "").strip()
    has_payee = bool(canonical_payee and _truthy_flag(rule.get("payee_changed"), default=True))
    has_memo = rule.get("canonical_memo") is not None and _truthy_flag(rule.get("memo_changed"), default=False)
    return has_payee or has_memo


def _display_signature(rule: dict[str, Any]) -> tuple[str, str]:
    payee = str(rule.get("canonical_payee") or "").strip() if _truthy_flag(rule.get("payee_changed"), default=True) else ""
    memo = str(rule.get("canonical_memo") or "") if _truthy_flag(rule.get("memo_changed"), default=False) else ""
    return payee, memo


def _profile_rule_candidate(row: dict[str, Any], rules: list[dict[str, Any]]) -> _ProfileRuleSelection:
    row_profile = build_transaction_text_profile_from_row(row)
    row_cluster = merchant_cluster_signature(row_profile)
    if row_cluster is None:
        return _ProfileRuleSelection()

    displays: dict[tuple[str, str], dict[str, Any]] = {}
    for rule in rules:
        if not _rule_has_cleanup(rule):
            continue
        sample_row = {
            "payee": rule.get("raw_payee_sample") or rule.get("raw_payee_key"),
            "memo": rule.get("raw_memo_sample") or rule.get("raw_memo_key"),
        }
        rule_profile = build_transaction_text_profile_from_row(sample_row)
        rule_cluster = merchant_cluster_signature(rule_profile)
        if rule_cluster is None or rule_cluster.signature != row_cluster.signature:
            continue

        display = _display_signature(rule)
        raw_key = _rule_raw_key(rule)
        item = displays.setdefault(display, {
            "display": display,
            "raw_keys": set(),
            "rules": [],
            "use_count": 0,
            "last_used_at": "",
        })
        item["raw_keys"].add(raw_key)
        item["rules"].append(rule)
        item["use_count"] = int(item["use_count"]) + _safe_int(rule.get("use_count"), default=1)
        item["last_used_at"] = max(str(item["last_used_at"] or ""), str(rule.get("last_used_at") or ""))

    if not displays:
        return _ProfileRuleSelection()

    ranked = sorted(
        displays.values(),
        key=lambda item: (
            len(item["raw_keys"]),
            int(item["use_count"]),
            str(item["last_used_at"] or ""),
        ),
        reverse=True,
    )
    top = ranked[0]
    top_support = len(top["raw_keys"])
    second_support = len(ranked[1]["raw_keys"]) if len(ranked) > 1 else 0
    total_support = sum(len(item["raw_keys"]) for item in ranked)
    evidence = {
        "cluster_signature": row_cluster.signature,
        "cluster_tokens": list(row_cluster.tokens),
        "cluster_quality": row_cluster.quality,
        "cluster_reason": row_cluster.reason,
        "support_count": top_support,
        "competing_display_count": max(len(ranked) - 1, 0),
    }

    if second_support and (top_support <= second_support or top_support / max(total_support, 1) < 0.67):
        return _ProfileRuleSelection(
            withheld_reason="profile_cluster_ambiguous",
            evidence=evidence,
        )
    if top_support < 2:
        return _ProfileRuleSelection(
            withheld_reason="profile_cluster_insufficient_support",
            evidence=evidence,
        )

    selected_rules = sorted(
        top["rules"],
        key=lambda rule: (
            _safe_int(rule.get("use_count"), default=1),
            str(rule.get("last_used_at") or ""),
            str(rule.get("id") or ""),
        ),
        reverse=True,
    )
    selected = dict(selected_rules[0])
    selected["_payee_cleanup_reason_code"] = "payee_learned_from_profile_cluster"
    selected["_payee_cleanup_evidence"] = evidence
    return _ProfileRuleSelection(rule=selected, evidence=evidence)


def _withheld_profile_cluster_prefill(row_index: int, selection: _ProfileRuleSelection) -> dict[str, Any]:
    reason = selection.withheld_reason or "profile_cluster_withheld"
    return {
        "row_index": row_index,
        "payee_prefill": False,
        "debug_reason_codes": [reason],
        "prediction_debug": prediction_debug_payload(
            engine="payee_cleanup",
            decision="no_prefill",
            prediction_type="payee_memo_cleanup",
            reason_codes=[reason],
            confidence="none",
            evidence=selection.evidence or {},
        ),
    }


def _rule_raw_key(rule: dict[str, Any]) -> tuple[str, str]:
    raw_payee_key = str(rule.get("raw_payee_key") or "")
    raw_memo_key = str(rule.get("raw_memo_key") or "")
    if raw_payee_key or raw_memo_key:
        return raw_payee_key, raw_memo_key
    return import_identity_keys(rule.get("raw_payee_sample"), rule.get("raw_memo_sample"))


def _safe_int(value: Any, *, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def list_cleanup_learning_examples(
    *,
    account_id: int,
    keys: Iterable[tuple[str, str]] | None = None,
    min_use_count: int | None = None,
    limit: int = 500,
) -> list[dict[str, Any]]:
    if not account_id:
        return []
    db = get_db()
    if not table_exists(db, "transaction_learning_examples"):
        return []

    key_set = {
        (str(payee_key or ""), str(memo_key or ""))
        for payee_key, memo_key in (keys or [])
        if payee_key or memo_key
    }
    if not key_set and min_use_count is None:
        return []

    rows = db.execute(
        """
        SELECT
            id,
            raw_payee,
            raw_memo,
            final_payee,
            final_memo,
            evidence_quality,
            source,
            created_at,
            updated_at
        FROM transaction_learning_examples
        WHERE account_id=?
          AND (raw_payee IS NOT NULL OR raw_memo IS NOT NULL)
          AND (final_payee IS NOT NULL OR final_memo IS NOT NULL)
        ORDER BY updated_at DESC, id DESC
        LIMIT ?
        """,
        (int(account_id), int(limit)),
    ).fetchall()

    grouped: dict[tuple[str, str, str, str, bool, bool], dict[str, Any]] = {}
    for row in rows:
        raw_payee = row["raw_payee"]
        raw_memo = row["raw_memo"]
        final_payee = row["final_payee"]
        final_memo = row["final_memo"]
        raw_payee_key, raw_memo_key = import_identity_keys(raw_payee, raw_memo)
        if key_set and (raw_payee_key, raw_memo_key) not in key_set:
            continue

        payee_changed = cleanup_part_differs_meaningfully(raw_payee, final_payee)
        memo_changed = cleanup_part_differs_meaningfully(raw_memo, final_memo)
        if not payee_changed and not memo_changed:
            continue

        canonical_payee = str(final_payee or "").strip()
        canonical_memo = "" if final_memo is None else str(final_memo)
        group_key = (
            raw_payee_key,
            raw_memo_key,
            canonical_payee,
            canonical_memo,
            payee_changed,
            memo_changed,
        )
        current = grouped.get(group_key)
        if current is None:
            grouped[group_key] = {
                "id": f"learning:{row['id']}",
                "account_id": int(account_id),
                "raw_payee_key": raw_payee_key,
                "raw_memo_key": raw_memo_key,
                "raw_payee_sample": raw_payee,
                "raw_memo_sample": raw_memo,
                "canonical_payee": canonical_payee,
                "canonical_memo": canonical_memo,
                "payee_changed": 1 if payee_changed else 0,
                "memo_changed": 1 if memo_changed else 0,
                "use_count": 1,
                "last_used_at": row["updated_at"] or row["created_at"] or "",
                "source": row["source"],
                "evidence_quality": row["evidence_quality"],
            }
        else:
            current["use_count"] = int(current.get("use_count") or 0) + 1

    rules = list(grouped.values())
    if min_use_count is not None:
        rules = [rule for rule in rules if int(rule.get("use_count") or 0) >= int(min_use_count)]
    return sorted(
        rules,
        key=lambda rule: (
            int(rule.get("use_count") or 0),
            str(rule.get("last_used_at") or ""),
            str(rule.get("id") or ""),
        ),
        reverse=True,
    )


def payee_normalization_example_from_import_row(row, *, account_id: int) -> dict[str, Any] | None:
    raw_payee = row.orig_payee or row.payee
    final_payee = row.payee
    raw_memo = row.orig_memo or row.memo
    final_memo = row.memo
    payee_changed = payee_differs_meaningfully(raw_payee, final_payee)
    memo_changed = cleanup_part_differs_meaningfully(raw_memo, final_memo)
    if not payee_changed and not memo_changed:
        return None

    raw_payee_key, raw_memo_key = import_identity_keys(raw_payee, raw_memo)
    if not raw_payee_key and not raw_memo_key:
        return None
    return {
        "account_id": int(account_id),
        "raw_payee_key": raw_payee_key,
        "raw_memo_key": raw_memo_key,
        "raw_payee_sample": raw_payee,
        "raw_memo_sample": raw_memo,
        "canonical_payee": final_payee or "",
        "canonical_memo": final_memo,
        "payee_changed": payee_changed,
        "memo_changed": memo_changed,
    }


def record_payee_normalization_from_import_row(
    row,
    *,
    account_id: int,
    record_func: Callable[..., int | None] = payee_normalization_repo.record_payee_normalization_example,
) -> int | None:
    example = payee_normalization_example_from_import_row(row, account_id=account_id)
    if example is None:
        return None
    return record_func(**example)
