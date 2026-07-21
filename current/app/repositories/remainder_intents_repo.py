from __future__ import annotations

from datetime import datetime
from typing import Any

from ..db import get_db, table_exists


def replace_remainder_intent(
    *,
    db,
    transaction_id: int,
    envelope_id: int | None = None,
    amount_cents: int | None = None,
) -> None:
    """Replace the stored remainder intent for one transaction.

    The authoritative split rows remain in transaction_splits. This table only
    records the user intent that one envelope was chosen as the computed
    remainder bucket, so import-prefill history can distinguish explicit split
    amounts from a variable leftover amount.
    """
    tx_id = int(transaction_id)
    if not table_exists(db, "transaction_remainder_intents"):
        return

    db.execute("DELETE FROM transaction_remainder_intents WHERE transaction_id=?", (tx_id,))
    if not envelope_id:
        return

    now = datetime.utcnow().isoformat(timespec="seconds")
    db.execute(
        """
        INSERT INTO transaction_remainder_intents (
            transaction_id, envelope_id, amount_cents, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (tx_id, int(envelope_id), int(amount_cents or 0), now, now),
    )


def get_remainder_intent(transaction_id: int) -> dict[str, Any] | None:
    db = get_db()
    if not table_exists(db, "transaction_remainder_intents"):
        return None

    row = db.execute(
        """
        SELECT transaction_id, envelope_id, amount_cents, created_at, updated_at
        FROM transaction_remainder_intents
        WHERE transaction_id=?
        """,
        (int(transaction_id),),
    ).fetchone()
    return dict(row) if row else None
