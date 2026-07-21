from datetime import datetime

from ..db import get_db

def list_envelopes(include_archived: bool = False):
    db = get_db()
    where = "" if include_archived else "WHERE archived_at IS NULL"
    rows = db.execute(f"SELECT * FROM envelopes {where} ORDER BY name").fetchall()
    return [dict(r) for r in rows]

def list_archived_envelopes():
    db = get_db()
    rows = db.execute(
        "SELECT * FROM envelopes WHERE archived_at IS NOT NULL ORDER BY archived_at DESC, name"
    ).fetchall()
    return [dict(r) for r in rows]

def list_envelopes_for_selector(existing_envelope_ids=None):
    """Active envelopes plus archived envelopes already used by the edited item."""
    existing_ids = {
        int(eid)
        for eid in (existing_envelope_ids or [])
        if eid is not None
    }
    if not existing_ids:
        return list_envelopes()

    placeholders = ",".join("?" for _ in existing_ids)
    db = get_db()
    rows = db.execute(
        f"""
        SELECT *
        FROM envelopes
        WHERE archived_at IS NULL OR id IN ({placeholders})
        ORDER BY name
        """,
        tuple(existing_ids),
    ).fetchall()
    return [dict(r) for r in rows]

def get_envelope(envelope_id: int):
    db = get_db()
    row = db.execute("SELECT * FROM envelopes WHERE id=?", (envelope_id,)).fetchone()
    return dict(row) if row else None

def list_active_envelopes_by_name(name: str):
    db = get_db()
    rows = db.execute(
        """
        SELECT *
        FROM envelopes
        WHERE archived_at IS NULL AND name = ?
        ORDER BY locked_account_id IS NOT NULL, locked_account_id, id
        """,
        (name,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_envelope_activity(envelope_id: int):
    """Return split rows affecting one envelope, newest first, with chronological running balance."""
    return get_envelope_activity_for_ids([envelope_id])


def get_envelope_activity_for_ids(envelope_ids):
    """
    Return split rows affecting one or more envelopes, newest first.
    Running balance is still calculated oldest-to-newest so each displayed row shows
    the balance after that transaction.
    """
    ids = [int(eid) for eid in envelope_ids if eid is not None]
    if not ids:
        return []

    placeholders = ",".join("?" for _ in ids)
    db = get_db()
    rows = db.execute(
        f"""
        SELECT
            s.id AS split_id,
            s.envelope_id,
            e.name AS envelope_name,
            s.amount_cents AS split_amount_cents,
            t.id AS transaction_id,
            t.posted_at,
            t.ttype,
            t.payee,
            t.memo,
            t.xfer_pair_id,
            a.id AS account_id,
            a.name AS account_name
        FROM transaction_splits s
        JOIN transactions t ON t.id = s.transaction_id
        JOIN accounts a ON a.id = t.account_id
        JOIN envelopes e ON e.id = s.envelope_id
        WHERE s.envelope_id IN ({placeholders})
        ORDER BY t.posted_at ASC, t.id ASC, s.id ASC
        """,
        tuple(ids),
    ).fetchall()

    running = 0
    activity = []
    for row in rows:
        item = dict(row)
        amount = int(item.get("split_amount_cents") or 0)
        running += amount
        item["running_balance_cents"] = running
        activity.append(item)

    return list(reversed(activity))

UNALLOCATED_ENVELOPE_NAME = "Unallocated"
TRANSFER_CAPABLE_ACCOUNT_TYPES = {"bank", "credit_card", "loan", "investment"}


def account_type_needs_locked_envelope(account_type: str | None) -> bool:
    return (account_type or "bank") in TRANSFER_CAPABLE_ACCOUNT_TYPES


def insert_envelope(data: dict, *, db=None) -> int:
    should_commit = db is None
    db = db or get_db()
    db.execute(
        "INSERT INTO envelopes (name, locked_account_id, default_amount_cents) VALUES (?, ?, ?)",
        (
            data.get('name'),
            data.get('locked_account_id'),
            int(data.get('default_amount_cents') or 0),
        )
    )
    if should_commit:
        db.commit()
    return db.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]


def active_locked_envelope_count(account_id: int, *, db=None) -> int:
    db = db or get_db()
    row = db.execute(
        """
        SELECT COUNT(*) AS count
        FROM envelopes
        WHERE locked_account_id = ? AND archived_at IS NULL
        """,
        (account_id,),
    ).fetchone()
    return int(row["count"] or 0)


def active_locked_unallocated_envelope(account_id: int, *, db=None):
    db = db or get_db()
    row = db.execute(
        """
        SELECT *
        FROM envelopes
        WHERE locked_account_id = ?
          AND archived_at IS NULL
          AND lower(name) = lower(?)
        ORDER BY id
        LIMIT 1
        """,
        (account_id, UNALLOCATED_ENVELOPE_NAME),
    ).fetchone()
    return dict(row) if row else None


def ensure_locked_unallocated_envelope(account_id: int, *, account_type: str | None = None, only_if_no_locked: bool = False, db=None) -> int | None:
    """Ensure a transfer-capable account has a locked Unallocated envelope.

    When only_if_no_locked is true, preserve existing intentional locked-envelope
    setups and create Unallocated only for accounts with zero active locked
    envelopes. The function is idempotent for active locked Unallocated rows.
    """
    if not account_type_needs_locked_envelope(account_type):
        return None

    existing = active_locked_unallocated_envelope(account_id, db=db)
    if existing:
        return int(existing["id"])

    if only_if_no_locked and active_locked_envelope_count(account_id, db=db) > 0:
        return None

    return insert_envelope(
        {
            "name": UNALLOCATED_ENVELOPE_NAME,
            "locked_account_id": account_id,
            "default_amount_cents": 0,
        },
        db=db,
    )

def update_envelope(envelope_id: int, data: dict) -> None:
    fields, values = [], []
    for k in ("name","locked_account_id","default_amount_cents"):
        if k in data:
            fields.append(f"{k}=?")
            values.append(int(data[k] or 0) if k == "default_amount_cents" else data[k])
    if not fields: return
    values.append(envelope_id)
    db = get_db()
    db.execute(f"UPDATE envelopes SET {', '.join(fields)} WHERE id=?", values)
    db.commit()

def delete_envelope(envelope_id: int) -> None:
    archive_envelope(envelope_id)

def archive_envelope(envelope_id: int) -> None:
    db = get_db()
    db.execute(
        """
        UPDATE envelopes
        SET archived_at = COALESCE(archived_at, ?)
        WHERE id=?
        """,
        (datetime.utcnow().isoformat(timespec="seconds"), envelope_id),
    )
    db.commit()

def restore_envelope(envelope_id: int) -> None:
    db = get_db()
    db.execute("UPDATE envelopes SET archived_at = NULL WHERE id=?", (envelope_id,))
    db.commit()
