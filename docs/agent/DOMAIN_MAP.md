# Domain and Test Map

This is a routing map, not a substitute for reading the code. Start with the named service and its tests, then follow imports and callers. Search for a current analogous path before adding a new abstraction. A file listed under “supporting code” may serve several domains.

## Cross-Cutting Owners

| Concern | Canonical starting point | Evidence |
| --- | --- | --- |
| App creation and request-wide user gate | `current/app/__init__.py` | `test_app_smoke.py`, `test_fin034_auth.py` |
| Environment, visible display name, and runtime paths | `current/app/config.py`; validation in `current/app/__init__.py` | `test_app_smoke.py`, `test_workspace_bootstrap.py`, `test_test_harness_isolation.py` |
| Selected-user database routing, units of work, schema migrations | `current/app/db.py` | `test_db_schema.py`, `test_test_harness_isolation.py` |
| Money parsing and Jinja helpers | `current/app/utils.py` | affected request/service tests |
| Canonical first-run schema | `current/app/base_schema.sql` | `test_db_schema.py`, `test_workspace_bootstrap.py` |
| Synthetic test app and database tripwire | `current/tests/helpers.py` | `test_test_harness_isolation.py` |

## Feature Domains

| Domain | HTTP/UI owner | Business owner | Data owner | Primary tests |
| --- | --- | --- | --- | --- |
| Dashboard and aggregate balances | `blueprints/core.py`; `templates/index.html`; `_dashboard_balance_panels.html` | dashboard model helpers in `core.py`; credit-capacity calculation in `services/credit_availability_service.py` | `repositories/aggregates_repo.py`, accounts/envelopes repositories | `test_dashboard_unallocated.py`, `test_app_smoke.py` |
| Accounts and account types | `blueprints/accounts.py`; `templates/accounts.html`, `account.html`, `account_edit.html` | account creation/edit coordination in the blueprint and existing helpers; type-specific services below | `repositories/accounts_repo.py`, plus credit/loan/invest repositories | `test_accounts.py`, `test_phase1_user_flows.py`, `test_phase1_audit_existing_behaviors.py` |
| Envelopes and account locks | `blueprints/envelopes.py`; `templates/envelopes.html`, `envelope_detail.html`, `envelope_edit.html`; shared envelope selectors | existing envelope validation in transaction/import services | `repositories/envelopes_repo.py`, `repositories/splits_repo.py`, `repositories/aggregates_repo.py` | `test_envelopes.py`, `test_envelope_archiving.py`, `test_transactions_service.py` |
| Transactions, splits, and transfers | `blueprints/transactions.py`; `templates/transactions.html`, `transaction_edit.html`, `transfer_edit.html`; shared transaction/envelope partials | `services/transactions_service.py` | `repositories/transactions_repo.py`, `splits_repo.py`, `remainder_intents_repo.py`; learning repositories for edit feedback | `test_transactions_service.py`, `test_phase1_audit_existing_behaviors.py`, `test_phase1_user_flows.py`, `test_phase2_transaction_list.py` |
| Reconciliation | `blueprints/reconciliation.py`; `templates/reconciliation_*.html` | `services/reconciliation_service.py` | `repositories/reconciliation_repo.py`; transaction mutations still go through transaction service | `test_reconciliation_service.py`, `test_reconciliation_ui.py` |
| Payday savings | `blueprints/savings.py`; `templates/savings_payday.html` | `services/savings_planner_service.py`; transfer recording delegates to transaction service | `repositories/savings_repo.py`, plus accounts/envelopes/transaction repositories | `test_savings_planner.py`, `test_workspace_bootstrap.py` |
| Statement import and review | registered `blueprints/imports.py` plus `imports_api.py`, `imports_commit.py`, `imports_drafts.py`, `imports_review.py`; `templates/import*.html`; `static/import_review.js` | `services/imports_service.py`, `import_draft_service.py`, `import_commit_service.py`, `import_undo_service.py` | import review/source/provenance/validation repositories and transaction repositories | `test_imports_service.py`, `test_import_commit_service.py`, `test_import_review_drafts.py`, `test_import_review_sources.py`, `test_import_provenance_repo.py`, `test_import_validation_repo.py`, `test_import_undo_service.py`, `test_app_smoke.py` |
| Import suggestions, matching, and learning | import review/rule routes in `blueprints/imports.py` and related API modules | `services/import_prefill_service.py`, `import_matching_rule_service.py`, `import_rule_proposal_service.py`, `transaction_learning_service.py`, `transaction_text_profile_service.py`, `payee_normalization_service.py`, `text_similarity_service.py`, `account_match_profile_service.py` | matching-rule, proposal, prefill, learning, normalization, validation, and provenance repositories | corresponding `test_*_service.py` files; `test_import_rule_proposal_review.py`; `test_transaction_learning_backfill_script.py` |
| Users and authentication | `blueprints/users.py`; `templates/users*.html`; request gate in `app/__init__.py` | `app/auth.py`; user/admin flow in the blueprint | `meta.sqlite` access through `app/db.py`; each registry row points to a separate ledger | `test_fin034_auth.py`, `test_app_smoke.py`, `test_test_harness_isolation.py` |
| Credit cards | `blueprints/credit.py`; `templates/credit_card.html` | `services/credit_availability_service.py`; payments/transfers use transaction service | `repositories/credit_repo.py`, accounts/transactions/splits repositories | `test_credit_availability_service.py`, `test_credit_card_running_balance.py`, account and phase-one audit tests |
| Loans | `blueprints/loans.py`; `templates/loan.html` | loan payment coordination in the blueprint and transaction service | `repositories/loans_repo.py`, accounts/transactions/splits repositories | `test_loan_balances.py`, `test_accounts.py`, `test_phase1_audit_existing_behaviors.py` |
| Investments | `blueprints/invest.py`; `templates/invest.html` | valuation/note workflows in the blueprint | `repositories/invest_repo.py`, accounts/transactions repositories | `test_invest_graph.py`, `test_invest_notes.py`, `test_invest_valuation_edit.py`, account audit tests |
| Theme and shared browser behavior | `templates/layout.html`, shared partials, `static/style.css`, `static/theme.generated.css`, small focused JavaScript files | presentation only; financial decisions remain server-side | no financial persistence | `test_app_smoke.py`, `test_checkbox_range.py`, plus affected request tests |

