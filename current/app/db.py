import shutil
import sqlite3
from flask import current_app, g, session
from pathlib import Path
from datetime import datetime
from contextlib import contextmanager


ACCOUNT_IDENTIFIER_INDEX = "idx_accounts_bankid_acctid"

def _connect(path, *, app=None, foreign_keys: bool = True) -> sqlite3.Connection:
    cfg = app.config if app is not None else current_app.config
    db_path = Path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=int(cfg.get("SQLITE_TIMEOUT_SECONDS", 30)))
    conn.row_factory = sqlite3.Row

    busy_timeout = int(cfg.get("SQLITE_BUSY_TIMEOUT_MS", 30000))
    conn.execute(f"PRAGMA busy_timeout = {busy_timeout}")
    if foreign_keys:
        conn.execute("PRAGMA foreign_keys = ON")

    journal_mode = (cfg.get("SQLITE_JOURNAL_MODE") or "").strip().upper()
    if journal_mode:
        if journal_mode in {"DELETE", "TRUNCATE", "PERSIST", "MEMORY", "WAL", "OFF"}:
            try:
                conn.execute(f"PRAGMA journal_mode = {journal_mode}")
            except sqlite3.DatabaseError:
                pass

    return conn


def get_meta_db():
    """Connection to the meta registry (users table)."""
    if 'meta_db' not in g:
        meta_path = Path(current_app.config['META_DB_PATH'])
        g.meta_db = _connect(meta_path, foreign_keys=False)
    return g.meta_db

def get_db():
    """
    Returns the per-request user DB connection.
    - If session['user_id'] is set and found in meta users table -> open that db_path.
    - Else fall back to app.config['DB_PATH'] (backward-compatible).
    """
    if 'db' in g:
        return g.db

    db_path = None
    try:
        uid = session.get('user_id')
        if uid is not None:
            row = get_meta_db().execute("SELECT db_path FROM users WHERE id=?", (uid,)).fetchone()
            if row and row['db_path']:
                db_path = Path(row['db_path'])
    except Exception:
        db_path = None

    if not db_path:
        db_path = Path(current_app.config['DB_PATH'])  # fallback single-user path

    _assert_test_db_path_isolated(db_path)

    conn = _connect(db_path)
    _ensure_schema(conn)
    
    g.db = conn
    return g.db

def _assert_test_db_path_isolated(db_path: Path) -> None:
    """In tests, fail closed rather than opening a DB outside APP_DATA_DIR."""
    if not current_app.config.get("TESTING"):
        return
    if not current_app.config.get("FORBID_EXTERNAL_TEST_DB_PATHS", False):
        return

    # Tests may intentionally create throwaway user DBs next to app-data
    # (for schema upgrade coverage), but must never reach outside that temp root.
    root = Path(current_app.config["APP_DATA_DIR"]).resolve().parent
    resolved = Path(db_path).expanduser().resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise RuntimeError(
            f"Refusing to open external test DB path {resolved}; expected a path under test root {root}."
        ) from exc


def close_db(e=None):
    db = g.pop('db', None)
    if db is not None:
        db.close()
    mdb = g.pop('meta_db', None)
    if mdb is not None:
        mdb.close()

def init_app(app):
    _ensure_runtime_dirs(app)
    if app.config.get("BOOTSTRAP_LEGACY_DATA"):
        _bootstrap_legacy_data(app)

    # Ensure meta registry exists and has current auth/security columns.
    meta = _connect(app.config['META_DB_PATH'], app=app, foreign_keys=False)
    ensure_meta_schema(meta, app)
    if app.config.get("REHOME_LEGACY_DB_PATHS"):
        _rehome_legacy_user_paths(meta, app)
    meta.commit()
    meta.close()

    @app.teardown_appcontext
    def _close_db(_exc):
        close_db()


def ensure_meta_schema(meta: sqlite3.Connection, app) -> None:
    meta.execute("""
        CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL UNIQUE,
        db_path TEXT NOT NULL,
        created_at TEXT NOT NULL
        )
    """)
    _add_column_if_missing(meta, "users", "role", "TEXT NOT NULL DEFAULT 'user'")
    _add_column_if_missing(meta, "users", "password_hash", "TEXT")
    _add_column_if_missing(meta, "users", "password_set_at", "TEXT")
    meta.execute("""
        CREATE TABLE IF NOT EXISTS user_password_reset_tokens (
            id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL,
            token_hash TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            used_at TEXT,
            created_by_admin_user_id INTEGER,
            FOREIGN KEY(user_id) REFERENCES users(id),
            FOREIGN KEY(created_by_admin_user_id) REFERENCES users(id)
        )
    """)
    meta.execute(
        "CREATE INDEX IF NOT EXISTS idx_user_password_reset_tokens_user_id "
        "ON user_password_reset_tokens(user_id)"
    )
    meta.execute(
        "CREATE INDEX IF NOT EXISTS idx_user_password_reset_tokens_token_hash "
        "ON user_password_reset_tokens(token_hash)"
    )

    row = meta.execute("SELECT COUNT(1) AS c FROM users").fetchone()
    if not row or int(row["c"] or 0) == 0:
        meta.execute(
            "INSERT INTO users(name, db_path, created_at, role) VALUES(?,?,?,?)",
            ("Default", str(app.config["DB_PATH"]), datetime.utcnow().isoformat(timespec="seconds"), "admin")
        )
    admin = meta.execute("SELECT id FROM users WHERE role='admin' ORDER BY id LIMIT 1").fetchone()
    if not admin:
        meta.execute(
            "UPDATE users SET role='admin' WHERE id=(SELECT id FROM users ORDER BY id LIMIT 1)"
        )


