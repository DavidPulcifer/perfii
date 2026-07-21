# Agent-Assisted Customization

The workflow is straightforward: download or clone the repository, open its top-level folder with a coding agent, and describe the desired outcome. The repository should help that agent discover the architecture, ask focused questions, work only with synthetic data, implement a bounded customization, and prove the result. This is guided engineering, not one-click generation.

## First Conversation

Begin by restating the user's goal. Ask only questions that change the plan, and skip groups that are irrelevant:

1. **Outcome and scope:** must-have result, acceptable deferrals, and two or three synthetic acceptance scenarios.
2. **Environment:** operating system, local browser versus another hosting conversion, trusted users, network/offline needs, data location, and backup expectations.
3. **Terminology and localization:** visible app name, names for envelopes/categories, account or workflow labels, and any currency/locale/date/time expectations. Localization is cross-cutting and must not be treated as a simple search-and-replace.
4. **Workflow:** recurring task, trigger, inputs, deterministic decisions, review point, persisted result, and correction path.
5. **Appearance:** light/dark preference, primary color, visual character, density, font style, and accessibility needs.
6. **Existing state:** starting empty or eventually bringing an existing ledger forward. Do not inspect or migrate real data during ordinary customization.

Do not request credentials, account numbers, real statements, exports, screenshots, institution names, or real balances. Roles such as “paycheck checking,” “quick-access savings,” and “long-term savings” are sufficient.

## Choose the Smallest Requirements Path

A theme-only or visible display-name-only request may use a short written brief instead of the full JSON profile when it does not change behavior, financial meaning, persistent identity, localization, authentication, data location, network exposure, packaging, or hosting. Record the requested appearance, one synthetic acceptance scenario, affected files, and the focused browser/check commands. Use `docs/agent/THEMING.md` and the constrained theme helper for supported visual tokens.

Use the full profile for every workflow, financial model, import, authentication/user, data migration, localization, hosting, packaging, or mixed-scope change. Also escalate to the full profile whenever a supposedly simple request touches a trust boundary or requires a consequential assumption.

## Full Requirements Profile

Start from the neutral `agent-config/customization-profile.template.json`. Use `agent-config/customization-profile.pay-yourself-first.example.json` as a worked synthetic example, not as the user's default requirements. Save answers to the ignored `agent-config/customization-profile.local.json`.

The profile is a requirements brief, not runtime configuration. For each consequential item, distinguish:

- **confirmed:** the user explicitly chose it;
- **assumed:** the agent proposes it and explains why;
- **unresolved:** implementation must wait because alternatives change financial behavior, authentication, data location, exposure, or recovery;
- **deferred:** intentionally outside this customization.

Do not silently retain example values. “Skip irrelevant questions” means the agent should not interrogate the user about them; it does not mean required JSON fields can be omitted. Record a safe explicit assumption, use `not_applicable` where the schema allows it, or mark an optional section/change deferred. Validate the completed profile in ready-only mode before implementation:

```powershell
python tools/validate_agent_config.py --profile agent-config/customization-profile.local.json --require-ready --json
```

The normal validator accepts tracked draft templates so the repository itself can be checked. `--require-ready` is the gate that prevents an agent from treating a valid but unresolved local draft as authorization to implement.

Never force-add or commit the ignored personal profile. If requirements must be shared, create a separately named sanitized profile containing only reviewed, non-identifying requirements and validate it explicitly.

## Turn Requirements into a Plan

1. Summarize the requested outcome in plain language.
2. Divide it into appearance, branding/terminology, workflow, financial model, imports, authentication/users, and hosting items.
3. Use `docs/agent/PLAYBOOKS.md` for each selected class and `docs/agent/DOMAIN_MAP.md` to locate code and tests.
4. Record financial and trust-boundary decisions that require confirmation.
5. Write success, invalid-input, cancellation/rollback, retry, and user-isolation scenarios as applicable.
6. Identify the smallest current feature that demonstrates the desired pattern.
7. Establish the synthetic baseline and checkpoint described in `docs/agent/README.md`.

Keep change classes separate in the plan and diff. A palette request should not trigger a schema change; a visible label change should not rename storage keys; a hosting conversion should not rewrite financial rules.

## Risk Classes

| Class | Typical surface | Main risk |
| --- | --- | --- |
| Appearance | theme profile and generated theme CSS | contrast, focus, responsive regressions |
| Branding/terminology | display-name setting, templates, messages, docs | confusing visible copy with persistent identity |
| Workflow | blueprint, service, repositories, template, focused tests | writes without review, non-atomic behavior |
| Financial model | schema, migration, repositories, services | balance, sign, split, upgrade, and isolation errors |
| Imports | parser through commit/provenance/undo pipeline | lost evidence, silent categorization, duplicates |
| Authentication/users | meta database, sessions, user administration | broken access or ledger isolation |
| Hosting/packaging | launcher, environment, gateway, recovery docs | data leakage, exposure, unrecoverable deployment |

## Stable Identity Versus Display Name

The user's preferred product name is presentation. Machine-facing identifiers are compatibility surfaces.

- A **display name** appears in page titles and navigation through the validated `APP_DISPLAY_NAME` setting and may also be reflected in user-facing documentation.
- A **stable application identity** includes default data-directory slugs, environment variable names, service/package identifiers, local-storage keys, JavaScript event names, and database identifiers.

Centralize visible branding when implementing it, but do not broadly replace stable identifiers. Changing an identity may strand settings or data and requires an explicit compatibility/migration plan. See the branding playbook for the required occurrence inventory.

## Workflow Brief

For a personal-finance workflow, capture:

- **Trigger:** What starts it?
- **Inputs:** What does the user enter or select?
- **Rules:** What deterministic choices and accounting assumptions apply?
- **Review:** What must the user see before records change?
- **Writes:** Which settings, transactions, transfer legs, splits, or evidence persist?
- **Recovery:** How can the user cancel, correct, retry, or undo it?
- **Privacy:** Which synthetic scenario demonstrates it?
- **Success:** What observable result proves it works?

The payday savings workflow is the current canonical preview/confirmation example. Preserve its exact hard cutoff unless the user explicitly requests a behavior change: below the target at preview start means the whole contribution goes to accessible savings, even when that contribution crosses the target; at or above target means the whole contribution goes long-term. The switch occurs on the next preview, and a contribution is never divided between the two destinations.

## Baseline and Checkpoints

Before editing, record the current commit, dirty files, focused-test result, and full-suite result. Create a new ignored synthetic demo workspace for interactive checks. Do not clean unrelated files or reuse an existing data directory.

Implement one vertical slice at a time. After each slice:

1. run the focused tests;
2. inspect the diff for private data, absolute paths, credentials, databases, and unrelated changes;
3. compare behavior with the written acceptance scenarios;
4. create a Git checkpoint only if commits are authorized, otherwise record the patch boundary in the completion report.

At completion, repeat focused and full checks. Report baseline failures separately from regressions introduced by the customization.

## Completion Standard

Report:

- the validated profile or explicit decisions used;
- confirmed, assumed, unresolved, and deferred items;
- files, migrations, persistent identifiers, and data structures changed;
- canonical project patterns followed and any new pattern introduced;
- financial invariants and trust boundaries exercised;
- synthetic acceptance scenarios and results;
- focused, full-suite, clean-start, browser, and target-specific checks actually run;
- recovery/rollback guidance and environment-specific work remaining.

An accurate claim is: “This repository is designed to help a coding agent customize and verify the application for a user's stated needs.” Do not promise that every agent, environment, or customization will succeed automatically.