Application paths in the first three columns are relative to `current/app/` unless they begin with `current/`. Test filenames in the final column live under `current/tests/`.

## Import Pipeline Trace

Import changes frequently cross more than one row of the table. Trace the pipeline in this order:

```text
upload and account detection
  -> parser/normalization
  -> source identity and draft persistence
  -> review rows and suggestion evidence
  -> server-side validation and commit plan
  -> transaction/transfer service writes
  -> provenance, validation, and learning feedback
  -> undo eligibility and reversal
```

Do not treat the browser's review-row state as the authoritative commit plan. Preserve source tokens and row identity so a stale, forged, or mismatched review fails closed.

## Savings Workflow Trace

```text
saved plan/rules
  -> blueprint loads selected-user balances
  -> savings service validates and calculates a write-free preview
  -> signed preview binds user, ledger, configuration, and recommendations
  -> explicit record request revalidates the preview
  -> transaction service creates paired transfers atomically
  -> savings repository records idempotent completion
```

Boundary tests belong in `test_savings_planner.py`: below target, exactly at target, crossing target, cents rounding, total percentages, stale configuration, cross-user/ledger tokens, transfer rollback, and duplicate submission.

## Choosing Tests

For a typical change, run tests in this order:

1. pure service or repository test for the rule being changed;
2. domain request-flow test for validation and rendering;
3. cross-domain invariant tests when transactions, imports, reconciliation, authentication, or migrations are involved;
4. the complete suite.

Examples:

- A label-only savings change still runs `test_savings_planner.py` because that file contains the page contract.
- A transfer change runs `test_transactions_service.py`, relevant phase-one/phase-two flow tests, reconciliation mutation tests, and savings/import tests that create transfers.
- An import suggestion change runs its focused service test plus commit, provenance, validation, undo, and request-flow tests if the suggested payload shape changed.
- A schema change runs `test_db_schema.py`, `test_workspace_bootstrap.py`, the affected domain, and the full suite.
- A user-routing change runs authentication, test isolation, and at least one two-user domain flow.

Do not weaken or delete an unrelated assertion merely to make a customization pass. If a test expresses an intentionally changed user requirement, update the requirement, implementation, and focused assertion together and explain the behavior change in the completion report.
