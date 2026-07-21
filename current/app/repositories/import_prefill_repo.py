from __future__ import annotations

from collections import defaultdict
import json
from typing import Any

from ..db import get_db, table_exists


def _normalize_int(value) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed else None


def _parse_json_object(value: Any) -> dict[str, Any]:
    if not value:
        return {}
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(str(value))
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _parse_json_list(value: Any) -> list[dict[str, Any]]:
    if not value:
        return []
    if isinstance(value, list):
        return [dict(item) for item in value if isinstance(item, dict)]
    try:
        parsed = json.loads(str(value))
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(parsed, list):
        return []
    return [dict(item) for item in parsed if isinstance(item, dict)]


def _opposite_transfer_type(ttype: Any) -> str | None:
    if ttype == "transfer_in":
        return "transfer_out"
    if ttype == "transfer_out":
        return "transfer_in"
    return None


def _split_rows_by_transaction(db, tx_ids: list[int]) -> dict[int, list[dict[str, Any]]]:
    ids = [int(tx_id) for tx_id in tx_ids if _normalize_int(tx_id)]
    if not ids:
        return {}
    if not table_exists(db, "transaction_splits"):
        return {}

    placeholders = ", ".join("?" for _ in ids)
    rows = db.execute(
        f"""
        SELECT
            s.transaction_id,
            s.envelope_id,
            s.amount_cents,
            e.name AS envelope_name,
            e.locked_account_id,
            e.archived_at AS envelope_archived_at
        FROM transaction_splits s
        LEFT JOIN envelopes e ON e.id = s.envelope_id
        WHERE s.transaction_id IN ({placeholders})
        ORDER BY s.transaction_id, s.envelope_id, s.id
        """,
        ids,
    ).fetchall()

    grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[int(row["transaction_id"])].append(dict(row))
    return grouped


def _remainder_intents_by_transaction(db, tx_ids: list[int]) -> dict[int, dict[str, Any]]:
    ids = [int(tx_id) for tx_id in tx_ids if _normalize_int(tx_id)]
    if not ids:
        return {}
    if not table_exists(db, "transaction_remainder_intents"):
        return {}

    placeholders = ", ".join("?" for _ in ids)
    rows = db.execute(
        f"""
        SELECT
            transaction_id,
            envelope_id,
            amount_cents,
            created_at,
            updated_at
        FROM transaction_remainder_intents
        WHERE transaction_id IN ({placeholders})
        """,
        ids,
    ).fetchall()
    return {int(row["transaction_id"]): dict(row) for row in rows}


def _account_row(db, account_id: int | None) -> dict[str, Any]:
    if not account_id:
        return {}
    row = db.execute(
        """
        SELECT id, name, acct_key, bankid, acctid, account_type
        FROM accounts
        WHERE id=?
        """,
        (int(account_id),),
    ).fetchone()
    return dict(row) if row else {}


