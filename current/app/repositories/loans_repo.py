from ..db import get_db

def get_loan(account_id: int) -> dict | None:
    db = get_db()
    row = db.execute("SELECT * FROM loans WHERE account_id=?", (account_id,)).fetchone()
    return dict(row) if row else None


def upsert_loan_details(
    account_id: int,
    *,
    original_principal_cents: int | None = None,
    normal_monthly_payment_cents: int | None = None,
    note: str | None = None,
    db=None,
) -> None:
    should_commit = db is None
    db = db or get_db()
    db.execute(
        """
        INSERT INTO loans(account_id, original_principal_cents, normal_monthly_payment_cents, note)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(account_id) DO UPDATE SET
            original_principal_cents=excluded.original_principal_cents,
            normal_monthly_payment_cents=excluded.normal_monthly_payment_cents,
            note=COALESCE(excluded.note, loans.note)
        """,
        (account_id, original_principal_cents, normal_monthly_payment_cents, note),
    )
    if should_commit:
        db.commit()

def parts_by_payment_tx_ids(payment_tx_ids: list[int]) -> list[dict]:
    if not payment_tx_ids:
        return []
    db = get_db()
    placeholders = ",".join(["?"] * len(payment_tx_ids))
    rows = db.execute(
        f"SELECT id, payment_tx_id, part_type, amount_cents, note "
        f"FROM loan_payment_parts WHERE payment_tx_id IN ({placeholders})",
        payment_tx_ids
    ).fetchall()
    return [dict(r) for r in rows]

def replace_parts(payment_tx_id: int, parts: list[tuple[str, int]], note: str | None) -> None:
    db = get_db()
    db.execute("DELETE FROM loan_payment_parts WHERE payment_tx_id=?", (payment_tx_id,))
    for part_type, amount_cents in parts:
        if amount_cents and amount_cents != 0:
            db.execute(
                "INSERT INTO loan_payment_parts (payment_tx_id, part_type, amount_cents, note) "
                "VALUES (?, ?, ?, ?)",
                (payment_tx_id, part_type, amount_cents, note)
            )
    db.commit()
