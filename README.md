# Perfii: Pay Yourself First

Perfii is a local-first personal finance application built around envelope accounting. Its Pay Yourself First planner turns a payday savings routine into a persistent workflow: enter take-home pay, calculate percentage-based contributions, build accessible reserves first, switch future contributions to longer-term savings after each target is reached, review grouped transfers, and record only the transfers the user explicitly approves.

The application does **not** move money at a bank. Recording a recommendation creates balanced, linked transfer entries in the local ledger. It also does **not** call the OpenAI API or require an OpenAI key.

## Download and customize

This repository is meant to be downloaded from GitHub or the project website and then adapted with a coding agent:

1. Download and extract the repository, or clone it with Git.
2. Open the top-level project folder—the one containing `README.md` and `AGENTS.md`—with the coding agent.
3. Describe how you want to run the app and what you want changed. Plain-language preferences and fictional examples are enough; do not provide bank credentials or real financial records.
4. Tell the agent to read [AGENTS.md](AGENTS.md) before editing. That file directs the agent to the architecture map, customization questionnaire, safety rules, and verification commands it needs.

The repository ships the web app. The desktop and private-server guides are implementation playbooks for a coding agent, not claims that prebuilt desktop or server editions are included. There is no ZIP builder or duplicate packaging workflow inside the app.

## Why this exists

Many budgeting products require people to adapt to a generic workflow while everyday friction in the core experience goes unresolved. This project began with a different premise: envelope accounting should be dependable, flexible, and responsive to how its owner actually manages money. Organizing envelopes, importing and reviewing transactions, learning useful category suggestions, reconciling accounts, and correcting mistakes are central product responsibilities rather than secondary add-ons.

The application brings accounts, envelope groups, transactions, linked transfers, statement imports, reviewable categorization assistance, reconciliation, credit cards, loans, investments, and savings planning into one local-first ledger. Financial changes remain visible and correctable, imported activity stays reviewable, and the user's data remains under their control.

Personal finance workflows vary too much for one fixed interface to suit everyone. This repository therefore documents its architecture, safety boundaries, customization surfaces, and deployment options for a coding agent that is adapting the app to one person's needs. The Pay Yourself First planner is one example of that philosophy: a recurring percentage-based savings practice can become a reviewed, durable workflow without forcing every user into the same routine.

## Privacy-safe quick start

The commands below create new fictional data. The bootstrap is create-only and refuses to reuse or reset an existing destination.

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

For a data-free starting point, replace `--profile demo` with `--profile schema` and use a new destination directory.

The current interface loads Bootstrap and investment-chart libraries from public CDNs, so a network connection is needed for complete styling and charts. Vendor and license those assets before describing an EXE or local installation as fully offline.

### Back up a managed workspace

The backup utility snapshots live SQLite databases with SQLite's online backup API and restores only into a new destination; it never overwrites or merges into an existing workspace. Rehearse with fictional data first:

```powershell
Set-Location current
python scripts/workspace_backup.py backup --data-dir .local\demo-data --backup-dir .local\backups\demo-data-backup
python scripts/workspace_backup.py restore --backup-dir .local\backups\demo-data-backup --data-dir .local\demo-data-restored
python scripts/doctor.py --data-dir .local\demo-data-restored
```

A workspace backup contains sensitive financial data even though it is stored as a directory rather than a ZIP. Keep it in an ignored, owner-controlled location. See [the data-operations guide](docs/agent/DATA_OPERATIONS.md) before asking an agent to operate on any non-fictional workspace.

## Demonstration scenario

The fictional demo is configured to show both sides of the cutoff:

- Emergency Reserve and Home and Car are still below their accessible targets, so their full contributions remain easy to reach.
- Future Adventures has reached its accessible target, so its full contribution goes to long-term savings.

With fictional take-home pay of `$3,200.00`, the configured 18% savings rate produces `$576.00` in total contributions and `$2,624.00` in remaining pay. The review groups those purposes into transfers by destination account. Recording one group creates equal-and-opposite ledger legs with matching envelope splits; it never contacts a financial institution.

## Tests and checks

Run from `current/`:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -t .
.\.venv\Scripts\python.exe scripts\doctor.py --data-dir .local\demo-data
.\.venv\Scripts\python.exe scripts\run_local.py --data-dir .local\demo-data --check-only
```

Theme configuration is checked from the Git root:

```powershell
python tools\agent_preflight.py --quick
python tools\validate_agent_config.py --json
python tools\customize_theme.py --profile agent-config\theme-profile.example.json --check
```

All databases, uploads, local environment files/secrets, logs, backups, and local customization profiles are ignored; the shareable `current/.env.example` template is intentionally tracked. Development and demonstration must use synthetic financial data.

## Included customization support

The project is optimized to give a coding agent a strong chance of safely adapting it to an owner's environment and preferences. It is not a one-click customization system and does not promise arbitrary changes without engineering and verification.

The support material includes an [agent system map](docs/agent/README.md), a [machine-readable project manifest](agent-config/project-manifest.json), a [customization questionnaire](docs/agent/CUSTOMIZATION.md), [change playbooks](docs/agent/PLAYBOOKS.md), a [domain and test map](docs/agent/DOMAIN_MAP.md), [data-safety rules](docs/agent/DATA_OPERATIONS.md), a constrained [theming tool](docs/agent/THEMING.md), and [hosting-conversion guidance](docs/agent/DEPLOYMENT.md).

For a theme-only or visible display-name-only change, the agent can use the lightweight brief in the questionnaire. Consequential workflow, financial, data, authentication, localization, or hosting changes use the full machine-readable profile in `agent-config/`. Personal answers should use the ignored `*.local.json` filename documented there, and the ready-only validation gate prevents an unresolved draft from being mistaken for implementation approval. Visible product branding uses `APP_DISPLAY_NAME`, separately from stable internal storage and deployment identifiers.

## Deployment status

The repository ships the Flask web application, with local web use on a trusted computer as the exercised path. The deployment guide gives a coding agent concrete conversion briefs for a desktop shell, private server, or private cloud VM, including the implementation decisions and target-specific verification each conversion needs. Direct public multi-user hosting remains outside this repository's scope. See [the deployment guide](docs/agent/DEPLOYMENT.md).

## What's in the future

The most important next step for the customization system is repeated blind handoff testing: give fresh coding agents only the downloaded repository and fictional user briefs, observe where they hesitate or fail, then tighten the directions and automated checks from that evidence. Broader agent-compatibility claims will wait until those trials have actually been run.

Other likely improvements include one typo-tolerant transaction search across account, payee, and memo; a more compact transaction-filter experience; prediction benchmarks built entirely from synthetic data; vendored and licensed browser assets for fully offline use; broader accessibility review; and additional hosting targets only after target-specific implementation and verification. Receipt parsing, cryptocurrency accounts, bank connections, and runtime AI remain intentionally outside the current scope.

## Maintainer release check

The normal public source is the GitHub repository. Before publishing a reviewed commit or tag, a maintainer should run:

```powershell
python tools/source_safety.py --root .
python tools/source_safety.py --root . --history --ref HEAD
python tools/agent_preflight.py --full
```

Review every reported issue and confirm that no database, export, upload, secret, local profile, log, backup, generated artifact, or real financial record is tracked. The repository does not build or store a second ZIP of itself.

## License

The project is released under the [MIT License](LICENSE). Direct dependency attribution is inventoried separately in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
