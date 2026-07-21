# Perfii: Pay Yourself First

Perfii is a personal finance app that runs locally and uses envelope accounting. It brings accounts, envelopes, transactions, transfers, statement imports, reconciliation, credit cards, loans, investments, and savings plans together in one ledger.

The **Pay Yourself First** planner helps you divide each paycheck by percentage. You choose what you are saving for, the percentage for each purpose, and where the money should go. Perfii directs each contribution to an accessible savings account until its target balance is reached. Future contributions then go to a longer-term savings account. You can review the plan before recording any transfers.

Perfii records balanced transfer entries in its local ledger. It does not connect to a bank or move money. It also runs without the OpenAI API or an OpenAI API key.

## Download and customize

This repository contains the web app and documentation that helps a coding agent understand and adapt it:

1. Download and extract the repository, or clone it with Git.
2. Open the top-level project folder, which contains `README.md` and `AGENTS.md`, with the coding agent.
3. Describe how you want to run the app and what you want changed. Use plain language and fictional examples. Do not provide bank credentials or real financial records.
4. Tell the agent to read [AGENTS.md](AGENTS.md) before editing. It points to the project map, customization questionnaire, safety rules, and verification commands.

Perfii currently runs as a local web app. The [deployment guide](docs/agent/DEPLOYMENT.md) explains what an agent would need to change and test for a desktop app, private server, or private cloud VM.

## Why this exists

Perfii grew out of years of using envelope-budgeting software whose everyday problems remained unfixed. The goal is to make the core work dependable: organizing envelopes, importing and reviewing transactions, suggesting useful categories, reconciling accounts, and correcting mistakes.

Imported transactions remain available for review. Category suggestions can improve from past choices. Transfers are stored as linked entries so both sides stay in balance. The user can see and correct financial changes while keeping the data on their own computer.

People manage money in different ways. The repository includes detailed guidance so a coding agent can adapt Perfii's terms, appearance, workflows, and hosting setup to one person's needs without guessing how the financial pieces fit together.

## Built with GPT-5.6 and Codex

Perfii existed before the July 13-21, 2026 submission period. During that period, the owner used Codex in the ChatGPT desktop app, powered by GPT-5.6, to add the Pay Yourself First planner and prepare the project for coding-agent customization and public release.

### What already existed

Before July 13, Perfii already supported separate local ledgers for different users, accounts, envelopes, transactions, linked transfers, statement imports, category suggestions, reconciliation, credit cards, loans, investments, and local user administration.

### What was added with GPT-5.6 and Codex

- Built the **Pay Yourself First** planner from the owner's payday savings spreadsheet. It saves percentage rules, calculates exact cent amounts, fills accessible savings targets first, and sends later contributions to long-term savings.
- Connected approved savings plans to the ledger as matching transfer-out and transfer-in entries. Safeguards prevent an expired plan or the same plan from being recorded twice.
- Added a fictional demo workspace and tools for first-time setup, health checks, backup and restore, and ledger consistency checks.
- Added project maps, a customization questionnaire, change guides, deployment guidance, data-safety rules, and a theme customization tool for coding agents.
- Added privacy scans, automated release checks, and a 637-test regression suite that uses generated financial data.

### How the collaboration worked

The owner explained the real payday workflow and made the product decisions: percentage-based savings, accessible balance targets, a full cutoff to long-term savings after each target, automatic rule saving, and review before transfers are recorded. The owner tested the running app and requested interface changes throughout the work.

Codex traced the existing application, studied the spreadsheet, proposed how the feature should fit into the ledger, implemented and revised the code, wrote tests, and checked for personal data, broken setup steps, and test failures. It also found and fixed a Windows-only test failure in GitHub Actions.

GPT-5.6 helped work through exact-cent calculations, savings cutoffs, stale previews, duplicate recording, database safety, and test coverage. The owner made the final decisions about the product, design, scope, and release.

