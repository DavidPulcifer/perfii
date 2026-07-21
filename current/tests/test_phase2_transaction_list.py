from app.db import get_db, get_meta_db
from app.repositories import accounts_repo, envelopes_repo, import_validation_repo
from app.services.transactions_service import TransactionsService
from tests.helpers import FinanceAppTestCase
from html import escape
from unittest import mock


class Phase2TransactionListTests(FinanceAppTestCase):
    """Regression coverage for shared transaction-list rendering.

    Phase 2 starts by collapsing the duplicated /tx/ and /tx/<account_id>
    list routes. These tests pin the important route-specific behavior before
    further transaction-flow cleanup.
    """

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

    def _global_envelope_pair(self) -> tuple[dict, dict]:
        """Create two synthetic envelopes that are valid for any account in the test."""
        first_id = envelopes_repo.insert_envelope(
            {"name": "Synthetic Flexible A", "locked_account_id": None}
        )
        second_id = envelopes_repo.insert_envelope(
            {"name": "Synthetic Flexible B", "locked_account_id": None}
        )
        return envelopes_repo.get_envelope(first_id), envelopes_repo.get_envelope(second_id)

    def _seed_list_transactions(self, account_id: int, count: int = 26) -> None:
        db = get_db()
        for idx in range(count):
            db.execute(
                """
                INSERT INTO transactions (
                    account_id, ttype, amount_cents, posted_at, payee, memo, ignore_match
                ) VALUES (?, 'expense', ?, ?, ?, ?, 1)
                """,
                (
                    account_id,
                    -(1000 + idx),
                    f"2026-05-{idx + 1:02d}",
                    f"Phase 2 List Payee {idx}",
                    "phase2-list-regression",
                ),
            )
        db.commit()

    def _insert_filter_transaction(
        self,
        *,
        account_id: int,
        envelope_id: int,
        ttype: str,
        amount_cents: int,
        posted_at: str,
        payee: str,
        memo: str = "fin030-multi-filter",
    ) -> None:
        db = get_db()
        cursor = db.execute(
            """
            INSERT INTO transactions (
                account_id, ttype, amount_cents, posted_at, payee, memo, ignore_match
            ) VALUES (?, ?, ?, ?, ?, ?, 1)
            """,
            (account_id, ttype, amount_cents, posted_at, payee, memo),
        )
        db.execute(
            "INSERT INTO transaction_splits (transaction_id, envelope_id, amount_cents) VALUES (?, ?, ?)",
            (cursor.lastrowid, envelope_id, amount_cents),
        )
        db.commit()

    def test_global_transaction_list_keeps_query_filters_and_root_pagination_url(self) -> None:
        self._select_user_in_client()
        account_id = accounts_repo.list_accounts()[0]["id"]
        self._seed_list_transactions(account_id)

        response = self.client.get(
            "/tx/",
            query_string={
                "account_id": str(account_id),
                "q_memo": "phase2-list-regression",
                "per_page": "25",
                "page": "1",
            },
        )

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("Phase 2 List Payee 2", html)
        self.assertIn('value="phase2-list-regression"', html)
        self.assertIn(f'href="/tx/?account_id={account_id}&amp;q_memo=phase2-list-regression&amp;per_page=25&amp;page=2"', html)

    def test_transaction_list_shows_top_and_bottom_pagination_when_multiple_pages(self) -> None:
        self._select_user_in_client()
        account_id = accounts_repo.list_accounts()[0]["id"]
        self._seed_list_transactions(account_id)

        response = self.client.get(
            "/tx/",
            query_string={
                "account_id": str(account_id),
                "q_memo": "phase2-list-regression",
                "per_page": "25",
                "page": "1",
            },
        )

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        next_href = f'href="/tx/?account_id={account_id}&amp;q_memo=phase2-list-regression&amp;per_page=25&amp;page=2"'
        self.assertIn('data-pagination-position="top"', html)
        self.assertIn('data-pagination-position="bottom"', html)
        self.assertEqual(html.count(next_href), 2)
        self.assertIn('data-pagination-position="top"', html.split('<div class="transaction-table-wrapper">', 1)[0])
        self.assertIn('data-pagination-position="bottom"', html.split('<div id="memoRevealPopover"', 1)[1])

    def test_transaction_list_omits_top_pagination_when_single_page(self) -> None:
        self._select_user_in_client()
        account_id = accounts_repo.list_accounts()[0]["id"]
        self._seed_list_transactions(account_id, count=2)

        response = self.client.get(
            "/tx/",
            query_string={
                "account_id": str(account_id),
                "q_memo": "phase2-list-regression",
                "per_page": "25",
                "page": "1",
            },
        )

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertNotIn('data-pagination-position="top"', html)
        self.assertIn('data-pagination-position="bottom"', html)
        self.assertIn('Showing 1-2 of 2', html)

    def test_account_transaction_list_uses_fixed_account_pagination_url(self) -> None:
        self._select_user_in_client()
        account_id = accounts_repo.list_accounts()[0]["id"]
        self._seed_list_transactions(account_id)

        response = self.client.get(
            f"/tx/{account_id}",
            query_string={
                "q_memo": "phase2-list-regression",
                "per_page": "25",
                "page": "1",
            },
        )

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("Phase 2 List Payee 2", html)
        self.assertIn(f'href="/tx/{account_id}?q_memo=phase2-list-regression&amp;per_page=25&amp;page=2"', html)
        self.assertIn(f'<option value="{account_id}" selected>', html)

    def test_transaction_list_supports_repeated_multi_select_filters_and_pagination(self) -> None:
        self._select_user_in_client()
        account_a, account_b = accounts_repo.list_accounts()[:2]
        envelope_a, envelope_b, envelope_c = envelopes_repo.list_envelopes()[:3]
        for idx in range(30):
            self._insert_filter_transaction(
                account_id=account_a["id"] if idx % 2 == 0 else account_b["id"],
                envelope_id=envelope_a["id"] if idx % 2 == 0 else envelope_b["id"],
                ttype="expense" if idx % 2 == 0 else "income",
                amount_cents=-(1200 + idx) if idx % 2 == 0 else (3400 + idx),
                posted_at=f"2026-05-{(idx % 28) + 1:02d}",
                payee=f"FIN030 Multi Match {idx:02d}",
            )
        self._insert_filter_transaction(
            account_id=account_a["id"],
            envelope_id=envelope_c["id"],
            ttype="transfer_in",
            amount_cents=5600,
            posted_at="2026-05-03",
            payee="FIN030 Multi Excluded",
        )

        response = self.client.get(
            "/tx/",
            query_string=[
                ("account_id", str(account_a["id"])),
                ("account_id", str(account_b["id"])),
                ("ttype", "expense"),
                ("ttype", "income"),
                ("envelope_id", str(envelope_a["id"])),
                ("envelope_id", str(envelope_b["id"])),
                ("q_memo", "fin030-multi-filter"),
                ("per_page", "25"),
                ("page", "1"),
            ],
        )

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("FIN030 Multi Match", html)
        self.assertNotIn("FIN030 Multi Excluded", html)
        self.assertIn(
            f'href="/tx/?account_id={account_a["id"]}&amp;account_id={account_b["id"]}&amp;'
            f'ttype=expense&amp;ttype=income&amp;envelope_id={envelope_a["id"]}&amp;'
            f'envelope_id={envelope_b["id"]}&amp;q_memo=fin030-multi-filter&amp;per_page=25&amp;page=2"',
            html,
        )
        self.assertIn(f'<option value="{account_a["id"]}" selected>', html)
        self.assertIn(f'<option value="{account_b["id"]}" selected>', html)
        self.assertRegex(html, r'<option value="expense"\s+selected>Expense</option>')
        self.assertRegex(html, r'<option value="income"\s+selected>Income</option>')
        self.assertIn(f'<option value="{envelope_a["id"]}" selected>', html)
        self.assertIn(f'<option value="{envelope_b["id"]}" selected>', html)

    def test_transaction_list_keeps_single_value_filter_query_compatibility(self) -> None:
        self._select_user_in_client()
        account_a, account_b = accounts_repo.list_accounts()[:2]
        envelope_a, envelope_b = self._global_envelope_pair()
        self._insert_filter_transaction(
            account_id=account_a["id"],
            envelope_id=envelope_a["id"],
            ttype="expense",
            amount_cents=-1200,
            posted_at="2026-05-01",
            payee="FIN030 Single Expense",
            memo="fin030-single-filter",
        )
        self._insert_filter_transaction(
            account_id=account_b["id"],
            envelope_id=envelope_b["id"],
            ttype="income",
            amount_cents=3400,
            posted_at="2026-05-02",
            payee="FIN030 Single Income",
            memo="fin030-single-filter",
        )

        response = self.client.get(
            "/tx/",
            query_string={
                "account_id": str(account_a["id"]),
                "ttype": "expense",
                "envelope_id": str(envelope_a["id"]),
                "q_memo": "fin030-single-filter",
            },
        )

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("FIN030 Single Expense", html)
        self.assertNotIn("FIN030 Single Income", html)
        self.assertIn(f'<option value="{account_a["id"]}" selected>', html)
        self.assertRegex(html, r'<option value="expense"\s+selected>Expense</option>')
        self.assertIn(f'<option value="{envelope_a["id"]}" selected>', html)


    def test_transaction_list_exact_amount_defaults_to_absolute_amounts(self) -> None:
        self._select_user_in_client()
        account_id = accounts_repo.list_accounts()[0]["id"]
        envelope_id = envelopes_repo.list_envelopes()[0]["id"]
        for idx in range(26):
            self._insert_filter_transaction(
                account_id=account_id,
                envelope_id=envelope_id,
                ttype="expense" if idx % 2 else "income",
                amount_cents=-1234 if idx % 2 else 1234,
                posted_at=f"2026-05-{(idx % 28) + 1:02d}",
                payee=f"FIN043 Exact Match {idx:02d}",
                memo="fin043-exact-filter",
            )
        self._insert_filter_transaction(
            account_id=account_id,
            envelope_id=envelope_id,
            ttype="income",
            amount_cents=1200,
            posted_at="2026-05-28",
            payee="FIN043 Not Exact",
            memo="fin043-exact-filter",
        )

        response = self.client.get(
            "/tx/",
            query_string={
                "amount_exact": "12.34",
                "amount_min": "1.00",
                "amount_max": "99.00",
                "q_memo": "fin043-exact-filter",
                "per_page": "25",
                "page": "1",
            },
        )

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("FIN043 Exact Match", html)
        self.assertNotIn("FIN043 Not Exact", html)
        self.assertIn('name="amount_exact" value="12.34"', html)
        self.assertIn('name="amount_min" value="1.00"', html)
        self.assertIn('name="amount_max" value="99.00"', html)
        self.assertIn('href="/tx/?amount_exact=12.34&amp;amount_min=1.00&amp;amount_max=99.00&amp;q_memo=fin043-exact-filter&amp;per_page=25&amp;page=2"', html)

        page_two = self.client.get(
            "/tx/",
            query_string={
                "amount_exact": "12.34",
                "q_memo": "fin043-exact-filter",
                "per_page": "25",
                "page": "2",
            },
        )
        page_two_html = page_two.get_data(as_text=True)
        self.assertIn("FIN043 Exact Match", page_two_html)
        self.assertNotIn("FIN043 Not Exact", page_two_html)

    def test_transaction_list_exact_amount_respects_signed_mode(self) -> None:
        self._select_user_in_client()
        account_id = accounts_repo.list_accounts()[0]["id"]
        envelope_id = envelopes_repo.list_envelopes()[0]["id"]
        self._insert_filter_transaction(
            account_id=account_id,
            envelope_id=envelope_id,
            ttype="expense",
            amount_cents=-1234,
            posted_at="2026-05-10",
            payee="FIN043 Signed Expense",
            memo="fin043-signed-exact",
        )
        self._insert_filter_transaction(
            account_id=account_id,
            envelope_id=envelope_id,
            ttype="income",
            amount_cents=1234,
            posted_at="2026-05-11",
            payee="FIN043 Signed Income",
            memo="fin043-signed-exact",
        )

        response = self.client.get(
            "/tx/",
            query_string={"amount_exact": "-12.34", "abs": "0", "q_memo": "fin043-signed-exact"},
        )

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("FIN043 Signed Expense", html)
        self.assertNotIn("FIN043 Signed Income", html)
        self.assertIn('name="amount_exact" value="-12.34"', html)
        self.assertIn('id="absChk" name="abs" value="1" ', html)

    def test_transaction_edit_preserves_filtered_return_state(self) -> None:
        self._select_user_in_client()
        account_id = accounts_repo.list_accounts()[0]["id"]
        envelope_id = envelopes_repo.list_envelopes()[0]["id"]
        tx_id = TransactionsService.create_expense(
            payload={
                "account_id": account_id,
                "posted_at": "2026-05-20",
                "payee": "FIN071 Edit State",
                "memo": "fin071-filter-state",
                "amount": "12.34",
            },
            splits=[{"envelope_id": envelope_id, "amount": "12.34"}],
        )
        return_to = "/tx/?q_memo=fin071-filter-state&amount_exact=12.34&per_page=25&page=1"

        list_response = self.client.get(return_to)
        self.assertEqual(list_response.status_code, 200)
        list_html = list_response.get_data(as_text=True)
        self.assertIn(f"/tx/{tx_id}/edit", list_html)
        self.assertIn("return_to=", list_html)
        self.assertIn("fin071-filter-state", list_html)

        edit_response = self.client.get(f"/tx/{tx_id}/edit", query_string={"return_to": return_to})
        self.assertEqual(edit_response.status_code, 200)
        edit_html = edit_response.get_data(as_text=True)
        self.assertIn('name="return_to" value="/tx/?q_memo=fin071-filter-state&amp;amount_exact=12.34&amp;per_page=25&amp;page=1"', edit_html)
        self.assertIn('href="/tx/?q_memo=fin071-filter-state&amp;amount_exact=12.34&amp;per_page=25&amp;page=1"', edit_html)

        post_response = self.client.post(
            f"/tx/{tx_id}/edit",
            data={
                "return_to": return_to,
                "account_id": str(account_id),
                "posted_at": "2026-05-21",
                "amount": "12.34",
                "payee": "FIN071 Edit State Saved",
                "memo": "fin071-filter-state",
                f"edit_amt_{envelope_id}": "12.34",
            },
            follow_redirects=False,
        )
        self.assertEqual(post_response.status_code, 302)
        self.assertEqual(post_response.headers["Location"], return_to)

    def test_transaction_edit_invalid_amount_keeps_return_state_on_edit_redirect(self) -> None:
        self._select_user_in_client()
        account_id = accounts_repo.list_accounts()[0]["id"]
        envelope_id = envelopes_repo.list_envelopes()[0]["id"]
        tx_id = TransactionsService.create_expense(
            payload={
                "account_id": account_id,
                "posted_at": "2026-05-20",
                "payee": "FIN071 Invalid Edit",
                "memo": "fin071-invalid-state",
                "amount": "12.34",
            },
            splits=[{"envelope_id": envelope_id, "amount": "12.34"}],
        )
        return_to = "/tx/?q_memo=fin071-invalid-state&per_page=25&page=2"

        response = self.client.post(
            f"/tx/{tx_id}/edit",
            data={
                "return_to": return_to,
                "account_id": str(account_id),
                "posted_at": "2026-05-21",
                "amount": "not-a-number",
                "payee": "FIN071 Invalid Edit",
                "memo": "fin071-invalid-state",
                f"edit_amt_{envelope_id}": "12.34",
            },
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 302)
        self.assertIn(f"/tx/{tx_id}/edit", response.headers["Location"])
        self.assertIn("return_to=", response.headers["Location"])
        self.assertIn("fin071-invalid-state", response.headers["Location"])

    def test_transaction_delete_preserves_filtered_return_state_and_rejects_external_return(self) -> None:
        self._select_user_in_client()
        account_id = accounts_repo.list_accounts()[0]["id"]
        envelope_id = envelopes_repo.list_envelopes()[0]["id"]
        tx_id = TransactionsService.create_expense(
            payload={
                "account_id": account_id,
                "posted_at": "2026-05-20",
                "payee": "FIN071 Delete State",
                "memo": "fin071-delete-state",
                "amount": "12.34",
            },
            splits=[{"envelope_id": envelope_id, "amount": "12.34"}],
        )
        return_to = "/tx/?q_memo=fin071-delete-state&per_page=25&page=2"

        list_response = self.client.get(return_to)
        self.assertEqual(list_response.status_code, 200)
        list_html = list_response.get_data(as_text=True)
        self.assertIn('name="return_to" value="/tx/?q_memo=fin071-delete-state&amp;per_page=25&amp;page=2"', list_html)

        delete_response = self.client.post(
            f"/tx/{tx_id}/delete",
            data={"return_to": return_to},
            follow_redirects=False,
        )
        self.assertEqual(delete_response.status_code, 302)
        self.assertEqual(delete_response.headers["Location"], return_to)

        evil_tx_id = TransactionsService.create_expense(
            payload={
                "account_id": account_id,
                "posted_at": "2026-05-22",
                "payee": "FIN071 External Return",
                "amount": "1.00",
            },
            splits=[{"envelope_id": envelope_id, "amount": "1.00"}],
        )
        evil_response = self.client.post(
            f"/tx/{evil_tx_id}/delete",
            data={"return_to": "https://example.invalid/steal"},
            follow_redirects=False,
        )
        self.assertEqual(evil_response.status_code, 302)
        self.assertEqual(evil_response.headers["Location"], "/tx/")

    def test_transfer_edit_preserves_filtered_return_state(self) -> None:
        self._select_user_in_client()
        account_from, account_to = accounts_repo.list_accounts()[:2]
        envelope_from, envelope_to = self._global_envelope_pair()
        tx_out_id, _tx_in_id = TransactionsService.create_transfer(
            payload={
                "from_account_id": account_from["id"],
                "to_account_id": account_to["id"],
                "posted_at": "2026-05-20",
                "amount": "25.00",
                "memo": "fin071-transfer-state",
            },
            out_splits=[{"envelope_id": envelope_from["id"], "amount_cents": -2500}],
            in_splits=[{"envelope_id": envelope_to["id"], "amount_cents": 2500}],
        )
        return_to = "/tx/?q_memo=fin071-transfer-state&ttype=transfer&per_page=25&page=2"

        edit_response = self.client.get(f"/tx/transfer/{tx_out_id}/edit", query_string={"return_to": return_to})
        self.assertEqual(edit_response.status_code, 200)
        edit_html = edit_response.get_data(as_text=True)
        self.assertIn('name="return_to" value="/tx/?q_memo=fin071-transfer-state&amp;ttype=transfer&amp;per_page=25&amp;page=2"', edit_html)
        self.assertIn('href="/tx/?q_memo=fin071-transfer-state&amp;ttype=transfer&amp;per_page=25&amp;page=2"', edit_html)

        post_response = self.client.post(
            f"/tx/transfer/{tx_out_id}/edit",
            data={
                "return_to": return_to,
                "posted_at": "2026-05-21",
                "amount": "25.00",
                "memo": "fin071-transfer-state",
                "from_account_id": str(account_from["id"]),
                "to_account_id": str(account_to["id"]),
                f"from_amount_{envelope_from['id']}": "25.00",
                f"to_amount_{envelope_to['id']}": "25.00",
            },
            follow_redirects=False,
        )
        self.assertEqual(post_response.status_code, 302)
        self.assertEqual(post_response.headers["Location"], return_to)

    def test_transaction_list_shows_compact_memo_preview_with_full_reveal(self) -> None:
        self._select_user_in_client()
        account_id = accounts_repo.list_accounts()[0]["id"]
        long_memo = (
            "FIN-020 memo preview regression with enough detail to overflow the "
            "transaction table if rendered fully inline."
        )
        db = get_db()
        db.execute(
            """
            INSERT INTO transactions (
                account_id, ttype, amount_cents, posted_at, payee, memo, ignore_match
            ) VALUES (?, 'expense', -1234, '2026-05-04', 'FIN-020 Memo Payee', ?, 1)
            """,
            (account_id, long_memo),
        )
        db.commit()

        response = self.client.get("/tx/", query_string={"q_payee": "FIN-020 Memo Payee"})

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('class="memo-preview-button"', html)
        self.assertIn('aria-label="Show full memo"', html)
        self.assertIn('data-memo-full=', html)
        self.assertIn('id="memoRevealPopover" class="memo-reveal-popover" role="tooltip"', html)
        self.assertIn('function showMemoReveal(anchor, pinned=false)', html)
        self.assertIn('position: fixed;', html)
        self.assertIn(escape(long_memo), html)
        self.assertIn("FIN-020 memo preview regression with enough det…", html)

    def test_manual_transfer_rejects_invalid_split_without_writing(self) -> None:
        self._select_user_in_client()
        account_from, account_to = accounts_repo.list_accounts()[:2]
        envelope_from, envelope_to = self._global_envelope_pair()
        db = get_db()
        before_count = db.execute("SELECT COUNT(*) AS c FROM transactions").fetchone()["c"]

        response = self.client.post(
            "/tx/new/transfer",
            data={
                "from_account_id": str(account_from["id"]),
                "to_account_id": str(account_to["id"]),
                "amount": "25.00",
                "posted_at": "2026-05-06",
                f"transfer_from_{envelope_from['id']}": "not-a-number",
                f"transfer_to_{envelope_to['id']}": "25.00",
            },
            follow_redirects=False,
        )

        after_count = db.execute("SELECT COUNT(*) AS c FROM transactions").fetchone()["c"]
        self.assertEqual(response.status_code, 302)
        self.assertEqual(after_count, before_count)

    def test_fin037_income_modal_bypass_rejects_split_mismatch_without_writing(self) -> None:
        self._select_user_in_client()
        account = accounts_repo.list_accounts()[0]
        envelope = envelopes_repo.list_envelopes()[0]
        db = get_db()
        before_count = db.execute("SELECT COUNT(*) AS c FROM transactions").fetchone()["c"]

        response = self.client.post(
            "/tx/new/income",
            data={
                "account_id": str(account["id"]),
                "amount": "50.00",
                "posted_at": "2026-06-06",
                "payee": "FIN-037 mismatch",
                f"income_{envelope['id']}": "10.00",
            },
            follow_redirects=False,
        )

        after_count = db.execute("SELECT COUNT(*) AS c FROM transactions").fetchone()["c"]
        self.assertEqual(response.status_code, 302)
        self.assertEqual(after_count, before_count)

    def test_manual_transfer_applies_remainder_split_on_both_sides(self) -> None:
        self._select_user_in_client()
        account_from, account_to = accounts_repo.list_accounts()[:2]
        envelope_from, envelope_to = self._global_envelope_pair()
        db = get_db()
        before_max_id = db.execute("SELECT COALESCE(MAX(id), 0) AS id FROM transactions").fetchone()["id"]

        response = self.client.post(
            "/tx/new/transfer",
            data={
                "from_account_id": str(account_from["id"]),
                "to_account_id": str(account_to["id"]),
                "amount": "25.00",
                "posted_at": "2026-05-06",
                "memo": "phase2 transfer remainder",
                f"transfer_from_{envelope_from['id']}": "10.00",
                "from_remainder": str(envelope_from["id"]),
                f"transfer_to_{envelope_to['id']}": "15.00",
                "to_remainder": str(envelope_to["id"]),
            },
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 302)
        rows = db.execute(
            "SELECT id, ttype, amount_cents FROM transactions WHERE id > ? AND memo=? ORDER BY id",
            (before_max_id, "phase2 transfer remainder"),
        ).fetchall()
        self.assertEqual(len(rows), 2)
        out_id = next(row["id"] for row in rows if row["ttype"] == "transfer_out")
        in_id = next(row["id"] for row in rows if row["ttype"] == "transfer_in")

        splits = db.execute(
            "SELECT transaction_id, envelope_id, amount_cents FROM transaction_splits WHERE transaction_id IN (?, ?) ORDER BY transaction_id, amount_cents",
            (out_id, in_id),
        ).fetchall()
        self.assertEqual(
            [(row["transaction_id"], row["envelope_id"], row["amount_cents"]) for row in splits],
            [
                (out_id, envelope_from["id"], -1500),
                (out_id, envelope_from["id"], -1000),
                (in_id, envelope_to["id"], 1000),
                (in_id, envelope_to["id"], 1500),
            ],
        )

    def test_transaction_edit_prefills_stored_remainder_selector(self) -> None:
        self._select_user_in_client()
        account = accounts_repo.list_accounts()[0]
        fixed_envelope, remainder_envelope = self._global_envelope_pair()
        tx_id = TransactionsService.create_expense(
            payload={
                "account_id": account["id"],
                "posted_at": "2026-05-18",
                "payee": "FIN048 Edit Prefill",
                "amount": "25.00",
            },
            splits=[{"envelope_id": fixed_envelope["id"], "amount": "10.00"}],
            remainder_envelope_id=remainder_envelope["id"],
        )

        response = self.client.get(f"/tx/{tx_id}/edit")

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('name="remainder_envelope_id"', html)
        self.assertIn(f'data-remainder-initial="{remainder_envelope["id"]}"', html)

    def test_allocation_edit_is_not_validated_against_zero_parent_amount(self) -> None:
        self._select_user_in_client()
        account = accounts_repo.list_accounts()[0]
        envelope_a, envelope_b = self._global_envelope_pair()
        tx_id = TransactionsService.create_allocation(
            payload={
                "account_id": account["id"],
                "posted_at": "2026-05-18",
                "memo": "allocation edit freeform",
            },
            splits=[
                {"envelope_id": envelope_a["id"], "amount_cents": 1000},
                {"envelope_id": envelope_b["id"], "amount_cents": -984},
            ],
            total_cents=16,
        )

        edit_response = self.client.get(f"/tx/{tx_id}/edit")
        self.assertEqual(edit_response.status_code, 200)
        html = edit_response.get_data(as_text=True)
        edit_form_html = html.split('<div class="d-flex gap-2 mt-4">', 1)[0]
        self.assertIn('data-scope-id="edit"', html)
        self.assertIn('data-total-selector=""', html)
        self.assertNotIn('name="remainder_envelope_id"', edit_form_html)

        post_response = self.client.post(
            f"/tx/{tx_id}/edit",
            data={
                "account_id": str(account["id"]),
                "posted_at": "2026-05-18",
                "amount": "0",
                "payee": "",
                "memo": "allocation edit freeform",
                f"edit_amt_{envelope_a['id']}": "0.16",
                f"edit_amt_{envelope_b['id']}": "3.00",
            },
            follow_redirects=True,
        )

        self.assertEqual(post_response.status_code, 200)
        self.assertIn("Transaction updated", post_response.get_data(as_text=True))
        rows = get_db().execute(
            """
            SELECT envelope_id, amount_cents
            FROM transaction_splits
            WHERE transaction_id=?
            ORDER BY envelope_id
            """,
            (tx_id,),
        ).fetchall()
        self.assertCountEqual(
            [(row["envelope_id"], row["amount_cents"]) for row in rows],
            [(envelope_a["id"], 16), (envelope_b["id"], 300)],
        )

    def test_transfer_edit_prefills_stored_remainder_selectors(self) -> None:
        self._select_user_in_client()
        account_from, account_to = accounts_repo.list_accounts()[:2]
        envelope_from, envelope_to = self._global_envelope_pair()
        tx_out_id, _tx_in_id = TransactionsService.create_transfer(
            payload={
                "from_account_id": account_from["id"],
                "to_account_id": account_to["id"],
                "posted_at": "2026-05-18",
                "amount": "25.00",
                "memo": "FIN048 transfer edit prefill",
            },
            out_splits=[{"envelope_id": envelope_from["id"], "amount_cents": -1000}],
            in_splits=[{"envelope_id": envelope_to["id"], "amount_cents": 1500}],
            out_remainder_envelope_id=envelope_from["id"],
            in_remainder_envelope_id=envelope_to["id"],
        )

        response = self.client.get(f"/tx/transfer/{tx_out_id}/edit")

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('name="from_remainder"', html)
        self.assertIn('name="to_remainder"', html)
        self.assertIn(f'data-remainder-initial="{envelope_from["id"]}"', html)
        self.assertIn(f'data-remainder-initial="{envelope_to["id"]}"', html)

    def test_transaction_edit_rejects_invalid_amount_without_changing_existing_transaction(self) -> None:
        self._select_user_in_client()
        account = accounts_repo.list_accounts()[0]
        envelope = envelopes_repo.list_envelopes()[0]
        tx_id = TransactionsService.create_expense(
            payload={
                "account_id": account["id"],
                "amount": "12.34",
                "posted_at": "2026-05-06",
                "payee": "Phase 2 invalid edit",
                "memo": "before invalid edit",
            },
            splits=[{"envelope_id": envelope["id"], "amount": "12.34"}],
        )
        db = get_db()
        before = dict(db.execute("SELECT * FROM transactions WHERE id=?", (tx_id,)).fetchone())

        response = self.client.post(
            f"/tx/{tx_id}/edit",
            data={
                "posted_at": "2026-05-07",
                "payee": "should not persist",
                "memo": "should not persist",
                "amount": "not-a-number",
                f"edit_amt_{envelope['id']}": "12.34",
            },
            follow_redirects=False,
        )

        after = dict(db.execute("SELECT * FROM transactions WHERE id=?", (tx_id,)).fetchone())
        self.assertEqual(response.status_code, 302)
        self.assertIn(f"/tx/{tx_id}/edit", response.headers["Location"])
        self.assertEqual(after, before)

    def test_fin070_expense_conversion_keeps_original_row_as_transfer_out_with_import_evidence(self) -> None:
        self._select_user_in_client()
        account_from, account_to = accounts_repo.list_accounts()[:2]
        envelope_from, envelope_to = self._global_envelope_pair()
        tx_id = TransactionsService.create_expense(
            payload={
                "account_id": account_from["id"],
                "posted_at": "2026-06-06",
                "payee": "Imported Card Payment",
                "memo": "fin070 expense conversion",
                "amount": "25.00",
                "fitid": "fin070-expense-fitid",
                "external_counterparty": "Imported Counterparty",
                "ignore_match": 1,
            },
            splits=[{"envelope_id": envelope_from["id"], "amount": "25.00"}],
        )
        import_validation_repo.record_transaction_import_validation(
            account_id=account_from["id"],
            transaction_id=tx_id,
            source="import_commit",
            fitid="fin070-expense-fitid",
        )
        return_to = "/tx/?q_memo=fin070%20expense%20conversion"

        response = self.client.post(
            f"/tx/{tx_id}/convert-transfer",
            data={
                "return_to": return_to,
                "other_account_id": str(account_to["id"]),
                f"convert_current_amount_{envelope_from['id']}": "25.00",
                f"convert_other_amount_{envelope_to['id']}": "25.00",
            },
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 302)
        self.assertIn(f"/tx/transfer/{tx_id}/edit", response.headers["Location"])
        db = get_db()
        original = dict(db.execute("SELECT * FROM transactions WHERE id=?", (tx_id,)).fetchone())
        pair = dict(db.execute("SELECT * FROM transactions WHERE id=?", (original["xfer_pair_id"],)).fetchone())
        self.assertEqual(original["account_id"], account_from["id"])
        self.assertEqual(original["ttype"], "transfer_out")
        self.assertEqual(original["amount_cents"], -2500)
        self.assertEqual(original["fitid"], "fin070-expense-fitid")
        self.assertEqual(original["external_counterparty"], "Imported Counterparty")
        self.assertEqual(original["ignore_match"], 1)
        self.assertEqual(pair["account_id"], account_to["id"])
        self.assertEqual(pair["ttype"], "transfer_in")
        self.assertEqual(pair["amount_cents"], 2500)
        self.assertEqual(pair["xfer_pair_id"], tx_id)
        self.assertIsNone(pair["fitid"])
        self.assertIsNotNone(import_validation_repo.get_transaction_import_validation(account_from["id"], tx_id))
        self.assertIsNone(import_validation_repo.get_transaction_import_validation(account_to["id"], pair["id"]))
        split_rows = db.execute(
            "SELECT transaction_id, envelope_id, amount_cents FROM transaction_splits WHERE transaction_id IN (?, ?) ORDER BY transaction_id, amount_cents",
            (tx_id, pair["id"]),
        ).fetchall()
        self.assertEqual(
            [(row["transaction_id"], row["envelope_id"], row["amount_cents"]) for row in split_rows],
            [(tx_id, envelope_from["id"], -2500), (pair["id"], envelope_to["id"], 2500)],
        )

    def test_fin070_income_conversion_keeps_original_row_as_transfer_in(self) -> None:
        self._select_user_in_client()
        account_to, account_from = accounts_repo.list_accounts()[:2]
        envelope_to, envelope_from = self._global_envelope_pair()
        tx_id = TransactionsService.create_income(
            payload={
                "account_id": account_to["id"],
                "posted_at": "2026-06-06",
                "payee": "Incoming Transfer",
                "memo": "fin070 income conversion",
                "amount": "40.00",
            },
            splits=[{"envelope_id": envelope_to["id"], "amount": "40.00"}],
        )

        response = self.client.post(
            f"/tx/{tx_id}/convert-transfer",
            data={
                "other_account_id": str(account_from["id"]),
                f"convert_current_amount_{envelope_to['id']}": "40.00",
                f"convert_other_amount_{envelope_from['id']}": "40.00",
            },
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 302)
        db = get_db()
        original = dict(db.execute("SELECT * FROM transactions WHERE id=?", (tx_id,)).fetchone())
        pair = dict(db.execute("SELECT * FROM transactions WHERE id=?", (original["xfer_pair_id"],)).fetchone())
        self.assertEqual(original["ttype"], "transfer_in")
        self.assertEqual(original["account_id"], account_to["id"])
        self.assertEqual(original["amount_cents"], 4000)
        self.assertEqual(pair["ttype"], "transfer_out")
        self.assertEqual(pair["account_id"], account_from["id"])
        self.assertEqual(pair["amount_cents"], -4000)
        split_rows = db.execute(
            "SELECT transaction_id, envelope_id, amount_cents FROM transaction_splits WHERE transaction_id IN (?, ?) ORDER BY transaction_id, amount_cents",
            (tx_id, pair["id"]),
        ).fetchall()
        self.assertEqual(
            [(row["transaction_id"], row["envelope_id"], row["amount_cents"]) for row in split_rows],
            [(tx_id, envelope_to["id"], 4000), (pair["id"], envelope_from["id"], -4000)],
        )

    def test_fin070_conversion_rejects_invalid_splits_without_writing(self) -> None:
        self._select_user_in_client()
        account_from, account_to = accounts_repo.list_accounts()[:2]
        envelope_from, envelope_to = self._global_envelope_pair()
        tx_id = TransactionsService.create_expense(
            payload={
                "account_id": account_from["id"],
                "posted_at": "2026-06-06",
                "payee": "Invalid Conversion",
                "memo": "fin070 invalid conversion",
                "amount": "25.00",
            },
            splits=[{"envelope_id": envelope_from["id"], "amount": "25.00"}],
        )
        db = get_db()
        before_tx = dict(db.execute("SELECT * FROM transactions WHERE id=?", (tx_id,)).fetchone())
        before_count = db.execute("SELECT COUNT(*) AS c FROM transactions").fetchone()["c"]

        response = self.client.post(
            f"/tx/{tx_id}/convert-transfer",
            data={
                "other_account_id": str(account_to["id"]),
                f"convert_current_amount_{envelope_from['id']}": "25.00",
                f"convert_other_amount_{envelope_to['id']}": "10.00",
            },
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 302)
        self.assertIn(f"/tx/{tx_id}/edit", response.headers["Location"])
        self.assertEqual(db.execute("SELECT COUNT(*) AS c FROM transactions").fetchone()["c"], before_count)
        self.assertEqual(dict(db.execute("SELECT * FROM transactions WHERE id=?", (tx_id,)).fetchone()), before_tx)

    def test_fin070_conversion_rolls_back_if_pair_split_insert_fails(self) -> None:
        self._select_user_in_client()
        account_from, account_to = accounts_repo.list_accounts()[:2]
        envelope_from, envelope_to = self._global_envelope_pair()
        tx_id = TransactionsService.create_expense(
            payload={
                "account_id": account_from["id"],
                "posted_at": "2026-06-06",
                "payee": "Atomic Conversion",
                "memo": "fin070 atomic conversion",
                "amount": "25.00",
                "fitid": "fin070-atomic-fitid",
            },
            splits=[{"envelope_id": envelope_from["id"], "amount": "25.00"}],
        )
        db = get_db()
        before_tx = dict(db.execute("SELECT * FROM transactions WHERE id=?", (tx_id,)).fetchone())
        before_splits = [dict(row) for row in db.execute("SELECT * FROM transaction_splits WHERE transaction_id=?", (tx_id,)).fetchall()]
        before_count = db.execute("SELECT COUNT(*) AS c FROM transactions").fetchone()["c"]

        from app.services import transactions_service as service_module
        real_insert = service_module.splits_repo.insert_split

        def fail_for_pair(*, db, transaction_id, envelope_id, amount_cents):
            if int(transaction_id) != int(tx_id):
                raise RuntimeError("simulated pair split failure")
            return real_insert(db=db, transaction_id=transaction_id, envelope_id=envelope_id, amount_cents=amount_cents)

        with mock.patch.object(service_module.splits_repo, "insert_split", side_effect=fail_for_pair):
            with self.assertRaises(RuntimeError):
                TransactionsService.convert_standard_transaction_to_transfer(
                    tx_id,
                    other_account_id=account_to["id"],
                    current_splits=[{"envelope_id": envelope_from["id"], "amount_cents": -2500}],
                    other_splits=[{"envelope_id": envelope_to["id"], "amount_cents": 2500}],
                )

        self.assertEqual(db.execute("SELECT COUNT(*) AS c FROM transactions").fetchone()["c"], before_count)
        self.assertEqual(dict(db.execute("SELECT * FROM transactions WHERE id=?", (tx_id,)).fetchone()), before_tx)
        self.assertEqual([dict(row) for row in db.execute("SELECT * FROM transaction_splits WHERE transaction_id=?", (tx_id,)).fetchall()], before_splits)

    def test_fin070_edit_page_shows_conversion_panel_for_standard_but_not_existing_transfer(self) -> None:
        self._select_user_in_client()
        account_from, account_to = accounts_repo.list_accounts()[:2]
        envelope_from, envelope_to = self._global_envelope_pair()
        tx_id = TransactionsService.create_expense(
            payload={"account_id": account_from["id"], "posted_at": "2026-06-06", "payee": "Panel", "amount": "10.00"},
            splits=[{"envelope_id": envelope_from["id"], "amount": "10.00"}],
        )
        transfer_out_id, _ = TransactionsService.create_transfer(
            payload={"from_account_id": account_from["id"], "to_account_id": account_to["id"], "posted_at": "2026-06-06", "amount": "5.00"},
            out_splits=[{"envelope_id": envelope_from["id"], "amount_cents": -500}],
            in_splits=[{"envelope_id": envelope_to["id"], "amount_cents": 500}],
        )

        standard_response = self.client.get(f"/tx/{tx_id}/edit")
        self.assertEqual(standard_response.status_code, 200)
        standard_html = standard_response.get_data(as_text=True)
        self.assertIn("Convert to transfer", standard_html)
        self.assertIn(f'/tx/{tx_id}/convert-transfer', standard_html)
        self.assertIn('name="other_account_id"', standard_html)

        transfer_response = self.client.get(f"/tx/transfer/{transfer_out_id}/edit")
        self.assertEqual(transfer_response.status_code, 200)
        self.assertNotIn("Convert to transfer", transfer_response.get_data(as_text=True))
