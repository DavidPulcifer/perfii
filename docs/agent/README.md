# Coding-Agent Orientation

This repository is designed to help a coding agent adapt the Finance App for one person's needs without first reverse-engineering every boundary. `AGENTS.md` is the controlling safety guide. This document explains what to read next, where behavior belongs, and how to establish a safe baseline before changing anything.

## How This Guide Fits

The root `README.md` is the human entry point and `AGENTS.md` is the coding-agent entry point. `AGENTS.md` controls the reading sequence, safety rules, and change workflow. This orientation is the system map; use the topic guides it names only when the requested change touches those areas.

## What Ships

The application is a server-rendered Flask web app intended to run for a trusted user or household. The exercised path is the local web app on loopback. The repository also explains how an agent can adapt the web app for a desktop shell, a private server, or a private cloud VM; those are conversion assignments for the agent, not prebuilt deliverables in the downloaded source.

The product supports users, accounts, account-linked envelopes, income/expense/transfer transactions, reconciliation, statement imports, loans, credit cards, investments, and a percentage-based payday savings workflow. It has no runtime OpenAI or other coding-agent dependency.

The application code is intentionally nested under `current/`. Do not flatten or relocate it.

```text
repository root
|-- AGENTS.md
|-- agent-config/                 synthetic requirements and theme profiles
|-- docs/agent/                   agent-oriented project guidance
|-- tools/                        bounded customization and verification helpers
`-- current/                      Flask application root
    |-- app/
    |   |-- blueprints/           HTTP boundary
    |   |-- services/             workflow/business boundary
    |   |-- repositories/         SQL boundary
    |   |-- templates/            Jinja views
    |   |-- static/               CSS and browser JavaScript
    |   |-- config.py             environment-derived settings
    |   `-- db.py                 connections, schema, migrations, UoW
    |-- scripts/                  bootstrap, doctor, backup/restore, and local launcher
    |-- tests/                    generated-fixture regression suite
    |-- desktop_app.py            starting point for a desktop conversion
    `-- wsgi.py                   private-server WSGI conversion entry point
```

## Sources of Truth

Use this order when files disagree:

1. `AGENTS.md` for safety, financial invariants, and change rules.
2. The current service/repository implementation and its focused tests for behavior.
3. `current/app/base_schema.sql` plus the dated migrations in `current/app/db.py` for schema behavior.
4. Current files in `docs/agent/` for customization and hosting guidance.
5. `agent-config/project-manifest.json` as a machine-readable index, verified against the paths above.

Search for an analogous current feature and its tests before introducing a new pattern. Files explicitly described as historical—including owner-specific deployment notes and old packaging commands—are context, not copy-ready instructions. Never revive a pattern that bundles, copies, or tests against a repository database. When an old example conflicts with the current schema-only bootstrap or data rules, follow the current bootstrap and this guide.

## Request and Data Flow

```text
browser form/request
  -> blueprint parses and rejects malformed input
  -> service enforces the financial workflow
  -> repository performs domain SQL with the supplied connection
  -> unit_of_work commits every related row or rolls everything back
  -> blueprint renders or redirects with a user-facing result
```

Do not skip a layer merely because a route can execute SQL directly. These boundaries make financial behavior testable and make the blast radius legible to the next agent.

## Database Topology

There are two database roles:

1. `meta.sqlite` contains the user registry, roles, password metadata, and each user's ledger path.
2. Each selected user has a separate SQLite ledger containing that user's financial records.

`current/app/db.py:get_db()` resolves the selected user's ledger from the session and registry. The configured `DB_PATH` is a compatibility fallback, not proof that the app has only one ledger. Connections enable foreign keys and are request-scoped. SQLite WAL and busy-timeout behavior is environment-configurable.

Never build a feature that reads all ledger paths, shares repository results across users, or writes to `DB_PATH` without considering selected-user routing. The repository contains code and schema only; runtime databases, uploads, local profiles, backups, logs, and secrets are not source assets.

## High-Risk Workflows

### Transactions and transfers

Use `current/app/services/transactions_service.py`. It normalizes money/signs and splits, creates linked transfer legs, protects reconciled records, and wraps coordinated writes in `unit_of_work()`. Direct route-level inserts can create one-sided transfers or envelope totals that no longer match the transaction.

### Imports

Import parsing, review drafts, matching, provenance, validation, commit, learning, and undo are coordinated but separate systems. Trace the entire path in `docs/agent/DOMAIN_MAP.md` before changing a row field, action, or status. A change on the upload screen can affect commit and undo later.

### Envelopes and reconciliation

An envelope may be locked to an account. UI filtering is convenience only; the server must validate compatibility. Reconciled transactions are historical evidence. Reuse existing mutation guards and reconciliation services rather than bypassing them for a bulk-edit workflow.

### Savings planning

Savings preview is side-effect free. For a rule with a long-term destination, routing compares the accessible account-and-envelope balance at the start of the preview with the target:

- below target: 100% of that rule's contribution goes to accessible savings, even if the contribution reaches or crosses the target;
- at or above target: 100% goes to the long-term destination;
- the contribution is never split across the two destinations; a crossing contribution changes routing on the next preview.

Recording requires explicit review and confirmation and uses the established transaction service so transfer pairs, split totals, account locks, selected-user binding, and idempotency remain intact.

## Baseline and Checkpoint Workflow

Before implementation:

1. Inspect the starting files and preserve every unrelated change. If Git metadata is present, inspect `git status` and record the starting commit; otherwise record the downloaded version if known and create a reversible local checkpoint before editing.
2. Convert the user's request into bounded change classes and synthetic acceptance scenarios.
3. Create a new fictional workspace under an ignored path; never point baseline checks at an existing workspace.
4. Run the focused tests for the affected domain, then record whether the full suite passes before the change.
5. Record consequential assumptions and obtain confirmation before changing financial meaning, authentication, data location, or network exposure.

During implementation, complete one vertical slice at a time. After each slice, run focused tests, inspect the diff for private data and path leakage, and create a Git checkpoint only when the user has authorized commits. Without commit authority, report the clean patch boundary instead. At completion, repeat the focused and full checks and distinguish new regressions from baseline failures.

## Dependency Setup

The verified development environment is Windows with Python 3.13. From the repository root, create an isolated environment and install the pinned application dependencies before running the baseline:

```powershell
Set-Location current
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Then use `.\.venv\Scripts\python.exe` in place of `python` for application commands in this guide. The dependency source of truth is `current/requirements.txt`; do not infer packages from imports or install an unreviewed global environment. On a non-Windows system, create a normal Python 3.13 virtual environment and use its Python executable, but treat that platform as newly exercised evidence rather than claiming the Windows verification applies to it.

