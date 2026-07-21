from __future__ import annotations

import sqlite3
from typing import Iterable

from ..db import get_db


def _db(db: sqlite3.Connection | None = None) -> sqlite3.Connection:
    return db or get_db()


def create_session(
    *,
    db: sqlite3.Connection | None = None,
    account_id: int,
    statement_date: str,
    statement_balance_cents: int,
    starting_balance_cents: int,
    label: str | None = None,
    note: str | None = None,
    now: str,
) -> int:
    conn = _db(db)
    conn.execute(
        """
        INSERT INTO reconciliation_sessions (
            account_id, statement_date, statement_balance_cents, starting_balance_cents,
            status, label, note, created_at, updated_at
        ) VALUES (?, ?, ?, ?, 'open', ?, ?, ?, ?)
        """,
        (
            int(account_id),
            statement_date,
            int(statement_balance_cents),
            int(starting_balance_cents),
            label,
            note,
            now,
            now,
        ),
    )
    return int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])


def get_session(session_id: int, *, db: sqlite3.Connection | None = None) -> dict | None:
    row = _db(db).execute(
        """
        SELECT rs.*, a.name AS account_name, a.account_type
        FROM reconciliation_sessions rs
        JOIN accounts a ON a.id = rs.account_id
        WHERE rs.id=?
        """,
        (int(session_id),),
    ).fetchone()
    return dict(row) if row else None


def list_sessions_for_account(account_id: int, *, db: sqlite3.Connection | None = None) -> list[dict]:
    rows = _db(db).execute(
        """
        SELECT rs.*,
               COUNT(ri.id) AS item_count,
               COALESCE(SUM(t.amount_cents), 0) AS selected_total_cents
        FROM reconciliation_sessions rs
        LEFT JOIN reconciliation_items ri ON ri.session_id = rs.id
        LEFT JOIN transactions t ON t.id = ri.transaction_id
        WHERE rs.account_id=?
        GROUP BY rs.id
        ORDER BY rs.statement_date DESC, rs.id DESC
        """,
        (int(account_id),),
    ).fetchall()
    return [dict(row) for row in rows]


def latest_editable_session_for_account(account_id: int, *, db: sqlite3.Connection | None = None) -> dict | None:
    row = _db(db).execute(
        """
        SELECT *
        FROM reconciliation_sessions
        WHERE account_id=? AND status IN ('open','reopened')
        ORDER BY updated_at DESC, id DESC
        LIMIT 1
        """,
        (int(account_id),),
    ).fetchone()
    return dict(row) if row else None


def update_session_fields(
    session_id: int,
    fields: dict,
    *,
    db: sqlite3.Connection | None = None,
) -> None:
    allowed = {
        "statement_date",
        "statement_balance_cents",
        "starting_balance_cents",
        "status",
        "label",
        "note",
        "updated_at",
        "closed_at",
        "reopened_at",
    }
    pairs = [(key, value) for key, value in fields.items() if key in allowed]
    if not pairs:
        return
    sql = ", ".join(f"{key}=?" for key, _ in pairs)
    values = [value for _, value in pairs]
    values.append(int(session_id))
    _db(db).execute(f"UPDATE reconciliation_sessions SET {sql} WHERE id=?", values)


def latest_closed_session_for_account(
    account_id: int,
    *,
    before_statement_date: str | None = None,
    db: sqlite3.Connection | None = None,
) -> dict | None:
    conn = _db(db)
    params: list = [int(account_id)]
    date_sql = ""
    if before_statement_date:
        date_sql = "AND statement_date < ?"
        params.append(before_statement_date)
    row = conn.execute(
        f"""
        SELECT *
        FROM reconciliation_sessions
        WHERE account_id=? AND status='closed' {date_sql}
        ORDER BY statement_date DESC, id DESC
        LIMIT 1
        """,
        params,
    ).fetchone()
    return dict(row) if row else None


