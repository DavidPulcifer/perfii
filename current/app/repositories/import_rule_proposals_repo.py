from __future__ import annotations

from datetime import datetime
import json
from typing import Any

from ..db import get_db, table_exists


PROPOSAL_STATUSES = {"pending", "accepted", "rejected", "ignored"}
STALE_REVIEWER_DECISION = "stale_source_changed"
STALE_VALIDATION_MESSAGE = (
    "Proposal source evidence was not present in the latest refresh. "
    "Refresh again after new matching activity before approving."
)
JSON_FIELDS = {
    "condition_json",
    "action_json",
    "suggested_rule_json",
    "evidence_json",
    "reason_codes_json",
    "validation_errors_json",
}


def _now() -> str:
    return datetime.utcnow().isoformat(timespec="seconds")


def _json_text(value: Any, default: Any = None) -> str:
    if value is None:
        value = default if default is not None else {}
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _decode_json(value: str | None, default: Any) -> Any:
    try:
        decoded = json.loads(value or "")
    except (TypeError, json.JSONDecodeError):
        return default
    return decoded


def _row_dict(row) -> dict[str, Any]:
    item = dict(row)
    item["condition_json"] = _decode_json(item.get("condition_json"), {})
    item["action_json"] = _decode_json(item.get("action_json"), {})
    item["suggested_rule_json"] = _decode_json(item.get("suggested_rule_json"), {})
    item["evidence_json"] = _decode_json(item.get("evidence_json"), {})
    item["reason_codes_json"] = _decode_json(item.get("reason_codes_json"), [])
    item["validation_errors_json"] = _decode_json(item.get("validation_errors_json"), [])
    return item


def import_rule_proposals_available(db=None) -> bool:
    db = db or get_db()
    return table_exists(db, "import_rule_proposals")


def list_import_rule_proposals(
    *,
    account_id: int | None = None,
    status: str | None = None,
    include_decided: bool = True,
) -> list[dict[str, Any]]:
    db = get_db()
    if not import_rule_proposals_available(db):
        return []
    clauses: list[str] = []
    params: list[Any] = []
    if account_id is not None:
        clauses.append("account_id = ?")
        params.append(int(account_id))
    if status:
        clauses.append("status = ?")
        params.append(str(status))
    elif not include_decided:
        clauses.append("status = 'pending'")
    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = db.execute(
        f"""
        SELECT id, fingerprint, candidate_key, account_id, status, condition_json,
               action_json, suggested_rule_json, evidence_json, reason_codes_json,
               reviewer_decision, reviewer_note, approved_rule_id,
               validation_errors_json, created_at, updated_at, last_seen_at, reviewed_at
        FROM import_rule_proposals
        {where_sql}
        ORDER BY
            CASE status
                WHEN 'pending' THEN 0
                WHEN 'accepted' THEN 1
                WHEN 'rejected' THEN 2
                ELSE 3
            END,
            updated_at DESC,
            id DESC
        """,
        params,
    ).fetchall()
    return [_row_dict(row) for row in rows]


def get_import_rule_proposal(proposal_id: int) -> dict[str, Any] | None:
    db = get_db()
    if not import_rule_proposals_available(db):
        return None
    row = db.execute(
        """
        SELECT id, fingerprint, candidate_key, account_id, status, condition_json,
               action_json, suggested_rule_json, evidence_json, reason_codes_json,
               reviewer_decision, reviewer_note, approved_rule_id,
               validation_errors_json, created_at, updated_at, last_seen_at, reviewed_at
        FROM import_rule_proposals
        WHERE id = ?
        """,
        (int(proposal_id),),
    ).fetchone()
    return _row_dict(row) if row else None


def get_import_rule_proposal_by_fingerprint(fingerprint: str) -> dict[str, Any] | None:
    db = get_db()
    if not import_rule_proposals_available(db):
        return None
    row = db.execute(
        """
        SELECT id, fingerprint, candidate_key, account_id, status, condition_json,
               action_json, suggested_rule_json, evidence_json, reason_codes_json,
               reviewer_decision, reviewer_note, approved_rule_id,
               validation_errors_json, created_at, updated_at, last_seen_at, reviewed_at
        FROM import_rule_proposals
        WHERE fingerprint = ?
        """,
        (str(fingerprint),),
    ).fetchone()
    return _row_dict(row) if row else None


