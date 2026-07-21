from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Iterable

from ..db import get_db
from ..services.transaction_learning_service import record_import_session_learning_events
from .import_validation_repo import list_import_validated_transaction_ids_for_account


def _now() -> str:
    return datetime.utcnow().isoformat(timespec="seconds")


def _json(value: dict[str, Any] | None) -> str:
    return json.dumps(value or {}, sort_keys=True, separators=(",", ":"))


def list_import_provenance_matches(
    account_id: int,
    fingerprints: Iterable[str],
    *,
    source_bankid: str | None = None,
    source_acctid: str | None = None,
) -> list[dict]:
    values = [str(v or "").strip() for v in fingerprints if str(v or "").strip()]
    if not account_id or not values:
        return []

    placeholders = ", ".join("?" for _ in values)
    params: list[Any] = [int(account_id), *values]
    source_where = ""
    if source_bankid:
        source_where += " AND COALESCE(s.source_bankid, '') = ?"
        params.append(source_bankid)
    if source_acctid:
        source_where += " AND COALESCE(s.source_acctid, '') = ?"
        params.append(source_acctid)

    db = get_db()
    rows = db.execute(
        f"""
        SELECT r.row_fingerprint, r.row_index, r.fitid, r.transaction_id, m.match_type, m.evidence_json
        FROM import_session_rows r
        JOIN import_sessions s ON s.id = r.session_id
        LEFT JOIN import_row_matches m ON m.row_id = r.id
        WHERE s.account_id = ?
          AND r.row_fingerprint IN ({placeholders})
          {source_where}
        ORDER BY r.id DESC
        """,
        params,
    ).fetchall()
    return [dict(row) for row in rows]


def _validation_source(match_type: str | None) -> str:
    return "manual_match" if match_type == "manual_match" else "import_commit"


def _record_validation_if_account_side(
    db,
    *,
    account_id: int,
    transaction_id: int,
    row_id: int,
    row: dict,
    match_type: str | None,
    created_at: str,
) -> None:
    tx = db.execute(
        "SELECT id, account_id FROM transactions WHERE id=?",
        (int(transaction_id),),
    ).fetchone()
    if not tx or int(tx["account_id"]) != int(account_id):
        return

    db.execute(
        """
        INSERT INTO transaction_import_validations(
            account_id, transaction_id, validated_at, source, fitid, row_fingerprint,
            import_session_row_id, match_type, evidence_json, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(account_id, transaction_id) DO UPDATE SET
            source=excluded.source,
            fitid=COALESCE(excluded.fitid, transaction_import_validations.fitid),
            row_fingerprint=COALESCE(excluded.row_fingerprint, transaction_import_validations.row_fingerprint),
            import_session_row_id=COALESCE(excluded.import_session_row_id, transaction_import_validations.import_session_row_id),
            match_type=COALESCE(excluded.match_type, transaction_import_validations.match_type),
            evidence_json=excluded.evidence_json,
            updated_at=excluded.updated_at
        """,
        (
            int(account_id),
            int(transaction_id),
            created_at,
            _validation_source(match_type),
            row.get("fitid"),
            row.get("row_fingerprint"),
            int(row_id),
            match_type,
            _json(dict(row.get("evidence") or {})),
            created_at,
            created_at,
        ),
    )


def record_import_session_rows(
    *,
    account_id: int,
    source_bankid: str | None = None,
    source_acctid: str | None = None,
    file_hash: str | None = None,
    rows: list[dict],
) -> int | None:
    if not account_id or not rows:
        return None

    db = get_db()
    created_at = _now()
    cur = db.execute(
        """
        INSERT INTO import_sessions(account_id, source_bankid, source_acctid, file_hash, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (int(account_id), source_bankid, source_acctid, file_hash, created_at),
    )
    session_id = int(cur.lastrowid)

    for row in rows:
        row_index = int(row["row_index"])
        evidence = dict(row.get("evidence") or {})
        db.execute(
            """
            INSERT INTO import_session_rows(
                session_id, row_index, posted_at, amount_cents, payee, memo, fitid,
                row_fingerprint, evidence_json, transaction_id, match_type, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                row_index,
                row.get("posted_at"),
                int(row.get("amount_cents") or 0),
                row.get("payee"),
                row.get("memo"),
                row.get("fitid"),
                row["row_fingerprint"],
                _json(evidence),
                row.get("transaction_id"),
                row.get("match_type"),
                created_at,
            ),
        )
        row_id = int(db.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
        tx_ids = row.get("transaction_ids") or ([row.get("transaction_id")] if row.get("transaction_id") else [])
        seen: set[int] = set()
        for tx_id in tx_ids:
            try:
                tx_id_int = int(tx_id)
            except (TypeError, ValueError):
                continue
            if not tx_id_int or tx_id_int in seen:
                continue
            seen.add(tx_id_int)
            match_type = row.get("match_type") or "created"
            db.execute(
                """
                INSERT INTO import_row_matches(row_id, transaction_id, match_type, evidence_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (row_id, tx_id_int, match_type, _json(evidence), created_at),
            )
            _record_validation_if_account_side(
                db,
                account_id=int(account_id),
                transaction_id=tx_id_int,
                row_id=row_id,
                row=row,
                match_type=match_type,
                created_at=created_at,
            )
    record_import_session_learning_events(db, session_id=session_id, now=created_at)
    db.commit()
    return session_id

def list_import_matched_transaction_ids(account_id: int) -> set[int]:
    return list_import_validated_transaction_ids_for_account(account_id)


def latest_import_session_id_for_account(account_id: int) -> int | None:
    if not account_id:
        return None
    row = get_db().execute(
        """
        SELECT id
        FROM import_sessions
        WHERE account_id=?
        ORDER BY id DESC
        LIMIT 1
        """,
        (int(account_id),),
    ).fetchone()
    return int(row["id"]) if row else None


def get_import_session_undo_candidate(session_id: int) -> dict | None:
    if not session_id:
        return None
    db = get_db()
    session_row = db.execute(
        """
        SELECT id, account_id, source_bankid, source_acctid, file_hash, created_at
        FROM import_sessions
        WHERE id=?
        """,
        (int(session_id),),
    ).fetchone()
    if not session_row:
        return None

    rows = db.execute(
        """
        SELECT
            r.id AS row_id,
            r.row_index,
            r.match_type AS row_match_type,
            m.transaction_id,
            m.match_type
        FROM import_session_rows r
        LEFT JOIN import_row_matches m ON m.row_id = r.id
        WHERE r.session_id=?
        ORDER BY r.row_index, m.id
        """,
        (int(session_id),),
    ).fetchall()
    candidate = dict(session_row)
    candidate["rows"] = [dict(row) for row in rows]
    return candidate


def delete_import_session_provenance(session_id: int) -> int:
    if not session_id:
        return 0
    db = get_db()
    row_ids = [
        int(row["id"])
        for row in db.execute(
            "SELECT id FROM import_session_rows WHERE session_id=?",
            (int(session_id),),
        ).fetchall()
    ]
    if row_ids:
        placeholders = ", ".join("?" for _ in row_ids)
        db.execute(
            f"DELETE FROM transaction_import_validations WHERE import_session_row_id IN ({placeholders})",
            row_ids,
        )
    cur = db.execute("DELETE FROM import_sessions WHERE id=?", (int(session_id),))
    db.commit()
    return int(cur.rowcount or 0)
