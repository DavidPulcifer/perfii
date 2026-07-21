from __future__ import annotations

import json
from typing import Any

from ..db import table_exists


def compact_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def parse_json_object(value: Any) -> dict[str, Any]:
    if not value:
        return {}
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(str(value))
    except (TypeError, json.JSONDecodeError):
        return {"unparsed": str(value)}
    return parsed if isinstance(parsed, dict) else {"value": parsed}


def learning_tables_available(db) -> bool:
    return table_exists(db, "transaction_learning_examples") and table_exists(db, "transaction_learning_events")


def prediction_feedback_available(db) -> bool:
    return table_exists(db, "prediction_feedback")


def fetch_transaction_snapshot(db, transaction_id: int) -> dict[str, Any] | None:
    row = db.execute(
        """
        SELECT
            t.id, t.account_id, t.ttype, t.amount_cents, t.posted_at,
            t.payee, t.memo, t.fitid, t.ignore_match, t.xfer_pair_id,
            t.external_counterparty,
            pair.account_id AS transfer_other_account_id
        FROM transactions t
        LEFT JOIN transactions pair ON pair.id = t.xfer_pair_id
        WHERE t.id=?
        """,
        (int(transaction_id),),
    ).fetchone()
    if not row:
        return None

    splits = []
    if table_exists(db, "transaction_splits"):
        splits = [
            {
                "id": int(split["id"]),
                "envelope_id": int(split["envelope_id"]),
                "amount_cents": int(split["amount_cents"]),
            }
            for split in db.execute(
                """
                SELECT id, envelope_id, amount_cents
                FROM transaction_splits
                WHERE transaction_id=?
                ORDER BY id
                """,
                (int(transaction_id),),
            ).fetchall()
        ]

    remainder_intent: dict[str, Any] = {}
    if table_exists(db, "transaction_remainder_intents"):
        intent = db.execute(
            """
            SELECT envelope_id, amount_cents, created_at, updated_at
            FROM transaction_remainder_intents
            WHERE transaction_id=?
            """,
            (int(transaction_id),),
        ).fetchone()
        if intent:
            remainder_intent = {
                "envelope_id": int(intent["envelope_id"]),
                "amount_cents": int(intent["amount_cents"]),
                "created_at": intent["created_at"],
                "updated_at": intent["updated_at"],
            }

    return {
        "transaction": {
            "id": int(row["id"]),
            "account_id": int(row["account_id"]),
            "ttype": row["ttype"],
            "amount_cents": int(row["amount_cents"]),
            "posted_at": row["posted_at"],
            "payee": row["payee"],
            "memo": row["memo"],
            "fitid": row["fitid"],
            "ignore_match": int(row["ignore_match"] or 0),
            "xfer_pair_id": row["xfer_pair_id"],
            "external_counterparty": row["external_counterparty"],
            "transfer_other_account_id": row["transfer_other_account_id"],
        },
        "splits": splits,
        "remainder_intent": remainder_intent,
    }


def latest_raw_import_evidence(db, transaction_id: int) -> dict[str, Any]:
    if not all(
        table_exists(db, table)
        for table in ("transaction_import_validations", "import_session_rows", "import_sessions")
    ):
        return {}

    validation = db.execute(
        """
        SELECT
            v.id AS transaction_import_validation_id,
            v.account_id,
            v.transaction_id,
            v.validated_at,
            v.source AS validation_source,
            v.fitid AS validation_fitid,
            v.row_fingerprint AS validation_row_fingerprint,
            v.match_type AS validation_match_type,
            v.evidence_json AS validation_evidence_json,
            v.created_at AS validation_created_at,
            v.updated_at AS validation_updated_at,
            r.id AS import_session_row_id,
            r.row_index,
            r.posted_at AS import_posted_at,
            r.amount_cents AS import_amount_cents,
            r.payee AS raw_payee,
            r.memo AS raw_memo,
            r.fitid AS import_fitid,
            r.row_fingerprint,
            r.evidence_json AS import_row_evidence_json,
            r.match_type AS import_row_match_type,
            r.created_at AS import_row_created_at,
            s.id AS import_session_id,
            s.source_bankid,
            s.source_acctid,
            s.file_hash,
            s.created_at AS import_session_created_at
        FROM transaction_import_validations v
        LEFT JOIN import_session_rows r ON r.id = v.import_session_row_id
        LEFT JOIN import_sessions s ON s.id = r.session_id
        WHERE v.transaction_id=?
        ORDER BY v.updated_at DESC, v.id DESC
        LIMIT 1
        """,
        (int(transaction_id),),
    ).fetchone()
    if not validation:
        return {}

    row = dict(validation)
    import_row_match = {}
    if table_exists(db, "import_row_matches") and row.get("import_session_row_id"):
        match = db.execute(
            """
            SELECT id, match_type, evidence_json, created_at
            FROM import_row_matches
            WHERE row_id=? AND transaction_id=?
            ORDER BY id
            LIMIT 1
            """,
            (int(row["import_session_row_id"]), int(transaction_id)),
        ).fetchone()
        if match:
            import_row_match = {
                "id": int(match["id"]),
                "match_type": match["match_type"],
                "evidence": parse_json_object(match["evidence_json"]),
                "created_at": match["created_at"],
            }

    return {
        "import_session_row_id": row.get("import_session_row_id"),
        "transaction_import_validation_id": row.get("transaction_import_validation_id"),
        "raw_payee": row.get("raw_payee"),
        "raw_memo": row.get("raw_memo"),
        "import_session": {
            "id": row.get("import_session_id"),
            "source_bankid": row.get("source_bankid"),
            "source_acctid": row.get("source_acctid"),
            "file_hash": row.get("file_hash"),
            "created_at": row.get("import_session_created_at"),
        },
        "import_row": {
            "id": row.get("import_session_row_id"),
            "row_index": row.get("row_index"),
            "posted_at": row.get("import_posted_at"),
            "amount_cents": row.get("import_amount_cents"),
            "fitid": row.get("import_fitid"),
            "row_fingerprint": row.get("row_fingerprint"),
            "match_type": row.get("import_row_match_type"),
            "evidence": parse_json_object(row.get("import_row_evidence_json")),
            "created_at": row.get("import_row_created_at"),
        },
        "import_row_match": import_row_match,
        "transaction_import_validation": {
            "id": row.get("transaction_import_validation_id"),
            "source": row.get("validation_source"),
            "fitid": row.get("validation_fitid"),
            "row_fingerprint": row.get("validation_row_fingerprint"),
            "match_type": row.get("validation_match_type"),
            "evidence": parse_json_object(row.get("validation_evidence_json")),
            "validated_at": row.get("validated_at"),
            "created_at": row.get("validation_created_at"),
            "updated_at": row.get("validation_updated_at"),
        },
    }


