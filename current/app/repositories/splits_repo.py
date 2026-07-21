from ..db import get_db

def get_splits_for_transaction(tx_id: int) -> list[dict]:
    db = get_db()
    rows = db.execute(
        """
        SELECT
            s.*,
            e.name AS envelope_name,
            e.archived_at AS envelope_archived_at
        FROM transaction_splits s
        LEFT JOIN envelopes e ON e.id = s.envelope_id
        WHERE s.transaction_id=?
        ORDER BY s.id
        """,
        (tx_id,)
    ).fetchall()
    return [dict(r) for r in rows]

def insert_split(
    *,
    db,  # REQUIRED: sqlite3.Connection (from unit_of_work)
    transaction_id: int,
    envelope_id: int,
    amount_cents: int,
) -> int:
    db.execute(
        """
        INSERT INTO transaction_splits (transaction_id, envelope_id, amount_cents)
        VALUES (?, ?, ?)
        """,
        (transaction_id, envelope_id, amount_cents),
    )
    return db.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]

def delete_splits_for_transaction(*, db, tx_id: int) -> None:
    db.execute("DELETE FROM transaction_splits WHERE transaction_id=?", (tx_id,))
