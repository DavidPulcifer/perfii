from __future__ import annotations

from datetime import datetime
import json
from typing import Any

from ..db import get_db, table_exists


def _now() -> str:
    return datetime.utcnow().isoformat(timespec="seconds")


def _json_text(value: dict[str, Any] | None) -> str:
    return json.dumps(value or {}, sort_keys=True, separators=(",", ":"))


def _decode_json(value: str | None) -> dict[str, Any]:
    try:
        decoded = json.loads(value or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _row_dict(row) -> dict[str, Any]:
    item = dict(row)
    item["condition_json"] = _decode_json(item.get("condition_json"))
    item["action_json"] = _decode_json(item.get("action_json"))
    return item


def import_matching_rules_available(db=None) -> bool:
    db = db or get_db()
    return table_exists(db, "import_matching_rules")


def list_import_matching_rules(*, account_id: int | None = None, include_disabled: bool = True) -> list[dict[str, Any]]:
    db = get_db()
    if not import_matching_rules_available(db):
        return []

    clauses: list[str] = []
    params: list[Any] = []
    if account_id is not None:
        clauses.append("(account_id IS NULL OR account_id = ?)")
        params.append(int(account_id))
    if not include_disabled:
        clauses.append("enabled = 1")
    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""

    rows = db.execute(
        f"""
        SELECT id, account_id, name, enabled, priority, condition_json, action_json,
               use_count, created_at, updated_at, last_used_at
        FROM import_matching_rules
        {where_sql}
        ORDER BY priority ASC, id ASC
        """,
        params,
    ).fetchall()
    return [_row_dict(row) for row in rows]


def get_import_matching_rule(rule_id: int) -> dict[str, Any] | None:
    db = get_db()
    if not import_matching_rules_available(db):
        return None
    row = db.execute(
        """
        SELECT id, account_id, name, enabled, priority, condition_json, action_json,
               use_count, created_at, updated_at, last_used_at
        FROM import_matching_rules
        WHERE id = ?
        """,
        (int(rule_id),),
    ).fetchone()
    return _row_dict(row) if row else None


def create_import_matching_rule(data: dict[str, Any]) -> int | None:
    db = get_db()
    if not import_matching_rules_available(db):
        return None
    now = _now()
    cur = db.execute(
        """
        INSERT INTO import_matching_rules(
            account_id, name, enabled, priority, condition_json, action_json,
            use_count, created_at, updated_at, last_used_at
        ) VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, NULL)
        """,
        (
            data.get("account_id"),
            str(data.get("name") or "").strip(),
            1 if data.get("enabled", True) else 0,
            int(data.get("priority") or 100),
            _json_text(data.get("condition_json")),
            _json_text(data.get("action_json")),
            now,
            now,
        ),
    )
    db.commit()
    return int(cur.lastrowid or 0) or None


def update_import_matching_rule(rule_id: int, data: dict[str, Any]) -> bool:
    db = get_db()
    if not import_matching_rules_available(db):
        return False
    cur = db.execute(
        """
        UPDATE import_matching_rules
        SET account_id = ?,
            name = ?,
            enabled = ?,
            priority = ?,
            condition_json = ?,
            action_json = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (
            data.get("account_id"),
            str(data.get("name") or "").strip(),
            1 if data.get("enabled", True) else 0,
            int(data.get("priority") or 100),
            _json_text(data.get("condition_json")),
            _json_text(data.get("action_json")),
            _now(),
            int(rule_id),
        ),
    )
    db.commit()
    return cur.rowcount > 0


def delete_import_matching_rule(rule_id: int) -> bool:
    db = get_db()
    if not import_matching_rules_available(db):
        return False
    cur = db.execute("DELETE FROM import_matching_rules WHERE id = ?", (int(rule_id),))
    db.commit()
    return cur.rowcount > 0


def record_import_matching_rule_use(rule_ids: list[int]) -> None:
    ids = [int(rule_id) for rule_id in rule_ids if rule_id]
    if not ids:
        return
    db = get_db()
    if not import_matching_rules_available(db):
        return
    now = _now()
    for rule_id in sorted(set(ids)):
        db.execute(
            """
            UPDATE import_matching_rules
            SET use_count = use_count + 1,
                updated_at = ?,
                last_used_at = ?
            WHERE id = ?
            """,
            (now, now, rule_id),
        )
    db.commit()
