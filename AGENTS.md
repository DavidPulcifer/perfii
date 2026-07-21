# Finance App Agent Guide

This file applies to the entire repository. It is written for coding agents that are new to the project. Read it before changing code, data, deployment, or packaging.

## Start Here

1. Read `docs/agent/README.md` for the system map and source-of-truth hierarchy.
2. Read `agent-config/project-manifest.json` for machine-readable entry points, customization surfaces, and checks.
3. Read `docs/agent/CUSTOMIZATION.md` and `docs/agent/PLAYBOOKS.md` before adapting the product for a person.
4. Read `docs/agent/DATA_OPERATIONS.md` before any operation involving an existing ledger.
5. Read `docs/agent/DEPLOYMENT.md` before changing hosting or packaging. The repository ships the web app; other targets are agent conversion assignments.
6. Read `docs/agent/THEMING.md` before changing colors or visual tokens.
7. Inspect the starting files and preserve unrelated user changes. If Git metadata is present, inspect `git status`; otherwise establish a reversible local checkpoint before editing. Never clean, reset, or overwrite owner work.
8. Treat `current/` as the application root. Do not flatten or relocate it; existing paths assume the repository root is one directory above it.

## Non-Negotiable Data Safety

- Never open, copy, migrate, seed, delete, upload, summarize, or test against a real financial database unless the owner explicitly identifies the exact disposable copy and authorizes that operation.
- Never ask a user to paste bank credentials, account numbers, statements, API keys, recovery codes, or real transaction exports into an agent conversation.
- Use only obviously synthetic people, institutions, accounts, balances, transactions, statements, and screenshots in development, tests, demos, logs, and documentation.
- Keep runtime data outside source files. SQLite databases, WAL/SHM files, uploads, backups, secrets, `.env` files, and packaged output must remain ignored.
- For tests, create all databases under a new temporary directory and enable the external-path tripwire (`TESTING=True` and `FORBID_EXTERNAL_TEST_DB_PATHS=True`). Do not satisfy missing fixtures by copying an owner's database.
- Before a migration or destructive operation, resolve and display the exact database paths, make a recoverable backup, and rehearse against synthetic data first.

## Architecture Boundaries

The app is Flask + Jinja + SQLite:

- `current/app/blueprints/`: HTTP methods, form parsing, validation messages, redirects, and view wiring.
- `current/app/services/`: business workflows, especially multi-table or multi-step operations.
- `current/app/repositories/`: direct SQL close to one domain/table shape.
- `current/app/db.py`: connection selection, per-user database routing, units of work, schema repair, and migrations.
- `current/app/templates/` and `current/app/static/`: presentation only; never duplicate financial persistence rules in JavaScript.
- `current/tests/`: unit and request-flow regression coverage.

Keep SQL out of blueprints and templates. A write touching multiple rows or concepts belongs in a service and must use `unit_of_work()` so it commits or rolls back as one operation. Repositories should accept the active connection rather than open an unrelated database.

## Financial Invariants

- Store and calculate money as integer cents. Parse decimal input at the boundary; do not use binary floating point for persisted or consequential amounts.
- An expense is negative, income is positive, `transfer_out` is negative, and `transfer_in` is positive.
- A transfer is a linked pair of equal-and-opposite legs. Create or edit both legs and their splits atomically; never insert one leg directly from a route.
- Non-zero transaction splits must sum exactly to the signed parent amount unless an existing service explicitly supports an unallocated case.
- Envelopes can be locked to an account. Validate account/envelope compatibility before writing.
- The selected user is resolved through `meta.sqlite`; their ledger lives in a separate database. Never assume one global ledger or cache one user's connection/data for another user.
- Preserve reconciliation protections. Do not silently mutate reconciled transaction amounts, dates, accounts, pairs, or splits.
- Import commit, provenance, validation, undo, matching, and learning are coordinated systems. Extend their existing services rather than bypassing them.
- Savings-plan previews and other calculators must be side-effect free. Writes require an explicit review/confirmation action.

