from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable

from ..db import get_db, table_columns, table_exists


def _now() -> str:
    return datetime.utcnow().isoformat(timespec="seconds")


def list_payee_normalization_rules(
    *,
    account_id: int,
    keys: Iterable[tuple[str, str]] | None = None,
    min_use_count: int | None = None,
    limit: int = 500,
) -> list[dict[str, Any]]:
    pairs = [(str(payee_key or ""), str(memo_key or "")) for payee_key, memo_key in (keys or [])]
    pairs = [(payee_key, memo_key) for payee_key, memo_key in pairs if payee_key or memo_key]
    if not account_id:
        return []

    db = get_db()
    if not table_exists(db, "payee_normalization_rules"):
        return []
    columns = set(table_columns(db, "payee_normalization_rules"))
    memo_selects = (
        "canonical_memo, payee_changed, memo_changed"
        if {"canonical_memo", "payee_changed", "memo_changed"}.issubset(columns)
        else "NULL AS canonical_memo, 1 AS payee_changed, 0 AS memo_changed"
    )

    clauses: list[str] = ["account_id = ?"]
    params: list[Any] = [int(account_id)]
    if pairs:
        where = " OR ".join("(raw_payee_key = ? AND raw_memo_key = ?)" for _ in pairs)
        clauses.append(f"({where})")
        for payee_key, memo_key in pairs:
            params.extend([payee_key, memo_key])
    elif min_use_count is not None:
        clauses.append("use_count >= ?")
        params.append(int(min_use_count))
    else:
        return []
    params.append(int(limit))
    where_sql = " AND ".join(clauses)

    rows = db.execute(
        f"""
        SELECT
            id,
            account_id,
            raw_payee_key,
            raw_memo_key,
            raw_payee_sample,
            raw_memo_sample,
            canonical_payee,
            {memo_selects},
            use_count,
            created_at,
            updated_at,
            last_used_at
        FROM payee_normalization_rules
        WHERE {where_sql}
        ORDER BY last_used_at DESC, updated_at DESC, id DESC
        LIMIT ?
        """,
        params,
    ).fetchall()
    return [dict(row) for row in rows]


def record_payee_normalization_example(
    *,
    account_id: int,
    raw_payee_key: str,
    raw_memo_key: str = "",
    raw_payee_sample: str | None = None,
    raw_memo_sample: str | None = None,
    canonical_payee: str,
    canonical_memo: str | None = None,
    payee_changed: bool = True,
    memo_changed: bool = False,
) -> int | None:
    if not account_id or not (raw_payee_key or raw_memo_key):
        return None
    if not (canonical_payee or "").strip() and canonical_memo is None:
        return None

    db = get_db()
    if not table_exists(db, "payee_normalization_rules"):
        return None
    columns = set(table_columns(db, "payee_normalization_rules"))
    has_memo_columns = {"canonical_memo", "payee_changed", "memo_changed"}.issubset(columns)

    now = _now()
    canonical_payee_value = (canonical_payee or "").strip()
    canonical_memo_value = canonical_memo.strip() if isinstance(canonical_memo, str) else canonical_memo
    if not has_memo_columns:
        if not canonical_payee_value:
            return None
        cur = db.execute(
            """
            INSERT INTO payee_normalization_rules(
                account_id,
                raw_payee_key,
                raw_memo_key,
                raw_payee_sample,
                raw_memo_sample,
                canonical_payee,
                use_count,
                created_at,
                updated_at,
                last_used_at
            ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
            ON CONFLICT(account_id, raw_payee_key, raw_memo_key)
            DO UPDATE SET
                raw_payee_sample = excluded.raw_payee_sample,
                raw_memo_sample = excluded.raw_memo_sample,
                canonical_payee = excluded.canonical_payee,
                use_count = payee_normalization_rules.use_count + 1,
                updated_at = excluded.updated_at,
                last_used_at = excluded.last_used_at
            """,
            (
                int(account_id),
                str(raw_payee_key or ""),
                str(raw_memo_key or ""),
                raw_payee_sample,
                raw_memo_sample,
                canonical_payee_value,
                now,
                now,
                now,
            ),
        )
        db.commit()
        return int(cur.lastrowid or 0) or None

    cur = db.execute(
        """
        INSERT INTO payee_normalization_rules(
            account_id,
            raw_payee_key,
            raw_memo_key,
            raw_payee_sample,
            raw_memo_sample,
            canonical_payee,
            canonical_memo,
            payee_changed,
            memo_changed,
            use_count,
            created_at,
            updated_at,
            last_used_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
        ON CONFLICT(account_id, raw_payee_key, raw_memo_key)
        DO UPDATE SET
            raw_payee_sample = excluded.raw_payee_sample,
            raw_memo_sample = excluded.raw_memo_sample,
            canonical_payee = excluded.canonical_payee,
            canonical_memo = excluded.canonical_memo,
            payee_changed = excluded.payee_changed,
            memo_changed = excluded.memo_changed,
            use_count = payee_normalization_rules.use_count + 1,
            updated_at = excluded.updated_at,
            last_used_at = excluded.last_used_at
        """,
        (
            int(account_id),
            str(raw_payee_key or ""),
            str(raw_memo_key or ""),
            raw_payee_sample,
            raw_memo_sample,
            canonical_payee_value,
            canonical_memo_value,
            1 if payee_changed else 0,
            1 if memo_changed else 0,
            now,
            now,
            now,
        ),
    )
    db.commit()
    return int(cur.lastrowid or 0) or None
