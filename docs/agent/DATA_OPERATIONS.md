# Data Operations and Privacy Boundary

This guide defines what a coding agent may use while customizing the project. The repository provides a narrowly scoped, create-only backup and restore tool for managed workspaces; it does not authorize an agent to run that tool on real financial data. It does not provide a tool for inspecting, sanitizing, migrating, or repairing a ledger. Repository access and a customization request do not authorize work on an owner's ledger.

## Default Boundary

Use only:

- tracked source code and the tracked schema;
- generated `schema`, `demo`, or test workspaces created by the repository bootstrap/test harness;
- obviously fictional import files written for a focused test;
- synthetic screenshots and logs.

Do not open, query, summarize, copy, rename, move, upload, archive, delete, repair, migrate, or use as a fixture any existing SQLite file, WAL/SHM file, statement, export, upload, backup, screenshot, or log that may contain personal financial information. File visibility is not consent.

If real-data work is ever requested, stop and obtain a separate, exact operation plan and explicit authorization for an identified disposable copy. Rehearse the operation against synthetic data first. Do not turn a project helper into a generic real-ledger migration tool as part of ordinary customization.

## Data Classes

| Class | Examples | Agent treatment |
| --- | --- | --- |
| Source | Python, templates, CSS, tests, `base_schema.sql`, synthetic profile examples | Read and modify within the requested scope |
| Generated synthetic | new `.local/...` demo workspace, temporary test databases, fictional CSV/QFX | May create and inspect; keep ignored and disposable |
| Personal configuration | completed local customization/theme profiles, local paths, hostnames | Keep ignored; include only a necessary non-sensitive summary in the completion report |
| Potentially real financial data | any pre-existing database, upload, statement, backup, WAL/SHM, screenshot, log | Do not inspect or operate on it without separate explicit authorization |
| Secrets and credentials | `.env`, passwords, tokens, keys, recovery codes | Never request, display, commit, package, or copy |

When classification is uncertain, treat the item as personal financial data and leave it untouched.

## Repository Data Boundary

The downloaded repository should contain the application source, tracked schema, tests, agent documentation, and synthetic examples. It must not contain:

- SQLite databases or WAL/SHM companions;
- uploads, imports, backups, or exports;
- `.env` files, local profiles, credentials, or recovery material;
- logs, screenshots, caches, build artifacts, or owner-specific output;
- absolute paths or metadata that identify the owner's environment unnecessarily.

A schema file defines empty structure; a database file is runtime data and is never a substitute for that schema. The root `README.md` gives maintainers the short safety check to run before publishing a reviewed GitHub commit or tag.

## Safe Development Workspaces

For a browser demonstration, create a new managed fictional workspace from `current/`:

```powershell
python scripts/bootstrap_workspace.py --data-dir .local/agent-demo-01 --profile demo
python scripts/doctor.py --data-dir .local/agent-demo-01
python scripts/run_local.py --data-dir .local/agent-demo-01 --check-only
```

Properties of this workflow:

- bootstrap is create-only and refuses every existing destination;
- `demo` uses fixed institution-neutral fictional records;
- `.local/` is ignored;
- the marker identifies the workspace and its synthetic profile;
- the doctor rejects registered ledger paths that escape the managed directory;
- direct doctor SQLite checks are read-only;
- the default Flask smoke may apply idempotent schema or metadata repair only inside the managed workspace; `--no-smoke` disables that startup path.

Do not reuse the path for a new baseline. Pick a new ignored directory so a prior run cannot influence the result.

## Managed Workspace Backup and Restore

`current/scripts/workspace_backup.py` makes a portable **data-workspace directory backup**. It does not build, package, or copy the source repository. Run it from `current/` only after the owner has identified the exact workspace and destination. For a fictional demo rehearsal:

```powershell
python scripts/workspace_backup.py backup `
  --data-dir .local/agent-demo-01 `
  --backup-dir .local/backups/agent-demo-01-backup

python scripts/workspace_backup.py restore `
  --backup-dir .local/backups/agent-demo-01-backup `
  --data-dir .local/agent-demo-01-restored

python scripts/doctor.py --data-dir .local/agent-demo-01-restored
```

Both operations are create-only. The backup destination must not exist and must be outside the source workspace. The restore destination must not exist and must be outside the backup. Neither command replaces, merges into, or deletes an existing directory. Use `--allow-external` only after verifying every resolved source and destination path when an owner explicitly requests an external location.

