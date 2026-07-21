import unittest

from app.db import get_db, get_meta_db, index_exists, table_columns, table_exists
from app.repositories import accounts_repo, envelopes_repo, import_matching_rules_repo, import_rule_proposals_repo
from app.services.import_rule_proposal_service import (
    approve_import_rule_proposal,
    ignore_import_rule_proposal,
    refresh_import_rule_proposals,
    reject_import_rule_proposal,
    safe_refresh_import_rule_proposals,
)
from tests.helpers import FinanceAppTestCase


def _candidate(account_id: int, envelope_id: int, *, key: str = "rule-proposal:test:coffee", action=None) -> dict:
    condition = {
        "direction": "expense",
        "field": "text",
        "operator": "contains",
        "value": "fin092b coffee",
    }
    action_json = action if action is not None else {
        "payee": "FIN092B Coffee",
        "transaction_type": "expense",
        "single_envelope_id": envelope_id,
    }
    return {
        "candidate_key": key,
        "decision": "suggest",
        "reason_codes": ["rule_proposal_conservative_support_met"],
        "condition_json": condition,
        "action_json": action_json,
        "evidence": {
            "support_examples": 3,
            "distinct_raw_identities": 3,
            "feedback_accepted": 3,
            "raw_samples": [{"payee": "FIN092B COFFEE 1001", "memo": "CARD"}],
        },
        "suggested_rule": {
            "account_id": account_id,
            "enabled": False,
            "priority": 100,
            "name": "Suggested import rule: fin092b coffee",
            "condition_json": condition,
            "action_json": action_json,
        },
    }