## Schema Changes

- Add a dated, uniquely named, idempotent migration to `SCHEMA_MIGRATIONS` in `current/app/db.py`.
- A migration with unmet prerequisites must leave the database unchanged and must not be recorded as applied.
- Never rewrite an already released migration to change its meaning.
- Exercise first-run, upgrade, repeat-run, and failure/rollback paths using disposable databases.
- `current/app/base_schema.sql` is the tracked canonical bootstrap. Preserve the create-only/refusal behavior in `current/scripts/bootstrap_workspace.py`; never restore a dependency on a personal or ignored template database.

## Change Workflow

1. Record the starting revision or reversible checkpoint, worktree state, and relevant baseline checks.
2. Restate the requested outcome and identify the affected blueprint, service, repositories, schema, templates, and tests.
3. Record assumptions that would change financial behavior. Ask for a decision when an assumption is consequential.
4. Write acceptance scenarios using synthetic values.
5. Implement the smallest vertical slice while preserving the boundaries and invariants above.
6. Add focused service tests plus a request-flow test when HTTP behavior changes.
7. Run targeted tests first, then the agent preflight and all relevant checks. Report existing fixture/setup failures separately from regressions.
8. Review the diff for personal data, absolute owner-specific paths, credentials, generated databases, and unsupported deployment claims.
9. Hand back what changed, what was verified, what remains unverified, and how to recover or roll back.

## Customization Rules

- Start from the questionnaire in `docs/agent/CUSTOMIZATION.md`. For a theme-only or display-name-only request that does not change behavior, data, hosting, identity, authentication, or localization, use the documented lightweight brief. For all consequential work, start from the neutral `agent-config/customization-profile.template.json`. Use the named Pay Yourself First profile only as a worked example.
- Validate a consequential completed profile with `python tools/validate_agent_config.py --profile agent-config/customization-profile.local.json --require-ready --json` before implementation.
- Separate terminology, appearance, workflow, hosting, and data-model changes. A color request should not trigger a financial schema rewrite.
- Use `tools/customize_theme.py` for supported palette/font/density/radius changes; do not perform a broad search-and-replace of hex colors.
- Use `APP_DISPLAY_NAME` for visible product branding. Stable storage keys, event names, service identifiers, and data-directory slugs are not branding tokens and require migration planning if changed.
- Make deployment instructions target-specific and label evidence honestly: verified, implemented but unverified, experimental, or unsupported.
- This project is optimized for coding-agent-assisted customization; it does not promise automatic, one-click, or arbitrary customization.
- No runtime OpenAI API, ChatGPT integration, or API key is required by the product.

## Useful Checks

Run commands from the Git root unless a document says otherwise:

```powershell
python tools/agent_preflight.py --quick
python tools/validate_agent_config.py --json
python tools/customize_theme.py --check
```

Use `python tools/agent_preflight.py --full` for the complete synthetic bootstrap, doctor, launcher, and regression verification. `README.md` contains the brief maintainer-only check for publishing a reviewed GitHub commit or tag.

Clean workspace and doctor checks run from the application root and use only generated fictional data:

```powershell
Set-Location current
python scripts/bootstrap_workspace.py --data-dir .local/agent-demo --profile demo
python scripts/doctor.py --data-dir .local/agent-demo
python scripts/run_local.py --data-dir .local/agent-demo --check-only
python -m unittest discover -s tests
```

The bootstrap refuses an existing destination. Reuse the same path only for diagnostic doctor or launch checks; choose a new ignored path for another bootstrap. Doctor's direct SQLite checks are read-only, but its default Flask smoke check may apply idempotent schema or metadata repair. Use `--no-smoke` when a strictly read-only inspection is required. The test harness generates isolated synthetic databases. Never reintroduce snapshot or personal databases to satisfy a test.