def replace_items(
    session_id: int,
    transaction_ids: Iterable[int],
    *,
    state: str = "cleared",
    now: str,
    db: sqlite3.Connection | None = None,
) -> None:
    conn = _db(db)
    conn.execute("DELETE FROM reconciliation_items WHERE session_id=?", (int(session_id),))
    ids = list(dict.fromkeys(int(tx_id) for tx_id in transaction_ids))
    conn.executemany(
        """
        INSERT INTO reconciliation_items (session_id, transaction_id, state, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        [(int(session_id), tx_id, state, now, now) for tx_id in ids],
    )


def set_item_state_for_session(
    session_id: int,
    state: str,
    *,
    now: str,
    db: sqlite3.Connection | None = None,
) -> None:
    _db(db).execute(
        "UPDATE reconciliation_items SET state=?, updated_at=? WHERE session_id=?",
        (state, now, int(session_id)),
    )


def list_items(session_id: int, *, db: sqlite3.Connection | None = None) -> list[dict]:
    rows = _db(db).execute(
        """
        SELECT ri.*, t.account_id, t.amount_cents, t.posted_at, t.payee, t.memo, t.fitid, t.xfer_pair_id
        FROM reconciliation_items ri
        LEFT JOIN transactions t ON t.id = ri.transaction_id
        WHERE ri.session_id=?
        ORDER BY t.posted_at, t.id
        """,
        (int(session_id),),
    ).fetchall()
    return [dict(row) for row in rows]


def list_candidate_transactions(
    account_id: int,
    *,
    session_id: int | None = None,
    statement_date: str | None = None,
    db: sqlite3.Connection | None = None,
) -> list[dict]:
    conn = _db(db)
    params: list = [int(session_id or 0), int(account_id)]
    date_sql = ""
    if statement_date:
        # Normal candidates are transactions through the statement date. Already
        # selected rows stay visible if a user edits the date later, so progress
        # never disappears invisibly.
        date_sql = "AND (t.posted_at <= ? OR ri_current.id IS NOT NULL)"
        params.append(statement_date)
    params.append(int(session_id or 0))
    rows = conn.execute(
        f"""
        SELECT t.*,
               ri_current.id IS NOT NULL AS is_selected,
               ri_current.state AS reconciliation_state
        FROM transactions t
        LEFT JOIN reconciliation_items ri_current
          ON ri_current.transaction_id = t.id
         AND ri_current.session_id = ?
        WHERE t.account_id = ?
          {date_sql}
          AND NOT EXISTS (
            SELECT 1
            FROM reconciliation_items ri
            JOIN reconciliation_sessions rs ON rs.id = ri.session_id
            WHERE ri.transaction_id = t.id
              AND ri.state='reconciled'
              AND rs.status='closed'
              AND ri.session_id <> ?
          )
        ORDER BY t.posted_at, t.id
        """,
        params,
    ).fetchall()
    return [dict(row) for row in rows]


def sum_items(session_id: int, *, db: sqlite3.Connection | None = None) -> int:
    row = _db(db).execute(
        """
        SELECT COALESCE(SUM(t.amount_cents), 0) AS total
        FROM reconciliation_items ri
        JOIN transactions t ON t.id = ri.transaction_id
        WHERE ri.session_id=?
        """,
        (int(session_id),),
    ).fetchone()
    return int(row["total"] or 0)


def list_transactions_by_ids(
    transaction_ids: Iterable[int],
    *,
    db: sqlite3.Connection | None = None,
) -> list[dict]:
    ids = list(dict.fromkeys(int(tx_id) for tx_id in transaction_ids))
    if not ids:
        return []
    placeholders = ", ".join("?" for _ in ids)
    rows = _db(db).execute(
        f"SELECT * FROM transactions WHERE id IN ({placeholders})",
        ids,
    ).fetchall()
    return [dict(row) for row in rows]


def reconciled_transaction_conflicts(
    transaction_ids: Iterable[int],
    *,
    excluding_session_id: int | None = None,
    db: sqlite3.Connection | None = None,
) -> list[dict]:
    ids = list(dict.fromkeys(int(tx_id) for tx_id in transaction_ids))
    if not ids:
        return []
    placeholders = ", ".join("?" for _ in ids)
    params: list = ids[:]
    exclude_sql = ""
    if excluding_session_id is not None:
        exclude_sql = "AND ri.session_id <> ?"
        params.append(int(excluding_session_id))
    rows = _db(db).execute(
        f"""
        SELECT ri.transaction_id, ri.session_id, rs.status
        FROM reconciliation_items ri
        JOIN reconciliation_sessions rs ON rs.id = ri.session_id
        WHERE ri.transaction_id IN ({placeholders})
          AND ri.state='reconciled'
          AND rs.status='closed'
          {exclude_sql}
        """,
        params,
    ).fetchall()
    return [dict(row) for row in rows]


def reconciled_transaction_ids(
    transaction_ids: Iterable[int],
    *,
    db: sqlite3.Connection | None = None,
) -> set[int]:
    ids = list(dict.fromkeys(int(tx_id) for tx_id in transaction_ids if tx_id))
    if not ids:
        return set()
    placeholders = ", ".join("?" for _ in ids)
    rows = _db(db).execute(
        f"""
        SELECT DISTINCT ri.transaction_id
        FROM reconciliation_items ri
        JOIN reconciliation_sessions rs ON rs.id = ri.session_id
        WHERE ri.transaction_id IN ({placeholders})
          AND ri.state='reconciled'
          AND rs.status='closed'
        """,
        ids,
    ).fetchall()
    return {int(row["transaction_id"]) for row in rows}


def account_opening_balance(account_id: int, *, db: sqlite3.Connection | None = None) -> int:
    row = _db(db).execute(
        "SELECT opening_balance_cents FROM accounts WHERE id=?",
        (int(account_id),),
    ).fetchone()
    if row is None:
        raise ValueError("Account not found")
    return int(row["opening_balance_cents"] or 0)