The Git history and [Release verification workflow](https://github.com/DavidPulcifer/perfii/actions/workflows/release-verification.yml) show the implementation and test results. The primary Codex task's `/feedback` Session ID is supplied separately in the Devpost submission.

## Quick Start

These commands create a new workspace with fictional data. The setup command refuses to reuse an existing destination.

Verified environment: Windows, Python 3.13.

```powershell
Set-Location current
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe scripts\bootstrap_workspace.py --data-dir .local\demo-data --profile demo
.\.venv\Scripts\python.exe scripts\doctor.py --data-dir .local\demo-data
.\.venv\Scripts\python.exe scripts\run_local.py --data-dir .local\demo-data
```

Open `http://127.0.0.1:8080`, choose **Demo User**, and select **Savings** in the navigation.

For an empty starting point, replace `--profile demo` with `--profile schema` and use a new destination directory.

The interface loads Bootstrap and investment-chart libraries from public CDNs. A fully offline installation would need local copies of those assets and their licenses.

### Back up a workspace

The backup utility copies live SQLite databases and restores them only into a new destination. Practice with fictional data first:

```powershell
Set-Location current
python scripts/workspace_backup.py backup --data-dir .local\demo-data --backup-dir .local\backups\demo-data-backup
python scripts/workspace_backup.py restore --backup-dir .local\backups\demo-data-backup --data-dir .local\demo-data-restored
python scripts/doctor.py --data-dir .local\demo-data-restored
```

Backups contain financial data. Keep them in an ignored directory that only the owner can access. Read [the data-operations guide](docs/agent/DATA_OPERATIONS.md) before asking an agent to work with a real ledger.

## Quick evaluation path

After starting the app with the fictional demo workspace:

1. Choose **Demo User** and open **Savings**.
2. Enter fictional take-home pay of `$3,200.00` and preview the plan.
3. Confirm that the configured 18% savings rate produces `$576.00` in contributions and `$2,624.00` in remaining pay.
4. Review which contributions go to accessible savings and which go to long-term savings.
5. Record one transfer group, then open **Transactions** and verify the matching transfer-out and transfer-in entries.

The demo shows both outcomes:

- **Emergency Reserve** and **Home and Car** are below their accessible savings targets, so their contributions go to accessible savings.
- **Future Adventures** has reached its accessible target, so its contribution goes to long-term savings.

Recording a group adds both sides of the transfer to the local ledger. It does not contact a financial institution.

## Tests and checks

Run from `current/`:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -t .
.\.venv\Scripts\python.exe scripts\doctor.py --data-dir .local\demo-data
.\.venv\Scripts\python.exe scripts\run_local.py --data-dir .local\demo-data --check-only
```

Run project and theme checks from the Git root:

```powershell
python tools\agent_preflight.py --quick
python tools\validate_agent_config.py --json
python tools\customize_theme.py --profile agent-config\theme-profile.example.json --check
```

Private runtime files such as databases, uploads, secrets, logs, backups, and local customization profiles are excluded by `.gitignore`. Tests and demos must use synthetic financial data.

## Customization support

The repository includes guides written for coding agents so they can understand the project before changing it:

- The [agent system map](docs/agent/README.md) and [project manifest](agent-config/project-manifest.json) identify the main parts of the application and the available checks.
- The [customization questionnaire](docs/agent/CUSTOMIZATION.md), [change playbooks](docs/agent/PLAYBOOKS.md), and [domain map](docs/agent/DOMAIN_MAP.md) help an agent turn a user's request into a focused change.
- The [data guide](docs/agent/DATA_OPERATIONS.md), [theme guide](docs/agent/THEMING.md), and [deployment guide](docs/agent/DEPLOYMENT.md) explain how to work with real data, change the appearance, and prepare other hosting setups.

For a color, font, or display-name change, use the short brief in the customization questionnaire. Larger workflow, financial, authentication, language, or hosting changes use the full profile in `agent-config/`. Save personal answers in the ignored `*.local.json` file described there. `APP_DISPLAY_NAME` controls the visible product name.

## Deployment

Local web use on a trusted computer is the tested setup. The [deployment guide](docs/agent/DEPLOYMENT.md) covers the work needed for a desktop shell, private server, or private cloud VM. An internet-facing installation also needs authentication, HTTPS, backups, updates, and monitoring appropriate to its users and data.

## What's in the future

Planned improvements include:

- Blind handoff tests in which a new coding agent receives only the repository and a fictional user request. The results will be used to improve unclear directions and checks.
- Typo-tolerant transaction search across account, payee, and memo.
- A more compact transaction-filter interface.
- Category-suggestion benchmarks made from synthetic data.
- Local copies of browser assets for offline use.
- A broader accessibility review.
- More hosting guides after each setup has been implemented and tested.

## Maintainer release check

Before publishing a commit or tag, run:

```powershell
python tools/source_safety.py --root .
python tools/source_safety.py --root . --history --ref HEAD
python tools/agent_preflight.py --full
```

Review any reported issue and confirm that the repository contains no database, export, upload, secret, local profile, log, backup, generated file, or real financial record.

## License

Perfii is released under the [MIT License](LICENSE). Direct dependency attribution is listed in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