def list_import_prefill_history(
    *,
    account_id: int | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = 2000,
) -> list[dict[str, Any]]:
    """Return read-only historical transactions for FIN-045 prefill analysis.

    Rows are scoped to normal import-relevant transaction types and include split
    rows plus paired transfer metadata/splits when available. The service layer
    owns prediction/pattern-selection behavior; this repository stays close to
    SQL/table shape.
    """
    db = get_db()
    where = ["t.ttype IN ('expense', 'income', 'transfer_in', 'transfer_out')"]
    params: list[Any] = []

    if account_id is not None:
        where.append("t.account_id = ?")
        params.append(int(account_id))
    if date_from:
        where.append("t.posted_at >= ?")
        params.append(date_from)
    if date_to:
        where.append("t.posted_at <= ?")
        params.append(date_to)

    where_sql = " AND ".join(where)
    rows = db.execute(
        f"""
        SELECT
            t.id,
            t.account_id,
            acct.name AS account_name,
            acct.account_type AS account_type,
            t.ttype,
            t.amount_cents,
            t.posted_at,
            t.payee,
            t.memo,
            t.fitid,
            t.xfer_pair_id,
            pair.id AS paired_id,
            pair.account_id AS paired_account_id,
            pair_acct.name AS paired_account_name,
            pair_acct.account_type AS paired_account_type,
            pair.ttype AS paired_ttype,
            pair.amount_cents AS paired_amount_cents,
            pair.posted_at AS paired_posted_at,
            pair.payee AS paired_payee,
            pair.memo AS paired_memo
        FROM transactions t
        LEFT JOIN accounts acct ON acct.id = t.account_id
        LEFT JOIN transactions pair ON pair.id = t.xfer_pair_id
        LEFT JOIN accounts pair_acct ON pair_acct.id = pair.account_id
        WHERE {where_sql}
        ORDER BY t.posted_at DESC, t.id DESC
        LIMIT ?
        """,
        (*params, int(limit)),
    ).fetchall()

    history = [dict(row) for row in rows]
    tx_ids: list[int] = []
    for row in history:
        tx_ids.append(int(row["id"]))
        paired_id = _normalize_int(row.get("paired_id"))
        if paired_id:
            tx_ids.append(paired_id)

    splits_by_tx = _split_rows_by_transaction(db, tx_ids)
    remainder_intents_by_tx = _remainder_intents_by_transaction(db, tx_ids)
    for row in history:
        tx_id = int(row["id"])
        paired_id = _normalize_int(row.get("paired_id"))
        row["splits"] = splits_by_tx.get(tx_id, [])
        row["remainder_intent"] = remainder_intents_by_tx.get(tx_id)
        row["paired_splits"] = splits_by_tx.get(paired_id, []) if paired_id else []
        row["paired_remainder_intent"] = remainder_intents_by_tx.get(paired_id) if paired_id else None
        row["paired_transaction"] = None
        if paired_id:
            row["paired_transaction"] = {
                "id": paired_id,
                "account_id": row.get("paired_account_id"),
                "account_name": row.get("paired_account_name"),
                "account_type": row.get("paired_account_type"),
                "ttype": row.get("paired_ttype"),
                "amount_cents": row.get("paired_amount_cents"),
                "posted_at": row.get("paired_posted_at"),
                "payee": row.get("paired_payee"),
                "memo": row.get("paired_memo"),
                "splits": row["paired_splits"],
                "remainder_intent": row["paired_remainder_intent"],
            }

    return history


