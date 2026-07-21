# Perfii: Pay Yourself First

Perfii is a personal finance app that makes envelope budgeting faster and easier by learning from your transaction history and doing the manual work for you. It brings bank accounts, credit cards, loans, investments, and savings plans together in one place. It also helps you enforce your own savings plans with the Pay Yourself First planner that makes it easy to put money into savings first.

The core feature of this app is transaction category suggestions which improve from past choices and can still predict even when transaction descriptions for the same company can vary.

The second core feature is guidance so a coding agent can adapt Perfii's appearance, workflows, and hosting setup to your needs.

Perfii's calculations and category suggestions run locally. It needs no API keys or external AI service. You can still use a coding agent to change the source code and make the app your own.

## Download and customize

This repository contains the web app and documentation that helps a coding agent understand and adapt it:

1. Download and extract the repository, or clone it with Git.
2. Open the top-level project folder, which contains `README.md` and `AGENTS.md`, with the coding agent. README is for you, AGENTS is for your coding agent.
3. Tell the agent to read [AGENTS.md](AGENTS.md) before editing. It points to the project map, customization questionnaire, safety rules, and verification commands.
4. Describe how you want to host the app (locally, VPS, etc) and what you want changed and your coding agent can help you implement.

Perfii runs out of the box as a local web app. The [deployment guide](docs/agent/DEPLOYMENT.md) explains what an agent would need to change to convert this to a desktop app, private server, or private cloud VM.

## Why this exists

Perfii grew out of years of using envelope-budgeting software and growing frustrated that core user experience issues were getting passed over in favor of premium add-on features. The goal for this app is to provide the core features needed in an envelope budgeting system and then make it as easy as possible for a user to modify the program for their own needs. The existence of coding agents means anyone can take this template and build truly useful software for themselves.

I built this to fit how I manage my budget, and then worked with GPT-5.6 and Codex in the ChatGPT app to make it as easy as possible for other people to change it to fit how they budget.

The inciting incident that led to starting on this app was a few years ago when Amazon transactions started showing up with a unique ID instead of just AMAZON.COM. The unique IDs completely broke the auto-detections in the app I was using at the time so I had to handle all Amazon transactions manually. It seems like table stakes for an envelope budgeting app with envelope predictions to handle something like this, but alas it could not.

I went to the support forum to see if other people were having this same issue and while I didn't get a solution to my problem I did see a lot of great features being requesting but there either wasn't enough support, or it was a feature the devs decided they did not intend to build regardless of interest level. So, it dawned on me the problem is you have one company trying to make a budget app that is everything for everyone, and so doesn't serve any one person very well.

My goal here is not to make the best budgeting app, but to provide a template that you can turn into the best budgeting app for you.

## Built with GPT-5.6 and Codex

Perfii existed before the OpenAI Build Week Hackathon of July 2026. The core interface was built in VS Code with assistance by ChatGPT back in 2024. As ChatGPT got better I gradually turned more of the coding work over. Starting in July as part of the OpenAI Build Week Hackathon I migrated this over to the ChatGPT app and worked with GPT-5.6 to build out the Pay Yourself First planner and the Coding Agent instructions to convert this to a template app that anyone could use.

### What already existed

Before July 13, Perfii already supported separate local ledgers for different users, accounts, envelopes, transactions, linked transfers, statement imports, category suggestions, reconciliation, credit cards, loans, investments, and local user administration.

### What was added with GPT-5.6 and Codex

- Built the **Pay Yourself First** planner. It allows you to set up savings rules based on a percentage of income. It also allows a tiered account transfer system where emergency cash can first be deposited into an accessible account (easy to access the funds in a pinch) and then after hitting a target it flows over into a longer term account where you would presumably make more interest or dividends on the money but it may be less accessible in an emergency. This is an artifact of how I like to handle my finances. If I have $2000 set aside for medical expenses, I usually don't need all of that instantly. I maybe only need $500 available instantly and the rest can sit in a high-yield savings account until I need it.
- Connected approved savings plans to the ledger as matching transfer-out and transfer-in entries. Safeguards prevent an expired plan or the same plan from being recorded twice.
- Added a demo workspace and tools for first-time setup, health checks, backup and restore, and ledger consistency checks.
- Added project maps, a customization questionnaire, change guides, deployment guidance, data-safety rules, and a theme customization tool for coding agents.
- Added privacy scans, automated release checks, and a 637-test regression suite that uses generated financial data.

### How the collaboration worked with GPT-5.6 and Codex

I explained the workflows and made the product decisions: percentage-based savings, accessible balance targets, a full cutoff to long-term savings after each target, automatic rule saving, and review before transfers are recorded. I tested the app and requested interface changes throughout the work.

Codex traced the existing application, studied my old budgeting spreadsheets, proposed how the Pay Yourself First feature should fit into the app, implemented and revised the code, wrote tests, and checked for personal data, broken setup steps, and test failures. It also found and fixed a Windows-only test failure in GitHub Actions.

GPT-5.6 helped work through exact-cent calculations, savings cutoffs, stale previews, duplicate recording, database safety, and test coverage. I made the final decisions about the product, design, scope, and release.

The Git history and [Release verification workflow](https://github.com/DavidPulcifer/perfii/actions/workflows/release-verification.yml) show the implementation and test results. The primary Codex task's `/feedback` Session ID is supplied separately in the Devpost submission.

## Quick Start

These commands create a new workspace with fictional data.

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

- Continuing to improve agent direction for more reliable implementation and customization in different environments.
- Improve transaction search across account, payee, and memo.
- A more compact transaction-filter interface.
- More hosting guides after each setup has been implemented and tested.
- Receipt scanner to automatically read PDF or email receipts and automatically detect item splits.

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