class ImportRuleProposalReviewTests(FinanceAppTestCase):
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

    def _account_and_envelope(self) -> tuple[dict, dict]:
        return accounts_repo.list_accounts()[0], envelopes_repo.list_envelopes()[0]

    def test_import_rule_proposals_schema_exists(self) -> None:
        db = get_db()

        self.assertTrue(table_exists(db, "import_rule_proposals"))
        columns = table_columns(db, "import_rule_proposals")
        self.assertIn("fingerprint", columns)
        self.assertIn("evidence_json", columns)
        self.assertIn("reviewer_decision", columns)
        self.assertTrue(index_exists(db, "idx_import_rule_proposals_status"))

    def test_refresh_persists_and_deduplicates_without_creating_rules(self) -> None:
        account, envelope = self._account_and_envelope()
        candidate = _candidate(account["id"], envelope["id"])
        before_rules = len(import_matching_rules_repo.list_import_matching_rules(include_disabled=True))

        first = refresh_import_rule_proposals(
            account_id=account["id"],
            build_proposals_func=lambda **_: {"proposals": [candidate], "withheld": [], "source_notes": []},
        )
        second = refresh_import_rule_proposals(
            account_id=account["id"],
            build_proposals_func=lambda **_: {"proposals": [candidate], "withheld": [], "source_notes": []},
        )

        proposals = import_rule_proposals_repo.list_import_rule_proposals(account_id=account["id"], status="pending")
        after_rules = len(import_matching_rules_repo.list_import_matching_rules(include_disabled=True))
        self.assertEqual(first["created"], 1)
        self.assertEqual(second["deduped"], 1)
        self.assertEqual(len(proposals), 1)
        self.assertEqual(proposals[0]["fingerprint"], candidate["candidate_key"])
        self.assertEqual(proposals[0]["status"], "pending")
        self.assertEqual(proposals[0]["evidence_json"]["support_examples"], 3)
        self.assertEqual(after_rules, before_rules)

    def test_refresh_marks_missing_pending_proposal_stale_and_reappearance_clears_it(self) -> None:
        account, envelope = self._account_and_envelope()
        candidate = _candidate(account["id"], envelope["id"], key="rule-proposal:test:stale")
        refresh_import_rule_proposals(
            account_id=account["id"],
            build_proposals_func=lambda **_: {"proposals": [candidate], "withheld": [], "source_notes": []},
        )
        proposal = import_rule_proposals_repo.list_import_rule_proposals(status="pending")[0]

        stale_result = refresh_import_rule_proposals(
            account_id=account["id"],
            build_proposals_func=lambda **_: {"proposals": [], "withheld": [], "source_notes": []},
        )
        stale = import_rule_proposals_repo.get_import_rule_proposal(proposal["id"])

        self.assertEqual(stale_result["stale"], 1)
        self.assertEqual(stale["status"], "pending")
        self.assertEqual(stale["reviewer_decision"], "stale_source_changed")
        self.assertEqual(stale["evidence_json"]["refresh_status"], "stale_source_changed")
        self.assertTrue(stale["validation_errors_json"])
        self.assertFalse(approve_import_rule_proposal(proposal["id"], enabled=False).ok)

        refresh_import_rule_proposals(
            account_id=account["id"],
            build_proposals_func=lambda **_: {"proposals": [candidate], "withheld": [], "source_notes": []},
        )
        fresh = import_rule_proposals_repo.get_import_rule_proposal(proposal["id"])
        self.assertIsNone(fresh["reviewer_decision"])
        self.assertFalse(fresh["validation_errors_json"])
        self.assertNotEqual(fresh["evidence_json"].get("refresh_status"), "stale_source_changed")

    def test_safe_refresh_failure_does_not_escape_to_commit_or_edit_callers(self) -> None:
        class Logger:
            def __init__(self) -> None:
                self.messages = []

            def exception(self, message, *args) -> None:
                self.messages.append(message % args)

        def failing_refresh(**_kwargs):
            raise RuntimeError("analysis exploded")

        logger = Logger()
        result = safe_refresh_import_rule_proposals(
            account_id=42,
            reason="unit_test",
            logger=logger,
            refresh_func=failing_refresh,
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["created"], 0)
        self.assertIn("analysis exploded", result["error"])
        self.assertTrue(logger.messages)

    def test_approval_creates_disabled_rule_only_after_user_action(self) -> None:
        account, envelope = self._account_and_envelope()
        refresh_import_rule_proposals(
            account_id=account["id"],
            build_proposals_func=lambda **_: {
                "proposals": [_candidate(account["id"], envelope["id"])],
                "withheld": [],
                "source_notes": [],
            },
        )
        proposal = import_rule_proposals_repo.list_import_rule_proposals(status="pending")[0]

        result = approve_import_rule_proposal(proposal["id"], enabled=False)

        self.assertTrue(result.ok)
        stored = import_rule_proposals_repo.get_import_rule_proposal(proposal["id"])
        rule = import_matching_rules_repo.get_import_matching_rule(result.rule_id)
        self.assertEqual(stored["status"], "accepted")
        self.assertEqual(stored["reviewer_decision"], "approved_disabled")
        self.assertEqual(stored["approved_rule_id"], result.rule_id)
        self.assertEqual(rule["enabled"], 0)
        self.assertEqual(rule["condition_json"]["value"], "fin092b coffee")
        self.assertEqual(rule["action_json"]["single_envelope_id"], envelope["id"])

    def test_approval_of_advanced_split_proposal_uses_existing_validation_path(self) -> None:
        account = accounts_repo.list_accounts()[0]
        envelopes = [
            envelope for envelope in envelopes_repo.list_envelopes()
            if envelope.get("locked_account_id") in (None, account["id"])
        ]
        if len(envelopes) < 2:
            envelopes_repo.insert_envelope({"name": "FIN092D Review Extra", "locked_account_id": account["id"]})
            envelopes = [
                envelope for envelope in envelopes_repo.list_envelopes()
                if envelope.get("locked_account_id") in (None, account["id"])
            ]
        self.assertGreaterEqual(len(envelopes), 2)
        fixed, remainder = envelopes[0], envelopes[1]
        action = {
            "transaction_type": "expense",
            "split_remainder": {
                "transaction_type": "expense",
                "splits": [{"envelope_id": fixed["id"], "amount_cents": -2500, "amount_mode": "signed"}],
                "remainder_envelope_id": remainder["id"],
            },
        }
        candidate = _candidate(
            account["id"],
            fixed["id"],
            key="rule-proposal:test:advanced-split",
            action=action,
        )

        refresh_import_rule_proposals(
            account_id=account["id"],
            build_proposals_func=lambda **_: {"proposals": [candidate], "withheld": [], "source_notes": []},
        )
        proposal = import_rule_proposals_repo.list_import_rule_proposals(status="pending")[0]

        result = approve_import_rule_proposal(proposal["id"], enabled=False)

        self.assertTrue(result.ok, result.errors)
        rule = import_matching_rules_repo.get_import_matching_rule(result.rule_id)
        self.assertEqual(rule["enabled"], 0)
        self.assertEqual(rule["action_json"]["split_remainder"]["remainder_envelope_id"], remainder["id"])
        self.assertEqual(rule["action_json"]["split_remainder"]["splits"][0]["amount_cents"], -2500)

    def test_enabled_approval_route_uses_existing_rule_creation_path(self) -> None:
        self._select_user_in_client()
        account, envelope = self._account_and_envelope()
        stored, _ = import_rule_proposals_repo.upsert_import_rule_proposal({
            "fingerprint": "rule-proposal:test:route",
            "candidate_key": "rule-proposal:test:route",
            "account_id": account["id"],
            "condition_json": _candidate(account["id"], envelope["id"])["condition_json"],
            "action_json": _candidate(account["id"], envelope["id"])["action_json"],
            "suggested_rule_json": _candidate(account["id"], envelope["id"])["suggested_rule"],
            "evidence_json": _candidate(account["id"], envelope["id"])["evidence"],
            "reason_codes_json": ["rule_proposal_conservative_support_met"],
        })

        page = self.client.get("/imports/rule-proposals")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Import Rule Proposals", page.get_data(as_text=True))
        approved = self.client.post(
            f"/imports/rule-proposals/{stored['id']}/approve",
            data={"enabled": "1"},
            follow_redirects=False,
        )

        self.assertEqual(approved.status_code, 302)
        proposal = import_rule_proposals_repo.get_import_rule_proposal(stored["id"])
        rule = import_matching_rules_repo.get_import_matching_rule(proposal["approved_rule_id"])
        self.assertEqual(proposal["status"], "accepted")
        self.assertEqual(proposal["reviewer_decision"], "approved_enabled")
        self.assertEqual(rule["enabled"], 1)

    def test_invalid_stale_payload_fails_closed_and_records_validation_errors(self) -> None:
        account, envelope = self._account_and_envelope()
        invalid = _candidate(
            account["id"],
            envelope["id"],
            key="rule-proposal:test:invalid",
            action={"single_envelope_id": "not-an-int"},
        )
        refresh_import_rule_proposals(
            account_id=account["id"],
            build_proposals_func=lambda **_: {"proposals": [invalid], "withheld": [], "source_notes": []},
        )
        proposal = import_rule_proposals_repo.list_import_rule_proposals(status="pending")[0]
        before_rules = len(import_matching_rules_repo.list_import_matching_rules(include_disabled=True))

        result = approve_import_rule_proposal(proposal["id"], enabled=True)

        after_rules = len(import_matching_rules_repo.list_import_matching_rules(include_disabled=True))
        stored = import_rule_proposals_repo.get_import_rule_proposal(proposal["id"])
        self.assertFalse(result.ok)
        self.assertEqual(after_rules, before_rules)
        self.assertEqual(stored["status"], "pending")
        self.assertEqual(stored["reviewer_decision"], "approval_failed")
        self.assertTrue(stored["validation_errors_json"])

    def test_reject_and_ignore_keep_deduped_state_on_regeneration(self) -> None:
        account, envelope = self._account_and_envelope()
        rejected_candidate = _candidate(account["id"], envelope["id"], key="rule-proposal:test:reject")
        ignored_candidate = _candidate(account["id"], envelope["id"], key="rule-proposal:test:ignore")
        refresh_import_rule_proposals(
            account_id=account["id"],
            build_proposals_func=lambda **_: {
                "proposals": [rejected_candidate, ignored_candidate],
                "withheld": [],
                "source_notes": [],
            },
        )
        proposals = import_rule_proposals_repo.list_import_rule_proposals(status="pending")
        by_fingerprint = {item["fingerprint"]: item for item in proposals}

        self.assertTrue(reject_import_rule_proposal(by_fingerprint["rule-proposal:test:reject"]["id"]).ok)
        self.assertTrue(ignore_import_rule_proposal(by_fingerprint["rule-proposal:test:ignore"]["id"]).ok)
        refresh_import_rule_proposals(
            account_id=account["id"],
            build_proposals_func=lambda **_: {
                "proposals": [rejected_candidate, ignored_candidate],
                "withheld": [],
                "source_notes": [],
            },
        )

        all_proposals = import_rule_proposals_repo.list_import_rule_proposals(include_decided=True)
        by_fingerprint = {item["fingerprint"]: item for item in all_proposals}
        self.assertEqual(by_fingerprint["rule-proposal:test:reject"]["status"], "rejected")
        self.assertEqual(by_fingerprint["rule-proposal:test:ignore"]["status"], "ignored")
        self.assertEqual(len([item for item in all_proposals if item["fingerprint"] == "rule-proposal:test:reject"]), 1)
        self.assertEqual(len([item for item in all_proposals if item["fingerprint"] == "rule-proposal:test:ignore"]), 1)

    def test_decided_proposal_keeps_decision_when_later_refresh_marks_source_stale(self) -> None:
        account, envelope = self._account_and_envelope()
        candidate = _candidate(account["id"], envelope["id"], key="rule-proposal:test:decided-stale")
        refresh_import_rule_proposals(
            account_id=account["id"],
            build_proposals_func=lambda **_: {"proposals": [candidate], "withheld": [], "source_notes": []},
        )
        proposal = import_rule_proposals_repo.list_import_rule_proposals(status="pending")[0]
        self.assertTrue(reject_import_rule_proposal(proposal["id"]).ok)

        refresh_import_rule_proposals(
            account_id=account["id"],
            build_proposals_func=lambda **_: {"proposals": [], "withheld": [], "source_notes": []},
        )
        stale = import_rule_proposals_repo.get_import_rule_proposal(proposal["id"])

        self.assertEqual(stale["status"], "rejected")
        self.assertEqual(stale["reviewer_decision"], "rejected")
        self.assertEqual(stale["evidence_json"]["refresh_status"], "stale_source_changed")
        self.assertFalse(stale["validation_errors_json"])

    def test_review_page_shows_useful_evidence_reasons_and_stale_state(self) -> None:
        self._select_user_in_client()
        account, envelope = self._account_and_envelope()
        candidate = _candidate(account["id"], envelope["id"], key="rule-proposal:test:evidence")
        candidate["evidence"]["distinct_transactions"] = 3
        candidate["evidence"]["feedback_modified"] = 1
        candidate["evidence"]["feedback_rejected"] = 0
        candidate["evidence"]["sources"] = {"import_commit": 3}
        refresh_import_rule_proposals(
            account_id=account["id"],
            build_proposals_func=lambda **_: {"proposals": [candidate], "withheld": [], "source_notes": []},
        )
        refresh_import_rule_proposals(
            account_id=account["id"],
            build_proposals_func=lambda **_: {"proposals": [], "withheld": [], "source_notes": []},
        )

        page = self.client.get(f"/imports/rule-proposals?status=all&account_id={account['id']}")
        body = page.get_data(as_text=True)

        self.assertEqual(page.status_code, 200)
        self.assertIn("Support examples: 3", body)
        self.assertIn("distinct raw identities: 3", body)
        self.assertIn("Reasons: rule_proposal_conservative_support_met", body)
        self.assertIn("FIN092B COFFEE 1001", body)
        self.assertIn("stale source evidence", body)
        self.assertIn("Last known support examples: 3", body)


if __name__ == "__main__":
    unittest.main()