## Safe Synthetic Baseline

From `current/`, create a disposable demonstration workspace at a new path:

```powershell
python scripts/bootstrap_workspace.py --data-dir .local/agent-baseline --profile demo
python scripts/doctor.py --data-dir .local/agent-baseline
python scripts/run_local.py --data-dir .local/agent-baseline --check-only
```

The bootstrap is create-only and refuses an existing destination. Pick another ignored path for the next run. The doctor directly inspects SQLite read-only; its default Flask smoke may apply idempotent schema or metadata repair inside that managed workspace. Add `--no-smoke` when inspection must be strictly read-only.

The test harness is safer than a copied sample database: `current/tests/helpers.py` creates a fresh temporary fictional workspace for each test case, enables `TESTING=True`, and enables the external-database tripwire. Run it from `current/`:

```powershell
python -m unittest discover -s tests
```

Never make a failing test pass by copying an owner's database, snapshot, export, or upload. See `docs/agent/DATA_OPERATIONS.md` for the data boundary.

## How to Add a Vertical Slice

1. Add a dated, idempotent migration if persistent state is required.
2. Put close-to-table SQL in one or more repositories.
3. Put calculations and coordinated writes in a service with no dependency on form objects.
4. Add a blueprint route that parses input and calls the service.
5. Render with a focused template and lightweight browser behavior.
6. Register a new blueprint in `current/app/__init__.py` when needed.
7. Test pure calculations, database rollback/invariants, authorization/user isolation, and the main request flow.

## Runtime Configuration

`current/app/config.py` reads environment variables directly; `.env.example` is a template and is not automatically loaded. Important path settings are `APP_DATA_DIR`, `DB_PATH`, `META_DB_PATH`, `USER_DB_DIR`, and `UPLOAD_DIR`.

For local work, use the create-only bootstrap and `current/scripts/run_local.py` so every runtime path points at an explicit managed workspace. Do not rely on the OS-default data directory, because it may contain a user's normal application data. Production mode intentionally requires a non-empty secret and a loopback host.

## Verification Snapshot: 2026-07-20

- `current/app/base_schema.sql` is the tracked canonical base schema. `current/scripts/bootstrap_workspace.py` creates a new schema-only or fictional-demo workspace and refuses an existing destination.
- `current/scripts/doctor.py` checks the workspace marker, managed ledger paths, SQLite integrity, foreign keys, essential schema, profile sanity, ledger invariants, and users/dashboard/savings HTTP responses.
- Direct schema/demo bootstrap and doctor checks passed on Windows with Python 3.13. A disposable local server was exercised in desktop and 390-pixel browser views through user selection, dashboard, savings preview, transfer recording, and transaction verification.
- `current/scripts/workspace_backup.py` passed a fictional managed-workspace round trip with online SQLite snapshots, uploads, internal ledger rehoming, tamper detection, and create-only refusal checks.
- The test harness creates an isolated fictional `test` profile. The complete generated-fixture regression suite passed 637 tests on Windows/Python 3.13. Repeat the full preflight and repository-safety checks from the final public repository and a fresh download before relying on a release.

This snapshot describes recorded evidence, not a permanent guarantee. Update it only after repeating the named verification.

## Orientation Check

Before editing, the agent should be able to answer:

- Which selected-user ledger will this request open?
- Which layer owns the behavior, and which current feature is the canonical example?
- Which money signs, split totals, locks, reconciliation rules, or import evidence must be preserved?
- Is every preview genuinely write-free?
- Which test proves rollback and user isolation?
- Does every test, screenshot, and log use only synthetic data?
- Is the request changing the visible display name, the stable application identity, or both?
- Is the hosting request using the shipped web path or asking the agent to perform a conversion?

If any answer is unclear, continue reading the domain code and tests before modifying it.