Safety properties:

- the source must have a valid managed-workspace marker;
- every ledger registered in `meta.sqlite`, plus the default ledger, must resolve inside that workspace;
- SQLite databases are snapshotted with SQLite's online backup API instead of copying a live database or its WAL/SHM files;
- SQLite integrity and foreign keys are checked for every snapshot and restore;
- the backup manifest records internal ledger mappings and file hashes without recording the source workspace path;
- ordinary upload files and directories are included, while links, special files, and unregistered SQLite-like files under `uploads/` are refused;
- restore accepts only manifest-listed, boundary-checked files and rewrites registered ledger paths to the new managed workspace;
- restored fictional `demo` and `test` profiles must pass the workspace doctor, including its application smoke path, before the destination becomes visible.

A completed backup is still sensitive financial data. Keep it in an ignored, owner-controlled location; do not commit, upload, attach, inspect, or summarize it. The utility produces a directory so the owner can choose a separate encrypted storage or archive method appropriate to their environment.

## Safe Automated Tests

Run tests from `current/`:

```powershell
python -m unittest discover -s tests
```

`current/tests/helpers.py` creates a temporary fictional workspace for each `FinanceAppTestCase`. Its configuration enables `TESTING=True`, points every runtime directory into that temporary root, and enables `FORBID_EXTERNAL_TEST_DB_PATHS=True`. `test_test_harness_isolation.py` verifies the tripwire.

New database-backed tests should inherit that harness or reproduce all of those safeguards in a new temporary directory. Pure unit tests should use in-memory values or a temporary database with an explicit schema. Never:

- change test configuration to point at an ordinary application directory;
- use the OS-default data directory;
- fall back to a repository or owner database when a fixture is missing;
- disable the external-path tripwire to make a test pass;
- include plausible real institutions, account suffixes, payees, balances, or statements in fixtures.

## Synthetic Import Fixtures

A focused import fixture should contain only the minimum rows needed to prove the behavior. Use names such as “Example Credit Union,” “Neighborhood Market,” and “Demo Employer”; invented account suffixes; deterministic dates; and modest round amounts. Do not derive a fixture by redacting a real statement because identifiers and spending patterns can remain.

Cover malformed and ambiguous inputs intentionally. Keep source identity, row fingerprints, FITIDs, and duplicate cases synthetic but structurally realistic. Store a fixture in source only when it is clearly fictional and useful to future regression tests; otherwise generate it in the temporary test directory.

## Schema and Migration Work

For a schema change:

1. Describe the old and new structure without inspecting an owner ledger.
2. Create the smallest synthetic pre-change database that exercises the migration prerequisites.
3. Test first run from `base_schema.sql`, upgrade, repeat run, and failure/rollback.
4. Run foreign-key and domain-invariant checks.
5. Do not mark a migration applied when prerequisites are missing.

A successful synthetic rehearsal is evidence about the code path, not authorization to run it on personal data. Backup, restore, and migration execution against an owner's workspace remain owner-controlled operational steps unless separately authorized for exact paths.

## Logs, Errors, and Screenshots

Keep diagnostic output data-minimal:

- log record counts and synthetic IDs instead of payees, memos, balances, or paths;
- do not paste database rows or request bodies into an agent conversation;
- redact secrets and private paths from tracebacks before including them in a report;
- use the fictional demo for screenshots and video;
- inspect generated artifacts for thumbnails, metadata, filenames, and embedded local URLs.

When adding an error message, tell the user what action failed without echoing a statement row, password, reset token, or full file path.

## Stop Conditions

Stop before proceeding when:

- a command resolves to an existing database or data directory;
- a registered ledger path escapes the intended synthetic workspace;
- a missing test fixture appears to require an owner snapshot;
- a deployment step proposes bundling or copying a database;
- a user asks to paste credentials or financial exports into the conversation;
- the requested operation could overwrite, delete, or irreversibly transform data without an exact backup and rollback plan.

Report the resolved path and the needed decision without opening the data.

## Completion Evidence

At completion, state:

- which synthetic profile and paths were used;
- that no existing financial database or export was inspected;
- which bootstrap, doctor, tests, and privacy scans ran;
- whether any operation was write-capable and, if so, that it targeted only a new managed synthetic workspace;
- any owner-run backup, restore, or migration steps that remain outside the coding task.