def upsert_import_rule_proposal(data: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    db = get_db()
    if not import_rule_proposals_available(db):
        return {}, False
    fingerprint = str(data.get("fingerprint") or data.get("candidate_key") or "").strip()
    if not fingerprint:
        return {}, False
    now = _now()
    existing = get_import_rule_proposal_by_fingerprint(fingerprint)
    if existing:
        update_payload = {
            "candidate_key": str(data.get("candidate_key") or fingerprint),
            "account_id": data.get("account_id"),
            "condition_json": data.get("condition_json") or {},
            "action_json": data.get("action_json") or {},
            "suggested_rule_json": data.get("suggested_rule_json") or {},
            "evidence_json": data.get("evidence_json") or {},
            "reason_codes_json": data.get("reason_codes_json") or [],
        }
        db.execute(
            """
            UPDATE import_rule_proposals
            SET candidate_key = ?,
                account_id = ?,
                condition_json = CASE WHEN status = 'pending' THEN ? ELSE condition_json END,
                action_json = CASE WHEN status = 'pending' THEN ? ELSE action_json END,
                suggested_rule_json = CASE WHEN status = 'pending' THEN ? ELSE suggested_rule_json END,
                evidence_json = ?,
                reason_codes_json = ?,
                reviewer_decision = CASE
                    WHEN status = 'pending' AND reviewer_decision = ? THEN NULL
                    ELSE reviewer_decision
                END,
                validation_errors_json = CASE
                    WHEN status = 'pending' AND reviewer_decision = ? THEN '[]'
                    ELSE validation_errors_json
                END,
                updated_at = ?,
                last_seen_at = ?
            WHERE fingerprint = ?
            """,
            (
                update_payload["candidate_key"],
                update_payload["account_id"],
                _json_text(update_payload["condition_json"]),
                _json_text(update_payload["action_json"]),
                _json_text(update_payload["suggested_rule_json"]),
                _json_text(update_payload["evidence_json"]),
                _json_text(update_payload["reason_codes_json"], []),
                STALE_REVIEWER_DECISION,
                STALE_REVIEWER_DECISION,
                now,
                now,
                fingerprint,
            ),
        )
        db.commit()
        return get_import_rule_proposal(existing["id"]) or existing, False

    cur = db.execute(
        """
        INSERT INTO import_rule_proposals(
            fingerprint, candidate_key, account_id, status, condition_json,
            action_json, suggested_rule_json, evidence_json, reason_codes_json,
            validation_errors_json, created_at, updated_at, last_seen_at
        ) VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, ?, '[]', ?, ?, ?)
        """,
        (
            fingerprint,
            str(data.get("candidate_key") or fingerprint),
            data.get("account_id"),
            _json_text(data.get("condition_json") or {}),
            _json_text(data.get("action_json") or {}),
            _json_text(data.get("suggested_rule_json") or {}),
            _json_text(data.get("evidence_json") or {}),
            _json_text(data.get("reason_codes_json") or [], []),
            now,
            now,
            now,
        ),
    )
    db.commit()
    return get_import_rule_proposal(int(cur.lastrowid)) or {}, True


def mark_missing_import_rule_proposals_stale(
    *,
    account_id: int,
    seen_fingerprints: set[str],
) -> int:
    """Mark current account proposals stale when a successful refresh no longer finds them."""
    db = get_db()
    if not import_rule_proposals_available(db):
        return 0
    seen = {str(value) for value in seen_fingerprints or set() if str(value or "").strip()}
    rows = db.execute(
        """
        SELECT id, fingerprint, status, evidence_json, validation_errors_json, reviewer_decision
        FROM import_rule_proposals
        WHERE account_id = ?
        """,
        (int(account_id),),
    ).fetchall()

    count = 0
    now = _now()
    for row in rows:
        item = _row_dict(row)
        if item["fingerprint"] in seen:
            continue
        evidence = item.get("evidence_json") if isinstance(item.get("evidence_json"), dict) else {}
        if evidence.get("refresh_status") == "stale_source_changed":
            continue
        stale_evidence = dict(evidence or {})
        stale_evidence["refresh_status"] = "stale_source_changed"
        stale_evidence["stale_reason"] = "Candidate did not appear in the latest successful proposal refresh."
        stale_evidence["last_known_support_examples"] = evidence.get("support_examples", 0)

        reviewer_decision = item.get("reviewer_decision")
        validation_errors = item.get("validation_errors_json") or []
        if item.get("status") == "pending":
            reviewer_decision = STALE_REVIEWER_DECISION
            validation_errors = [STALE_VALIDATION_MESSAGE]

        db.execute(
            """
            UPDATE import_rule_proposals
            SET evidence_json = ?,
                reviewer_decision = ?,
                validation_errors_json = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                _json_text(stale_evidence),
                reviewer_decision,
                _json_text(validation_errors, []),
                now,
                int(item["id"]),
            ),
        )
        count += 1
    if count:
        db.commit()
    return count


def mark_import_rule_proposal_decision(
    proposal_id: int,
    *,
    status: str,
    reviewer_decision: str,
    approved_rule_id: int | None = None,
    reviewer_note: str | None = None,
    validation_errors: list[str] | None = None,
) -> bool:
    if status not in PROPOSAL_STATUSES:
        return False
    db = get_db()
    if not import_rule_proposals_available(db):
        return False
    now = _now()
    cur = db.execute(
        """
        UPDATE import_rule_proposals
        SET status = ?,
            reviewer_decision = ?,
            reviewer_note = ?,
            approved_rule_id = ?,
            validation_errors_json = ?,
            reviewed_at = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (
            status,
            str(reviewer_decision or "").strip() or None,
            str(reviewer_note or "").strip() or None,
            approved_rule_id,
            _json_text(validation_errors or [], []),
            now,
            now,
            int(proposal_id),
        ),
    )
    db.commit()
    return cur.rowcount > 0


def record_import_rule_proposal_validation_error(proposal_id: int, errors: list[str]) -> bool:
    db = get_db()
    if not import_rule_proposals_available(db):
        return False
    cur = db.execute(
        """
        UPDATE import_rule_proposals
        SET reviewer_decision = 'approval_failed',
            validation_errors_json = ?,
            reviewed_at = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (_json_text(errors or [], []), _now(), _now(), int(proposal_id)),
    )
    db.commit()
    return cur.rowcount > 0
