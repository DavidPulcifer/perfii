from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

from app.repositories import transaction_learning_repo as learning_repo
from app.services.transaction_text_profile_service import build_transaction_text_profile_from_row


EXAMPLE_SOURCE_FOR_EVENT = {
    "import_commit": "import_commit",
    "manual_match": "manual_match",
    "transaction_edit": "transaction_edit",
    "split_edit": "split_edit",
    "remainder_intent_change": "split_edit",
    "transfer_edit": "transfer_edit",
    "transfer_conversion": "transfer_edit",
    "manual_entry": "manual_entry",
}


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def snapshot_transaction(db, transaction_id: int) -> dict[str, Any] | None:
    return learning_repo.fetch_transaction_snapshot(db, int(transaction_id))


def record_transaction_write_event(
    db,
    *,
    transaction_id: int,
    source: str,
    event_type: str | None = None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    raw_evidence: dict[str, Any] | None = None,
    dedupe_key: str | None = None,
    now: str | None = None,
) -> int | None:
    if not learning_repo.learning_tables_available(db):
        return None
    now = now or utc_now()
    event_type = event_type or source
    before = before or {}
    after = after or snapshot_transaction(db, int(transaction_id)) or {}
    if before == after:
        return None

    raw_evidence = raw_evidence or learning_repo.latest_raw_import_evidence(db, int(transaction_id))
    quality = evidence_quality(raw_evidence)
    example = build_learning_example(
        transaction_id=int(transaction_id),
        source=EXAMPLE_SOURCE_FOR_EVENT.get(source, "transaction_edit"),
        source_action=source,
        after=after,
        raw_evidence=raw_evidence,
        evidence_quality=quality,
        dedupe_key=dedupe_key,
        now=now,
    )
    example_id = learning_repo.insert_learning_example(db, example)
    return learning_repo.insert_learning_event(
        db,
        {
            "learning_example_id": example_id,
            "transaction_id": int(transaction_id),
            "event_type": event_type,
            "source": source,
            "evidence_quality": quality,
            "before_json": learning_repo.compact_json(before),
            "after_json": learning_repo.compact_json(after),
            "raw_evidence_json": learning_repo.compact_json(raw_evidence),
            "created_at": now,
        },
    )


def record_import_session_learning_events(db, *, session_id: int, now: str | None = None) -> int:
    if not session_id or not learning_repo.learning_tables_available(db):
        return 0
    now = now or utc_now()
    rows = db.execute(
        """
        SELECT
            r.id AS import_session_row_id,
            m.transaction_id,
            m.match_type
        FROM import_session_rows r
        JOIN import_row_matches m ON m.row_id = r.id
        JOIN import_sessions s ON s.id = r.session_id
        JOIN transactions t ON t.id = m.transaction_id
        WHERE r.session_id=?
          AND m.transaction_id IS NOT NULL
          AND t.account_id = s.account_id
        ORDER BY r.id, m.id
        """,
        (int(session_id),),
    ).fetchall()

    recorded = 0
    seen: set[tuple[int, int]] = set()
    for row in rows:
        row_id = int(row["import_session_row_id"])
        tx_id = int(row["transaction_id"])
        key = (row_id, tx_id)
        if key in seen:
            continue
        seen.add(key)

        source = "manual_match" if row["match_type"] == "manual_match" else "import_commit"
        after = snapshot_transaction(db, tx_id)
        if not after:
            continue
        event_id = record_transaction_write_event(
            db,
            transaction_id=tx_id,
            source=source,
            event_type=source,
            before={},
            after=after,
            raw_evidence=learning_repo.latest_raw_import_evidence(db, tx_id),
            dedupe_key=f"{source}:row:{row_id}:tx:{tx_id}",
            now=now,
        )
        if event_id:
            recorded += 1
    return recorded


def record_prediction_feedback_rows(
    db,
    feedback_rows: list[dict[str, Any]],
    *,
    now: str | None = None,
) -> int:
    if not feedback_rows or not learning_repo.prediction_feedback_available(db):
        return 0
    now = now or utc_now()
    recorded = 0
    for row in feedback_rows:
        feedback = dict(row)
        feedback.setdefault("created_at", now)
        if learning_repo.insert_prediction_feedback(db, feedback):
            recorded += 1
    return recorded


