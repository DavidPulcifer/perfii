from io import BytesIO
from pathlib import Path
import re
import unittest

from app import create_app
from app.db import get_db, get_meta_db
from app.repositories import accounts_repo, envelopes_repo
from app.services.transactions_service import TransactionsService
from tests.helpers import FinanceAppTestCase, build_test_config


class AppSmokeTests(FinanceAppTestCase):
    def _select_user_in_client(self) -> None:
        row = get_meta_db().execute(
            "SELECT id FROM users WHERE LOWER(name)=LOWER(?) ORDER BY id LIMIT 1",
            ("test user",),
        ).fetchone()
        if row is None:
            row = get_meta_db().execute("SELECT id FROM users ORDER BY id LIMIT 1").fetchone()
        self.assertIsNotNone(row)
        with self.client.session_transaction() as client_session:
            client_session["user_id"] = int(row["id"])

    def test_users_page_loads_without_active_user(self) -> None:
        response = self.client.get("/users/")
        self.assertEqual(response.status_code, 200)

    def test_display_name_configuration_changes_visible_branding(self) -> None:
        class BrandedConfig(build_test_config(self.app_data_dir)):
            APP_DISPLAY_NAME = "Synthetic Household Plan"

        app = create_app(BrandedConfig)
        response = app.test_client().get("/users/")
        html = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn("<title>Synthetic Household Plan</title>", html)
        self.assertIn(">Synthetic Household Plan</a>", html)

    def test_display_name_configuration_rejects_control_characters(self) -> None:
        class InvalidBrandConfig(build_test_config(self.app_data_dir)):
            APP_DISPLAY_NAME = "Unsafe\nName"

        with self.assertRaisesRegex(RuntimeError, "APP_DISPLAY_NAME"):
            create_app(InvalidBrandConfig)

    def test_users_page_shows_user_names_without_database_paths(self) -> None:
        response = self.client.get("/users/")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("User", html)
        self.assertNotIn("DB Path", html)
        for row in get_meta_db().execute("SELECT name, db_path FROM users"):
            self.assertIn(row["name"], html)
            self.assertNotIn(row["db_path"], html)

    def test_root_redirects_to_user_selection_without_active_user(self) -> None:
        response = self.client.get("/", follow_redirects=False)
        self.assertEqual(response.status_code, 302)
        self.assertIn("/users/", response.headers["Location"])

    def test_dev_db_route_is_not_registered_even_if_legacy_flag_is_set(self) -> None:
        class LegacyFlagConfig(build_test_config(self.app_data_dir)):
            DEV_DB_TOOLS = True

        app = create_app(LegacyFlagConfig)

        self.assertNotIn("dev_db.dev_db_index", app.view_functions)
        self.assertFalse(
            any(rule.rule.startswith("/dev-db/") for rule in app.url_map.iter_rules())
        )

    def test_dashboard_transfer_envelope_fields_start_blank(self) -> None:
        self._select_user_in_client()
        response = self.client.get("/")

        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertRegex(html, r'name="transfer_from_\d+"\s+value=""')
        self.assertRegex(html, r'name="transfer_to_\d+"\s+value=""')

    def test_dashboard_transfer_modal_has_account_envelope_balance_json(self) -> None:
        self._select_user_in_client()
        account_id = accounts_repo.insert_account({"name": "Balance JSON Account", "account_type": "bank"})
        envelope_id = envelopes_repo.insert_envelope({"name": "Balance JSON Envelope"})
        db = get_db()
        tx_id = db.execute(
            """
            INSERT INTO transactions (account_id, ttype, amount_cents, posted_at, payee, memo)
            VALUES (?, 'income', 12345, '2026-07-05', 'Seed', 'modal balance json')
            RETURNING id
            """,
            (account_id,),
        ).fetchone()["id"]
        db.execute(
            "INSERT INTO transaction_splits (transaction_id, envelope_id, amount_cents) VALUES (?, ?, 12345)",
            (tx_id, envelope_id),
        )
        db.commit()

        response = self.client.get("/")

        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn('id="balances-json"', html)
        self.assertIn(f'"{account_id}"', html)
        self.assertIn(f'"{envelope_id}": 12345', html)

    def test_dashboard_async_balances_preserve_selector_json_shape(self) -> None:
        template = Path("app/templates/index.html").read_text()

        self.assertIn("function selectorBalancesJSON(raw)", template)
        self.assertIn("Array.isArray(parsed)", template)
        self.assertIn("out[accountKey][envelopeKey] = parseInt(row.total || 0, 10) || 0", template)
        self.assertIn("modalJSON.textContent = selectorBalancesJSON(asyncJSON.textContent)", template)

    def test_dashboard_expense_and_income_defaults_are_unchanged(self) -> None:
        self._select_user_in_client()
        response = self.client.get("/")

        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertRegex(html, r'name="expense_\d+"\s+value="0\.00"')
        self.assertRegex(html, r'name="income_\d+"\s+value="0\.00"')


    def test_dashboard_account_rows_link_name_and_balance_without_reconcile_or_view_buttons(self) -> None:
        self._select_user_in_client()
        response = self.client.get("/")

        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('>Reconcile</a>', html)
        self.assertNotIn('>View</a>', html)
        self.assertIn('href="/bank/1"', html)
        credit_account = next(
            account for account in accounts_repo.list_accounts()
            if account["account_type"] == "credit_card"
        )
        self.assertIn(f'href="/credit/{credit_account["id"]}"', html)


    def test_dashboard_bank_accounts_hide_low_value_metadata(self) -> None:
        self._select_user_in_client()
        accounts_repo.insert_account({
            "name": "Metadata Hidden Checking",
            "account_type": "bank",
            "bankid": "EXAMPLEBANK",
            "acctid": "DEMOACCT0001",
            "opening_date": "2026-01-02",
        })

        response = self.client.get("/")

        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Metadata Hidden Checking", html)
        self.assertNotIn("BANK EXAMPLEBANK", html)
        self.assertNotIn("ACCT DEMOACCT0001", html)
        self.assertNotIn("start 2026-01-02", html)

    def test_dashboard_template_does_not_show_account_start_dates(self) -> None:
        template = Path("app/templates/index.html").read_text()

        self.assertNotIn("start {{ a.opening_date }}", template)
        self.assertNotIn("a.opening_date", template)

    def test_bank_account_detail_uses_single_clean_summary_panel(self) -> None:
        self._select_user_in_client()
        response = self.client.get("/bank/1")

        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Account Summary", html)
        self.assertIn("Current Balance", html)
        self.assertIn("Envelope balances", html)
        self.assertNotIn("Account Details", html)
        self.assertNotIn("Envelopes on this Account", html)
        summary = html.split('<!-- Recent transactions -->', 1)[0]
        self.assertNotIn('>Type</th>', summary)
        self.assertNotIn('>bank</td>', summary)
        self.assertNotIn('text-bg-light ms-2">locked</span>', summary)
        bank_template = Path("app/templates/bank.html").read_text()
        self.assertNotIn("BANK {{ acct.bankid }}", bank_template)
        self.assertNotIn("ACCT {{ acct.acctid }}", bank_template)
        self.assertIn('href="/tx/1"', html)
        self.assertIn('href="/reconcile/accounts/1"', html)
        self.assertIn('href="/accounts/1/edit"', html)


    def test_accounts_form_groups_investment_opening_fields(self) -> None:
        template = Path("app/templates/accounts.html").read_text()

        opening_date_index = template.index("Opening date")
        opening_balance_index = template.index("Opening balance ($)")
        initial_value_index = template.index("Initial value ($)")
        self.assertLess(opening_date_index, opening_balance_index)
        self.assertLess(opening_balance_index, initial_value_index)
        self.assertIn('class="col-md-4 d-none" data-type="investment"', template)

    def test_import_upload_template_includes_account_picker_and_detection_endpoint(self) -> None:
        self._select_user_in_client()
        accounts_repo.insert_account({
            "name": "JSON Safe Checking",
            "account_type": "bank",
            "bankid": "ROUTE-SMOKE-SECRET",
            "acctid": "ACCT-SMOKE-SECRET",
        })
        response = self.client.get("/imports/")

        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn('id="importUploadForm"', html)
        self.assertIn('data-detect-account-url="/imports/detect-account"', html)
        self.assertIn('id="uploadAccount"', html)
        self.assertIn('Auto-detect from file', html)
        self.assertNotIn('id="accountDetectionStatus"', html)
        self.assertIn("JSON Safe Checking", html)
        self.assertNotIn("ROUTE-SMOKE-SECRET", html)
        self.assertNotIn("ACCT-SMOKE-SECRET", html)

    def test_import_rules_page_and_modal_create_rule_flow(self) -> None:
        self._select_user_in_client()
        account = accounts_repo.list_accounts()[0]
        envelope = envelopes_repo.list_envelopes()[0]

        response = self.client.get("/imports/rules")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("Import Rules", html)
        self.assertIn("Create Rule", html)

        created = self.client.post(
            "/imports/rules",
            data={
                "name": "Smoke Grocery Rule",
                "account_scope": "account",
                "account_id": str(account["id"]),
                "enabled": "1",
                "priority": "100",
                "direction": "expense",
                "match_field": "payee",
                "match_operator": "contains",
                "match_value": "grocery",
                "action_payee": "Groceries",
                "action_transaction_type": "expense",
                "action_envelope_id": str(envelope["id"]),
            },
            follow_redirects=True,
        )
        self.assertEqual(created.status_code, 200)
        self.assertIn("Smoke Grocery Rule", created.get_data(as_text=True))

        home = self.client.get("/")
        self.assertEqual(home.status_code, 200)
        home_html = home.get_data(as_text=True)
        self.assertIn('id="createImportRuleModal"', home_html)
        self.assertIn('data-create-rule-from-form="#expenseForm"', home_html)
        self.assertIn('data-create-rule-from-form="#incomeForm"', home_html)
        self.assertIn('data-create-rule-from-form="#transferForm"', home_html)

    def test_transaction_edit_page_has_create_rule_entry_point(self) -> None:
        self._select_user_in_client()
        account = accounts_repo.list_accounts()[0]
        envelope = envelopes_repo.list_envelopes()[0]
        tx_id = TransactionsService.create_expense(
            payload={
                "account_id": account["id"],
                "posted_at": "2026-07-14",
                "payee": "Past Rule Merchant",
                "memo": "proactive rule source",
                "amount": "12.34",
            },
            splits=[{"envelope_id": envelope["id"], "amount": "12.34"}],
        )

        response = self.client.get(f"/tx/{tx_id}/edit")

        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn('id="transactionEditForm"', html)
        self.assertIn('data-type="expense"', html)
        self.assertIn('data-create-rule-from-form="#transactionEditForm"', html)
        self.assertIn('data-bs-target="#createImportRuleModal"', html)
        self.assertIn(f'name="edit_amt_{envelope["id"]}"', html)

    def test_transfer_edit_page_offers_transfer_rule_button(self) -> None:
        self._select_user_in_client()
        account_from, account_to = accounts_repo.list_accounts()[:2]
        envelope_from_id = envelopes_repo.insert_envelope(
            {
                "name": "Synthetic Transfer Rule Source",
                "locked_account_id": account_from["id"],
            }
        )
        envelope_to_id = envelopes_repo.insert_envelope(
            {
                "name": "Synthetic Transfer Rule Destination",
                "locked_account_id": account_to["id"],
            }
        )
        tx_out_id, _ = TransactionsService.create_transfer(
            payload={
                "from_account_id": account_from["id"],
                "to_account_id": account_to["id"],
                "posted_at": "2026-07-14",
                "memo": "transfer rule gated",
                "amount": "45.67",
            },
            out_splits=[{"envelope_id": envelope_from_id, "amount": "45.67"}],
            in_splits=[{"envelope_id": envelope_to_id, "amount": "45.67"}],
        )

        response = self.client.get(f"/tx/transfer/{tx_out_id}/edit")

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('id="transferEditForm"', html)
        self.assertIn('data-type="transfer"', html)
        self.assertIn('data-create-rule-from-form="#transferEditForm"', html)

    def test_import_detect_account_endpoint_matches_statement_identifier(self) -> None:
        self._select_user_in_client()
        account_id = accounts_repo.insert_account({
            "name": "Example Checking 0001",
            "account_type": "bank",
            "bankid": "EXAMPLEBANK",
            "acctid": "DEMOACCT0001",
        })

        response = self.client.post(
            "/imports/detect-account",
            data={
                "statement": (
                    BytesIO(
                        b"<OFX>\n"
                        b"<BANKID>EXAMPLEBANK\n"
                        b"<ACCTID>DEMOACCT0001\n"
                        b"<STMTTRN>\n"
                        b"<DTPOSTED>20260429120000[-8:UTC]\n"
                        b"<TRNAMT>-45.67\n"
                        b"<NAME>Grocery Store\n"
                        b"<FITID>fit-001\n"
                        b"</STMTTRN>\n"
                    ),
                    "statement.qfx",
                )
            },
            content_type="multipart/form-data",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["detected"])
        self.assertEqual(payload["account_id"], account_id)
        self.assertEqual(payload["account_name"], "Example Checking 0001")
        self.assertNotIn("bankid", payload)
        self.assertNotIn("acctid", payload)

    def test_import_detect_account_endpoint_reports_filename_match(self) -> None:
        self._select_user_in_client()
        account_id = accounts_repo.insert_account({
            "name": "Northstar Cash Account",
            "account_type": "bank",
            "bankid": "",
            "acctid": "",
        })

        response = self.client.post(
            "/imports/detect-account",
            data={
                "statement": (
                    BytesIO(b"Date,Amount,Name,Memo,Id\n2026-05-08,-12.34,Coffee,Latte,fit-001\n"),
                    "Northstar_Transactions_2026-06.csv",
                )
            },
            content_type="multipart/form-data",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["detected"])
        self.assertEqual(payload["account_id"], account_id)
        self.assertEqual(payload["message"], "Detected account from filename: Northstar Cash Account.")

    def test_import_upload_requires_manual_account_when_auto_detection_fails(self) -> None:
        self._select_user_in_client()
        response = self.client.post(
            "/imports/upload",
            data={
                "account_id": "auto",
                "statement": (
                    BytesIO(b"Date,Amount,Name,Memo,Id\n2026-05-08,-12.34,Coffee,Latte,fit-001\n"),
                    "statement.csv",
                ),
            },
            content_type="multipart/form-data",
        )

        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn('id="importUploadForm"', html)
        self.assertNotIn('id="importReviewForm"', html)
        self.assertIn("couldn&#39;t confidently detect", html)

    def test_import_review_template_references_static_controller(self) -> None:
        self._select_user_in_client()
        account = accounts_repo.list_accounts()[0]
        response = self.client.post(
            "/imports/upload",
            data={
                "account_id": str(account["id"]),
                "statement": (
                    BytesIO(b"Date,Amount,Name,Memo,Id\n2026-05-08,-12.34,Coffee,Latte,fit-001\n"),
                    "statement.csv",
                )
            },
            content_type="multipart/form-data",
            follow_redirects=True,
        )

        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn('id="importReviewForm"', html)
        self.assertIn("/static/import_review.js", html)
        self.assertIn('data-manual-candidates-url="/imports/manual-candidates"', html)
        self.assertIn('data-dupes-url="/imports/dupes"', html)
        self.assertIn('data-draft-save-url="/imports/draft/save"', html)
        self.assertIn('data-draft-discard-url="/imports/draft/discard"', html)
        self.assertIn('id="IMPORT_PREFILLS_JSON"', html)
        self.assertIn('id="IMPORT_PAYEE_PREFILLS_JSON"', html)
        review_header = Path("app/templates/import_review/_review_header.html").read_text()
        self.assertNotIn("<strong>Bank:</strong>", review_header)
        self.assertNotIn("{{ parsed.bankid or 'Unknown' }}", review_header)
        self.assertIn('id="IMPORT_DRAFT_IDENTITY_JSON"', html)
        self.assertIn('id="IMPORT_REVIEW_DRAFT_JSON"', html)
        self.assertIn('id="importDraftRestoreModal"', html)

    def test_qfx_import_review_uses_source_token_for_ajax_commit_and_provenance(self) -> None:
        self._select_user_in_client()
        account_id = accounts_repo.insert_account({
            "name": "Token Review Checking",
            "account_type": "bank",
            "bankid": "",
            "acctid": "",
        })
        statement = (
            b"<OFX>\n"
            b"<BANKID>FIN079BANK\n"
            b"<ACCTID>FIN079ACCT\n"
            b"<STMTTRN>\n"
            b"<DTPOSTED>20260429120000[-8:UTC]\n"
            b"<TRNAMT>-45.67\n"
            b"<NAME>Token Grocery\n"
            b"<MEMO>Weekly groceries\n"
            b"<FITID>fin079-fit-001\n"
            b"</STMTTRN>\n"
        )

        response = self.client.post(
            "/imports/upload",
            data={
                "account_id": str(account_id),
                "statement": (BytesIO(statement), "token-review.qfx"),
            },
            content_type="multipart/form-data",
        )

        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn('name="import_source_token"', html)
        self.assertNotIn('name="src_bankid"', html)
        self.assertNotIn('name="src_acctid"', html)
        self.assertNotIn('name="file_hash"', html)
        self.assertNotIn("FIN079BANK", html)
        self.assertNotIn("FIN079ACCT", html)
        token_match = re.search(r'name="import_source_token" value="([^"]+)"', html)
        self.assertIsNotNone(token_match)
        token = token_match.group(1)
        self.assertGreaterEqual(len(token), 32)

        imports_payload = [{
            "index": 0,
            "posted_at": "2026-04-29",
            "amount_cents": -4567,
            "payee": "Token Grocery - Weekly groceries",
            "memo": "",
            "fitid": "fin079-fit-001",
        }]
        manual_response = self.client.post(
            "/imports/manual-candidates",
            json={"account_id": account_id, "imports": imports_payload, "import_source_token": token},
        )
        self.assertEqual(manual_response.status_code, 200)
        self.assertIn("items", manual_response.get_json())
        dupe_before = self.client.post(
            "/imports/dupes",
            json={"account_id": account_id, "imports": imports_payload, "import_source_token": token},
        )
        self.assertEqual(dupe_before.status_code, 200)

        envelope_id = envelopes_repo.list_envelopes()[0]["id"]
        commit_response = self.client.post(
            "/imports/commit",
            data={
                "account_id": str(account_id),
                "count": "1",
                "import_source_token": token,
                "row_0": "on",
                "posted_at_0": "2026-04-29",
                "amount_0": "-45.67",
                "payee_0": "Token Grocery - Weekly groceries",
                "orig_payee_0": "Token Grocery - Weekly groceries",
                "memo_0": "",
                "orig_memo_0": "",
                "fitid_0": "fin079-fit-001",
                "exp_remainder_0": str(envelope_id),
            },
            follow_redirects=True,
        )
        self.assertEqual(commit_response.status_code, 200)

        account = get_db().execute("SELECT bankid, acctid FROM accounts WHERE id=?", (account_id,)).fetchone()
        self.assertEqual(account["bankid"], "FIN079BANK")
        self.assertEqual(account["acctid"], "FIN079ACCT")
        session_row = get_db().execute(
            "SELECT source_bankid, source_acctid, file_hash FROM import_sessions WHERE account_id=? ORDER BY id DESC LIMIT 1",
            (account_id,),
        ).fetchone()
        self.assertEqual(session_row["source_bankid"], "FIN079BANK")
        self.assertEqual(session_row["source_acctid"], "FIN079ACCT")
        self.assertTrue(session_row["file_hash"])

        dupe_after = self.client.post(
            "/imports/dupes",
            json={"account_id": account_id, "imports": imports_payload, "import_source_token": token},
        )
        self.assertEqual(dupe_after.status_code, 200)
        self.assertIn(0, dupe_after.get_json()["row_indexes"])

    def test_import_commit_with_invalid_source_token_fails_closed(self) -> None:
        self._select_user_in_client()
        account = accounts_repo.list_accounts()[0]

        response = self.client.post(
            "/imports/commit",
            data={
                "account_id": str(account["id"]),
                "count": "0",
                "import_source_token": "missing-token",
            },
            follow_redirects=True,
        )

        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Upload the statement again", html)
        self.assertIn('id="importUploadForm"', html)

    def test_csv_upload_prompts_for_column_mapping_when_vital_column_is_ambiguous(self) -> None:
        self._select_user_in_client()
        account = accounts_repo.list_accounts()[0]
        response = self.client.post(
            "/imports/upload",
            data={
                "account_id": str(account["id"]),
                "statement": (
                    BytesIO(b"When,Amount,Name\n2026-05-08,-12.34,Coffee\n"),
                    "statement.csv",
                )
            },
            content_type="multipart/form-data",
        )

        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn('id="csvColumnMappingForm"', html)
        self.assertIn('name="csv_upload_token"', html)
        self.assertNotIn('id="importReviewForm"', html)

    def test_csv_column_mapping_submission_continues_to_import_review(self) -> None:
        self._select_user_in_client()
        account = accounts_repo.list_accounts()[0]
        prompt = self.client.post(
            "/imports/upload",
            data={
                "account_id": str(account["id"]),
                "statement": (
                    BytesIO(b"When,Amount,Name\n2026-05-08,-12.34,Coffee\n"),
                    "statement.csv",
                )
            },
            content_type="multipart/form-data",
        )
        html = prompt.get_data(as_text=True)
        marker = 'name="csv_upload_token" value="'
        token = html.split(marker, 1)[1].split('"', 1)[0]

        response = self.client.post(
            "/imports/upload",
            data={
                "account_id": str(account["id"]),
                "csv_upload_token": token,
                "csv_date_col": "When",
                "csv_amount_col": "Amount",
                "csv_payee_col": "Name",
            },
            follow_redirects=True,
        )

        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn('id="importReviewForm"', html)
        self.assertIn("2026-05-08", html)
        self.assertIn("Coffee", html)

    def test_credit_card_csv_review_can_flip_positive_charge_polarity(self) -> None:
        self._select_user_in_client()
        account_id = accounts_repo.insert_account({
            "name": "Synthetic CSV Polarity Card",
            "account_type": "credit_card",
            "opening_balance_cents": 0,
        })
        csv_data = (
            b"Reference Number,Transaction Post Date,Description of Transaction,Transaction Type,Amount\n"
            b"1,06/25/26,Demo Grocery,clearing,50.00\n"
            b"2,06/24/26,Demo Cafe,clearing,20.00\n"
            b"3,06/23/26,Demo Pharmacy,clearing,30.00\n"
            b"4,06/22/26,,payment_transaction,-100.00\n"
        )

        response = self.client.post(
            "/imports/upload",
            data={
                "account_id": str(account_id),
                "statement": (BytesIO(csv_data), "card.csv"),
            },
            content_type="multipart/form-data",
        )

        html = response.get_data(as_text=True)
        marker = 'name="csv_upload_token" value="'
        token = html.split(marker, 1)[1].split('"', 1)[0]
        self.assertIn("This credit card CSV looks like charges are positive and payments are negative.", html)
        self.assertIn("Flip signs for this review", html)

        flipped = self.client.post(
            "/imports/upload",
            data={
                "account_id": str(account_id),
                "csv_upload_token": token,
                "csv_date_col": "Transaction Post Date",
                "csv_amount_col": "Amount",
                "csv_payee_col": "Description of Transaction",
                "csv_memo_col": "Transaction Type",
                "csv_fitid_col": "Reference Number",
                "csv_polarity": "inverted",
            },
        )

        flipped_html = flipped.get_data(as_text=True)
        expense_pos = flipped_html.index(">Expenses<")
        income_pos = flipped_html.index(">Income<")
        self.assertLess(expense_pos, flipped_html.index("Demo Grocery"))
        self.assertLess(flipped_html.index("Demo Grocery"), income_pos)
        self.assertLess(income_pos, flipped_html.index("payment_transaction"))
        self.assertIn("CSV signs are flipped for this review.", flipped_html)
        self.assertIn("Use original signs", flipped_html)

    def test_manual_candidates_accepts_import_rows_in_post_body(self) -> None:
        self._select_user_in_client()
        imports = [
            {
                "index": idx,
                "posted_at": "2026-05-01",
                "amount_cents": 1000 + idx,
                "payee": "Payment Thank You - Web",
                "memo": "",
                "fitid": f"long-fitid-{idx:03d}-" + ("x" * 80),
            }
            for idx in range(80)
        ]

        response = self.client.post(
            "/imports/manual-candidates",
            json={"account_id": 1, "imports": imports},
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("items", response.get_json())



    def test_envelope_default_amount_inputs_allow_negative_values(self) -> None:
        list_template = Path("app/templates/envelopes.html").read_text()
        edit_template = Path("app/templates/envelope_edit.html").read_text()
        blueprint = Path("app/blueprints/envelopes.py").read_text()
        selector_template = Path("app/templates/_envelope_selector.html").read_text()
        import_review_js = Path("app/static/import_review.js").read_text()

        self.assertIn("Default amount", list_template)
        self.assertIn("Default amount ($)", edit_template)
        self.assertNotIn('name="default_amount" placeholder="Default $" type="number" min="0"', list_template)
        self.assertNotIn('name="default_amount" type="number" min="0"', edit_template)
        self.assertNotIn("return abs(int(form.get('default_amount_cents')))", blueprint)
        self.assertNotIn("return abs(parse_money_to_cents_strict", blueprint)
        self.assertIn('data-default-cents="{{ def_cents }}"', selector_template)
        self.assertIn('data-default-cents="${defCents}"', import_review_js)

class RemainderAmountStaticTests(unittest.TestCase):


    def test_import_review_dupes_payload_includes_resolved_rows(self) -> None:
        js = Path("app/static/import_review.js").read_text()
        self.assertIn("function duplicateRefreshPayload()", js)
        self.assertIn("collectImports({ includeResolved: true })", js)

    def test_import_review_manual_payload_includes_resolved_rows_for_date_window(self) -> None:
        js = Path("app/static/import_review.js").read_text()
        self.assertIn("function manualCandidatesPayload()", js)
        self.assertIn("const current = collectImports({ includeResolved: true });", js)
        self.assertIn("if (!includeResolved && rowIsResolved(i)) return;", js)
        self.assertNotIn("cb?.disabled && tr.classList.contains('table-success')", js)

    def test_import_review_dupes_payload_uses_intent_specific_payload_helper(self) -> None:
        js = Path("app/static/import_review.js").read_text()
        start = js.index("function fetchDupesForAccount()")
        end = js.index("async function refreshDupes()", start)
        body = js[start:end]

        self.assertIn("const importsPayload = duplicateRefreshPayload();", body)
        self.assertNotIn("rows.map", body)
    def test_shared_envelope_selector_renders_remainder_amount_display(self) -> None:
        template = Path("app/templates/_envelope_selector.html").read_text()

        self.assertIn("data-remainder-target-sign", template)
        self.assertIn("data-remainder-legacy-outflow", template)
        self.assertIn("data-remainder-amount", template)
        self.assertIn("Remainder amount", template)
        self.assertIn("No remainder assigned", template)

    def test_shared_remainder_helper_calculates_signed_selected_amount(self) -> None:
        js = Path("app/static/app_envelope_filter.js").read_text()

        self.assertIn("function signedRemainderCents(scopeEl, totalCents, values)", js)
        self.assertIn("function updateRemainderAmount(scopeEl, hasSelection, cents)", js)
        self.assertIn("targetIsOutflow && usesLegacyOutflow(scopeEl) && !hasNegative", js)
        self.assertIn("updateRemainderAmount(scopeEl, hasRemainderSelection, remainderCents)", js)
        self.assertIn("const remainingCents = hasRemainderSelection ? 0 : displayRemainingCents", js)

    def test_transfer_modals_show_envelope_balances_on_each_side(self) -> None:
        modal_template = Path("app/templates/_modals.html").read_text()
        transfer_partial = Path("app/templates/import_review/_transfer_modal.html").read_text()
        import_review_js = Path("app/static/import_review.js").read_text()

        self.assertEqual(modal_template.count("env_selector_show_zero_balance = True"), 2)
        self.assertEqual(transfer_partial.count("env_selector_show_zero_balance = True"), 2)
        self.assertIn('data-show-zero-balance="1"', import_review_js)

    def test_fin037_shared_selector_modal_validation_stays_in_modal(self) -> None:
        js = Path("app/static/app_envelope_filter.js").read_text()
        modal_template = Path("app/templates/_modals.html").read_text()
        split_partial = Path("app/templates/import_review/_split_modal.html").read_text()
        transfer_partial = Path("app/templates/import_review/_transfer_modal.html").read_text()
        import_review_template = Path("app/templates/import_review.html").read_text()
        import_review_js = Path("app/static/import_review.js").read_text()
        selector_template = Path("app/templates/_envelope_selector.html").read_text()
        layout = Path("app/templates/layout.html").read_text()

        self.assertIn('data-validation-label="{{ _validation_label }}"', selector_template)
        self.assertIn('data-validation-toggle="{{ _validation_toggle }}"', selector_template)
        self.assertIn("env_selector_validation_label  = 'income split'", modal_template)
        self.assertIn("env_selector_validation_label  = 'expense split'", modal_template)
        self.assertIn("env_selector_validation_label  = 'source envelope amounts'", modal_template)
        self.assertIn("env_selector_validation_label  = 'destination envelope amounts'", modal_template)
        self.assertIn('data-validate-env-dismiss>Done</button>', split_partial)
        self.assertIn('data-validate-env-dismiss>Done</button>', transfer_partial)
        self.assertIn('data-validate-env-dismiss>Done</button>', import_review_template)
        self.assertIn('env_selector_validation_toggle = "#isTransfer_" ~ i', transfer_partial)
        self.assertIn('validationLabel = \'\'', import_review_js)
        self.assertIn('data-validation-label="${esc(validationLabel)}"', import_review_js)
        self.assertIn('validationToggle: `#isTransfer_${rowIndex}`', import_review_js)
        self.assertIn('20260705-import-grid', import_review_template)
        self.assertIn("function validateEnvelopeScope(scopeEl)", js)
        self.assertIn("function wireDismissValidation(scopeEl)", js)
        self.assertIn("form.addEventListener(\"submit\"", js)
        self.assertIn('modal.addEventListener("click"', js)
        self.assertIn("e.preventDefault();", js)
        self.assertIn("e.stopImmediatePropagation();", js)
        self.assertIn("showFormError(form, result.message)", js)
        self.assertIn("Choose a remainder envelope or adjust the amounts", js)
        self.assertIn("20260705-transfer-balances-init", layout)
        self.assertIn('data-bs-theme="light"', layout)
        self.assertIn('data-theme-select', layout)
        self.assertIn('fitft:themechange', layout)


    def test_import_review_fin044_transfer_remainders_filter_before_account_selection(self) -> None:
        js = Path("app/static/import_review.js").read_text()
        helper = Path("app/static/app_envelope_filter.js").read_text()

        self.assertIn("blankAccountRemainderMode = 'all'", js)
        self.assertIn('data-blank-account-remainder-mode', js)
        self.assertIn("blankAccountRemainderMode: 'global'", js)
        self.assertIn('function remainderTemplates(scopeEl, allowedTemplates)', helper)
        self.assertIn('blankMode === "global"', helper)
        self.assertIn('t.locked === ""', helper)
        self.assertIn('remainderAllowed.forEach', helper)
        self.assertIn('20260705-transfer-balances-init', Path("app/templates/layout.html").read_text())

    def test_dark_mode_uses_theme_aware_tables_and_badges(self) -> None:
        template_paths = Path("app/templates").glob("*.html")
        template_html = "\n".join(path.read_text(encoding="utf-8") for path in template_paths)
        import_review_js = Path("app/static/import_review.js").read_text(encoding="utf-8")
        css = Path("app/static/style.css").read_text(encoding="utf-8")

        self.assertNotIn("table-light", template_html)
        self.assertNotIn("text-bg-light", template_html)
        self.assertNotIn("else 'light'", template_html)
        self.assertNotIn("table-light", import_review_js)
        self.assertIn("app-table-head", template_html)
        self.assertIn("app-table-head", import_review_js)
        self.assertIn('[data-bs-theme="dark"]', css)
        self.assertIn(".theme-select", css)
        self.assertIn('[data-bs-theme="dark"] .table-success', css)
        self.assertIn('[data-bs-theme="dark"] .table-info', css)
        self.assertIn("--bs-table-hover-bg", css)

    def test_investment_chart_updates_when_theme_changes(self) -> None:
        template = Path("app/templates/invest.html").read_text()

        self.assertIn("Chart.defaults.color = initialChartTheme.text", template)
        self.assertIn("function applyChartTheme()", template)
        self.assertIn("window.addEventListener('fitft:themechange', applyChartTheme)", template)

    def test_browser_chart_dependencies_use_reviewed_exact_versions(self) -> None:
        template = Path("app/templates/invest.html").read_text(encoding="utf-8")

        self.assertIn("chartjs-adapter-date-fns@3.0.0/", template)
        self.assertIn("chartjs-plugin-zoom@2.2.0/", template)
        self.assertNotIn("chartjs-adapter-date-fns@3\"", template)
        self.assertNotIn("chartjs-plugin-zoom@2\"", template)

    def test_import_review_lazy_selector_uses_same_remainder_amount_markup(self) -> None:
        js = Path("app/static/import_review.js").read_text()

        self.assertIn("targetSign = 1, legacyOutflow = false", js)
        self.assertIn("data-remainder-target-sign", js)
        self.assertIn("data-remainder-legacy-outflow", js)
        self.assertIn("data-remainder-amount", js)
        self.assertIn("targetSign: isExpense ? -1 : 1", js)
        self.assertIn("targetSign: isOut ? -1 : 1", js)

    def test_legacy_modal_scripts_do_not_bind_shared_selector_forms(self) -> None:
        template = Path("app/templates/layout.html").read_text()

        self.assertIn('if (form.querySelector(".env-scope")) return', template)
        self.assertIn("shared envelope selector owns filtering, remaining, and remainder display", template)

    def test_zero_currency_inputs_auto_select_on_focus(self) -> None:
        template = Path("app/templates/layout.html").read_text()

        self.assertIn("function installZeroCurrencyAutoSelect()", template)
        self.assertIn('input[type="number"][step="0.01"]', template)
        self.assertIn('input[type="number"][inputmode="decimal"]', template)
        self.assertIn('input[inputmode="decimal"]', template)
        self.assertIn("zeroCurrencyPattern", template)
        self.assertIn("document.addEventListener('focusin'", template)
        self.assertIn("document.addEventListener('pointerup'", template)
        self.assertIn("el.select()", template)



    def test_import_review_default_buttons_render_for_split_and_transfer_selectors_only_when_defaults_exist(self) -> None:
        js = Path("app/static/import_review.js").read_text()
        split_partial = Path("app/templates/import_review/_split_modal.html").read_text()
        transfer_partial = Path("app/templates/import_review/_transfer_modal.html").read_text()

        self.assertIn("showDefaultButtons = false", js)
        self.assertIn("showDefaultButtons && defCents !== 0", js)
        self.assertIn('data-show-zero-balance="1"', js)
        self.assertIn('Balance:', js)
        self.assertIn('Default: <span>${money(defCents)}</span>', js)
        self.assertLess(js.index('Balance:'), js.index('Default: <span>${money(defCents)}</span>'))
        self.assertEqual(js.count("showDefaultButtons: true"), 3)
        self.assertIn("data-env-default>Default</button>", js)
        self.assertLess(js.index("data-env-default>Default</button>"), js.index("input-group input-group-sm env-amt"))
        selector_template = Path("app/templates/_envelope_selector.html").read_text()
        self.assertLess(selector_template.index("data-env-default"), selector_template.index("input-group input-group-sm env-amt"))
        self.assertIn("input.dispatchEvent(evt)", Path("app/static/app_envelope_filter.js").read_text())
        self.assertIn("env_selector_show_default_buttons = True", split_partial)
        self.assertIn("env_selector_show_default_buttons = True", transfer_partial)

class TransferModalStaticTests(unittest.TestCase):
    def test_global_transfer_modal_is_scrollable_and_fullscreen_on_small_screens(self) -> None:
        template = Path("app/templates/_modals.html").read_text()

        self.assertIn('id="newTransferModal"', template)
        self.assertIn("modal-dialog-scrollable modal-fullscreen-sm-down modal-xl", template)
        self.assertIn('form id="transferForm" class="modal-content"', template)
        transfer_block = template.split('{# ========== TRANSFER MODAL ========== #}', 1)[1]
        self.assertEqual(transfer_block.count("env_selector_show_mode         = True"), 2)
        self.assertEqual(transfer_block.count("env_selector_show_default_buttons = True"), 2)
        selector_js = Path("app/static/app_envelope_filter.js").read_text()
        self.assertIn('setTimeout(() => rebuild(scopeEl), 0);', selector_js)
        self.assertNotIn('<div class="modal-content">\n      <div class="modal-header">\n        <h5 class="modal-title" id="newTransferModalLabel">Transfer</h5>', template)
        self.assertIn("modal-footer", template)
        self.assertIn("Make Transfer", template)

    def test_import_review_transfer_modal_is_scrollable_and_fullscreen_on_small_screens(self) -> None:
        template = Path("app/templates/import_review/_transfer_modal.html").read_text()

        self.assertIn('id="trfModal-{{ i }}"', template)
        self.assertIn("modal-dialog-scrollable modal-fullscreen-sm-down modal-lg", template)
        self.assertIn("modal-footer", template)
        self.assertIn("Done", template)


class ImportReviewStaticTests(unittest.TestCase):
    def test_import_review_match_dropdown_labels_read_imported_payee_inputs_without_changing_values(self) -> None:
        js = Path("app/static/import_review.js").read_text(encoding="utf-8")

        self.assertIn('tr.querySelector(`input[name="payee_${i}"]`)', js)
        self.assertIn('payeeInput?.value.trim()', js)
        self.assertIn("const label = `${postedAt} — ${amountText} — ${payee || 'No payee'}", js)
        self.assertIn("opt.value = String(r.i)", js)
        self.assertIn("opt.textContent = r.label", js)

    def test_import_review_fetches_manual_candidates_with_post_body(self) -> None:
        js = Path("app/static/import_review.js").read_text()

        self.assertIn("method: 'POST'", js)
        self.assertIn("account_id: aid", js)
        self.assertIn("imports: importsPayload", js)
        self.assertIn("posted_at: r.posted_at", js)
        self.assertNotIn("posted_at: r.date", js)
        self.assertNotIn("imports: JSON.stringify(importsPayload)", js)

    def test_import_review_manual_matches_render_overflow_accordion(self) -> None:
        js = Path("app/static/import_review.js").read_text()

        self.assertIn("function buildManualAccordion", js)
        self.assertIn("overflowItems = []", js)
        self.assertIn("data.overflow_items || []", js)
        self.assertIn('class="accordion mt-2"', js)
        self.assertIn('data-bs-target="#${collapseId}"', js)

    def test_import_review_static_url_cache_busts_row_store_change(self) -> None:
        template = Path("app/templates/import_review.html").read_text()

        self.assertIn("20260705-import-grid", template)
        self.assertIn("IMPORT_ROW_STATES_JSON", template)

    def test_import_review_commit_prunes_unchecked_rows_before_submit(self) -> None:
        js = Path("app/static/import_review.js").read_text()

        self.assertIn("function pruneUncheckedRowsForCommit()", js)
        self.assertIn("selectedRowIndexesForCommit()", js)
        self.assertIn("tr.querySelectorAll('input[name], select[name], textarea[name]')", js)
        self.assertIn("field.disabled = true", js)
        self.assertIn("lazyState.delete(rowIndex)", js)
        self.assertIn("predictionFeedbackRows.delete(rowIndex)", js)
        self.assertIn("pruneUncheckedRowsForCommit();", js)

    def test_import_action_preview_only_shows_for_active_button_state(self) -> None:
        js = Path("app/static/import_review.js").read_text()

        self.assertIn("const isTransfer = anchor.classList.contains('trf-btn')", js)
        self.assertIn("? rowHasAnyTransferConfig(rowIndex)", js)
        self.assertIn(": rowHasAnySplitConfig(rowIndex)", js)
        self.assertIn("if (!isActive) {", js)
        self.assertIn("hideActionPreview();", js)
        self.assertIn("return;", js)

    def test_import_action_preview_focus_is_accessibility_opt_in(self) -> None:
        js = Path("app/static/import_review.js").read_text()

        self.assertIn("function actionPreviewFocusEnabled()", js)
        self.assertIn("window.FITFT_ACCESSIBILITY?.importActionPreviewFocus", js)
        self.assertIn("fitft.importActionPreviewFocus", js)
        self.assertIn("if (!actionPreviewFocusEnabled()) return;", js)

    def test_import_payee_hover_tooltip_hides_while_field_has_focus(self) -> None:
        template = Path("app/templates/import_review.html").read_text()
        css = Path("app/static/style.css").read_text()
        js = Path("app/static/import_review.js").read_text()

        self.assertIn("import-payee-hover", template)
        self.assertIn("data-payee-tooltip", template)
        self.assertIn("import-payee-input", template)
        self.assertIn(".import-source-tooltip-popover", css)
        self.assertIn("position: fixed;", css)
        self.assertNotIn(".import-payee-hover::after", css)
        self.assertIn("function showSourceTooltip(anchor)", js)
        self.assertIn("function hideSourceTooltip()", js)
        self.assertIn("function syncPayeeTooltip(input)", js)
        self.assertIn("function originalPayeeTooltipText(payee, memo)", js)
        self.assertIn('input[name="orig_payee_${CSS.escape(rowIndex)}"]', js)
        self.assertIn('input[name="orig_memo_${CSS.escape(rowIndex)}"]', js)
        self.assertNotIn("tooltip.dataset.payeeTooltip = input.value", js)

    def test_import_split_transfer_done_auto_checks_only_active_row(self) -> None:
        js = Path("app/static/import_review.js").read_text()

        self.assertIn("let modalDoneRowToCheck = null", js)
        self.assertIn("function autoCheckImportRow(rowIndex)", js)
        self.assertIn("input[name=\"row_${CSS.escape(String(rowIndex))}\"]", js)
        self.assertIn("const done = e.target.closest('[data-validate-env-dismiss]')", js)
        self.assertIn("if (hasActiveConfig) autoCheckImportRow(rowIndex)", js)
        self.assertIn("modalDoneRowToCheck?.type === 'split'", js)
        self.assertIn("modalDoneRowToCheck?.type === 'transfer'", js)
        self.assertIn("autoCheckImportRow(rowIndex)", js)

    def test_import_review_split_and_transfer_states_are_mutually_exclusive(self) -> None:
        js = Path("app/static/import_review.js").read_text()

        self.assertIn("clearTransferFields(next, rowIndex)", js)
        self.assertIn("clearSplitFields(next, rowIndex)", js)
        self.assertIn("clearSplitFields(valid, rowIndex)", js)
        self.assertIn("clearTransferFields(valid, rowIndex)", js)
        self.assertIn("clearLazyFieldsForRow(rowIndex, splitFieldMatchers(rowIndex))", js)
        self.assertIn("function splitFieldMatchers(rowIndex)", js)
        self.assertIn("function transferFieldMatchers(rowIndex)", js)

    def test_import_upload_form_runs_account_detection_before_review(self) -> None:
        template = Path("app/templates/import.html").read_text()

        self.assertIn("imports.detect_account", template)
        self.assertIn("fetch(detectUrl", template)
        self.assertIn("setCustomValidity('Choose the account for this statement.')", template)
        self.assertIn("accountSelect.value = String(data.account_id)", template)


    def test_import_review_quick_assign_placeholder_is_visually_unassigned(self) -> None:
        template = Path("app/templates/import_review.html").read_text(encoding="utf-8")
        css = Path("app/static/style.css").read_text(encoding="utf-8")
        js = Path("app/static/import_review.js").read_text(encoding="utf-8")

        self.assertIn('class="form-select form-select-sm exp-quick exp-quick-placeholder import-assign-select"', template)
        self.assertIn('data-role="exp-quick"', template)
        self.assertIn("Quick assign to envelope…", template)
        self.assertIn(".exp-quick.exp-quick-placeholder", css)
        self.assertIn("color: var(--bs-secondary-color);", css)
        self.assertIn("function updateQuickAssignPlaceholderState(sel)", js)
        self.assertIn("const isMatched = !!sel.closest('tr')?.classList.contains('table-info')", js)
        self.assertIn("sel.classList.toggle('exp-quick-placeholder', !sel.value && !isMatched)", js)
        self.assertIn("quick.classList.remove('exp-quick-placeholder')", js)
        self.assertIn("if (quick.value) autoCheckImportRow(quick.dataset.rowIndex)", js)
        self.assertIn("sel.addEventListener('change', () => updateQuickAssignPlaceholderState(sel))", js)
        self.assertIn("updateQuickAssignPlaceholderState(sel);", js)


    def test_import_review_fin069_hides_csv_no_fitid_status_noise_only(self) -> None:
        template = Path("app/templates/import_review.html").read_text()

        self.assertNotIn('show_missing_fitid_badges and not fit', template)
        self.assertNotIn('{% if not fit %}\n                    <span class="badge text-bg-secondary">no FITID</span>', template)
        self.assertNotIn('data-role="already-imported"', template)
        self.assertNotIn('title="Missing FITID"', template)


    def test_import_review_fin061_keeps_workflow_improvements_minimal(self) -> None:
        template = Path("app/templates/import_review.html").read_text()
        js = Path("app/static/import_review.js").read_text()

        self.assertIn("Clear", template)
        self.assertNotIn("Clear prediction", template)
        self.assertIn('class="btn btn-sm btn-outline-secondary ms-1 predict-clear-btn"', template)
        self.assertIn('class="form-select form-select-sm exp-quick exp-quick-placeholder import-assign-select"', template)
        self.assertIn('<th style="width:6rem;">Amount</th>', template)
        self.assertIn('<th style="width:26rem;">Assign</th>', template)
        self.assertNotIn('data-role="resolved-note"', template)
        self.assertNotIn("no review needed", template)
        self.assertNotIn("Matched imports will be highlighted below", js)
        self.assertIn("function clearCreationStateForRow(rowIndex", js)
        self.assertIn("function updatePredictionButtonStyle(rowIndex)", js)
        self.assertIn("prefilledRows.has(idx)", js)
        self.assertIn("rowHasAnySplitConfig(idx) || rowHasAnyTransferConfig(idx)", js)
        self.assertIn("Changing accounts clears import predictions and draft assignments. Continue?", js)
        self.assertIn("trfBtn && (trfBtn.disabled = true)", js)
        self.assertNotIn("confidence", template.lower())
        self.assertNotIn("Apply Suggestion", template)


    def test_import_review_fin062_sort_headers_are_panel_scoped_and_preserve_row_identity(self) -> None:
        template = Path("app/templates/import_review.html").read_text()
        css = Path("app/static/style.css").read_text()
        js = Path("app/static/import_review.js").read_text()

        self.assertIn('data-import-sort-section="exp"', template)
        self.assertIn('data-import-sort-section="inc"', template)
        self.assertIn('data-import-sort="date">Date</button>', template)
        self.assertEqual(template.count('data-import-sort="source">Source</button>'), 2)
        self.assertEqual(template.count('data-import-sort="payee">Payee</button>'), 2)
        self.assertEqual(template.count('class="table table-sm align-middle mb-0 import-review-table"'), 2)
        self.assertEqual(template.count('class="import-review-text-col"'), 12)
        self.assertNotIn('Status</th>', template)
        self.assertIn('class="import-review-action-col"', template)
        self.assertIn('class="import-payee-hover import-source-preview small"', template)
        self.assertIn(".import-review-table", css)
        self.assertIn("display: grid;", css)
        self.assertIn("width: 100%;", css)
        self.assertIn(".import-review-text-col", css)
        self.assertNotIn(".import-review-status-col", css)
        self.assertIn(".import-review-action-col", css)
        self.assertIn(".import-source-preview", css)
        self.assertIn("text-overflow: ellipsis;", css)
        self.assertIn("white-space: nowrap;", css)
        self.assertNotIn("b.textContent = 'M';", js)
        self.assertIn('data-original-order="{{ i }}"', template)
        self.assertIn("const importSortState = new Map()", js)
        self.assertIn("function sortableSectionRows(section)", js)
        self.assertIn('table[data-import-sort-section="${CSS.escape(section)}"] tbody tr[data-section="${CSS.escape(section)}"]', js)
        self.assertIn("function currentPayeeValue(tr)", js)
        self.assertIn("function originalSourceValue(tr)", js)
        self.assertIn('input[name="payee_${CSS.escape(rowIndex)}"]', js)
        self.assertIn('input[name="orig_payee_${CSS.escape(rowIndex)}"]', js)
        self.assertIn("key === 'source'", js)
        self.assertIn("function sortImportSection(section, key)", js)
        self.assertIn("persistModal(splitModal)", js)
        self.assertIn("persistModal(transferModal)", js)
        self.assertIn("tbody.appendChild(row)", js)
        self.assertNotIn("name = `row_", js)
        self.assertIn("20260705-import-grid", template)

    def test_import_review_fin052_modal_clear_buttons_reset_draft_fields_without_closing(self) -> None:
        template = Path("app/templates/import_review.html").read_text()
        split_partial = Path("app/templates/import_review/_split_modal.html").read_text()
        transfer_partial = Path("app/templates/import_review/_transfer_modal.html").read_text()
        js = Path("app/static/import_review.js").read_text()

        self.assertIn('data-role="split-clear"', template)
        self.assertIn('data-role="transfer-clear"', template)
        self.assertIn('data-role="split-clear"', split_partial)
        self.assertIn('data-role="transfer-clear"', transfer_partial)
        self.assertNotIn('data-role="split-clear" data-bs-dismiss="modal"', template)
        self.assertNotIn('data-role="transfer-clear" data-bs-dismiss="modal"', template)
        self.assertIn("function clearLazyFieldsForRow(rowIndex, matchers)", js)
        self.assertIn("function clearSplitModalDraft(rowIndex)", js)
        self.assertIn("function clearTransferModalDraft(rowIndex)", js)
        self.assertIn("modal?.querySelectorAll('input.env-input').forEach(clearInputAndNotify)", js)
        self.assertIn("modal?.querySelectorAll('[data-remainder-select]').forEach(clearSelectAndNotify)", js)
        self.assertIn('select[name="transfer_account_', js)
        self.assertIn('input[name="is_transfer_', js)
        self.assertIn("new RegExp(`^(exp|inc)_amount_${idx}_`)", js)
        self.assertIn("new RegExp(`^trf_from_remainder_${idx}$`)", js)
        self.assertIn("clearSplitModalDraft(activeLazy.rowIndex)", js)
        self.assertIn("clearTransferModalDraft(activeLazy.rowIndex)", js)
        self.assertIn("20260705-import-grid", template)

    def test_import_review_prefills_use_existing_controls_and_hidden_fields(self) -> None:
        template = Path("app/templates/import_review.html").read_text()
        js = Path("app/static/import_review.js").read_text()

        self.assertIn('id="IMPORT_PREFILLS_JSON"', template)
        self.assertNotIn("exp_suggestions", template)
        self.assertNotIn("suggested_eid", template)
        self.assertNotIn("{% if suggested_eid", template)
        self.assertIn("const importPrefills = readJson('IMPORT_PREFILLS_JSON', [])", js)
        self.assertIn("const importPayeePrefills = readJson('IMPORT_PAYEE_PREFILLS_JSON', [])", js)
        self.assertIn("applyPayeeNormalizationPrefills", js)
        self.assertIn("seedSingleExpensePrefill", js)
        self.assertIn("seedSingleIncomePrefill", js)
        self.assertIn('setLazyField(rowIndex, `${prefix}_amount_${rowIndex}_${envelopeId}`', js)
        self.assertIn("seedTransferPrefill", js)
        self.assertIn("seedRemainderPrefill", js)
        self.assertIn("prefill.remainder_envelope_id", js)
        self.assertIn("setLazyField(rowIndex, `is_transfer_${rowIndex}`, '1')", js)
        self.assertIn("setLazyField(rowIndex, `transfer_account_${rowIndex}`, String(otherAccountId))", js)
        self.assertIn("seedTransferLeg(rowIndex, 'trf_from_amt', transfer.current_account_splits || [], { forcePositive: true })", js)
        self.assertIn("function amountInputValue(cents, options = {})", js)
        self.assertIn("const forcePositive = !!options.forcePositive", js)
        self.assertIn("transfer.current_account_remainder_envelope_id", js)
        self.assertIn("transfer.other_account_remainder_envelope_id", js)
        self.assertIn("seedTransferLeg(rowIndex, 'trf_amt', transfer.other_account_splits || [])", js)
        self.assertIn("function manualRuleSuggestionItems()", js)
        self.assertIn("manualRuleSuggestionItems()", js)
        self.assertIn("clearCreationStateForRow(i);", js)

    def test_import_review_fin063_draft_recovery_uses_existing_controls(self) -> None:
        template = Path("app/templates/import_review.html").read_text()
        js = Path("app/static/import_review.js").read_text()
        self.assertIn('data-draft-fingerprint', template)
        self.assertIn('IMPORT_DRAFT_IDENTITY_JSON', template)
        self.assertIn('IMPORT_REVIEW_DRAFT_JSON', template)
        self.assertIn('function collectDraftRows()', js)
        self.assertIn('function applyImportReviewDraft', js)
        self.assertIn('restoreQuickEnvelope', js)
        self.assertIn('restoreLazyFields', js)
        self.assertIn('restoreManualMatch', js)
        self.assertIn('restoreIgnoredManualCandidates', js)
        self.assertNotIn('draft saved</span>', template.lower())
