"""Run the app on localhost against an explicitly bootstrapped workspace."""

from __future__ import annotations

import argparse
import secrets
import sys
from pathlib import Path


SCRIPT_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(SCRIPT_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_PROJECT_ROOT))

from scripts.bootstrap_workspace import PROJECT_ROOT, _is_within
from scripts.doctor import run_doctor


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run a managed finance-app workspace at http://127.0.0.1."
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=PROJECT_ROOT / ".local" / "demo-data",
        help="Workspace previously created by bootstrap_workspace.py.",
    )
    parser.add_argument("--port", type=int, default=8080, help="Local TCP port (default: 8080).")
    parser.add_argument(
        "--allow-external",
        action="store_true",
        help="Use a verified managed workspace outside the project directory.",
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="Validate configuration without starting the blocking web server.",
    )
    return parser


def _local_config(data_dir: Path, *, port: int):
    from app.config import Config

    class LocalConfig(Config):
        APP_ENV = "development"
        TESTING = False
        DEBUG = False
        SECRET_KEY = secrets.token_urlsafe(32)
        HOST = "127.0.0.1"
        PORT = port

        APP_DATA_DIR = data_dir
        DB_PATH = data_dir / "data.sqlite"
        META_DB_PATH = data_dir / "meta.sqlite"
        USER_DB_DIR = data_dir / "user_dbs"
        UPLOAD_DIR = data_dir / "uploads"

        SESSION_COOKIE_SECURE = False
        TRUST_PROXY_HEADERS = False
        BOOTSTRAP_LEGACY_DATA = False
        REHOME_LEGACY_DB_PATHS = False
        ALLOW_ABSOLUTE_USER_DB_PATHS = False
        SNAPSHOT_ALERT_ENABLED = False

    return LocalConfig


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    data_dir = args.data_dir.expanduser().resolve()
    if not args.allow_external and not _is_within(data_dir, PROJECT_ROOT):
        print("Launch refused: data directory is outside the project. Verify it, then pass --allow-external.")
        return 2
    if not 1 <= args.port <= 65535:
        print("Launch refused: port must be between 1 and 65535.")
        return 2

    report = run_doctor(data_dir, smoke=False)
    if not report["ok"]:
        print("Launch refused: workspace health checks failed.")
        for check in report["checks"]:
            if not check["passed"]:
                print(f"- {check['check']}: {check['detail']}")
        return 2

    if args.check_only:
        print(f"Local launch configuration is healthy for {data_dir}")
        print(f"Would bind only to http://127.0.0.1:{args.port}")
        return 0

    from app import create_app

    app = create_app(_local_config(data_dir, port=args.port))
    print(f"Using managed workspace: {data_dir}")
    print(f"Open http://127.0.0.1:{args.port}")
    try:
        app.run(host="127.0.0.1", port=args.port, debug=False, use_reloader=False)
    except OSError as exc:
        print(f"Local server could not start: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
