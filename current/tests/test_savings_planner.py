import html
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from unittest.mock import patch

from app.db import get_db, get_meta_db
from app.repositories import accounts_repo, envelopes_repo, savings_repo
from app.services.savings_planner_service import (
    SavingsPlannerError,
    calculate_preview,
    long_term_share_basis_points,
    validate_configuration,
    validate_rule,
)
from tests.helpers import FinanceAppTestCase


class SavingsPlannerCalculationTests(FinanceAppTestCase):
    def _maps(self):
        accounts = {
            1: {"id": 1, "name": "Everyday Checking", "account_type": "bank"},
            2: {"id": 2, "name": "Quick-Access Savings", "account_type": "bank"},
            3: {"id": 3, "name": "Long-Term Savings", "account_type": "bank"},
        }
        envelopes = {
            10: {"id": 10, "name": "Paycheck", "locked_account_id": 1, "archived_at": None},
            20: {"id": 20, "name": "Emergency Reserve", "locked_account_id": 2, "archived_at": None},
            30: {"id": 30, "name": "Emergency Long-Term", "locked_account_id": 3, "archived_at": None},
            21: {"id": 21, "name": "Travel Reserve", "locked_account_id": 2, "archived_at": None},
        }
        return accounts, envelopes

    def _plan(self):
        return {
            "id": 1,
            "name": "Pay Yourself First",
            "source_account_id": 1,
            "source_envelope_id": 10,
        }

    def test_long_term_hard_cutoff_is_exact(self) -> None:
        values = [
            (-100, 0),
            (0, 0),
            (999_999, 0),
            (1_000_000, 10_000),
            (1_500_000, 10_000),
        ]
        for current_cents, expected_share in values:
            with self.subTest(current_cents=current_cents):
                self.assertEqual(
                    long_term_share_basis_points(
                        current_cents,
                        1_000_000,
                        has_long_term_destination=True,
                    ),
                    expected_share,
                )
        self.assertEqual(
            long_term_share_basis_points(
                900_000,
                0,
                has_long_term_destination=False,
            ),
            0,
        )
        with self.assertRaisesRegex(SavingsPlannerError, "target greater than \\$0"):
            long_term_share_basis_points(
                0,
                0,
                has_long_term_destination=True,
            )

    def test_preview_is_cent_safe_and_groups_destination_envelopes(self) -> None:
        accounts, envelopes = self._maps()
        rules = [
            {
                "id": 1,
                "name": "Emergency Reserve",
                "contribution_basis_points": 3333,
                "accessible_account_id": 2,
                "accessible_envelope_id": 20,
                "long_term_account_id": None,
                "long_term_envelope_id": None,
                "accessible_target_cents": 0,
                "enabled": 1,
                "display_order": 1,
            },
            {
                "id": 2,
                "name": "Future Travel",
                "contribution_basis_points": 6667,
                "accessible_account_id": 2,
                "accessible_envelope_id": 21,
                "long_term_account_id": None,
                "long_term_envelope_id": None,
                "accessible_target_cents": 0,
                "enabled": 1,
                "display_order": 2,
            },
        ]
        preview = calculate_preview(
            take_home_cents=10_001,
            posted_at="2026-07-20",
            plan=self._plan(),
            rules=rules,
            accounts_by_id=accounts,
            envelopes_by_id=envelopes,
            account_envelope_balances={},
        )

        self.assertEqual(preview["total_contribution_cents"], 10_001)
        self.assertEqual(preview["remaining_pay_cents"], 0)
        self.assertEqual(sum(row["contribution_cents"] for row in preview["contributions"]), 10_001)
        self.assertEqual(len(preview["recommendations"]), 1)
        recommendation = preview["recommendations"][0]
        self.assertEqual(recommendation["destination_account_id"], 2)
        self.assertEqual(
            sum(split["amount_cents"] for split in recommendation["destination_splits"]),
            10_001,
        )

    def test_preview_uses_opening_balance_for_hard_cutoff(self) -> None:
        accounts, envelopes = self._maps()
        rules = [{
            "id": 1,
            "name": "Emergency Reserve",
            "contribution_basis_points": 1000,
            "accessible_account_id": 2,
            "accessible_envelope_id": 20,
            "long_term_account_id": 3,
            "long_term_envelope_id": 30,
            "accessible_target_cents": 1_000_000,
            "enabled": 1,
            "display_order": 1,
        }]
        preview = calculate_preview(
            take_home_cents=300_000,
            posted_at="2026-07-20",
            plan=self._plan(),
            rules=rules,
            accounts_by_id=accounts,
            envelopes_by_id=envelopes,
            account_envelope_balances={(2, 20): 990_000},
        )

        row = preview["contributions"][0]
        self.assertEqual(row["contribution_cents"], 30_000)
        self.assertEqual(row["long_term_share_basis_points"], 0)
        self.assertEqual(row["accessible_cents"], 30_000)
        self.assertEqual(row["long_term_cents"], 0)
        self.assertEqual(sum(item["amount_cents"] for item in preview["recommendations"]), 30_000)

        after_target = calculate_preview(
            take_home_cents=300_000,
            posted_at="2026-08-03",
            plan=self._plan(),
            rules=rules,
            accounts_by_id=accounts,
            envelopes_by_id=envelopes,
            account_envelope_balances={(2, 20): 1_000_000},
        )
        after_row = after_target["contributions"][0]
        self.assertEqual(after_row["long_term_share_basis_points"], 10_000)
        self.assertEqual(after_row["accessible_cents"], 0)
        self.assertEqual(after_row["long_term_cents"], 30_000)

    def test_configuration_rejects_over_100_percent_and_invalid_destinations(self) -> None:
        accounts, envelopes = self._maps()
        bad_rule = {
            "id": 1,
            "name": "Too Much",
            "contribution_basis_points": 6000,
            "accessible_account_id": 2,
            "accessible_envelope_id": 20,
            "long_term_account_id": None,
            "long_term_envelope_id": None,
            "accessible_target_cents": 0,
            "enabled": 1,
        }
        with self.assertRaisesRegex(SavingsPlannerError, "more than 100%"):
            validate_configuration(
                self._plan(),
                [bad_rule, dict(bad_rule, id=2, name="Also Too Much")],
                accounts_by_id=accounts,
                envelopes_by_id=envelopes,
            )

        with self.assertRaisesRegex(SavingsPlannerError, "must differ"):
            validate_rule(
                dict(bad_rule, accessible_account_id=1, accessible_envelope_id=10),
                source_account_id=1,
                accounts_by_id=accounts,
                envelopes_by_id=envelopes,
            )
        with self.assertRaisesRegex(SavingsPlannerError, "target greater than \\$0"):
            validate_rule(
                dict(
                    bad_rule,
                    long_term_account_id=3,
                    long_term_envelope_id=30,
                    accessible_target_cents=0,
                ),
                source_account_id=1,
                accounts_by_id=accounts,
                envelopes_by_id=envelopes,
            )
        with self.assertRaisesRegex(SavingsPlannerError, "locked to a different account"):
            validate_rule(
                dict(bad_rule, accessible_envelope_id=30),
                source_account_id=1,
                accounts_by_id=accounts,
                envelopes_by_id=envelopes,
            )

    def test_schema_requires_complete_long_term_destination_pair(self) -> None:
        db = get_db()
        db.execute(
            """
            INSERT INTO savings_plans(
                id, name, source_account_id, source_envelope_id, created_at, updated_at
            ) VALUES(1, 'Test', 1, 1, '2026-07-20', '2026-07-20')
            """
        )
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute(
                """
                INSERT INTO savings_rules(
                    plan_id, name, contribution_basis_points,
                    accessible_account_id, accessible_envelope_id,
                    long_term_account_id, long_term_envelope_id,
                    accessible_target_cents, enabled, display_order,
                    created_at, updated_at
                ) VALUES(1, 'Broken pair', 1000, 2, 6, 1, NULL, 10000, 1, 1, '2026-07-20', '2026-07-20')
                """
            )
        db.rollback()