def classify_standard_edit(before: dict[str, Any] | None, after: dict[str, Any] | None) -> tuple[str, str]:
    if not before or not after:
        return "transaction_edit", "transaction_edit"
    before_tx = before.get("transaction") or {}
    after_tx = after.get("transaction") or {}
    core_fields = (
        "account_id",
        "ttype",
        "amount_cents",
        "posted_at",
        "payee",
        "memo",
        "fitid",
        "ignore_match",
        "external_counterparty",
    )
    core_changed = any(before_tx.get(field) != after_tx.get(field) for field in core_fields)
    splits_changed = _split_signature(before.get("splits") or []) != _split_signature(after.get("splits") or [])
    remainder_changed = (before.get("remainder_intent") or {}) != (after.get("remainder_intent") or {})

    if core_changed:
        return "transaction_edit", "transaction_edit"
    if splits_changed:
        return "split_edit", "split_edit"
    if remainder_changed:
        return "remainder_intent_change", "remainder_intent_change"
    return "transaction_edit", "transaction_edit"


def evidence_quality(raw_evidence: dict[str, Any] | None) -> str:
    if not raw_evidence:
        return "low"
    has_raw_text = bool((raw_evidence.get("raw_payee") or "").strip() or (raw_evidence.get("raw_memo") or "").strip())
    if raw_evidence.get("transaction_import_validation_id") and has_raw_text:
        return "high"
    if raw_evidence.get("import_session_row_id") or (raw_evidence.get("import_row_match") or {}).get("id"):
        return "medium"
    return "low"


def _split_signature(splits: list[dict[str, Any]]) -> list[tuple[int, int]]:
    return [
        (int(split.get("envelope_id") or 0), int(split.get("amount_cents") or 0))
        for split in splits
    ]


def build_learning_example(
    *,
    transaction_id: int,
    source: str,
    source_action: str,
    after: dict[str, Any],
    raw_evidence: dict[str, Any] | None,
    evidence_quality: str,
    dedupe_key: str | None,
    now: str,
) -> dict[str, Any]:
    tx = dict((after or {}).get("transaction") or {})
    splits = list((after or {}).get("splits") or [])
    remainder_intent = dict((after or {}).get("remainder_intent") or {})
    raw_evidence = raw_evidence or {}
    raw_payee = raw_evidence.get("raw_payee")
    raw_memo = raw_evidence.get("raw_memo")
    final_payee = tx.get("payee")
    final_memo = tx.get("memo")
    raw_profile = asdict(build_transaction_text_profile_from_row({"payee": raw_payee, "memo": raw_memo}))
    final_profile = asdict(build_transaction_text_profile_from_row({"payee": final_payee, "memo": final_memo}))
    transaction_type = tx.get("ttype")
    decision = {
        "kind": transaction_kind(transaction_type),
        "transaction_type": transaction_type,
        "transfer_other_account_id": tx.get("transfer_other_account_id"),
        "transfer_pair_id": tx.get("xfer_pair_id"),
        "external_counterparty": tx.get("external_counterparty"),
        "split_count": len(splits),
        "has_remainder_intent": bool(remainder_intent),
        "source_action": source_action,
    }

    return {
        "account_id": int(tx["account_id"]),
        "transaction_id": int(transaction_id),
        "import_session_row_id": raw_evidence.get("import_session_row_id"),
        "transaction_import_validation_id": raw_evidence.get("transaction_import_validation_id"),
        "source": source,
        "evidence_quality": evidence_quality,
        "dedupe_key": dedupe_key,
        "posted_at": tx.get("posted_at"),
        "amount_cents": tx.get("amount_cents"),
        "raw_payee": raw_payee,
        "raw_memo": raw_memo,
        "raw_profile_json": learning_repo.compact_json(raw_profile),
        "final_payee": final_payee,
        "final_memo": final_memo,
        "final_profile_json": learning_repo.compact_json(final_profile),
        "transaction_type": transaction_type,
        "transfer_other_account_id": tx.get("transfer_other_account_id"),
        "splits_json": learning_repo.compact_json(splits),
        "remainder_intent_json": learning_repo.compact_json(remainder_intent),
        "decision_json": learning_repo.compact_json(decision),
        "evidence_json": learning_repo.compact_json(raw_evidence),
        "created_at": now,
        "updated_at": now,
    }


def transaction_kind(ttype: str | None) -> str | None:
    if not ttype:
        return None
    return "transfer" if str(ttype).startswith("transfer_") else str(ttype)
