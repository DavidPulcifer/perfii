# Perfii: Pay Yourself First

Perfii is a personal finance app that makes envelope budgeting faster by learning from your transaction history and reducing repetitive categorization work. It brings accounts, envelopes, credit cards, loans, investments, and savings plans together in one place. The **Pay Yourself First** planner also helps you follow through on your savings goals by making savings the first step when you process a paycheck.

Perfii's main feature is transaction category suggestions. The suggestions improve from your past choices and can recognize the same company even when its transaction descriptions vary.

The repository also includes guidance for coding agents so you can adapt Perfii's appearance, workflows, and hosting setup to your needs.

Perfii's calculations and category suggestions run locally. It needs no API keys or external AI service. You can still use a coding agent to change the source code and make the app your own.

## Download and customize

This repository contains the web app and documentation that helps a coding agent understand and adapt it:

1. Download and extract the repository, or clone it with Git.
2. Open the top-level project folder, which contains `README.md` and `AGENTS.md`, with the coding agent. This README is for you; `AGENTS.md` is for your coding agent.
3. Tell the agent to read [AGENTS.md](AGENTS.md) before editing. It points to the project map, customization questionnaire, safety rules, and verification commands.
4. Describe how you want to host the app, such as on your own computer or a private server, and explain what you want changed. Your coding agent can use the project guides to help implement those changes.

Perfii runs as a local web app after setup. The [deployment guide](docs/agent/DEPLOYMENT.md) explains the work needed to convert it to a desktop app, private server, or private cloud VM.

## Why this exists

Perfii grew out of years of using envelope-budgeting software and becoming frustrated that core user-experience problems were passed over in favor of premium add-on features. My goal is to provide the essential features of an envelope-budgeting system and make the program easy to adapt for your own needs. Coding agents make it possible for more people to take a project like this and build software that genuinely fits them.

I built Perfii around the way I manage my budget. I then worked with GPT-5.6 and Codex in the ChatGPT app to make the project easier for other people to change around the way they budget.

The incident that started this project happened a few years ago. Amazon transactions began including unique IDs in their descriptions, so the stable `AMAZON.COM` description disappeared. That change broke the automatic categorization in the budgeting app I was using, and I had to handle every Amazon transaction manually. An envelope-budgeting app with category predictions should be able to handle a change like that, but this one could not.

I visited the support forum to see whether other people had the same problem. I did not find a solution, but I found many useful feature requests. Some did not have enough votes, while others were features the developers had decided not to build. That experience showed me the limitation of one company trying to make a single budgeting app for everyone: no fixed product will fit every person's financial routine equally well.

My goal is to provide a template you can turn into the best budgeting app for you.

## Built with GPT-5.6 and Codex

Perfii existed before the OpenAI Build Week Hackathon in July 2026. I built the core interface in VS Code with help from ChatGPT in 2024. As ChatGPT improved, I gradually used it for more of the coding work. During the July 2026 hackathon, I moved the project into the ChatGPT app and worked with GPT-5.6 through Codex to build the Pay Yourself First planner and the coding-agent guides that make Perfii easier to adapt.

### What already existed

Before July 13, Perfii already supported separate local ledgers for different users, accounts, envelopes, transactions, linked transfers, statement imports, category suggestions, reconciliation, credit cards, loans, investments, and local user administration.

### What was added with GPT-5.6 and Codex

- Built the **Pay Yourself First** planner. You can set savings rules as percentages of income and assign an accessible account and a longer-term account to each savings purpose. Contributions go to the accessible account until its target is reached, then future contributions go to the longer-term account. This reflects how I handle goals such as medical savings: out of `$2,000` set aside, I might keep `$500` immediately accessible and hold the rest in an account intended for longer-term savings.
- Connected approved savings plans to the ledger as matching transfer-out and transfer-in entries. Safeguards prevent an expired plan or the same plan from being recorded twice.
- Added a fictional demo workspace and tools for first-time setup, health checks, backup and restore, and ledger consistency checks.
- Added project maps, a customization questionnaire, change guides, deployment guidance, data-safety rules, and a theme customization tool for coding agents.
- Added privacy scans, automated release checks, and a 637-test regression suite that uses generated financial data.

### How the collaboration worked with GPT-5.6 and Codex

I explained the workflows and made the product decisions: percentage-based savings, accessible balance targets, a full cutoff to long-term savings after each target, automatic rule saving, and review before transfers are recorded. I tested the app and requested interface changes throughout the work.

Codex traced the existing application, studied my old budgeting spreadsheets, proposed how the Pay Yourself First feature should fit into the app, implemented and revised the code, wrote tests, and checked for personal data, broken setup steps, and test failures. It also found and fixed a Windows-only test failure in GitHub Actions.

GPT-5.6 helped work through exact-cent calculations, savings cutoffs, stale previews, duplicate recording, database safety, and test coverage. I made the final decisions about the product, design, scope, and release.

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

## Back up a workspace

The backup utility copies live SQLite databases and restores them only into a new destination. Practice with fictional data first:

```powershell
Set-Location current
python scripts/workspace_backup.py backup --data-dir .local\demo-data --backup-dir .local\backups\demo-data-backup
python scripts/workspace_backup.py restore --backup-dir .local\backups\demo-data-backup --data-dir .local\demo-data-restored
python scripts/doctor.py --data-dir .local\demo-data-restored
```

Backups contain financial data. Keep them in an ignored directory that only the owner can access. Read [the data-operations guide](docs/agent/DATA_OPERATIONS.md) before asking an agent to work with a real ledger.

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

- Improve the coding-agent instructions as more people use them in different environments.
- Improve transaction search across account, payee, and memo.
- Make the transaction-filter interface more compact.
- Add more hosting guides after each setup has been implemented and tested.
- Add receipt import that can read PDF or email receipts and suggest item splits.

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
