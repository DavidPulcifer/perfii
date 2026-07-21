# app/repositories/credit_repo.py
from ..db import get_db

def get_credit_limit(account_id: int) -> int | None:
    db = get_db()
    row = db.execute(
        "SELECT credit_limit_cents FROM credit_cards WHERE account_id=?",
        (account_id,)
    ).fetchone()
    return row["credit_limit_cents"] if row else None


def get_credit_limits() -> dict[int, int]:
    db = get_db()
    rows = db.execute(
        "SELECT account_id, credit_limit_cents FROM credit_cards"
    ).fetchall()
    return {int(row["account_id"]): int(row["credit_limit_cents"] or 0) for row in rows}


def set_credit_limit(account_id: int, credit_limit_cents: int, *, db=None) -> None:
    """
    Upsert the credit limit for this credit account into credit_cards.
    Safe for SQLite even if the row doesn't exist yet.
    """
    should_commit = db is None
    db = db or get_db()
    cur = db.execute(
        "UPDATE credit_cards SET credit_limit_cents=? WHERE account_id=?",
        (credit_limit_cents, account_id)
    )
    if cur.rowcount == 0:
        db.execute(
            "INSERT INTO credit_cards (account_id, credit_limit_cents) VALUES (?, ?)",
            (account_id, credit_limit_cents)
        )
    if should_commit:
        db.commit()

def list_recent_charges(account_id: int, limit: int = 200) -> list[dict]:
    db = get_db()
    rows = db.execute(
        "SELECT id, account_id, ttype, amount_cents, posted_at, payee, memo, fitid "
        "FROM transactions WHERE account_id=? "
        "ORDER BY posted_at DESC, id DESC LIMIT ?",
        (account_id, limit)
    ).fetchall()
    return [dict(r) for r in rows]

def list_allocations_for_account(account_id: int) -> list[dict]:
    """
    All allocations tied to this credit account's payment transactions.
    NOTE: Uses cc_payment_allocations (no 'note' column), so we return '' as note for template compatibility.
    """
    db = get_db()
    rows = db.execute("""
        SELECT
            a.id,
            a.payment_tx_id,
            a.envelope_id,
            a.amount_cents,
            '' AS note,                 -- table has no note; keep field for template safety
            e.name AS envelope_name,
            t.posted_at
        FROM cc_payment_allocations a
        JOIN transactions t ON t.id = a.payment_tx_id
        LEFT JOIN envelopes e ON e.id = a.envelope_id
        WHERE t.account_id = ?
        ORDER BY t.posted_at DESC, a.id DESC
    """, (account_id,)).fetchall()
    return [dict(r) for r in rows]

def allocate_payment(payment_tx_id: int, envelope_id: int, amount_cents: int, note: str | None) -> int:
    """
    Insert an allocation. 'note' is accepted for call-site compatibility but not stored (no column in schema).
    """
    db = get_db()
    db.execute(
        "INSERT INTO cc_payment_allocations (payment_tx_id, envelope_id, amount_cents) "
        "VALUES (?, ?, ?)",
        (payment_tx_id, envelope_id, amount_cents)
    )
    db.commit()
    return db.execute("SELECT last_insert_rowid() AS id").fetchone()['id']


def delete_credit_limit(account_id: int, *, db=None) -> None:
    should_commit = db is None
    db = db or get_db()
    db.execute("DELETE FROM credit_cards WHERE account_id=?", (account_id,))
    if should_commit:
        db.commit()
