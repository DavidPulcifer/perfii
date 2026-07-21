#!/usr/bin/env python3
"""Create a short-lived one-time Finance App password reset link/code.

Server break-glass tool for Admin lockout and operator-mediated password resets.
It never clears a password and stores only a hash of the reset token.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app import create_app  # noqa: E402
from app.auth import create_reset_token, local_reset_url  # noqa: E402
from app.config import Config  # noqa: E402
from app.db import get_meta_db  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a one-time password reset link/code for a Finance App user.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--user", help="Exact user name to reset")
    group.add_argument("--user-id", type=int, help="User id to reset")
    parser.add_argument("--ttl-minutes", type=int, default=30, help="Reset expiry in minutes (default: 30)")
    parser.add_argument("--base-url", default="", help="Optional private base URL to prepend, e.g. https://finance.example.test/")
    args = parser.parse_args()

    if args.ttl_minutes <= 0 or args.ttl_minutes > 24 * 60:
        parser.error("--ttl-minutes must be between 1 and 1440")

    app = create_app(Config)
    with app.app_context():
        meta = get_meta_db()
        if args.user_id is not None:
            rows = meta.execute(
                "SELECT id, name, role FROM users WHERE id=?",
                (args.user_id,),
            ).fetchall()
        else:
            rows = meta.execute(
                "SELECT id, name, role FROM users WHERE name=? COLLATE NOCASE",
                (args.user,),
            ).fetchall()
        if not rows:
            print("No matching user found.", file=sys.stderr)
            return 2
        if len(rows) > 1:
            print("Ambiguous user; retry with --user-id.", file=sys.stderr)
            for row in rows:
                print(f"  {row['id']}: {row['name']} ({row['role']})", file=sys.stderr)
            return 2
        user = rows[0]
        token, expires_at = create_reset_token(int(user["id"]), ttl_minutes=args.ttl_minutes)
        path = local_reset_url(token)
        if args.base_url:
            reset_url = args.base_url.rstrip("/") + path
        else:
            reset_url = path
        print(f"Created one-time reset for {user['name']} ({user['role']}).")
        print(f"Expires: {expires_at.isoformat(timespec='seconds')}")
        print(f"Reset URL/code: {reset_url}")
        print("Only a token hash was stored; this output is the only time the reset token is shown.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
