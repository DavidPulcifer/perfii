from typing import Dict, Optional
from ..db import get_db
from .import_validation_repo import (
    list_import_validated_fitid_rows_for_account,
    list_import_validated_fitids_for_account,
)


def _normalize_int_ids(values) -> list[int]:
    ids: list[int] = []
    seen: set[int] = set()
    for value in values or []:
        if value is None:
            continue
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            continue
        if parsed and parsed not in seen:
            ids.append(parsed)
            seen.add(parsed)
    return ids


def _normalize_ttypes(values) -> list[str]:
    allowed = {"income", "expense", "transfer", "transfer_in", "transfer_out", "allocation"}
    ttypes: list[str] = []
    seen: set[str] = set()
    for value in values or []:
        ttype = str(value or "").strip()
        if not ttype or ttype not in allowed:
            continue
        if ttype not in seen:
            ttypes.append(ttype)
            seen.add(ttype)
    return ttypes


def list_transactions(
    *,
    limit: int = 200,
    offset: int = 0,
    account_id: int | None = None,
    account_ids: list[int] | tuple[int, ...] | None = None,
    date_from: str | None = None,   # 'YYYY-MM-DD'
    date_to: str | None = None,     # 'YYYY-MM-DD'
    ttype: str | None = None,       # 'income'|'expense'|'transfer'|'transfer_in'|'transfer_out'|'allocation'
    ttypes: list[str] | tuple[str, ...] | None = None,
    amount_min_cents: int | None = None,
    amount_max_cents: int | None = None,
    amount_exact_cents: int | None = None,
    envelope_id: int | None = None,
    envelope_ids: list[int] | tuple[int, ...] | None = None,
    q_payee: str | None = None,
    q_memo: str | None = None,
    use_abs: bool = True,
    reconciliation_status: str | None = None,
):
    """
    Returns (rows, total_count) with filters applied.
    """
    db = get_db()
    where = []
    params = []

    # account filter
    selected_account_ids = _normalize_int_ids(account_ids)
    if not selected_account_ids and account_id:
        selected_account_ids = _normalize_int_ids([account_id])
    if selected_account_ids:
        placeholders = ", ".join("?" for _ in selected_account_ids)
        where.append(f"t.account_id IN ({placeholders})")
        params.extend(selected_account_ids)
    elif account_id:
        where.append("t.account_id = ?")
        params.append(account_id)

    # date range
    if date_from:
        where.append("t.posted_at >= ?")
        params.append(date_from)
    if date_to:
        where.append("t.posted_at <= ?")
        params.append(date_to)

    # type filter
    selected_ttypes = _normalize_ttypes(ttypes)
    if not selected_ttypes and ttype:
        selected_ttypes = _normalize_ttypes([ttype])
    if selected_ttypes:
        expanded_ttypes = []
        for selected in selected_ttypes:
            if selected == "transfer":
                expanded_ttypes.extend(["transfer_in", "transfer_out"])
            else:
                expanded_ttypes.append(selected)
        expanded_ttypes = list(dict.fromkeys(expanded_ttypes))
        placeholders = ", ".join("?" for _ in expanded_ttypes)
        where.append(f"t.ttype IN ({placeholders})")
        params.extend(expanded_ttypes)

    # amount filters (optionally absolute)
    col = "ABS(t.amount_cents)" if use_abs else "t.amount_cents"
    if amount_exact_cents is not None:
        where.append(f"{col} = ?")
        params.append(int(amount_exact_cents))
    elif amount_min_cents is not None:
        where.append(f"{col} >= ?")
        params.append(int(amount_min_cents))
    if amount_exact_cents is None and amount_max_cents is not None:
        where.append(f"{col} <= ?")
        params.append(int(amount_max_cents))

    # envelope filter: require a split to that envelope
    selected_envelope_ids = _normalize_int_ids(envelope_ids)
    if not selected_envelope_ids and envelope_id:
        selected_envelope_ids = _normalize_int_ids([envelope_id])
    if selected_envelope_ids:
        placeholders = ", ".join("?" for _ in selected_envelope_ids)
        where.append("""
            EXISTS (
              SELECT 1 FROM transaction_splits s
              WHERE s.transaction_id = t.id AND s.envelope_id IN (""" + placeholders + """)
            )
        """)
        params.extend(selected_envelope_ids)

    # text filters (case-insensitive contains)
    if q_payee:
        where.append("LOWER(t.payee) LIKE LOWER(?)")
        params.append(f"%{q_payee}%")
    if q_memo:
        where.append("LOWER(t.memo) LIKE LOWER(?)")
        params.append(f"%{q_memo}%")

    if reconciliation_status == "reconciled":
        where.append("""
            EXISTS (
              SELECT 1
              FROM reconciliation_items ri
              JOIN reconciliation_sessions rs ON rs.id = ri.session_id
              WHERE ri.transaction_id = t.id
                AND ri.state='reconciled'
                AND rs.status='closed'
            )
        """)
    elif reconciliation_status == "unreconciled":
        where.append("""
            NOT EXISTS (
              SELECT 1
              FROM reconciliation_items ri
              JOIN reconciliation_sessions rs ON rs.id = ri.session_id
              WHERE ri.transaction_id = t.id
                AND ri.state='reconciled'
                AND rs.status='closed'
            )
        """)

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    # total count
    total = db.execute(f"SELECT COUNT(*) AS c FROM transactions t {where_sql}", params).fetchone()["c"]

    # rows page
    rows = db.execute(
        f"""
        SELECT t.*,
               EXISTS (
                 SELECT 1
                 FROM transaction_import_validations tiv
                 WHERE tiv.account_id = t.account_id
                   AND tiv.transaction_id = t.id
               ) AS import_validated,
               EXISTS (
                 SELECT 1
                 FROM reconciliation_items ri
                 JOIN reconciliation_sessions rs ON rs.id = ri.session_id
                 WHERE ri.transaction_id = t.id
                   AND ri.state='reconciled'
                   AND rs.status='closed'
               ) AS is_reconciled
        FROM transactions t
        {where_sql}
        ORDER BY t.posted_at DESC, t.id DESC
        LIMIT ? OFFSET ?
        """,
        (*params, int(limit), int(offset)),
    ).fetchall()

    return [dict(r) for r in rows], int(total)