def list_import_prefill_learning_examples(
    *,
    account_id: int | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = 2000,
) -> list[dict[str, Any]]:
    db = get_db()
    if not table_exists(db, "transaction_learning_examples"):
        return []

    where = ["le.transaction_type IN ('expense', 'income', 'transfer_in', 'transfer_out')"]
    where.append("le.amount_cents IS NOT NULL")
    params: list[Any] = []
    if account_id is not None:
        where.append("le.account_id = ?")
        params.append(int(account_id))
    if date_from:
        where.append("le.posted_at >= ?")
        params.append(date_from)
    if date_to:
        where.append("le.posted_at <= ?")
        params.append(date_to)

    rows = db.execute(
        f"""
        SELECT
            le.id AS learning_example_id,
            le.account_id,
            acct.name AS account_name,
            acct.account_type AS account_type,
            acct.acct_key AS acct_key,
            acct.bankid AS bankid,
            acct.acctid AS acctid,
            le.transaction_id,
            le.source,
            le.evidence_quality,
            le.posted_at,
            le.amount_cents,
            le.raw_payee,
            le.raw_memo,
            le.final_payee,
            le.final_memo,
            le.transaction_type,
            le.transfer_other_account_id,
            le.splits_json,
            le.remainder_intent_json,
            le.decision_json,
            le.evidence_json,
            le.created_at,
            le.updated_at,
            COALESCE(pf.accepted_count, 0) AS feedback_accepted_count,
            COALESCE(pf.modified_count, 0) AS feedback_modified_count,
            COALESCE(pf.rejected_count, 0) AS feedback_rejected_count,
            pair.id AS paired_id,
            pair.account_id AS paired_account_id,
            pair_acct.name AS paired_account_name,
            pair_acct.account_type AS paired_account_type,
            pair_acct.acct_key AS paired_acct_key,
            pair_acct.bankid AS paired_bankid,
            pair_acct.acctid AS paired_acctid,
            pair.ttype AS paired_ttype,
            pair.amount_cents AS paired_amount_cents,
            pair.posted_at AS paired_posted_at,
            pair.payee AS paired_payee,
            pair.memo AS paired_memo
        FROM transaction_learning_examples le
        LEFT JOIN accounts acct ON acct.id = le.account_id
        LEFT JOIN transactions t ON t.id = le.transaction_id
        LEFT JOIN transactions pair ON pair.id = t.xfer_pair_id
        LEFT JOIN accounts pair_acct ON pair_acct.id = COALESCE(pair.account_id, le.transfer_other_account_id)
        LEFT JOIN (
            SELECT
                learning_example_id,
                SUM(CASE WHEN outcome = 'accepted' THEN 1 ELSE 0 END) AS accepted_count,
                SUM(CASE WHEN outcome = 'modified' THEN 1 ELSE 0 END) AS modified_count,
                SUM(CASE WHEN outcome IN ('rejected','skipped','cleared') THEN 1 ELSE 0 END) AS rejected_count
            FROM prediction_feedback
            WHERE learning_example_id IS NOT NULL
            GROUP BY learning_example_id
        ) pf ON pf.learning_example_id = le.id
        WHERE {" AND ".join(where)}
        ORDER BY
            CASE le.evidence_quality WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END,
            le.updated_at DESC,
            le.id DESC
        LIMIT ?
        """,
        (*params, int(limit)),
    ).fetchall()

    tx_ids: list[int] = []
    paired_ids: list[int] = []
    for row in rows:
        tx_id = _normalize_int(row["transaction_id"])
        paired_id = _normalize_int(row["paired_id"])
        if tx_id:
            tx_ids.append(tx_id)
        if paired_id:
            paired_ids.append(paired_id)

    paired_splits = _split_rows_by_transaction(db, paired_ids)
    paired_remainders = _remainder_intents_by_transaction(db, paired_ids)
    current_fallback_splits = _split_rows_by_transaction(db, tx_ids)
    current_fallback_remainders = _remainder_intents_by_transaction(db, tx_ids)

    examples: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        quality = str(item.get("evidence_quality") or "low")
        has_raw_text = bool((item.get("raw_payee") or "").strip() or (item.get("raw_memo") or "").strip())
        use_raw_text = quality in {"high", "medium"} and has_raw_text
        tx_id = _normalize_int(item.get("transaction_id"))
        paired_id = _normalize_int(item.get("paired_id"))
        other_account_id = _normalize_int(item.get("transfer_other_account_id") or item.get("paired_account_id"))
        other_account = _account_row(db, other_account_id) if other_account_id and not item.get("paired_account_name") else {}

        splits = _parse_json_list(item.get("splits_json"))
        if not splits and tx_id:
            splits = current_fallback_splits.get(tx_id, [])
        remainder_intent = _parse_json_object(item.get("remainder_intent_json"))
        if not remainder_intent and tx_id:
            remainder_intent = current_fallback_remainders.get(tx_id)

        paired_transaction = None
        if other_account_id:
            paired_transaction = {
                "id": paired_id,
                "account_id": other_account_id,
                "account_name": item.get("paired_account_name") or other_account.get("name") or "",
                "account_type": item.get("paired_account_type") or other_account.get("account_type"),
                "acct_key": item.get("paired_acct_key") or other_account.get("acct_key"),
                "bankid": item.get("paired_bankid") or other_account.get("bankid"),
                "acctid": item.get("paired_acctid") or other_account.get("acctid"),
                "ttype": item.get("paired_ttype") or _opposite_transfer_type(item.get("transaction_type")),
                "amount_cents": item.get("paired_amount_cents") or -int(item.get("amount_cents") or 0),
                "posted_at": item.get("paired_posted_at") or item.get("posted_at"),
                "payee": item.get("paired_payee") or item.get("final_payee"),
                "memo": item.get("paired_memo") or item.get("final_memo"),
                "splits": paired_splits.get(paired_id, []) if paired_id else [],
                "remainder_intent": paired_remainders.get(paired_id) if paired_id else None,
            }

        examples.append({
            "id": f"learning:{item['learning_example_id']}",
            "learning_example_id": int(item["learning_example_id"]),
            "account_id": int(item["account_id"]),
            "account_name": item.get("account_name"),
            "account_type": item.get("account_type"),
            "acct_key": item.get("acct_key"),
            "bankid": item.get("bankid"),
            "acctid": item.get("acctid"),
            "transaction_id": tx_id,
            "source": item.get("source"),
            "evidence_quality": quality,
            "ttype": item.get("transaction_type"),
            "amount_cents": int(item.get("amount_cents") or 0),
            "posted_at": item.get("posted_at"),
            "payee": item.get("raw_payee") if use_raw_text else item.get("final_payee"),
            "memo": item.get("raw_memo") if use_raw_text else item.get("final_memo"),
            "raw_payee": item.get("raw_payee"),
            "raw_memo": item.get("raw_memo"),
            "final_payee": item.get("final_payee"),
            "final_memo": item.get("final_memo"),
            "splits": splits,
            "remainder_intent": remainder_intent,
            "paired_account_id": other_account_id,
            "paired_account_name": (paired_transaction or {}).get("account_name", ""),
            "paired_acct_key": (paired_transaction or {}).get("acct_key"),
            "paired_bankid": (paired_transaction or {}).get("bankid"),
            "paired_acctid": (paired_transaction or {}).get("acctid"),
            "paired_account_type": (paired_transaction or {}).get("account_type"),
            "paired_splits": (paired_transaction or {}).get("splits", []),
            "paired_remainder_intent": (paired_transaction or {}).get("remainder_intent"),
            "paired_transaction": paired_transaction,
            "decision": _parse_json_object(item.get("decision_json")),
            "learning_evidence": _parse_json_object(item.get("evidence_json")),
            "prediction_feedback": {
                "accepted": int(item.get("feedback_accepted_count") or 0),
                "modified": int(item.get("feedback_modified_count") or 0),
                "rejected": int(item.get("feedback_rejected_count") or 0),
            },
        })

    return examples


def list_import_prefill_history_with_learning(
    *,
    account_id: int | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = 2000,
) -> list[dict[str, Any]]:
    learning_rows = list_import_prefill_learning_examples(
        account_id=account_id,
        date_from=date_from,
        date_to=date_to,
        limit=limit,
    )
    strong_learning_tx_ids = {
        int(row["transaction_id"])
        for row in learning_rows
        if row.get("transaction_id") and row.get("evidence_quality") in {"high", "medium"}
    }
    usable_learning = [
        row for row in learning_rows
        if row.get("evidence_quality") in {"high", "medium"} or not row.get("transaction_id")
    ]
    history_limit = max(int(limit) - len(usable_learning), 0)
    final_history = []
    if history_limit:
        final_history = list_import_prefill_history(
            account_id=account_id,
            date_from=date_from,
            date_to=date_to,
            limit=history_limit,
        )
    fallback_history = [
        row for row in final_history
        if int(row.get("id") or 0) not in strong_learning_tx_ids
    ]
    for row in fallback_history:
        row.setdefault("evidence_quality", "low")
        row.setdefault("source", "final_transaction_history")
    return (usable_learning + fallback_history)[: int(limit)]
