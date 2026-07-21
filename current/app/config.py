import os
from pathlib import Path

def _bool_env(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


def _path_env(name: str, default: Path) -> Path:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return Path(raw).expanduser()


def _text_env(name: str, default: str) -> str:
    raw = os.environ.get(name)
    value = default if raw is None else raw
    return value.strip() or default


def _default_data_dir() -> Path:
    if os.name == "nt":
        local_app_data = os.environ.get("LOCALAPPDATA")
        base = Path(local_app_data).expanduser() if local_app_data else Path.home()
        return base / "EnvelopeBudget"
    xdg_state_home = os.environ.get("XDG_STATE_HOME")
    base = Path(xdg_state_home).expanduser() if xdg_state_home else Path.home() / ".local" / "state"
    return base / "fitft-envelope-budget"


def _configured_data_dir() -> Path:
    raw = os.environ.get("APP_DATA_DIR")
    if raw is not None and raw.strip() != "":
        return Path(raw).expanduser()
    return _default_data_dir()


class Config:
    APP_DIR = Path(__file__).resolve().parent
    APP_ENV = os.environ.get("APP_ENV", os.environ.get("FLASK_ENV", "production")).strip().lower()
    # Branding is intentionally separate from stable storage keys, event names,
    # service identifiers, and data-directory slugs.
    APP_DISPLAY_NAME = _text_env("APP_DISPLAY_NAME", "Perfii")

    HOST = os.environ.get("HOST", "127.0.0.1")
    PORT = _int_env("PORT", 8080)

    APP_DATA_DIR = _configured_data_dir()
    DB_PATH = _path_env("DB_PATH", APP_DATA_DIR / "data.sqlite")
    META_DB_PATH = _path_env("META_DB_PATH", APP_DATA_DIR / "meta.sqlite")
    UPLOAD_DIR = _path_env("UPLOAD_DIR", APP_DATA_DIR / "uploads")
    USER_DB_DIR = _path_env("USER_DB_DIR", APP_DATA_DIR / "user_dbs")

    SECRET_KEY = os.environ.get("SECRET_KEY")
    DEBUG = _bool_env("FLASK_DEBUG", APP_ENV == "development")
    TESTING = _bool_env("TESTING", False)
    DESKTOP_MODE = _bool_env("DESKTOP_MODE", APP_ENV == "desktop")

    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = os.environ.get("SESSION_COOKIE_SAMESITE", "Lax")
    SESSION_COOKIE_SECURE = _bool_env("SESSION_COOKIE_SECURE", APP_ENV == "production")

    TRUST_PROXY_HEADERS = _bool_env("TRUST_PROXY_HEADERS", APP_ENV == "production")
    BOOTSTRAP_LEGACY_DATA = _bool_env("BOOTSTRAP_LEGACY_DATA", True)
    REHOME_LEGACY_DB_PATHS = _bool_env("REHOME_LEGACY_DB_PATHS", True)
    ALLOW_ABSOLUTE_USER_DB_PATHS = _bool_env("ALLOW_ABSOLUTE_USER_DB_PATHS", APP_ENV != "production")

    SQLITE_TIMEOUT_SECONDS = _int_env("SQLITE_TIMEOUT_SECONDS", 30)
    SQLITE_BUSY_TIMEOUT_MS = _int_env("SQLITE_BUSY_TIMEOUT_MS", 30000)
    SQLITE_JOURNAL_MODE = os.environ.get("SQLITE_JOURNAL_MODE", "WAL")

    MAX_CONTENT_LENGTH = _int_env("MAX_CONTENT_LENGTH", 25 * 1024 * 1024)

    SNAPSHOT_REPO_ROOT = _path_env("SNAPSHOT_REPO_ROOT", APP_DIR.parent.parent)
    SNAPSHOT_ALERT_ENABLED = _bool_env("SNAPSHOT_ALERT_ENABLED", APP_ENV == "production")
    SNAPSHOT_ALERT_THRESHOLD_BYTES = _int_env("SNAPSHOT_ALERT_THRESHOLD_BYTES", 512 * 1024 * 1024)
    SNAPSHOT_ALERT_CACHE_SECONDS = _int_env("SNAPSHOT_ALERT_CACHE_SECONDS", 300)
