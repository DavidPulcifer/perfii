from __future__ import annotations

from datetime import datetime, timezone

from ..db import get_db


PLAN_ID = 1


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_plan() -> dict | None:
    row = get_db().execute(
        "SELECT * FROM savings_plans WHERE id=?",
        (PLAN_ID,),
    ).fetchone()
    return dict(row) if row else None


def save_plan(*, name: str, source_account_id: int, source_envelope_id: int) -> dict:
    db = get_db()
    now = _now()
    db.execute(
        """
        INSERT INTO savings_plans(
            id, name, source_account_id, source_envelope_id, created_at, updated_at
        ) VALUES(?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            name=excluded.name,
            source_account_id=excluded.source_account_id,
            source_envelope_id=excluded.source_envelope_id,
            updated_at=excluded.updated_at
        """,
        (PLAN_ID, name, source_account_id, source_envelope_id, now, now),
    )
    db.commit()
    return get_plan() or {}


def list_rules(*, enabled_only: bool = False) -> list[dict]:
    where = "WHERE plan_id=?"
    params: list[object] = [PLAN_ID]
    if enabled_only:
        where += " AND enabled=1"
    rows = get_db().execute(
        f"""
        SELECT *
        FROM savings_rules
        {where}
        ORDER BY display_order, id
        """,
        tuple(params),
    ).fetchall()
    return [dict(row) for row in rows]


def get_rule(rule_id: int) -> dict | None:
    row = get_db().execute(
        "SELECT * FROM savings_rules WHERE id=? AND plan_id=?",
        (int(rule_id), PLAN_ID),
    ).fetchone()
    return dict(row) if row else None


def insert_rule(data: dict, *, db=None) -> int:
    active_db = db or get_db()
    now = _now()
    order_row = active_db.execute(
        "SELECT COALESCE(MAX(display_order), 0) + 1 AS next_order FROM savings_rules WHERE plan_id=?",
        (PLAN_ID,),
    ).fetchone()
    cursor = active_db.execute(
        """
        INSERT INTO savings_rules(
            plan_id, name, contribution_basis_points,
            accessible_account_id, accessible_envelope_id,
            long_term_account_id, long_term_envelope_id,
            accessible_target_cents, enabled, display_order,
            created_at, updated_at
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            PLAN_ID,
            data["name"],
            int(data["contribution_basis_points"]),
            int(data["accessible_account_id"]),
            int(data["accessible_envelope_id"]),
            data.get("long_term_account_id"),
            data.get("long_term_envelope_id"),
            int(data.get("accessible_target_cents") or 0),
            1 if data.get("enabled", True) else 0,
            int(order_row["next_order"] if order_row else 1),
            now,
            now,
        ),
    )
    if db is None:
        active_db.commit()
    return int(cursor.lastrowid)


def update_rule(rule_id: int, data: dict, *, db=None) -> None:
    active_db = db or get_db()
    active_db.execute(
        """
        UPDATE savings_rules
        SET name=?, contribution_basis_points=?,
            accessible_account_id=?, accessible_envelope_id=?,
            long_term_account_id=?, long_term_envelope_id=?,
            accessible_target_cents=?, enabled=?, updated_at=?
        WHERE id=? AND plan_id=?
        """,
        (
            data["name"],
            int(data["contribution_basis_points"]),
            int(data["accessible_account_id"]),
            int(data["accessible_envelope_id"]),
            data.get("long_term_account_id"),
            data.get("long_term_envelope_id"),
            int(data.get("accessible_target_cents") or 0),
            1 if data.get("enabled", True) else 0,
            _now(),
            int(rule_id),
            PLAN_ID,
        ),
    )
    if db is None:
        active_db.commit()


def delete_rule(rule_id: int) -> None:
    db = get_db()
    db.execute(
        "DELETE FROM savings_rules WHERE id=? AND plan_id=?",
        (int(rule_id), PLAN_ID),
    )
    db.commit()


def account_dependencies(account_id: int) -> list[str]:
    """Return savings settings that must be reassigned before account deletion."""
    db = get_db()
    dependencies: list[str] = []
    if db.execute(
        "SELECT 1 FROM savings_plans WHERE source_account_id=? LIMIT 1",
        (int(account_id),),
    ).fetchone():
        dependencies.append("the paycheck source")
    if db.execute(
        "SELECT 1 FROM savings_rules WHERE accessible_account_id=? LIMIT 1",
        (int(account_id),),
    ).fetchone():
        dependencies.append("an accessible-savings destination")
    if db.execute(
        "SELECT 1 FROM savings_rules WHERE long_term_account_id=? LIMIT 1",
        (int(account_id),),
    ).fetchone():
        dependencies.append("a long-term savings destination")
    return dependencies


def has_transfer_record(idempotency_key: str) -> bool:
    return get_db().execute(
        "SELECT 1 FROM savings_transfer_records WHERE idempotency_key=?",
        (str(idempotency_key),),
    ).fetchone() is not None


def recorded_transfer_keys(idempotency_keys: list[str]) -> set[str]:
    keys = [str(value) for value in idempotency_keys if value]
    if not keys:
        return set()
    placeholders = ",".join("?" for _ in keys)
    rows = get_db().execute(
        f"SELECT idempotency_key FROM savings_transfer_records WHERE idempotency_key IN ({placeholders})",
        tuple(keys),
    ).fetchall()
    return {str(row["idempotency_key"]) for row in rows}


def reserve_transfer_record(
    *,
    db,
    idempotency_key: str,
    group_index: int,
) -> bool:
    """Reserve a preview group inside the caller's unit of work."""
    cursor = db.execute(
        """
        INSERT OR IGNORE INTO savings_transfer_records(
            idempotency_key, plan_id, group_index, created_at
        ) VALUES(?, ?, ?, ?)
        """,
        (str(idempotency_key), PLAN_ID, int(group_index), _now()),
    )
    return int(cursor.rowcount or 0) == 1


def complete_transfer_record(
    *,
    db,
    idempotency_key: str,
    tx_out_id: int,
    tx_in_id: int,
) -> None:
    cursor = db.execute(
        """
        UPDATE savings_transfer_records
        SET tx_out_id=?, tx_in_id=?
        WHERE idempotency_key=?
        """,
        (int(tx_out_id), int(tx_in_id), str(idempotency_key)),
    )
    if int(cursor.rowcount or 0) != 1:
        raise RuntimeError("Savings transfer idempotency record was not reserved.")