def insert_learning_example(db, example: dict[str, Any]) -> int | None:
    if not learning_tables_available(db):
        return None
    cur = db.execute(
        """
        INSERT OR IGNORE INTO transaction_learning_examples(
            account_id, transaction_id, import_session_row_id,
            transaction_import_validation_id, source, evidence_quality,
            dedupe_key, posted_at, amount_cents, raw_payee, raw_memo,
            raw_profile_json, final_payee, final_memo, final_profile_json,
            transaction_type, transfer_other_account_id, splits_json,
            remainder_intent_json, decision_json, evidence_json, created_at, updated_at
        )
        VALUES (
            :account_id, :transaction_id, :import_session_row_id,
            :transaction_import_validation_id, :source, :evidence_quality,
            :dedupe_key, :posted_at, :amount_cents, :raw_payee, :raw_memo,
            :raw_profile_json, :final_payee, :final_memo, :final_profile_json,
            :transaction_type, :transfer_other_account_id, :splits_json,
            :remainder_intent_json, :decision_json, :evidence_json, :created_at, :updated_at
        )
        """,
        example,
    )
    if cur.rowcount == 0:
        if example.get("dedupe_key"):
            row = db.execute(
                "SELECT id FROM transaction_learning_examples WHERE dedupe_key=?",
                (example["dedupe_key"],),
            ).fetchone()
            return int(row["id"]) if row else None
        return None
    return int(cur.lastrowid)


def insert_learning_event(db, event: dict[str, Any]) -> int | None:
    if not learning_tables_available(db):
        return None
    cur = db.execute(
        """
        INSERT INTO transaction_learning_events(
            learning_example_id, transaction_id, event_type, source,
            evidence_quality, before_json, after_json, raw_evidence_json, created_at
        )
        VALUES (
            :learning_example_id, :transaction_id, :event_type, :source,
            :evidence_quality, :before_json, :after_json, :raw_evidence_json, :created_at
        )
        """,
        event,
    )
    return int(cur.lastrowid)


def insert_prediction_feedback(db, feedback: dict[str, Any]) -> int | None:
    if not prediction_feedback_available(db):
        return None
    cur = db.execute(
        """
        INSERT INTO prediction_feedback(
            prediction_id, learning_example_id, transaction_id, import_session_row_id,
            prediction_type, accepted, modified, rejected, predicted_json, final_json,
            outcome, created_at
        )
        VALUES (
            :prediction_id, :learning_example_id, :transaction_id, :import_session_row_id,
            :prediction_type, :accepted, :modified, :rejected, :predicted_json, :final_json,
            :outcome, :created_at
        )
        ON CONFLICT(prediction_id) WHERE prediction_id IS NOT NULL DO UPDATE SET
            learning_example_id=excluded.learning_example_id,
            transaction_id=excluded.transaction_id,
            import_session_row_id=excluded.import_session_row_id,
            prediction_type=excluded.prediction_type,
            accepted=excluded.accepted,
            modified=excluded.modified,
            rejected=excluded.rejected,
            predicted_json=excluded.predicted_json,
            final_json=excluded.final_json,
            outcome=excluded.outcome,
            created_at=excluded.created_at
        """,
        feedback,
    )
    if cur.lastrowid:
        return int(cur.lastrowid)
    if feedback.get("prediction_id"):
        row = db.execute(
            "SELECT id FROM prediction_feedback WHERE prediction_id=?",
            (feedback["prediction_id"],),
        ).fetchone()
        return int(row["id"]) if row else None
    return None
