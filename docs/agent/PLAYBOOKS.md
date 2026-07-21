# Customization Playbooks

Use only the playbooks that match the user's request. Each playbook begins with discovery, identifies the likely change surface, and ends with evidence. Keep appearance, wording, workflow, financial-model, data, authentication, and hosting changes as separate plan items even when the user requests several at once.

Before any playbook, complete the baseline in `docs/agent/README.md`, write synthetic acceptance scenarios, and consult `docs/agent/DOMAIN_MAP.md`. Do not use real financial data to make a customization feel realistic.

## Appearance

Use for colors, fonts, spacing density, and corner radius. Use `docs/agent/THEMING.md` for the supported token workflow.

Ask:

- What visual qualities should the app convey?
- Light, dark, or both? Comfortable or compact?
- What primary/accent color and font character are preferred?
- What contrast, vision, motion, or readability needs apply?

Implementation boundary:

- Start with an ignored theme profile and `tools/customize_theme.py`.
- Change `current/app/static/theme.generated.css` only through the helper.
- Treat layout, navigation, icons, logos, charts, and new components as a separate UI change.
- Do not mix palette work with financial code or broad CSS search-and-replace.

Evidence:

- run theme validation before and after applying;
- inspect both color modes, keyboard focus, forms, tables, alerts, modals, charts, savings screens, and a narrow viewport;
- capture only synthetic screenshots.

## Branding and Terminology

Use for the visible product name, labels such as “envelope,” and user-facing workflow language.

Ask:

- What display name should people see?
- Which terms should change, and what should each new term mean?
- Are changes global, or limited to one workflow or household vocabulary?
- Must existing stored names or records change, or only interface wording?

Separate two concepts:

- **Display name:** visible title and navigation label. Use the validated `APP_DISPLAY_NAME` setting in `current/app/config.py`; templates read it from application configuration.
- **Stable application identity:** data-directory names, environment variables, service/package names, local-storage keys, JavaScript event names, database identifiers, and other machine-facing slugs. Keep these stable unless the user explicitly requests a migration and accepts the compatibility impact.

Set a deployment-specific name through `APP_DISPLAY_NAME`; change its default only when the customized source should carry that name everywhere. Do not run a repository-wide replacement for “Perfii,” `fitft`, “envelope,” or another existing term. First inventory each occurrence and classify it as visible copy, persistent data, URL/API shape, storage key, test fixture, or historical documentation. Changing a label does not change the underlying financial concept. If the user wants categories to behave differently, use the financial-model playbook instead.

Evidence:

- test page titles, navigation, headings, form labels, validation messages, accessibility names, and empty states;
- verify stable URLs, storage keys, data paths, and persisted identifiers did not change unintentionally;
- search for obsolete visible wording after the change and review every remaining match rather than replacing it blindly.

## Personal Financial Workflow

Use for a new or changed sequence such as paycheck allocation, bill review, or a guided transaction workflow.

Capture this brief before implementation:

- trigger and user goal;
- inputs and deterministic rules;
- assumptions that change financial meaning;
- review point before writes;
- exact transactions, transfers, splits, settings, or evidence written;
- cancellation, correction, retry, and duplicate-submission behavior;
- synthetic success and failure scenarios.

Implementation boundary:

1. Blueprint parses the request and presents validation.
2. Service owns calculations, decisions, and coordinated writes.
3. Repositories own direct domain SQL and accept the active connection.
4. A multi-row or multi-concept write uses `unit_of_work()`.
5. Templates display results; browser JavaScript may improve interaction but must not become the only financial validator.

The savings planner is the canonical reviewed-workflow example. Its preview is deterministic and write-free, and recording uses an explicit confirmation plus the transaction service. Its hard-cutoff rule is exact: when the opening accessible account-and-envelope balance is below target, the entire contribution remains accessible—even when it reaches or crosses the target. When the opening balance is at or above target, the entire contribution goes long-term. The crossing contribution is never divided; routing switches on the next preview.

Evidence:

- pure calculation tests for boundary values and cents rounding;
- service tests for validation, atomic rollback, retry/idempotency, and selected-user binding;
- request-flow tests for preview, confirmation, stale input, and errors;
- a synthetic browser exercise when the interaction is consequential.

## Financial Model or Schema

Use when the request adds a persisted concept, changes financial meaning, or changes how balances are calculated.

Ask for explicit confirmation of:

- the accounting meaning and sign of every amount;
- the relationship to accounts, envelopes, transfers, users, reconciliation, and imports;
- first-run versus existing-ledger behavior;
- deletion, archival, undo, and migration rollback expectations.