def _ensure_runtime_dirs(app):
    for key in ("APP_DATA_DIR", "DB_PATH", "META_DB_PATH", "UPLOAD_DIR", "USER_DB_DIR"):
        path = Path(app.config[key])
        directory = path if key.endswith("_DIR") else path.parent
        directory.mkdir(parents=True, exist_ok=True)


def _copy_if_missing(src: Path, dst: Path) -> None:
    if not src.exists() or dst.exists():
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def _bootstrap_legacy_data(app) -> None:
    """Copy repo-era SQLite files into APP_DATA_DIR once, without overwriting."""
    legacy_app_dir = Path(app.root_path)
    _copy_if_missing(legacy_app_dir / "data.sqlite", Path(app.config["DB_PATH"]))
    _copy_if_missing(legacy_app_dir / "meta.sqlite", Path(app.config["META_DB_PATH"]))

    legacy_user_dir = legacy_app_dir / "user_dbs"
    target_user_dir = Path(app.config["USER_DB_DIR"])
    if legacy_user_dir.exists():
        for src in legacy_user_dir.glob("*.sqlite"):
            _copy_if_missing(src, target_user_dir / src.name)


def _basename_any_platform(path_value: str) -> str:
    return (path_value or "").replace("\\", "/").rstrip("/").split("/")[-1]


def _parts_any_platform(path_value: str) -> set[str]:
    return {part.lower() for part in (path_value or "").replace("\\", "/").split("/") if part}


def _path_exists(path: Path) -> bool:
    try:
        return path.exists()
    except OSError:
        return False


def quote_identifier(identifier: str) -> str:
    return '"' + str(identifier).replace('"', '""') + '"'


def list_table_names(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    return [row["name"] for row in rows]


def table_schema_sql(conn: sqlite3.Connection, table_name: str) -> list[str]:
    rows = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    ).fetchall()
    return [row["sql"] for row in rows if row["sql"]]


def table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    ).fetchone() is not None


def index_exists(conn: sqlite3.Connection, index_name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?",
        (index_name,),
    ).fetchone() is not None


def table_columns(conn: sqlite3.Connection, table_name: str) -> list[str]:
    return [
        row["name"]
        for row in conn.execute(f"PRAGMA table_info({quote_identifier(table_name)})").fetchall()
    ]


def index_columns(conn: sqlite3.Connection, index_name: str) -> list[str]:
    return [
        row["name"]
        for row in conn.execute(f"PRAGMA index_info({quote_identifier(index_name)})").fetchall()
    ]


def ensure_schema_migrations_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            name TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL
        )
        """
    )


def applied_schema_migrations(conn: sqlite3.Connection) -> set[str]:
    if not table_exists(conn, "schema_migrations"):
        return set()
    rows = conn.execute("SELECT name FROM schema_migrations").fetchall()
    return {row["name"] for row in rows}


def _add_column_if_missing(conn: sqlite3.Connection, table_name: str, column_name: str, column_sql: str) -> None:
    if column_name in table_columns(conn, table_name):
        return
    conn.execute(
        f"ALTER TABLE {quote_identifier(table_name)} "
        f"ADD COLUMN {quote_identifier(column_name)} {column_sql}"
    )


def ensure_account_metadata_schema(conn: sqlite3.Connection) -> None:
    if not table_exists(conn, "accounts"):
        return

    _add_column_if_missing(conn, "accounts", "opening_balance_cents", "INTEGER NOT NULL DEFAULT 0")
    _add_column_if_missing(conn, "accounts", "opening_date", "TEXT")
    _add_column_if_missing(conn, "accounts", "bankid", "TEXT")
    _add_column_if_missing(conn, "accounts", "acctid", "TEXT")

    if (
        index_exists(conn, ACCOUNT_IDENTIFIER_INDEX)
        and index_columns(conn, ACCOUNT_IDENTIFIER_INDEX) != ["bankid", "acctid"]
    ):
        conn.execute(f"DROP INDEX {quote_identifier(ACCOUNT_IDENTIFIER_INDEX)}")
    conn.execute(
        f"CREATE INDEX IF NOT EXISTS {quote_identifier(ACCOUNT_IDENTIFIER_INDEX)} "
        "ON accounts(bankid, acctid)"
    )


def ensure_envelope_archive_schema(conn: sqlite3.Connection) -> None:
    if not table_exists(conn, "envelopes"):
        return

    _add_column_if_missing(conn, "envelopes", "archived_at", "TEXT")


def ensure_investment_notes_schema(conn: sqlite3.Connection) -> None:
    if not table_exists(conn, "accounts"):
        return

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS investment_notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id INTEGER NOT NULL,
            note_date TEXT NOT NULL,
            body TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(account_id) REFERENCES accounts(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_investment_notes_account_date
            ON investment_notes(account_id, note_date, id)
        """
    )


