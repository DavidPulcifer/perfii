from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from ..db import get_db


def _now() -> str:
    return datetime.utcnow().isoformat(timespec="seconds")


def _json(value: dict[str, Any] | None) -> str:
    return json.dumps(value or {}, sort_keys=True, separators=(",", ":"))


def record_transaction_import_validation(
    *,
    account_id: int,
    transaction_id: int,
    source: str,
    fitid: str | None = None,
    row_fingerprint: str | None = None,
    import_session_row_id: int | None = None,
    match_type: str | None = None,
    evidence: dict[str, Any] | None = None,
    commit: bool = True,
) -> int | None:
    """Idempotently record that one account-side transaction was validated by import evidence."""
    if not account_id or not transaction_id:
        return None

    db = get_db()
    now = _now()
    evidence_json = _json(evidence)
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
            now,
            source,
            fitid,
            row_fingerprint,
            import_session_row_id,
            match_type,
            evidence_json,
            now,
            now,
        ),
    )
    if commit:
        db.commit()
    row = db.execute(
        """
        SELECT id FROM transaction_import_validations
        WHERE account_id=? AND transaction_id=?
        """,
        (int(account_id), int(transaction_id)),
    ).fetchone()
    return int(row["id"]) if row else None


def get_transaction_import_validation(account_id: int, transaction_id: int) -> dict | None:
    if not account_id or not transaction_id:
        return None
    row = get_db().execute(
        """
        SELECT * FROM transaction_import_validations
        WHERE account_id=? AND transaction_id=?
        """,
        (int(account_id), int(transaction_id)),
    ).fetchone()
    return dict(row) if row else None


def list_transaction_import_validations_for_evidence(
    account_id: int,
    *,
    fitid: str | None = None,
    row_fingerprint: str | None = None,
) -> list[dict]:
    if not account_id or not (fitid or row_fingerprint):
        return []
    where = ["account_id=?"]
    params: list[Any] = [int(account_id)]
    if fitid:
        where.append("fitid=?")
        params.append(fitid)
    if row_fingerprint:
        where.append("row_fingerprint=?")
        params.append(row_fingerprint)
    rows = get_db().execute(
        f"""
        SELECT * FROM transaction_import_validations
        WHERE {' AND '.join(where)}
        ORDER BY id DESC
        """,
        params,
    ).fetchall()
    return [dict(row) for row in rows]


def list_import_validated_fitid_rows_for_account(account_id: int) -> list[dict]:
    """Return FITID duplicate evidence that is validated for this account.

    Transaction FITIDs alone are not authoritative anymore: a transfer's other
    leg can carry a copied/source FITID without that account-side transaction
    having been confirmed by that account's own statement import.
    """
    if not account_id:
        return []
    rows = get_db().execute(
        """
        SELECT v.fitid, COALESCE(t.payee, '') AS payee, COALESCE(t.memo, '') AS memo
        FROM transaction_import_validations v
        LEFT JOIN transactions t ON t.id = v.transaction_id
        WHERE v.account_id = ?
          AND v.fitid IS NOT NULL
          AND TRIM(v.fitid) <> ''
        ORDER BY v.id DESC
        """,
        (int(account_id),),
    ).fetchall()
    return [dict(row) for row in rows]


def list_import_validated_fitids_for_account(account_id: int) -> list[str]:
    return [row["fitid"] for row in list_import_validated_fitid_rows_for_account(account_id) if row.get("fitid")]


def list_import_validated_transaction_ids_for_account(account_id: int) -> set[int]:
    if not account_id:
        return set()
    rows = get_db().execute(
        """
        SELECT DISTINCT transaction_id
        FROM transaction_import_validations
        WHERE account_id = ?
          AND transaction_id IS NOT NULL
        """,
        (int(account_id),),
    ).fetchall()
    return {int(row["transaction_id"]) for row in rows if row["transaction_id"]}
