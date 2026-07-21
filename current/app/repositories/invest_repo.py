# app/repositories/invest_repo.py
from datetime import UTC, datetime

from ..db import get_db

def list_valuations(account_id: int) -> list[dict]:
    db = get_db()
    rows = db.execute(
        "SELECT id, account_id, asof_date, value_cents, source, note "
        "FROM investment_valuations WHERE account_id=? "
        "ORDER BY asof_date DESC, id DESC",
        (account_id,)
    ).fetchall()
    return [dict(r) for r in rows]

def get_valuation(valuation_id: int, *, account_id: int | None = None) -> dict | None:
    db = get_db()
    sql = "SELECT id, account_id, asof_date, value_cents, source, note FROM investment_valuations WHERE id=?"
    params: list[int] = [valuation_id]
    if account_id is not None:
        sql += " AND account_id=?"
        params.append(account_id)
    row = db.execute(sql, params).fetchone()
    return dict(row) if row else None

def insert_valuation(data: dict, *, db=None) -> int:
    should_commit = db is None
    db = db or get_db()
    db.execute(
        "INSERT INTO investment_valuations (account_id, asof_date, value_cents, source, note) "
        "VALUES (?, ?, ?, 'manual', ?)",
        (data['account_id'], data['asof_date'], data['value_cents'], data.get('note'))
    )
    if should_commit:
        db.commit()
    return db.execute("SELECT last_insert_rowid() AS id").fetchone()['id']

def update_valuation(valuation_id: int, data: dict) -> None:
    db = get_db()
    db.execute(
        """
        UPDATE investment_valuations
        SET asof_date=?, value_cents=?, note=?
        WHERE id=? AND account_id=?
        """,
        (
            data["asof_date"],
            data["value_cents"],
            data.get("note"),
            valuation_id,
            data["account_id"],
        ),
    )
    db.commit()

def delete_valuation(valuation_id: int) -> None:
    db = get_db()
    db.execute("DELETE FROM investment_valuations WHERE id=?", (valuation_id,))
    db.commit()

def list_notes(account_id: int) -> list[dict]:
    db = get_db()
    rows = db.execute(
        """
        SELECT id, account_id, note_date, body, created_at, updated_at
        FROM investment_notes
        WHERE account_id=?
        ORDER BY note_date DESC, id DESC
        """,
        (account_id,),
    ).fetchall()
    return [dict(r) for r in rows]

def get_note(note_id: int, *, account_id: int | None = None) -> dict | None:
    db = get_db()
    sql = """
        SELECT id, account_id, note_date, body, created_at, updated_at
        FROM investment_notes
        WHERE id=?
    """
    params: list[int] = [note_id]
    if account_id is not None:
        sql += " AND account_id=?"
        params.append(account_id)
    row = db.execute(sql, params).fetchone()
    return dict(row) if row else None

def insert_note(data: dict) -> int:
    db = get_db()
    now = datetime.now(UTC).isoformat(timespec="seconds")
    db.execute(
        """
        INSERT INTO investment_notes (account_id, note_date, body, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (data["account_id"], data["note_date"], data["body"], now, now),
    )
    db.commit()
    return db.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]

def update_note(note_id: int, data: dict) -> None:
    db = get_db()
    now = datetime.now(UTC).isoformat(timespec="seconds")
    db.execute(
        """
        UPDATE investment_notes
        SET note_date=?, body=?, updated_at=?
        WHERE id=? AND account_id=?
        """,
        (data["note_date"], data["body"], now, note_id, data["account_id"]),
    )
    db.commit()

def delete_note(note_id: int, *, account_id: int) -> None:
    db = get_db()
    db.execute("DELETE FROM investment_notes WHERE id=? AND account_id=?", (note_id, account_id))
    db.commit()

def get_latest_valuation_cents(account_id: int) -> int | None:
    db = get_db()
    row = db.execute("""
        SELECT value_cents
        FROM investment_valuations
        WHERE account_id = ?
        ORDER BY asof_date DESC, id DESC
        LIMIT 1
    """, (account_id,)).fetchone()
    return int(row["value_cents"]) if row else None

def get_latest_valuation_summary(account_id: int) -> dict | None:
    db = get_db()
    row = db.execute("""
        SELECT asof_date, value_cents
        FROM investment_valuations
        WHERE account_id = ?
        ORDER BY asof_date DESC, id DESC
        LIMIT 1
    """, (account_id,)).fetchone()
    if not row:
        return None
    return {"asof_date": row["asof_date"], "value_cents": int(row["value_cents"])}