def ensure_reconciliation_schema(conn: sqlite3.Connection) -> None:
    if not table_exists(conn, "accounts") or not table_exists(conn, "transactions"):
        return

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS reconciliation_sessions (
            id INTEGER PRIMARY KEY,
            account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
            statement_date TEXT NOT NULL,
            statement_balance_cents INTEGER NOT NULL,
            starting_balance_cents INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL CHECK(status IN ('open','closed','reopened','void')) DEFAULT 'open',
            label TEXT,
            note TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            closed_at TEXT,
            reopened_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS reconciliation_items (
            id INTEGER PRIMARY KEY,
            session_id INTEGER NOT NULL REFERENCES reconciliation_sessions(id) ON DELETE CASCADE,
            transaction_id INTEGER NOT NULL REFERENCES transactions(id) ON DELETE CASCADE,
            state TEXT NOT NULL CHECK(state IN ('cleared','reconciled')),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(session_id, transaction_id)
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_reconciliation_sessions_account_status_date
            ON reconciliation_sessions(account_id, status, statement_date, id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_reconciliation_items_session
            ON reconciliation_items(session_id, transaction_id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_reconciliation_items_transaction
            ON reconciliation_items(transaction_id)
        """
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_reconciliation_items_one_reconciled_tx
            ON reconciliation_items(transaction_id)
            WHERE state='reconciled'
        """
    )


def ensure_transaction_remainder_intents_schema(conn: sqlite3.Connection) -> bool | None:
    if not table_exists(conn, "transactions") or not table_exists(conn, "envelopes"):
        return False

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS transaction_remainder_intents (
            transaction_id INTEGER PRIMARY KEY,
            envelope_id INTEGER NOT NULL,
            amount_cents INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(transaction_id) REFERENCES transactions(id) ON DELETE CASCADE,
            FOREIGN KEY(envelope_id) REFERENCES envelopes(id)
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_transaction_remainder_intents_envelope
            ON transaction_remainder_intents(envelope_id)
        """
    )



def ensure_import_provenance_schema(conn: sqlite3.Connection) -> bool | None:
    if not table_exists(conn, "accounts") or not table_exists(conn, "transactions"):
        return False

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS import_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id INTEGER NOT NULL,
            source_bankid TEXT,
            source_acctid TEXT,
            file_hash TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(account_id) REFERENCES accounts(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS import_session_rows (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER NOT NULL,
            row_index INTEGER NOT NULL,
            posted_at TEXT,
            amount_cents INTEGER NOT NULL,
            payee TEXT,
            memo TEXT,
            fitid TEXT,
            row_fingerprint TEXT NOT NULL,
            evidence_json TEXT NOT NULL DEFAULT '{}',
            transaction_id INTEGER,
            match_type TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(session_id) REFERENCES import_sessions(id) ON DELETE CASCADE,
            FOREIGN KEY(transaction_id) REFERENCES transactions(id) ON DELETE SET NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS import_row_matches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            row_id INTEGER NOT NULL,
            transaction_id INTEGER NOT NULL,
            match_type TEXT NOT NULL,
            evidence_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            FOREIGN KEY(row_id) REFERENCES import_session_rows(id) ON DELETE CASCADE,
            FOREIGN KEY(transaction_id) REFERENCES transactions(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_import_sessions_account_source
            ON import_sessions(account_id, source_bankid, source_acctid, id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_import_session_rows_fingerprint
            ON import_session_rows(row_fingerprint, session_id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_import_session_rows_fitid
            ON import_session_rows(fitid, session_id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_import_row_matches_transaction
            ON import_row_matches(transaction_id, row_id)
        """
    )



def ensure_transaction_import_validations_schema(conn: sqlite3.Connection) -> bool | None:
    if not table_exists(conn, "accounts") or not table_exists(conn, "transactions"):
        return False

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS transaction_import_validations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id INTEGER NOT NULL,
            transaction_id INTEGER NOT NULL,
            validated_at TEXT NOT NULL,
            source TEXT NOT NULL CHECK(source IN ('import_commit','manual_match','backfill')),
            fitid TEXT,
            row_fingerprint TEXT,
            import_session_row_id INTEGER,
            match_type TEXT,
            evidence_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(account_id) REFERENCES accounts(id) ON DELETE CASCADE,
            FOREIGN KEY(transaction_id) REFERENCES transactions(id) ON DELETE CASCADE,
            FOREIGN KEY(import_session_row_id) REFERENCES import_session_rows(id) ON DELETE SET NULL,
            UNIQUE(account_id, transaction_id)
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_transaction_import_validations_account
            ON transaction_import_validations(account_id, transaction_id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_transaction_import_validations_fitid
            ON transaction_import_validations(account_id, fitid)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_transaction_import_validations_fingerprint
            ON transaction_import_validations(account_id, row_fingerprint)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_transaction_import_validations_row
            ON transaction_import_validations(import_session_row_id)
        """
    )
    _backfill_transaction_import_validations(conn)


def _backfill_transaction_import_validations(conn: sqlite3.Connection) -> None:
    if not (
        table_exists(conn, "import_sessions")
        and table_exists(conn, "import_session_rows")
        and table_exists(conn, "import_row_matches")
    ):
        return
    now = datetime.utcnow().isoformat(timespec="seconds")
    conn.execute(
        """
        INSERT OR IGNORE INTO transaction_import_validations(
            account_id, transaction_id, validated_at, source, fitid, row_fingerprint,
            import_session_row_id, match_type, evidence_json, created_at, updated_at
        )
        SELECT
            s.account_id,
            m.transaction_id,
            COALESCE(m.created_at, r.created_at, s.created_at, ?),
            'backfill',
            r.fitid,
            r.row_fingerprint,
            r.id,
            m.match_type,
            COALESCE(NULLIF(m.evidence_json, ''), NULLIF(r.evidence_json, ''), '{}'),
            ?,
            ?
        FROM import_row_matches m
        JOIN import_session_rows r ON r.id = m.row_id
        JOIN import_sessions s ON s.id = r.session_id
        JOIN transactions t ON t.id = m.transaction_id AND t.account_id = s.account_id
        WHERE s.account_id IS NOT NULL
          AND m.transaction_id IS NOT NULL
        """,
        (now, now, now),
    )


def ensure_import_review_drafts_schema(conn: sqlite3.Connection) -> bool | None:
    if not table_exists(conn, "accounts"):
        return False

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS import_review_drafts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fingerprint TEXT NOT NULL UNIQUE,
            account_id INTEGER NOT NULL,
            source_type TEXT NOT NULL,
            source_filename TEXT,
            file_sha256 TEXT,
            row_count INTEGER NOT NULL DEFAULT 0,
            draft_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            FOREIGN KEY(account_id) REFERENCES accounts(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_import_review_drafts_account_updated
            ON import_review_drafts(account_id, updated_at)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_import_review_drafts_expires
            ON import_review_drafts(expires_at)
        """
    )


def ensure_import_review_sources_schema(conn: sqlite3.Connection) -> bool | None:
    if not table_exists(conn, "accounts"):
        return False

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS import_review_sources (
            token TEXT PRIMARY KEY,
            account_id INTEGER NOT NULL,
            source_bankid TEXT,
            source_acctid TEXT,
            file_hash TEXT,
            source_type TEXT NOT NULL,
            source_filename TEXT,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            FOREIGN KEY(account_id) REFERENCES accounts(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_import_review_sources_account_token
            ON import_review_sources(account_id, token)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_import_review_sources_expires
            ON import_review_sources(expires_at)
        """
    )


def ensure_account_locked_unallocated_backfill(conn: sqlite3.Connection) -> bool | None:
    """Create a locked Unallocated envelope for transfer-capable accounts with none."""
    if not table_exists(conn, "accounts") or not table_exists(conn, "envelopes"):
        return False
    if "locked_account_id" not in table_columns(conn, "envelopes"):
        return False
    if "archived_at" not in table_columns(conn, "envelopes"):
        ensure_envelope_archive_schema(conn)

    conn.execute(
        """
        INSERT INTO envelopes(name, locked_account_id, default_amount_cents)
        SELECT 'Unallocated', a.id, 0
        FROM accounts a
        WHERE COALESCE(a.account_type, 'bank') IN ('bank', 'credit_card', 'loan', 'investment')
          AND NOT EXISTS (
              SELECT 1
              FROM envelopes e
              WHERE e.locked_account_id = a.id
                AND e.archived_at IS NULL
          )
        """
    )
    return True


def ensure_loan_monthly_payment_schema(conn: sqlite3.Connection) -> bool | None:
    if not table_exists(conn, "loans"):
        return False

    _add_column_if_missing(conn, "loans", "normal_monthly_payment_cents", "INTEGER")
    return True


def ensure_credit_cards_default_paying_bank_removed(conn: sqlite3.Connection) -> bool | None:
    """Retire the unused default-paying-bank column from credit card metadata."""
    if not table_exists(conn, "credit_cards"):
        return False
    if "default_paying_account_id" not in table_columns(conn, "credit_cards"):
        return True

    try:
        conn.execute('ALTER TABLE credit_cards DROP COLUMN default_paying_account_id')
        return True
    except sqlite3.OperationalError:
        # Older/stricter SQLite builds can reject DROP COLUMN when foreign-key
        # metadata is involved. Rebuild the small metadata table and preserve
        # the only live fields the app still uses.
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute(
            """
            CREATE TABLE credit_cards_fin038 (
                account_id INTEGER PRIMARY KEY REFERENCES accounts(id) ON DELETE CASCADE,
                credit_limit_cents INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute(
            """
            INSERT INTO credit_cards_fin038(account_id, credit_limit_cents)
            SELECT account_id, COALESCE(credit_limit_cents, 0)
            FROM credit_cards
            """
        )
        conn.execute("DROP TABLE credit_cards")
        conn.execute("ALTER TABLE credit_cards_fin038 RENAME TO credit_cards")
        conn.execute("PRAGMA foreign_keys=ON")
        return True


def ensure_payee_normalization_schema(conn: sqlite3.Connection) -> bool | None:
    if not table_exists(conn, "accounts"):
        return False

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS payee_normalization_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id INTEGER NOT NULL,
            raw_payee_key TEXT NOT NULL DEFAULT '',
            raw_memo_key TEXT NOT NULL DEFAULT '',
            raw_payee_sample TEXT,
            raw_memo_sample TEXT,
            canonical_payee TEXT NOT NULL,
            canonical_memo TEXT,
            payee_changed INTEGER NOT NULL DEFAULT 1,
            memo_changed INTEGER NOT NULL DEFAULT 0,
            use_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            last_used_at TEXT NOT NULL,
            FOREIGN KEY(account_id) REFERENCES accounts(id) ON DELETE CASCADE,
            UNIQUE(account_id, raw_payee_key, raw_memo_key)
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_payee_normalization_rules_lookup
            ON payee_normalization_rules(account_id, raw_payee_key, raw_memo_key)
        """
    )


def ensure_payee_memo_normalization_schema(conn: sqlite3.Connection) -> bool | None:
    if not table_exists(conn, "payee_normalization_rules"):
        return False

    columns = set(table_columns(conn, "payee_normalization_rules"))
    if "canonical_memo" not in columns:
        conn.execute("ALTER TABLE payee_normalization_rules ADD COLUMN canonical_memo TEXT")
    if "payee_changed" not in columns:
        conn.execute(
            "ALTER TABLE payee_normalization_rules "
            "ADD COLUMN payee_changed INTEGER NOT NULL DEFAULT 1"
        )
    if "memo_changed" not in columns:
        conn.execute(
            "ALTER TABLE payee_normalization_rules "
            "ADD COLUMN memo_changed INTEGER NOT NULL DEFAULT 0"
        )
    return True


def ensure_transaction_learning_schema(conn: sqlite3.Connection) -> bool | None:
    if not table_exists(conn, "accounts") or not table_exists(conn, "transactions"):
        return False

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS transaction_learning_examples (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id INTEGER NOT NULL,
            transaction_id INTEGER,
            import_session_row_id INTEGER,
            transaction_import_validation_id INTEGER,
            source TEXT NOT NULL CHECK(source IN (
                'import_commit',
                'manual_match',
                'transaction_edit',
                'split_edit',
                'transfer_edit',
                'prediction_feedback',
                'backfill',
                'manual_entry'
            )),
            evidence_quality TEXT NOT NULL CHECK(evidence_quality IN ('high','medium','low')),
            dedupe_key TEXT,
            posted_at TEXT,
            amount_cents INTEGER,
            raw_payee TEXT,
            raw_memo TEXT,
            raw_profile_json TEXT NOT NULL DEFAULT '{}',
            final_payee TEXT,
            final_memo TEXT,
            final_profile_json TEXT NOT NULL DEFAULT '{}',
            transaction_type TEXT,
            transfer_other_account_id INTEGER,
            splits_json TEXT NOT NULL DEFAULT '[]',
            remainder_intent_json TEXT NOT NULL DEFAULT '{}',
            decision_json TEXT NOT NULL DEFAULT '{}',
            evidence_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(account_id) REFERENCES accounts(id) ON DELETE CASCADE,
            FOREIGN KEY(transaction_id) REFERENCES transactions(id) ON DELETE SET NULL,
            FOREIGN KEY(import_session_row_id) REFERENCES import_session_rows(id) ON DELETE SET NULL,
            FOREIGN KEY(transaction_import_validation_id) REFERENCES transaction_import_validations(id) ON DELETE SET NULL,
            FOREIGN KEY(transfer_other_account_id) REFERENCES accounts(id) ON DELETE SET NULL
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_transaction_learning_examples_account_source
            ON transaction_learning_examples(account_id, source, evidence_quality, id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_transaction_learning_examples_transaction
            ON transaction_learning_examples(transaction_id, id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_transaction_learning_examples_import_row
            ON transaction_learning_examples(import_session_row_id, id)
        """
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_transaction_learning_examples_dedupe
            ON transaction_learning_examples(dedupe_key)
            WHERE dedupe_key IS NOT NULL
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS transaction_learning_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            learning_example_id INTEGER,
            transaction_id INTEGER,
            event_type TEXT NOT NULL,
            source TEXT NOT NULL,
            evidence_quality TEXT CHECK(evidence_quality IN ('high','medium','low')),
            before_json TEXT NOT NULL DEFAULT '{}',
            after_json TEXT NOT NULL DEFAULT '{}',
            raw_evidence_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            FOREIGN KEY(learning_example_id) REFERENCES transaction_learning_examples(id) ON DELETE CASCADE,
            FOREIGN KEY(transaction_id) REFERENCES transactions(id) ON DELETE SET NULL
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_transaction_learning_events_example
            ON transaction_learning_events(learning_example_id, id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_transaction_learning_events_transaction
            ON transaction_learning_events(transaction_id, id)
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS prediction_feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            prediction_id TEXT,
            learning_example_id INTEGER,
            transaction_id INTEGER,
            import_session_row_id INTEGER,
            prediction_type TEXT NOT NULL,
            accepted INTEGER NOT NULL DEFAULT 0 CHECK(accepted IN (0, 1)),
            modified INTEGER NOT NULL DEFAULT 0 CHECK(modified IN (0, 1)),
            rejected INTEGER NOT NULL DEFAULT 0 CHECK(rejected IN (0, 1)),
            predicted_json TEXT NOT NULL DEFAULT '{}',
            final_json TEXT NOT NULL DEFAULT '{}',
            outcome TEXT NOT NULL DEFAULT 'modified' CHECK(outcome IN ('accepted','modified','rejected','skipped','cleared')),
            created_at TEXT NOT NULL,
            FOREIGN KEY(learning_example_id) REFERENCES transaction_learning_examples(id) ON DELETE SET NULL,
            FOREIGN KEY(transaction_id) REFERENCES transactions(id) ON DELETE SET NULL,
            FOREIGN KEY(import_session_row_id) REFERENCES import_session_rows(id) ON DELETE SET NULL
        )
        """
    )
    prediction_feedback_columns = set(table_columns(conn, "prediction_feedback"))
    if "outcome" not in prediction_feedback_columns:
        conn.execute(
            """
            ALTER TABLE prediction_feedback
            ADD COLUMN outcome TEXT NOT NULL DEFAULT 'modified'
            CHECK(outcome IN ('accepted','modified','rejected','skipped','cleared'))
            """
        )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_prediction_feedback_prediction
            ON prediction_feedback(prediction_id)
            WHERE prediction_id IS NOT NULL
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_prediction_feedback_example
            ON prediction_feedback(learning_example_id, id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_prediction_feedback_transaction
            ON prediction_feedback(transaction_id, id)
        """
    )
    return True


def ensure_prediction_feedback_outcome_schema(conn: sqlite3.Connection) -> bool | None:
    if not table_exists(conn, "prediction_feedback"):
        return False
    prediction_feedback_columns = set(table_columns(conn, "prediction_feedback"))
    if "outcome" not in prediction_feedback_columns:
        conn.execute(
            """
            ALTER TABLE prediction_feedback
            ADD COLUMN outcome TEXT NOT NULL DEFAULT 'modified'
            CHECK(outcome IN ('accepted','modified','rejected','skipped','cleared'))
            """
        )
    return True


def ensure_import_matching_rules_schema(conn: sqlite3.Connection) -> bool | None:
    if not table_exists(conn, "accounts"):
        return False

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS import_matching_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id INTEGER REFERENCES accounts(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0, 1)),
            priority INTEGER NOT NULL DEFAULT 100,
            condition_json TEXT NOT NULL DEFAULT '{}',
            action_json TEXT NOT NULL DEFAULT '{}',
            use_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            last_used_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_import_matching_rules_active
            ON import_matching_rules(enabled, account_id, priority, id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_import_matching_rules_account
            ON import_matching_rules(account_id, id)
        """
    )
    return True


def ensure_import_rule_proposals_schema(conn: sqlite3.Connection) -> bool | None:
    if not table_exists(conn, "accounts"):
        return False

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS import_rule_proposals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fingerprint TEXT NOT NULL UNIQUE,
            candidate_key TEXT NOT NULL,
            account_id INTEGER REFERENCES accounts(id) ON DELETE CASCADE,
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK(status IN ('pending','accepted','rejected','ignored')),
            condition_json TEXT NOT NULL DEFAULT '{}',
            action_json TEXT NOT NULL DEFAULT '{}',
            suggested_rule_json TEXT NOT NULL DEFAULT '{}',
            evidence_json TEXT NOT NULL DEFAULT '{}',
            reason_codes_json TEXT NOT NULL DEFAULT '[]',
            reviewer_decision TEXT,
            reviewer_note TEXT,
            approved_rule_id INTEGER REFERENCES import_matching_rules(id) ON DELETE SET NULL,
            validation_errors_json TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            reviewed_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_import_rule_proposals_status
            ON import_rule_proposals(status, account_id, updated_at)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_import_rule_proposals_account
            ON import_rule_proposals(account_id, id)
        """
    )
    return True


def ensure_savings_planner_schema(conn: sqlite3.Connection) -> bool | None:
    """Add the per-user Pay Yourself First plan and its percentage rules."""
    if not table_exists(conn, "accounts") or not table_exists(conn, "envelopes"):
        return False

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS savings_plans (
            id INTEGER PRIMARY KEY CHECK(id = 1),
            name TEXT NOT NULL DEFAULT 'Pay Yourself First',
            source_account_id INTEGER,
            source_envelope_id INTEGER,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(source_account_id) REFERENCES accounts(id) ON DELETE SET NULL,
            FOREIGN KEY(source_envelope_id) REFERENCES envelopes(id) ON DELETE SET NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS savings_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            plan_id INTEGER NOT NULL DEFAULT 1,
            name TEXT NOT NULL,
            contribution_basis_points INTEGER NOT NULL
                CHECK(contribution_basis_points > 0 AND contribution_basis_points <= 10000),
            accessible_account_id INTEGER NOT NULL,
            accessible_envelope_id INTEGER NOT NULL,
            long_term_account_id INTEGER,
            long_term_envelope_id INTEGER,
            accessible_target_cents INTEGER NOT NULL DEFAULT 0
                CHECK(accessible_target_cents >= 0),
            enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0, 1)),
            display_order INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            CHECK(
                (long_term_account_id IS NULL AND long_term_envelope_id IS NULL)
                OR
                (long_term_account_id IS NOT NULL AND long_term_envelope_id IS NOT NULL)
            ),
            FOREIGN KEY(plan_id) REFERENCES savings_plans(id) ON DELETE CASCADE,
            FOREIGN KEY(accessible_account_id) REFERENCES accounts(id),
            FOREIGN KEY(accessible_envelope_id) REFERENCES envelopes(id),
            FOREIGN KEY(long_term_account_id) REFERENCES accounts(id),
            FOREIGN KEY(long_term_envelope_id) REFERENCES envelopes(id)
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_savings_rules_plan_order
            ON savings_rules(plan_id, enabled DESC, display_order, id)
        """
    )
    return True


def ensure_savings_transfer_records_schema(conn: sqlite3.Connection) -> bool | None:
    """Persist consumed savings-preview groups for ledger-level idempotency."""
    if not table_exists(conn, "savings_plans") or not table_exists(conn, "transactions"):
        return False

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS savings_transfer_records (
            idempotency_key TEXT PRIMARY KEY,
            plan_id INTEGER NOT NULL DEFAULT 1,
            group_index INTEGER NOT NULL CHECK(group_index >= 0),
            tx_out_id INTEGER,
            tx_in_id INTEGER,
            created_at TEXT NOT NULL,
            FOREIGN KEY(plan_id) REFERENCES savings_plans(id) ON DELETE RESTRICT,
            FOREIGN KEY(tx_out_id) REFERENCES transactions(id) ON DELETE SET NULL,
            FOREIGN KEY(tx_in_id) REFERENCES transactions(id) ON DELETE SET NULL
        )
        """
    )
    return True



SCHEMA_MIGRATIONS = (
    ("20260502_01_account_metadata_schema", ensure_account_metadata_schema),
    ("20260503_01_envelope_archive_schema", ensure_envelope_archive_schema),
    ("20260506_01_investment_notes_schema", ensure_investment_notes_schema),
    ("20260512_01_reconciliation_schema", ensure_reconciliation_schema),
    ("20260517_01_transaction_remainder_intents_schema", ensure_transaction_remainder_intents_schema),
    ("20260525_01_import_provenance_schema", ensure_import_provenance_schema),
    ("20260527_01_payee_normalization_schema", ensure_payee_normalization_schema),
    ("20260602_01_transaction_import_validations_schema", ensure_transaction_import_validations_schema),
    ("20260604_01_import_review_drafts_schema", ensure_import_review_drafts_schema),
    ("20260604_02_import_review_sources_schema", ensure_import_review_sources_schema),
    ("20260605_01_remove_credit_card_default_paying_bank", ensure_credit_cards_default_paying_bank_removed),
    ("20260605_02_account_locked_unallocated_backfill", ensure_account_locked_unallocated_backfill),
    ("20260605_03_loan_monthly_payment_schema", ensure_loan_monthly_payment_schema),
    ("20260621_01_transaction_learning_schema", ensure_transaction_learning_schema),
    ("20260621_02_payee_memo_normalization_schema", ensure_payee_memo_normalization_schema),
    ("20260621_03_prediction_feedback_outcome_schema", ensure_prediction_feedback_outcome_schema),
    ("20260713_01_import_matching_rules_schema", ensure_import_matching_rules_schema),
    ("20260715_01_import_rule_proposals_schema", ensure_import_rule_proposals_schema),
    ("20260720_01_savings_planner_schema", ensure_savings_planner_schema),
    ("20260720_02_savings_transfer_records_schema", ensure_savings_transfer_records_schema),
)


def run_schema_migrations(conn: sqlite3.Connection) -> None:
    run_savepoint = "schema_migrations_run"
    conn.execute(f"SAVEPOINT {run_savepoint}")
    try:
        ensure_schema_migrations_table(conn)
        applied = applied_schema_migrations(conn)
        for index, (name, migration) in enumerate(SCHEMA_MIGRATIONS):
            if name in applied:
                continue

            step_savepoint = f"schema_migration_step_{index}"
            conn.execute(f"SAVEPOINT {step_savepoint}")
            try:
                applied_now = migration(conn)
                if applied_now is False:
                    # An unmet prerequisite must not leave partial DDL or data
                    # behind merely because the migration declined to apply.
                    conn.execute(f"ROLLBACK TO {step_savepoint}")
                    conn.execute(f"RELEASE {step_savepoint}")
                    continue
                conn.execute(
                    "INSERT INTO schema_migrations(name, applied_at) VALUES(?, ?)",
                    (name, datetime.utcnow().isoformat(timespec="seconds")),
                )
                conn.execute(f"RELEASE {step_savepoint}")
            except Exception:
                conn.execute(f"ROLLBACK TO {step_savepoint}")
                conn.execute(f"RELEASE {step_savepoint}")
                raise
        conn.execute(f"RELEASE {run_savepoint}")
    except Exception:
        conn.execute(f"ROLLBACK TO {run_savepoint}")
        conn.execute(f"RELEASE {run_savepoint}")
        raise
    conn.commit()


def schema_sql_from_template(template: Path) -> str:
    src = sqlite3.connect(template)
    try:
        schema_lines = []
        for line in src.iterdump():
            statement = line.lstrip().upper()
            if statement.startswith("INSERT INTO"):
                continue
            if statement.startswith("DELETE FROM") and "SQLITE_SEQUENCE" in statement:
                continue
            schema_lines.append(line)
        return "\n".join(schema_lines)
    finally:
        src.close()


def initialize_empty_db_from_template(db_path: Path, template: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    dst = sqlite3.connect(db_path)
    dst.row_factory = sqlite3.Row
    try:
        dst.executescript(schema_sql_from_template(template))
        dst.execute("PRAGMA foreign_keys = ON")
        run_schema_migrations(dst)
        dst.commit()
    finally:
        dst.close()


def _rehome_legacy_user_paths(meta: sqlite3.Connection, app) -> None:
    if app.config.get("TESTING") and app.config.get("FORBID_EXTERNAL_TEST_DB_PATHS", False):
        # Test fixtures are explicitly rewritten by tests.helpers.prepare_app_data().
        # If a test deliberately points metadata outside the temp tree, preserve that
        # value so the runtime tripwire can fail closed instead of silently rehoming it.
        return

    default_db = Path(app.config["DB_PATH"]).resolve()
    user_db_dir = Path(app.config["USER_DB_DIR"]).resolve()
    rows = meta.execute("SELECT id, name, db_path FROM users").fetchall()

    for row in rows:
        db_path_str = row["db_path"] or ""
        current = Path(db_path_str)
        if _path_exists(current):
            continue

        name = (row["name"] or "").strip().lower()
        basename = _basename_any_platform(db_path_str)
        parts = _parts_any_platform(db_path_str)
        new_path = None

        if name == "default" or basename == "data.sqlite":
            new_path = default_db
        elif "user_dbs" in parts and basename:
            candidate = user_db_dir / basename
            if _path_exists(candidate):
                new_path = candidate

        if new_path and str(new_path) != db_path_str:
            meta.execute("UPDATE users SET db_path=? WHERE id=?", (str(new_path), row["id"]))

def _ensure_schema(conn: sqlite3.Connection):
    """If essential tables are missing, load template schema, then run migrations."""
    try:
        if not table_exists(conn, "accounts"):
            template = Path(current_app.config['DB_PATH'])
            if not template.exists():
                return  # nothing we can do

            conn.executescript(schema_sql_from_template(template))
            conn.execute("PRAGMA foreign_keys = ON")
            conn.commit()
        run_schema_migrations(conn)
    except Exception:
        conn.rollback()
        raise

@contextmanager
def unit_of_work(*, immediate: bool = False):
    """
    Single SQLite transaction using the request's DB connection.
    Ensures all writes succeed or none do.

    Use ``immediate=True`` when a read-validate-write sequence must serialize
    with other writers before it reads the state it will validate.
    """
    db = get_db()
    try:
        db.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        yield db
        db.commit()
    except Exception:
        # Best-effort rollback; re-raise original error
        try:
            db.rollback()
        except Exception:
            pass
        raise