def list_account_transactions_with_running_balance(*, account_id: int, limit: int = 25, offset: int = 0):
    """Return one account's transactions with signed running balances.

    Running balances are computed in canonical ledger order
    (posted_at ASC, id ASC), then rows are returned newest-first for display.
    The balance on each row is the account balance after that transaction.
    """
    db = get_db()
    total = db.execute(
        "SELECT COUNT(*) AS c FROM transactions WHERE account_id=?",
        (int(account_id),),
    ).fetchone()["c"]

    rows = db.execute(
        """
        WITH ordered AS (
            SELECT
                t.*,
                SUM(t.amount_cents) OVER (
                    ORDER BY t.posted_at ASC, t.id ASC
                    ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                ) AS running_balance_cents
            FROM transactions t
            WHERE t.account_id = ?
        )
        SELECT ordered.*,
               EXISTS (
                 SELECT 1
                 FROM transaction_import_validations tiv
                 WHERE tiv.account_id = ordered.account_id
                   AND tiv.transaction_id = ordered.id
               ) AS import_validated,
               EXISTS (
                 SELECT 1
                 FROM reconciliation_items ri
                 JOIN reconciliation_sessions rs ON rs.id = ri.session_id
                 WHERE ri.transaction_id = ordered.id
                   AND ri.state='reconciled'
                   AND rs.status='closed'
               ) AS is_reconciled
        FROM ordered
        ORDER BY ordered.posted_at DESC, ordered.id DESC
        LIMIT ? OFFSET ?
        """,
        (int(account_id), int(limit), int(offset)),
    ).fetchall()

    return [dict(r) for r in rows], int(total)

def list_fitids_for_account(account_id: int):
    return list_import_validated_fitids_for_account(account_id)

def list_imported_fitid_rows_for_account(account_id: int):
    return list_import_validated_fitid_rows_for_account(account_id)

def insert_transaction(
    *,
    db,  # REQUIRED
    account_id: int,
    ttype: str,
    amount_cents: int,
    posted_at: str,
    payee: Optional[str] = None,
    memo: Optional[str] = None,
    fitid: Optional[str] = None,
    external_counterparty: Optional[str] = None,
    ignore_match: int = 0,
) -> int:
    db.execute(
        """
        INSERT INTO transactions (
            account_id, ttype, amount_cents, posted_at, payee, memo, fitid, external_counterparty, ignore_match
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (account_id, ttype, amount_cents, posted_at, payee, memo, fitid, external_counterparty, int(ignore_match)),
    )
    return db.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]

def insert_split(*, transaction_id: int, envelope_id: int, amount_cents: int):
    db = get_db()
    db.execute(
        "INSERT INTO transaction_splits (transaction_id, envelope_id, amount_cents) "
        "VALUES (?, ?, ?)",
        (transaction_id, envelope_id, amount_cents),
    )
    db.commit()
    # return last row id if you ever need it
    return db.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]

def get_transaction(tx_id: int) -> dict | None:
    db = get_db()
    row = db.execute("SELECT * FROM transactions WHERE id=?", (tx_id,)).fetchone()
    return dict(row) if row else None

def update_transaction(*, db, tx_id: int, data: Dict) -> None:
    if not data:
        return
    fields, values = [], []
    for k in ("account_id", "ttype", "amount_cents", "posted_at", "payee", "memo", "fitid", "external_counterparty", "ignore_match"):
        if k in data and data[k] is not None:
            fields.append(f"{k}=?")
            values.append(data[k])
    if not fields:
        return
    values.append(tx_id)
    db.execute(f"UPDATE transactions SET {', '.join(fields)} WHERE id=?", values)

def delete_transaction(*, db, tx_id: int) -> None:
    db.execute("DELETE FROM transactions WHERE id=?", (tx_id,))

def find_matching_transfer_leg(tx_id: int) -> dict | None:
    """
    Naive match: find another row on the same date with equal/opposite amount.
    """
    db = get_db()
    t = db.execute("SELECT posted_at, amount_cents, account_id FROM transactions WHERE id=?", (tx_id,)).fetchone()
    if not t:
        return None
    amt = -int(t["amount_cents"])
    date = t["posted_at"]
    row = db.execute(
        "SELECT * FROM transactions WHERE id<>? AND posted_at=? AND amount_cents=? ORDER BY id DESC LIMIT 1",
        (tx_id, date, amt)
    ).fetchone()
    return dict(row) if row else None

def link_transfer_pair(*, db, tx_out_id: int, tx_in_id: int) -> None:
    db.execute("UPDATE transactions SET xfer_pair_id=? WHERE id=?", (tx_in_id, tx_out_id))
    db.execute("UPDATE transactions SET xfer_pair_id=? WHERE id=?", (tx_out_id, tx_in_id))
