from __future__ import annotations

import secrets
from datetime import datetime, timedelta

from ..db import get_db


IMPORT_REVIEW_SOURCE_TTL_HOURS = 24


def _now() -> str:
    return datetime.utcnow().isoformat(timespec="seconds")


def _expires_at(now: datetime | None = None) -> str:
    base = now or datetime.utcnow()
    return (base + timedelta(hours=IMPORT_REVIEW_SOURCE_TTL_HOURS)).isoformat(timespec="seconds")


def _blank_to_none(value) -> str | None:
    text = str(value or "").strip()
    return text or None


def cleanup_expired_import_review_sources(now: str | None = None) -> int:
    db = get_db()
    cur = db.execute(
        "DELETE FROM import_review_sources WHERE expires_at IS NOT NULL AND expires_at < ?",
        (now or _now(),),
    )
    db.commit()
    return int(cur.rowcount or 0)


def get_import_review_source(token: str | None, account_id: int | None = None) -> dict | None:
    token = (token or "").strip()
    if not token:
        return None

    params: list[object] = [token, _now()]
    account_where = ""
    if account_id is not None:
        account_where = " AND account_id=?"
        params.append(int(account_id))

    row = get_db().execute(
        f"""
        SELECT token, account_id, source_bankid, source_acctid, file_hash,
               source_type, source_filename, created_at, expires_at
        FROM import_review_sources
        WHERE token=?
          AND expires_at >= ?
          {account_where}
        """,
        params,
    ).fetchone()
    return dict(row) if row else None


def create_import_review_source(
    *,
    account_id: int,
    source_bankid: str | None = None,
    source_acctid: str | None = None,
    file_hash: str | None = None,
    source_type: str | None = None,
    source_filename: str | None = None,
    expires_at: str | None = None,
) -> dict:
    now = _now()
    token = secrets.token_urlsafe(32)
    db = get_db()
    db.execute(
        """
        INSERT INTO import_review_sources(
            token, account_id, source_bankid, source_acctid, file_hash,
            source_type, source_filename, created_at, expires_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            token,
            int(account_id),
            _blank_to_none(source_bankid),
            _blank_to_none(source_acctid),
            _blank_to_none(file_hash),
            _blank_to_none(source_type) or "unknown",
            _blank_to_none(source_filename),
            now,
            expires_at or _expires_at(),
        ),
    )
    db.commit()
    return get_import_review_source(token, account_id) or {}
