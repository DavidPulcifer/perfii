from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from flask import current_app, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from .db import get_meta_db

ROLE_ADMIN = "admin"
ROLE_USER = "user"
RESET_TOKEN_BYTES = 32
RESET_TOKEN_TTL_MINUTES = 30
MIN_PASSWORD_LENGTH = 8


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utcnow().isoformat(timespec="seconds")


def parse_iso(value: str | None) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def hash_password(password: str) -> str:
    return generate_password_hash(password)


def verify_password(password_hash: str | None, password: str) -> bool:
    if not password_hash:
        return False
    return check_password_hash(password_hash, password or "")


def validate_password(password: str, confirmation: str | None = None) -> str | None:
    password = password or ""
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
    if confirmation is not None and password != confirmation:
        return "Password confirmation does not match."
    return None


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def get_user(user_id: int):
    return get_meta_db().execute(
        """
        SELECT id, name, db_path, created_at, role, password_hash, password_set_at
        FROM users WHERE id=?
        """,
        (user_id,),
    ).fetchone()


def selected_user():
    uid = session.get("user_id")
    if uid is None:
        return None
    try:
        return get_user(int(uid))
    except Exception:
        return None


def user_has_password(user) -> bool:
    return bool(user and user["password_hash"])


def is_admin(user=None) -> bool:
    user = user if user is not None else selected_user()
    return bool(user and user["role"] == ROLE_ADMIN)


def is_selected_user_authenticated() -> bool:
    user = selected_user()
    if not user:
        return False
    if not user_has_password(user):
        return True
    return session.get("auth_user_id") == int(user["id"])


def mark_user_authenticated(user_id: int) -> None:
    session["auth_user_id"] = int(user_id)


def clear_selected_user() -> None:
    session.pop("user_id", None)
    session.pop("auth_user_id", None)


def select_user_session(user_id: int, *, authenticated: bool = False) -> None:
    session["user_id"] = int(user_id)
    session.pop("auth_user_id", None)
    if authenticated:
        mark_user_authenticated(user_id)


def admin_count() -> int:
    row = get_meta_db().execute(
        "SELECT COUNT(1) AS c FROM users WHERE role=?",
        (ROLE_ADMIN,),
    ).fetchone()
    return int(row["c"] or 0)


def can_demote_or_delete_user(user_id: int) -> bool:
    user = get_user(user_id)
    if not user or user["role"] != ROLE_ADMIN:
        return True
    if admin_count() > 1:
        return True
    return False


def create_reset_token(user_id: int, *, created_by_admin_user_id: int | None = None, ttl_minutes: int = RESET_TOKEN_TTL_MINUTES) -> tuple[str, datetime]:
    token = secrets.token_urlsafe(RESET_TOKEN_BYTES)
    expires_at = utcnow() + timedelta(minutes=ttl_minutes)
    meta = get_meta_db()
    meta.execute(
        """
        INSERT INTO user_password_reset_tokens(
            user_id, token_hash, created_at, expires_at, used_at, created_by_admin_user_id
        ) VALUES(?,?,?,?,NULL,?)
        """,
        (user_id, token_hash(token), iso_now(), expires_at.isoformat(timespec="seconds"), created_by_admin_user_id),
    )
    meta.commit()
    return token, expires_at


def consume_reset_token(token: str):
    digest = token_hash(token or "")
    meta = get_meta_db()
    row = meta.execute(
        """
        SELECT id, user_id, expires_at, used_at
        FROM user_password_reset_tokens
        WHERE token_hash=?
        ORDER BY id DESC
        LIMIT 1
        """,
        (digest,),
    ).fetchone()
    if not row or row["used_at"]:
        return None, "Reset link is invalid or has already been used."
    expires_at = parse_iso(row["expires_at"])
    if not expires_at or expires_at < utcnow():
        return None, "Reset link has expired."
    user = get_user(int(row["user_id"]))
    if not user:
        return None, "Reset user no longer exists."
    return row, None


def complete_reset_token(token_id: int, user_id: int, new_password: str) -> None:
    meta = get_meta_db()
    meta.execute(
        "UPDATE users SET password_hash=?, password_set_at=? WHERE id=?",
        (hash_password(new_password), iso_now(), user_id),
    )
    meta.execute(
        "UPDATE user_password_reset_tokens SET used_at=? WHERE id=?",
        (iso_now(), token_id),
    )
    meta.commit()


def local_reset_url(token: str) -> str:
    try:
        return url_for("users.reset_password_form", token=token, _external=False)
    except RuntimeError:
        return f"/users/reset/{token}"