Implementation boundary:

- store consequential money as integer cents;
- add a uniquely named, dated, idempotent migration to `SCHEMA_MIGRATIONS` in `current/app/db.py`;
- update `current/app/base_schema.sql` when new workspaces need the structure;
- leave an unmet migration unrecorded and the database unchanged;
- put table-local SQL in a repository and coordinated behavior in a service;
- preserve transfer pairs, exact split totals, account locks, reconciliation protection, selected-user isolation, and import evidence.

Use current migrations and their tests as canonical examples. Do not copy SQL from an owner-specific database, old snapshot, or ad hoc repair script.

Evidence:

- schema-only first run;
- upgrade from a minimal synthetic pre-change schema;
- repeat run/idempotency;
- prerequisite failure and rollback;
- foreign-key check and affected-domain tests;
- two-user isolation when user-owned records are involved.

## Imports and Categorization

Imports are a coordinated pipeline, not one upload route. Before editing, trace parsing, source identity, draft/review state, matching rules and learned suggestions, validation, commit, provenance, and undo in `docs/agent/DOMAIN_MAP.md`.

Ask:

- What source formats and account types are in scope?
- Which values are source evidence and which may the user edit?
- What may be suggested automatically, and what always requires review?
- How should duplicates, ambiguous account matches, partial rows, and retries behave?
- What must undo restore or retain for auditability?

Implementation boundary:

- retain source tokens, fingerprints, FITIDs, provenance, and validation evidence as the existing flow requires;
- fail closed on missing or mismatched source identity;
- use current import commit and transaction services for writes, including transfers;
- keep prediction/suggestion output reviewable and do not silently commit a learned category;
- never test against a user's statement or export. Build a tiny synthetic CSV/QFX fixture with fictional institutions, payees, account suffixes, and amounts.

Evidence:

- parser and mapping tests for the new source shape;
- draft/review recovery and duplicate tests;
- commit atomicity, provenance, feedback, and undo tests;
- account/envelope compatibility and transfer-pair tests;
- a request-flow test that begins with a synthetic upload.

## Authentication and Users

The user registry and authentication metadata live in `meta.sqlite`; each user ledger is separate. Changing login or user administration changes a trust boundary even when financial tables are untouched.

Ask:

- Is this one trusted person, a trusted household, or untrusted users?
- Is an optional local password sufficient, or will an upstream gateway own authentication?
- Who administers users and recovery?
- Must users be isolated from one another, and who may change ledger paths?

Implementation boundary:

- preserve selected-user session routing and authentication checks in `current/app/__init__.py`, `current/app/auth.py`, and `current/app/blueprints/users.py`;
- keep password hashes and reset-token hashes in `meta.sqlite`; never log or persist plaintext credentials or reset tokens;
- preserve the last-admin guard and path confinement under the configured user database directory;
- do not treat the current household user picker as public multi-tenant authorization;
- require a separate security scope for public/untrusted access.

Evidence:

- passwordless and password-protected selection;
- stale-session rejection;
- normal-user versus admin authorization;
- single-use recovery and last-admin protection;
- two-user ledger isolation and managed-path rejection.

## Hosting and Packaging

The downloaded source ships the web app. Use `docs/agent/DEPLOYMENT.md` to either configure that web app for the user's trusted environment or plan and implement a target-specific conversion.

Ask:

- target OS/architecture and browser, window, LAN/VPN, or private hostname access;
- trusted users and authentication boundary;
- persistent data, uploads, logs, backups, restores, and secret locations;
- offline/network requirements;
- required clean-machine and recovery evidence.

Implementation boundary:

- package source, templates, static assets, and schema—not a database, upload, local profile, backup, log, or secret;
- use the schema-only bootstrap for first-run data;
- keep the stable application identity separate from the display name;
- keep the Flask origin on loopback behind any private access gateway;
- treat desktop, private-server, and private-cloud requests as conversions with their own implementation and acceptance tests;
- stop and rescope before exposing the app as a public multi-tenant service.

Evidence depends on the target but always includes clean start, persistent restart, migration, backup/restore, private-artifact scan, and a documented rollback or recovery path.

## Completion Report

Report:

- user decisions, remaining assumptions, and the profile used;
- change classes completed and intentionally deferred;
- current patterns followed and any new pattern introduced;
- affected files, migrations, and stable identifiers;
- synthetic acceptance scenarios and their results;
- focused, full-suite, clean-start, and target-specific checks actually run;
- recovery steps and anything still requiring the user's environment.

Describe only the target and workflow that were actually verified. The repository helps an agent customize the project; it does not make the agent's work automatically correct.