class SavingsPlannerFlowTests(FinanceAppTestCase):
    def _select_user_in_client(self) -> None:
        row = get_meta_db().execute("SELECT id FROM users ORDER BY id LIMIT 1").fetchone()
        self.assertIsNotNone(row)
        with self.client.session_transaction() as client_session:
            client_session["user_id"] = int(row["id"])

    def setUp(self) -> None:
        super().setUp()
        self._select_user_in_client()
        self.long_term_account_id = accounts_repo.insert_account(
            {"name": "Long-Term Savings", "account_type": "bank"}
        )
        self.long_term_envelope_id = envelopes_repo.insert_envelope(
            {"name": "Emergency Long-Term", "locked_account_id": self.long_term_account_id}
        )
        savings_repo.save_plan(
            name="Pay Yourself First",
            source_account_id=1,
            source_envelope_id=1,
        )
        savings_repo.insert_rule({
            "name": "Emergency Reserve",
            "contribution_basis_points": 1000,
            "accessible_account_id": 2,
            "accessible_envelope_id": 6,
            "long_term_account_id": self.long_term_account_id,
            "long_term_envelope_id": self.long_term_envelope_id,
            "accessible_target_cents": 100_000,
            "enabled": 1,
        })
        db = get_db()
        cursor = db.execute(
            """
            INSERT INTO transactions(account_id, ttype, amount_cents, posted_at, payee, memo)
            VALUES(2, 'income', 50000, '2026-07-01', 'Opening balance', 'Accessible reserve')
            """
        )
        db.execute(
            "INSERT INTO transaction_splits(transaction_id, envelope_id, amount_cents) VALUES(?, 6, 50000)",
            (int(cursor.lastrowid),),
        )
        db.commit()

    def _rule_update_payload(self, **overrides) -> tuple[int, dict[str, str]]:
        rule = savings_repo.list_rules()[0]
        payload = {
            "name": str(rule["name"]),
            "percentage": str(int(rule["contribution_basis_points"]) / 100),
            "accessible_account_id": str(rule["accessible_account_id"]),
            "accessible_envelope_id": str(rule["accessible_envelope_id"]),
            "long_term_account_id": str(rule["long_term_account_id"] or ""),
            "long_term_envelope_id": str(rule["long_term_envelope_id"] or ""),
            "accessible_target": str(int(rule["accessible_target_cents"]) / 100),
            "enabled": "1",
        }
        payload.update({key: str(value) for key, value in overrides.items()})
        return int(rule["id"]), payload

    def test_rule_autosave_returns_json_and_updates_live_total(self) -> None:
        rule_id, payload = self._rule_update_payload(
            name="Rainy Day Fund",
            percentage="12.5",
        )
        response = self.client.post(
            f"/savings/rules/{rule_id}",
            data=payload,
            headers={"X-Savings-Autosave": "1", "Accept": "application/json"},
        )

        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["rule"]["name"], "Rainy Day Fund")
        self.assertEqual(body["rule"]["contribution_basis_points"], 1250)
        self.assertEqual(body["total_basis_points"], 1250)
        self.assertEqual(body["total_percent"], "12.5")
        saved = savings_repo.get_rule(rule_id)
        self.assertEqual(saved["name"], "Rainy Day Fund")
        self.assertEqual(saved["contribution_basis_points"], 1250)

        payload.pop("enabled")
        paused = self.client.post(
            f"/savings/rules/{rule_id}",
            data=payload,
            headers={"X-Savings-Autosave": "1", "Accept": "application/json"},
        )
        self.assertEqual(paused.status_code, 200)
        self.assertFalse(paused.get_json()["rule"]["enabled"])
        self.assertEqual(paused.get_json()["total_basis_points"], 0)

    def test_invalid_rule_autosave_is_json_and_preserves_saved_rule(self) -> None:
        rule_id, payload = self._rule_update_payload(percentage="101")
        before = savings_repo.get_rule(rule_id)
        response = self.client.post(
            f"/savings/rules/{rule_id}",
            data=payload,
            headers={"X-Savings-Autosave": "1", "Accept": "application/json"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.get_json()["ok"])
        self.assertIn("no more than 100%", response.get_json()["error"])
        after = savings_repo.get_rule(rule_id)
        self.assertEqual(after["contribution_basis_points"], before["contribution_basis_points"])
        self.assertEqual(after["updated_at"], before["updated_at"])

    def test_concurrent_rule_autosaves_cannot_exceed_100_percent(self) -> None:
        second_rule_id = savings_repo.insert_rule({
            "name": "Second Goal",
            "contribution_basis_points": 1000,
            "accessible_account_id": 2,
            "accessible_envelope_id": 6,
            "long_term_account_id": self.long_term_account_id,
            "long_term_envelope_id": self.long_term_envelope_id,
            "accessible_target_cents": 100_000,
            "enabled": 1,
        })
        rules = savings_repo.list_rules()
        user_id = int(get_meta_db().execute("SELECT id FROM users ORDER BY id LIMIT 1").fetchone()["id"])
        start_together = Barrier(2)

        def payload(rule: dict) -> dict[str, str]:
            return {
                "name": str(rule["name"]),
                "percentage": "60",
                "accessible_account_id": str(rule["accessible_account_id"]),
                "accessible_envelope_id": str(rule["accessible_envelope_id"]),
                "long_term_account_id": str(rule["long_term_account_id"]),
                "long_term_envelope_id": str(rule["long_term_envelope_id"]),
                "accessible_target": str(int(rule["accessible_target_cents"]) / 100),
                "enabled": "1",
            }

        def autosave(rule: dict) -> tuple[int, dict]:
            client = self.app.test_client()
            with client.session_transaction() as client_session:
                client_session["user_id"] = user_id
            start_together.wait()
            response = client.post(
                f"/savings/rules/{rule['id']}",
                data=payload(rule),
                headers={"X-Savings-Autosave": "1", "Accept": "application/json"},
            )
            return response.status_code, response.get_json()

        selected_rules = [rule for rule in rules if int(rule["id"]) in {int(rules[0]["id"]), second_rule_id}]
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(autosave, selected_rules))

        self.assertEqual(sorted(status for status, _body in results), [200, 400])
        self.assertTrue(any("more than 100%" in body["error"] for status, body in results if status == 400))
        saved_total = sum(
            int(rule["contribution_basis_points"])
            for rule in savings_repo.list_rules()
            if int(rule["enabled"]) == 1
        )
        self.assertEqual(saved_total, 7000)

    def test_concurrent_rule_create_and_autosave_share_percentage_lock(self) -> None:
        existing = savings_repo.list_rules()[0]
        user_id = int(get_meta_db().execute("SELECT id FROM users ORDER BY id LIMIT 1").fetchone()["id"])
        start_together = Barrier(2)

        def client_with_user():
            client = self.app.test_client()
            with client.session_transaction() as client_session:
                client_session["user_id"] = user_id
            return client

        def update_existing() -> int:
            client = client_with_user()
            start_together.wait()
            response = client.post(
                f"/savings/rules/{existing['id']}",
                data={
                    "name": str(existing["name"]),
                    "percentage": "60",
                    "accessible_account_id": str(existing["accessible_account_id"]),
                    "accessible_envelope_id": str(existing["accessible_envelope_id"]),
                    "long_term_account_id": str(existing["long_term_account_id"]),
                    "long_term_envelope_id": str(existing["long_term_envelope_id"]),
                    "accessible_target": str(int(existing["accessible_target_cents"]) / 100),
                    "enabled": "1",
                },
                headers={"X-Savings-Autosave": "1", "Accept": "application/json"},
            )
            return response.status_code

        def create_competing_rule() -> int:
            client = client_with_user()
            start_together.wait()
            response = client.post(
                "/savings/rules",
                data={
                    "name": "Competing Goal",
                    "percentage": "50",
                    "accessible_account_id": "2",
                    "accessible_envelope_id": "6",
                    "long_term_account_id": str(self.long_term_account_id),
                    "long_term_envelope_id": str(self.long_term_envelope_id),
                    "accessible_target": "1000",
                    "enabled": "1",
                },
            )
            return response.status_code

        with ThreadPoolExecutor(max_workers=2) as executor:
            update_future = executor.submit(update_existing)
            create_future = executor.submit(create_competing_rule)
            update_status = update_future.result()
            create_status = create_future.result()

        saved_rules = savings_repo.list_rules()
        saved_total = sum(
            int(rule["contribution_basis_points"])
            for rule in saved_rules
            if int(rule["enabled"]) == 1
        )
        self.assertEqual(create_status, 302)
        self.assertIn(update_status, {200, 400})
        self.assertEqual(saved_total, 6000)
        self.assertEqual(len(saved_rules), 1 if update_status == 200 else 2)

    def test_savings_page_uses_autosave_and_plain_language_copy(self) -> None:
        page = self.client.get("/savings/").get_data(as_text=True)

        self.assertNotIn("This planner records transfers in your budget", page)
        self.assertNotIn("% of take-home", page)
        self.assertNotIn(">Save rule<", page)
        self.assertNotIn("Configured", page)
        self.assertNotIn(">Enabled</span>", page)
        self.assertNotIn("Review only", page)
        self.assertIn("Percentage to Savings: 10%", page)
        self.assertIn("How the two savings accounts work", page)
        self.assertIn("data-savings-rule-form", page)
        self.assertIn("data-flush-rule-saves", page)
        self.assertIn("Rule changes save automatically", page)
        self.assertIn('data-autosave-status role="status" aria-live="polite"></span>', page)
        self.assertRegex(page, r'<label class="form-label" for="ruleName\d+">Goal name</label>')
        self.assertRegex(page, r'<select class="form-select" id="ruleAccessibleAccount\d+"')
        self.assertIn('for="newRuleAccessibleEnvelope"', page)

    def test_preview_is_write_free_then_record_is_balanced_and_duplicate_safe(self) -> None:
        db = get_db()
        transactions_before = db.execute("SELECT COUNT(*) AS count FROM transactions").fetchone()["count"]

        response = self.client.post(
            "/savings/preview",
            data={"take_home": "1000.00", "posted_at": "2026-07-20"},
        )
        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        self.assertIn("$100.00", page)
        self.assertIn('name="take_home" inputmode="decimal" placeholder="2,500.00" value="1000.00"', page)
        self.assertIn('name="posted_at" type="date" value="2026-07-20"', page)
        self.assertIn("Target not yet reached · 100% goes to accessible savings", page)
        self.assertEqual(
            db.execute("SELECT COUNT(*) AS count FROM transactions").fetchone()["count"],
            transactions_before,
        )

        token_match = re.search(r'name="preview_token" value="([^"]+)"', page)
        self.assertIsNotNone(token_match)
        token = html.unescape(token_match.group(1))
        record_data = {"preview_token": token, "group_index": "0"}
        recorded = self.client.post("/savings/record", data=record_data)
        self.assertEqual(recorded.status_code, 200)
        self.assertIn("Transfer recorded", recorded.get_data(as_text=True))
        self.assertNotIn(">Recorded</span>", recorded.get_data(as_text=True))
        self.assertEqual(
            db.execute("SELECT COUNT(*) AS count FROM transactions").fetchone()["count"],
            transactions_before + 2,
        )
        self.assertEqual(
            db.execute("SELECT COUNT(*) AS count FROM savings_transfer_records").fetchone()["count"],
            1,
        )

        legs = db.execute(
            """
            SELECT id, account_id, ttype, amount_cents, xfer_pair_id
            FROM transactions ORDER BY id DESC LIMIT 2
            """
        ).fetchall()
        self.assertEqual({row["ttype"] for row in legs}, {"transfer_out", "transfer_in"})
        self.assertEqual(sum(int(row["amount_cents"]) for row in legs), 0)
        self.assertEqual({int(row["amount_cents"]) for row in legs}, {-10_000, 10_000})
        self.assertTrue(all(row["xfer_pair_id"] for row in legs))
        leg_ids = [int(row["id"]) for row in legs]
        placeholders = ",".join("?" for _ in leg_ids)
        splits = db.execute(
            f"""
            SELECT t.ttype, s.envelope_id, s.amount_cents
            FROM transaction_splits s
            JOIN transactions t ON t.id=s.transaction_id
            WHERE t.id IN ({placeholders})
            ORDER BY t.ttype
            """,
            tuple(leg_ids),
        ).fetchall()
        self.assertEqual(
            {(row["ttype"], row["envelope_id"], row["amount_cents"]) for row in splits},
            {("transfer_out", 1, -10_000), ("transfer_in", 6, 10_000)},
        )

        with self.client.session_transaction() as client_session:
            selected_user_id = client_session["user_id"]
            client_session.clear()
            client_session["user_id"] = selected_user_id

        duplicate = self.client.post("/savings/record", data=record_data)
        self.assertEqual(duplicate.status_code, 200)
        self.assertIn("already recorded", duplicate.get_data(as_text=True))
        self.assertEqual(
            db.execute("SELECT COUNT(*) AS count FROM transactions").fetchone()["count"],
            transactions_before + 2,
        )

    def test_record_rejects_preview_when_current_balance_changes_the_routing(self) -> None:
        response = self.client.post(
            "/savings/preview",
            data={"take_home": "1000.00", "posted_at": "2026-07-20"},
        )
        page = response.get_data(as_text=True)
        token_match = re.search(r'name="preview_token" value="([^"]+)"', page)
        self.assertIsNotNone(token_match)
        token = html.unescape(token_match.group(1))

        db = get_db()
        cursor = db.execute(
            """
            INSERT INTO transactions(account_id, ttype, amount_cents, posted_at, payee, memo)
            VALUES(2, 'income', 50000, '2026-07-19', 'Synthetic adjustment', 'Reach savings target')
            """
        )
        db.execute(
            "INSERT INTO transaction_splits(transaction_id, envelope_id, amount_cents) VALUES(?, 6, 50000)",
            (int(cursor.lastrowid),),
        )
        db.commit()
        transactions_before_record = db.execute(
            "SELECT COUNT(*) AS count FROM transactions"
        ).fetchone()["count"]

        rejected = self.client.post(
            "/savings/record",
            data={"preview_token": token, "group_index": "0"},
            follow_redirects=True,
        )

        self.assertEqual(rejected.status_code, 200)
        self.assertIn("Savings balances changed after this preview", rejected.get_data(as_text=True))
        self.assertEqual(
            db.execute("SELECT COUNT(*) AS count FROM transactions").fetchone()["count"],
            transactions_before_record,
        )
        self.assertEqual(
            db.execute("SELECT COUNT(*) AS count FROM savings_transfer_records").fetchone()["count"],
            0,
        )

    def test_record_rejects_preview_after_savings_settings_change(self) -> None:
        response = self.client.post(
            "/savings/preview",
            data={"take_home": "1000.00", "posted_at": "2026-07-20"},
        )
        page = response.get_data(as_text=True)
        token_match = re.search(r'name="preview_token" value="([^"]+)"', page)
        self.assertIsNotNone(token_match)
        token = html.unescape(token_match.group(1))

        db = get_db()
        db.execute(
            "UPDATE savings_rules SET contribution_basis_points=1100 WHERE id=(SELECT MIN(id) FROM savings_rules)"
        )
        db.commit()
        transactions_before_record = db.execute(
            "SELECT COUNT(*) AS count FROM transactions"
        ).fetchone()["count"]

        rejected = self.client.post(
            "/savings/record",
            data={"preview_token": token, "group_index": "0"},
            follow_redirects=True,
        )

        self.assertIn("Savings settings changed after this preview", rejected.get_data(as_text=True))
        self.assertEqual(
            db.execute("SELECT COUNT(*) AS count FROM transactions").fetchone()["count"],
            transactions_before_record,
        )
        self.assertEqual(
            db.execute("SELECT COUNT(*) AS count FROM savings_transfer_records").fetchone()["count"],
            0,
        )

    def test_preview_token_is_bound_to_selected_user_and_ledger(self) -> None:
        response = self.client.post(
            "/savings/preview",
            data={"take_home": "1000.00", "posted_at": "2026-07-20"},
        )
        page = response.get_data(as_text=True)
        token_match = re.search(r'name="preview_token" value="([^"]+)"', page)
        self.assertIsNotNone(token_match)
        token = html.unescape(token_match.group(1))

        meta = get_meta_db()
        first_user = meta.execute(
            "SELECT db_path FROM users ORDER BY id LIMIT 1"
        ).fetchone()
        cursor = meta.execute(
            """
            INSERT INTO users(name, db_path, created_at, role)
            VALUES('Second Test User', ?, '2026-07-20T00:00:00+00:00', 'admin')
            """,
            (first_user["db_path"],),
        )
        meta.commit()
        with self.client.session_transaction() as client_session:
            client_session["user_id"] = int(cursor.lastrowid)

        transactions_before = get_db().execute(
            "SELECT COUNT(*) AS count FROM transactions"
        ).fetchone()["count"]
        rejected = self.client.post(
            "/savings/record",
            data={"preview_token": token, "group_index": "0"},
            follow_redirects=True,
        )
        self.assertIn("different user or ledger", rejected.get_data(as_text=True))
        self.assertEqual(
            get_db().execute("SELECT COUNT(*) AS count FROM transactions").fetchone()["count"],
            transactions_before,
        )

    def test_idempotency_reservation_rolls_back_with_transfer_failure(self) -> None:
        response = self.client.post(
            "/savings/preview",
            data={"take_home": "1000.00", "posted_at": "2026-07-20"},
        )
        page = response.get_data(as_text=True)
        token_match = re.search(r'name="preview_token" value="([^"]+)"', page)
        self.assertIsNotNone(token_match)
        token = html.unescape(token_match.group(1))
        db = get_db()
        transactions_before = db.execute(
            "SELECT COUNT(*) AS count FROM transactions"
        ).fetchone()["count"]

        with patch(
            "app.blueprints.savings.TransactionsService.create_transfer",
            side_effect=RuntimeError("synthetic transfer failure"),
        ):
            failed = self.client.post(
                "/savings/record",
                data={"preview_token": token, "group_index": "0"},
                follow_redirects=True,
            )

        self.assertIn("No partial transfer was saved", failed.get_data(as_text=True))
        self.assertEqual(
            db.execute("SELECT COUNT(*) AS count FROM transactions").fetchone()["count"],
            transactions_before,
        )
        self.assertEqual(
            db.execute("SELECT COUNT(*) AS count FROM savings_transfer_records").fetchone()["count"],
            0,
        )

    def test_recording_serializes_settings_validation_with_transfer_write(self) -> None:
        response = self.client.post(
            "/savings/preview",
            data={"take_home": "1000.00", "posted_at": "2026-07-20"},
        )
        page = response.get_data(as_text=True)
        token_match = re.search(r'name="preview_token" value="([^"]+)"', page)
        self.assertIsNotNone(token_match)
        token = html.unescape(token_match.group(1))
        db = get_db()
        transactions_before = db.execute(
            "SELECT COUNT(*) AS count FROM transactions"
        ).fetchone()["count"]
        selected_db_path = Path(
            get_meta_db().execute(
                "SELECT db_path FROM users ORDER BY id LIMIT 1"
            ).fetchone()["db_path"]
        )

        from app.blueprints import savings as savings_blueprint

        original_fingerprint = savings_blueprint.configuration_fingerprint
        competing_write_was_blocked = False

        def fingerprint_then_mutate(plan, rules):
            nonlocal competing_write_was_blocked
            fingerprint = original_fingerprint(plan, rules)
            if not competing_write_was_blocked:
                concurrent = sqlite3.connect(selected_db_path, timeout=0.05)
                try:
                    try:
                        concurrent.execute(
                            "UPDATE savings_rules SET contribution_basis_points=1100 WHERE id=(SELECT MIN(id) FROM savings_rules)"
                        )
                        concurrent.commit()
                    except sqlite3.OperationalError as exc:
                        self.assertIn("locked", str(exc).lower())
                        concurrent.rollback()
                        competing_write_was_blocked = True
                finally:
                    concurrent.close()
            return fingerprint

        with patch(
            "app.blueprints.savings.configuration_fingerprint",
            side_effect=fingerprint_then_mutate,
        ):
            recorded = self.client.post(
                "/savings/record",
                data={"preview_token": token, "group_index": "0"},
                follow_redirects=True,
            )

        self.assertTrue(competing_write_was_blocked)
        self.assertIn("Transfer recorded", recorded.get_data(as_text=True))
        self.assertEqual(
            db.execute("SELECT COUNT(*) AS count FROM transactions").fetchone()["count"],
            transactions_before + 2,
        )
        self.assertEqual(
            db.execute("SELECT COUNT(*) AS count FROM savings_transfer_records").fetchone()["count"],
            1,
        )
        self.assertEqual(
            db.execute(
                "SELECT contribution_basis_points FROM savings_rules ORDER BY id LIMIT 1"
            ).fetchone()["contribution_basis_points"],
            1000,
        )

    def test_account_delete_requires_savings_settings_to_be_reassigned(self) -> None:
        protected_account_id = accounts_repo.insert_account(
            {"name": "Protected Savings", "account_type": "bank"}
        )
        protected_envelope_id = envelopes_repo.insert_envelope(
            {"name": "Protected Goal", "locked_account_id": protected_account_id}
        )
        savings_repo.insert_rule({
            "name": "Protected Goal",
            "contribution_basis_points": 500,
            "accessible_account_id": protected_account_id,
            "accessible_envelope_id": protected_envelope_id,
            "long_term_account_id": None,
            "long_term_envelope_id": None,
            "accessible_target_cents": 0,
            "enabled": 1,
        })

        response = self.client.post(
            f"/accounts/{protected_account_id}/delete",
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("Reassign or remove those savings settings", response.get_data(as_text=True))
        self.assertIsNotNone(accounts_repo.get_account(protected_account_id))
