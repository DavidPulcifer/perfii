import sqlite3
from unittest.mock import patch

from app.db import (
    ACCOUNT_IDENTIFIER_INDEX,
    applied_schema_migrations,
    close_db,
    index_columns,
    index_exists,
    list_table_names,
    quote_identifier,
    initialize_empty_db_from_template,
    get_db,
    get_meta_db,
    run_schema_migrations,
    schema_sql_from_template,
    table_columns,
    table_schema_sql,
    table_exists,
)
from flask import session
from tests.helpers import FinanceAppTestCase


class DbSchemaTests(FinanceAppTestCase):
    def test_schema_sql_from_template_excludes_data_inserts(self) -> None:
        schema_sql = schema_sql_from_template(self.app_data_dir / "data.sqlite")

        self.assertIn("CREATE TABLE", schema_sql)
        self.assertNotIn("INSERT INTO", schema_sql.upper())

    def test_initialize_empty_db_from_template_copies_schema_without_data(self) -> None:
        dst = self.temp_path / "new-user.sqlite"

        initialize_empty_db_from_template(dst, self.app_data_dir / "data.sqlite")

        conn = sqlite3.connect(dst)
        try:
            accounts_table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='accounts'"
            ).fetchone()
            account_count = conn.execute("SELECT COUNT(1) FROM accounts").fetchone()[0]
            split_foreign_keys = conn.execute("PRAGMA foreign_key_list(transaction_splits)").fetchall()
            migrations_table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
            ).fetchone()
            migrations_count = conn.execute("SELECT COUNT(1) FROM schema_migrations").fetchone()[0]
        finally:
            conn.close()

        self.assertIsNotNone(accounts_table)
        self.assertEqual(account_count, 0)
        self.assertGreaterEqual(len(split_foreign_keys), 1)
        self.assertIsNotNone(migrations_table)
        self.assertGreaterEqual(migrations_count, 4)

    def test_schema_introspection_helpers_read_tables_and_indexes(self) -> None:
        conn = sqlite3.connect(self.app_data_dir / "data.sqlite")
        conn.row_factory = sqlite3.Row
        try:
            self.assertEqual(quote_identifier('weird"name'), '"weird""name"')
            self.assertIn("accounts", list_table_names(conn))
            self.assertTrue(table_exists(conn, "accounts"))
            self.assertFalse(table_exists(conn, "missing_table"))
            self.assertIn("name", table_columns(conn, "accounts"))
            self.assertTrue(index_exists(conn, "sqlite_autoindex_accounts_1"))
            self.assertIn("acct_key", index_columns(conn, "sqlite_autoindex_accounts_1"))
            self.assertTrue(any("CREATE TABLE accounts" in sql for sql in table_schema_sql(conn, "accounts")))
        finally:
            conn.close()

    def test_import_matching_rules_schema_exists(self) -> None:
        conn = sqlite3.connect(self.app_data_dir / "data.sqlite")
        conn.row_factory = sqlite3.Row
        try:
            run_schema_migrations(conn)
            self.assertTrue(table_exists(conn, "import_matching_rules"))
            columns = table_columns(conn, "import_matching_rules")
            self.assertIn("condition_json", columns)
            self.assertIn("action_json", columns)
            self.assertTrue(index_exists(conn, "idx_import_matching_rules_active"))
        finally:
            conn.close()

    def test_remainder_intent_migration_not_marked_applied_without_base_tables(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        try:
            run_schema_migrations(conn)

            self.assertNotIn(
                "20260517_01_transaction_remainder_intents_schema",
                applied_schema_migrations(conn),
            )
            self.assertFalse(table_exists(conn, "transaction_remainder_intents"))
        finally:
            conn.close()

    def test_schema_migration_failure_rolls_back_the_complete_run(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row

        def successful_step(active_conn: sqlite3.Connection) -> None:
            active_conn.execute("CREATE TABLE synthetic_first_step(id INTEGER PRIMARY KEY)")

        def failing_step(active_conn: sqlite3.Connection) -> None:
            active_conn.execute("CREATE TABLE synthetic_partial_step(id INTEGER PRIMARY KEY)")
            raise RuntimeError("synthetic migration failure")

        try:
            with patch(
                "app.db.SCHEMA_MIGRATIONS",
                (
                    ("synthetic_01_success", successful_step),
                    ("synthetic_02_failure", failing_step),
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "synthetic migration failure"):
                    run_schema_migrations(conn)

            self.assertFalse(table_exists(conn, "synthetic_first_step"))
            self.assertFalse(table_exists(conn, "synthetic_partial_step"))
            self.assertFalse(table_exists(conn, "schema_migrations"))
        finally:
            conn.close()

    def test_declined_migration_rolls_back_its_partial_changes(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row

        def declined_step(active_conn: sqlite3.Connection) -> bool:
            active_conn.execute("CREATE TABLE synthetic_declined_step(id INTEGER PRIMARY KEY)")
            return False

        try:
            with patch("app.db.SCHEMA_MIGRATIONS", (("synthetic_declined", declined_step),)):
                run_schema_migrations(conn)

            self.assertFalse(table_exists(conn, "synthetic_declined_step"))
            self.assertNotIn("synthetic_declined", applied_schema_migrations(conn))
        finally:
            conn.close()

    def test_run_schema_migrations_records_applied_migrations_and_is_repeatable(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        try:
            conn.executescript(
                """
                CREATE TABLE accounts (
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    account_type TEXT NOT NULL DEFAULT 'bank',
                    acct_key TEXT NOT NULL UNIQUE,
                    display_order INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE envelopes (
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL
                );
                CREATE TABLE transactions (
                    id INTEGER PRIMARY KEY,
                    account_id INTEGER,
                    amount_cents INTEGER
                );
                """
            )

            run_schema_migrations(conn)
            first_applied = applied_schema_migrations(conn)
            run_schema_migrations(conn)
            second_applied = applied_schema_migrations(conn)

            self.assertEqual(first_applied, second_applied)
            self.assertIn("20260502_01_account_metadata_schema", first_applied)
            self.assertNotIn("20260502_02_payee_learning_schema", first_applied)
            self.assertIn("20260503_01_envelope_archive_schema", first_applied)
            self.assertIn("20260506_01_investment_notes_schema", first_applied)
            self.assertIn("20260517_01_transaction_remainder_intents_schema", first_applied)
            self.assertTrue(table_exists(conn, "transaction_remainder_intents"))
            self.assertTrue(index_exists(conn, "idx_transaction_remainder_intents_envelope"))
            self.assertTrue(table_exists(conn, "import_sessions"))
            self.assertTrue(table_exists(conn, "import_session_rows"))
            self.assertTrue(table_exists(conn, "import_row_matches"))
            self.assertTrue(index_exists(conn, "idx_import_session_rows_fingerprint"))
            self.assertTrue(table_exists(conn, "import_review_sources"))
            self.assertTrue(index_exists(conn, "idx_import_review_sources_account_token"))
            self.assertTrue(index_exists(conn, "idx_import_review_sources_expires"))
            self.assertTrue(table_exists(conn, "transaction_import_validations"))
            self.assertTrue(index_exists(conn, "idx_transaction_import_validations_account"))
            self.assertTrue(index_exists(conn, "idx_transaction_import_validations_fitid"))
            self.assertTrue(index_exists(conn, "idx_transaction_import_validations_fingerprint"))
            self.assertTrue(table_exists(conn, "transaction_learning_examples"))
            self.assertTrue(table_exists(conn, "transaction_learning_events"))
            self.assertTrue(table_exists(conn, "prediction_feedback"))
            self.assertTrue(index_exists(conn, "idx_transaction_learning_examples_account_source"))
            self.assertTrue(index_exists(conn, "idx_transaction_learning_examples_dedupe"))
            self.assertTrue(index_exists(conn, "idx_transaction_learning_events_example"))
            self.assertTrue(index_exists(conn, "idx_prediction_feedback_prediction"))
            learning_columns = table_columns(conn, "transaction_learning_examples")
            self.assertIn("outcome", table_columns(conn, "prediction_feedback"))
            for column_name in (
                "raw_payee",
                "raw_memo",
                "raw_profile_json",
                "final_payee",
                "final_memo",
                "final_profile_json",
                "transaction_type",
                "transfer_other_account_id",
                "splits_json",
                "remainder_intent_json",
                "decision_json",
                "evidence_quality",
                "created_at",
                "updated_at",
            ):
                self.assertIn(column_name, learning_columns)
            payee_cleanup_columns = table_columns(conn, "payee_normalization_rules")
            self.assertIn("canonical_memo", payee_cleanup_columns)
            self.assertIn("payee_changed", payee_cleanup_columns)
            self.assertIn("memo_changed", payee_cleanup_columns)
            self.assertIn("opening_balance_cents", table_columns(conn, "accounts"))
            self.assertIn("opening_date", table_columns(conn, "accounts"))
            self.assertIn("bankid", table_columns(conn, "accounts"))
            self.assertIn("acctid", table_columns(conn, "accounts"))
            self.assertFalse(table_exists(conn, "payee_aliases"))
            self.assertFalse(table_exists(conn, "payee_envelope_stats"))
            self.assertTrue(index_exists(conn, ACCOUNT_IDENTIFIER_INDEX))
            self.assertEqual(index_columns(conn, ACCOUNT_IDENTIFIER_INDEX), ["bankid", "acctid"])

            conn.execute(
                """
                CREATE TABLE credit_cards (
                    account_id INTEGER PRIMARY KEY REFERENCES accounts(id) ON DELETE CASCADE,
                    credit_limit_cents INTEGER NOT NULL DEFAULT 0,
                    default_paying_account_id INTEGER REFERENCES accounts(id) ON DELETE SET NULL
                )
                """
            )
            conn.execute(
                "INSERT INTO credit_cards(account_id, credit_limit_cents, default_paying_account_id) VALUES(1, 250000, NULL)"
            )
            run_schema_migrations(conn)
            self.assertIn("20260605_01_remove_credit_card_default_paying_bank", applied_schema_migrations(conn))
            self.assertEqual(table_columns(conn, "credit_cards"), ["account_id", "credit_limit_cents"])
            row = conn.execute("SELECT account_id, credit_limit_cents FROM credit_cards").fetchone()
            self.assertEqual(dict(row), {"account_id": 1, "credit_limit_cents": 250000})

            conn.execute(
                """
                CREATE TABLE loans (
                    account_id INTEGER PRIMARY KEY REFERENCES accounts(id) ON DELETE CASCADE,
                    original_principal_cents INTEGER,
                    note TEXT
                )
                """
            )
            run_schema_migrations(conn)
            self.assertIn("20260605_03_loan_monthly_payment_schema", applied_schema_migrations(conn))
            self.assertIn("normal_monthly_payment_cents", table_columns(conn, "loans"))
        finally:
            conn.close()

    def test_transaction_learning_schema_preserves_evidence_and_fk_behavior(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.executescript(
                """
                CREATE TABLE accounts (
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    account_type TEXT NOT NULL DEFAULT 'bank',
                    acct_key TEXT NOT NULL UNIQUE,
                    display_order INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE envelopes (
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL
                );
                CREATE TABLE transactions (
                    id INTEGER PRIMARY KEY,
                    account_id INTEGER,
                    amount_cents INTEGER
                );
                INSERT INTO accounts(id, name, account_type, acct_key, display_order)
                VALUES (1, 'Checking', 'bank', 'acct:checking', 1),
                       (2, 'Savings', 'bank', 'acct:savings', 2);
                INSERT INTO transactions(id, account_id, amount_cents) VALUES (10, 1, -12500);
                """
            )
            run_schema_migrations(conn)
            run_schema_migrations(conn)

            now = "2026-06-21T08:00:00"
            conn.execute(
                """
                INSERT INTO import_sessions(id, account_id, created_at)
                VALUES (1, 1, ?)
                """,
                (now,),
            )
            conn.execute(
                """
                INSERT INTO import_session_rows(
                    id, session_id, row_index, posted_at, amount_cents, payee, memo,
                    fitid, row_fingerprint, evidence_json, transaction_id, match_type, created_at
                )
                VALUES (1, 1, 0, '2026-06-20', -12500, 'Online Transfer', 'to SAV ...0101',
                        'fit-1', 'fp-1', '{"source":"csv"}', 10, 'created', ?)
                """,
                (now,),
            )
            conn.execute(
                """
                INSERT INTO transaction_import_validations(
                    id, account_id, transaction_id, validated_at, source, fitid,
                    row_fingerprint, import_session_row_id, match_type, evidence_json,
                    created_at, updated_at
                )
                VALUES (1, 1, 10, ?, 'import_commit', 'fit-1', 'fp-1', 1, 'created',
                        '{"validated":true}', ?, ?)
                """,
                (now, now, now),
            )
            conn.execute(
                """
                INSERT INTO transaction_learning_examples(
                    account_id, transaction_id, import_session_row_id,
                    transaction_import_validation_id, source, evidence_quality,
                    dedupe_key, posted_at, amount_cents, raw_payee, raw_memo,
                    raw_profile_json, final_payee, final_memo, final_profile_json,
                    transaction_type, transfer_other_account_id, splits_json,
                    remainder_intent_json, decision_json, evidence_json, created_at, updated_at
                )
                VALUES (
                    1, 10, 1, 1, 'import_commit', 'high', 'import-row:1',
                    '2026-06-20', -12500, 'Online Transfer', 'to SAV ...0101',
                    '{"account_suffixes":["0101"]}', 'Example Bank - 0101', 'Savings transfer',
                    '{"merchant_tokens":["example"]}', 'transfer', 2,
                    '[{"envelope_id":5,"amount_cents":-12500}]',
                    '{"envelope_id":5,"amount_cents":-12500}',
                    '{"kind":"transfer","other_account_id":2}',
                    '{"source_bankid":"EXAMPLE-BANK"}', ?, ?
                )
                """,
                (now, now),
            )
            example_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                """
                INSERT INTO transaction_learning_events(
                    learning_example_id, transaction_id, event_type, source,
                    evidence_quality, before_json, after_json, raw_evidence_json, created_at
                )
                VALUES (?, 10, 'created', 'import_commit', 'high',
                        '{"payee":"Online Transfer"}', '{"payee":"Example Bank - 0101"}',
                        '{"raw_memo":"to SAV ...0101"}', ?)
                """,
                (example_id, now),
            )
            conn.execute(
                """
                INSERT INTO prediction_feedback(
                    prediction_id, learning_example_id, transaction_id, import_session_row_id,
                    prediction_type, accepted, modified, rejected, predicted_json, final_json, outcome, created_at
                )
                VALUES ('pred-1', ?, 10, 1, 'transfer', 1, 0, 0,
                        '{"other_account_id":2}', '{"other_account_id":2}', 'accepted', ?)
                """,
                (example_id, now),
            )

            example = conn.execute("SELECT * FROM transaction_learning_examples").fetchone()
            self.assertEqual(example["raw_payee"], "Online Transfer")
            self.assertEqual(example["raw_memo"], "to SAV ...0101")
            self.assertEqual(example["raw_profile_json"], '{"account_suffixes":["0101"]}')
            self.assertEqual(example["final_payee"], "Example Bank - 0101")
            self.assertEqual(example["final_memo"], "Savings transfer")
            self.assertEqual(example["transaction_type"], "transfer")
            self.assertEqual(example["transfer_other_account_id"], 2)
            self.assertEqual(example["evidence_quality"], "high")
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    """
                    INSERT INTO transaction_learning_examples(
                        account_id, source, evidence_quality, dedupe_key, created_at, updated_at
                    )
                    VALUES (1, 'backfill', 'medium', 'import-row:1', ?, ?)
                    """,
                    (now, now),
                )

            conn.execute("DELETE FROM import_session_rows WHERE id=1")
            self.assertIsNone(
                conn.execute("SELECT import_session_row_id FROM transaction_learning_examples").fetchone()[0]
            )
            self.assertIsNone(conn.execute("SELECT import_session_row_id FROM prediction_feedback").fetchone()[0])

            conn.execute("DELETE FROM transactions WHERE id=10")
            self.assertIsNone(conn.execute("SELECT transaction_id FROM transaction_learning_examples").fetchone()[0])
            self.assertIsNone(conn.execute("SELECT transaction_id FROM transaction_learning_events").fetchone()[0])
            self.assertIsNone(conn.execute("SELECT transaction_id FROM prediction_feedback").fetchone()[0])

            conn.execute("DELETE FROM transaction_learning_examples WHERE id=?", (example_id,))
            self.assertEqual(conn.execute("SELECT COUNT(1) FROM transaction_learning_events").fetchone()[0], 0)
            self.assertIsNone(conn.execute("SELECT learning_example_id FROM prediction_feedback").fetchone()[0])
        finally:
            conn.close()

    def test_get_db_upgrades_partial_user_database_through_ensure_schema(self) -> None:
        old_db = self.temp_path / "old-user.sqlite"
        conn = sqlite3.connect(old_db)
        try:
            conn.executescript(
                """
                CREATE TABLE accounts (
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    account_type TEXT NOT NULL DEFAULT 'bank',
                    acct_key TEXT NOT NULL UNIQUE,
                    display_order INTEGER NOT NULL DEFAULT 0
                );
                INSERT INTO accounts (id, name, account_type, acct_key, display_order)
                VALUES (1, 'Old Checking', 'bank', 'acct:old-checking', 1);
                """
            )
            conn.commit()
        finally:
            conn.close()

        meta = get_meta_db()
        meta.execute(
            "INSERT INTO users(name, db_path, created_at) VALUES(?, ?, '2026-05-02T00:00:00')",
            ("old-partial", str(old_db)),
        )
        user_id = meta.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        meta.commit()
        close_db()
        session["user_id"] = int(user_id)

        db = get_db()

        self.assertEqual(db.execute("SELECT name FROM accounts WHERE id=1").fetchone()["name"], "Old Checking")
        self.assertEqual(db.execute("SELECT opening_balance_cents FROM accounts WHERE id=1").fetchone()[0], 0)
        self.assertIn("schema_migrations", list_table_names(db))
        self.assertNotIn("payee_aliases", list_table_names(db))
        self.assertNotIn("payee_envelope_stats", list_table_names(db))
        self.assertIn("payee_normalization_rules", list_table_names(db))
