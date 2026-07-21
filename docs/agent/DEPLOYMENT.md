# Hosting and Packaging Conversion Guide

The repository ships a Flask web application. Running it in a local browser is the exercised default. When a user prefers a desktop window, private server, or private cloud VM, this guide helps a coding agent turn that preference into a target-specific implementation and verification plan.

The guide does not claim that one artifact fits every environment. The agent is expected to confirm the target, adapt the relevant building blocks, and prove clean start, persistence, privacy, and recovery on that target.

## Choose the Target

| User goal | Starting path | Agent deliverable |
| --- | --- | --- |
| Use in a browser on one trusted computer | `current/wsgi.py`, `current/scripts/run_local.py`, tracked schema/bootstrap | configured local launcher and verified data/backup location |
| Use in a Windows desktop window or EXE | Flask web app plus `current/desktop_app.py` and desktop dependencies | packaged loopback app/window, first-run bootstrap, installer or artifact, target test record |
| Use from trusted devices through a private server | Flask/Gunicorn building blocks | target-specific service, authenticated gateway, persistent storage, backup/recovery runbook |
| Use on a private cloud VM | private-server path on a named provider/OS | VM/service configuration with the same gateway and recovery controls |
| Offer a service to unknown public users | separate product/security program | stop and scope multi-tenancy, security, privacy, and operations before deployment |

“Cloud” is not a sufficient target. A private VM behind an authenticated gateway has a different trust model from a public multi-user service.

## Target Brief

Before changing a launcher or deployment file, record:

- operating system and CPU architecture;
- browser, desktop window, private LAN/VPN, or private hostname access;
- one trusted person, trusted household, or untrusted users;
- persistent data, uploads, logs, backups, and restore locations;
- who can read those locations;
- secret installation and rotation method;
- TLS and access-control owner before requests reach Flask;
- network/offline requirement, including browser assets;
- restart, upgrade, disk-full, rollback, and recovery expectations;
- exact clean-machine evidence required at completion.

Use fictional values in the brief. Do not collect host credentials, tokens, account information, or a personal database.

## Packaging Boundary

A packaged application artifact may include code, templates, static assets, dependencies/licenses, and `current/app/base_schema.sql`. It must not include a SQLite database, WAL/SHM file, upload, statement, backup, `.env`, local customization profile, secret, log, or owner-specific output.

Follow `docs/agent/DATA_OPERATIONS.md` for the privacy boundary. First run creates a new workspace from the tracked schema. Copying a “template database” is not the current bootstrap pattern. Inspect the completed artifact, not just its build input list.

Visible branding and stable identity are separate deployment concerns. Set the visible name with `APP_DISPLAY_NAME`. Default data-directory slugs, environment names, service IDs, package IDs, local-storage keys, and database identifiers should remain stable unless the agent implements and tests an explicit compatibility/migration plan.

## Shipped Path: Local Web App

For development or a trusted local installation, bind to `127.0.0.1` and use an explicit managed data directory. `current/app/config.py` reads environment variables directly; `.env.example` is a server-oriented template, is not automatically loaded, and its `/srv/...` examples must not be copied literally into a Windows setup.

Create a new synthetic workspace from `current/`:

```powershell
python scripts/bootstrap_workspace.py --data-dir .local/demo-data --profile demo
python scripts/doctor.py --data-dir .local/demo-data
python scripts/run_local.py --data-dir .local/demo-data --check-only
```

Bootstrap refuses an existing destination. Choose a new ignored path for another clean run. Doctor's direct SQLite checks are read-only; its default Flask smoke may apply idempotent schema or metadata repair inside that managed workspace. Add `--no-smoke` for strictly read-only inspection.

After `--check-only` passes, omit it to start the loopback server at `http://127.0.0.1:8080`; `--port` selects a different local port.

The synthetic bootstrap, doctor, users/dashboard/savings smoke, and an interactive savings workflow have been exercised on Windows/Python 3.13 at desktop and narrow browser widths. For a particular user's installation, also verify:

1. dependencies install in a clean environment;
2. the launcher always uses the intended persistent directory;
3. restart preserves data;
4. backup and restore work;
5. errors identify recovery steps without exposing private paths or rows;
6. nothing writes outside the selected data/log directories.

The current pages load Bootstrap CSS/JavaScript, Chart.js, chartjs-adapter-date-fns, Hammer.js, and chartjs-plugin-zoom from public CDNs. Internet access is therefore required for complete styling, responsive navigation/modals, and investment charts. An offline conversion vendors these assets, records their licenses/versions, updates templates, and exercises all affected pages without a network.

## Desktop Window or EXE Conversion

`current/desktop_app.py` demonstrates the intended shell: start Flask on loopback and display it in a PyWebView window. `current/requirements-desktop.txt` identifies the additional dependencies. Treat the historical packaging note as background only; create a reviewed build specification for the user's target. The current starting file sets `APP_DATA_DIR` to `~/.envelope-budget`, while the normal Windows configuration has an OS-specific default. A desktop conversion must deliberately choose one persistent location, make every derived path use it, and test upgrade behavior rather than inheriting that discrepancy accidentally.

An agent performing the conversion should:

1. choose and record the desktop shell, packaging tool, Python version, OS, and architecture;
2. route writable data, uploads, logs, and backups to an OS-appropriate persistent user directory;
3. initialize a new workspace from `base_schema.sql` and current migrations—never bundle a database;
4. bundle templates and static assets and make resource lookup work in both source and packaged execution;
5. manage loopback startup readiness, port selection, window lifecycle, shutdown, and actionable startup errors;
6. decide whether the app may use CDN assets or must vendor them for offline operation;
7. create a reproducible build specification and record dependency/license information;
8. inspect the final artifact and runtime logs for databases, secrets, source-machine paths, or private metadata;
9. add signing, installer, shortcuts, and uninstall behavior only when the user's delivery expectation requires them.

Desktop acceptance evidence should cover a clean Windows account, first launch, user/workspace creation, browser-window workflow, restart, upgrade/migrations, backup, restore, corrupt/missing data handling, uninstall data policy, and artifact privacy. Report the exact tested artifact hash and platform.

The deliverable is a verified conversion for the named target, not a generic assertion that every Windows environment will behave identically.

## Private Server Conversion

Use `current/wsgi.py`, the Gunicorn dependencies/configuration, `current/.env.example`, and `current/deploy/envelope-budget.service.example` as building blocks. The service file is deliberately a conversion template: derive identities, paths, access controls, and recovery commands from the target brief rather than installing it unchanged.

An agent performing the conversion should:

1. create a dedicated least-privilege OS account and persistent local data directory;
2. install a pinned application environment and start from the schema-only bootstrap;
3. install secrets outside source control;
4. run one application worker until SQLite concurrency behavior is deliberately tested;
5. keep the Flask/Gunicorn origin on loopback;
6. put an authenticated reverse proxy, tunnel, VPN, or access gateway in front of it;
7. configure secure-cookie and trusted-proxy settings for the actual TLS boundary;
8. create service startup, readiness, restart, upgrade, rollback, backup, restore, log rotation, storage monitoring, and disk-full procedures;
9. verify that the origin is not directly reachable from an untrusted network.

Target evidence should include least-privilege ownership, access enforcement, service and machine restart, schema upgrade, backup/restore rehearsal, failure recovery, log privacy, storage alerts, and a synthetic end-to-end workflow through the gateway.

## Private Cloud VM Conversion

A private VM follows the private-server playbook plus provider-specific controls:

- firewall/security-group rules that expose only the authenticated gateway;
- persistent volume choice and backup retention;
- instance replacement and restore procedure;
- provider identity/secrets integration where appropriate;
- cost and storage monitoring;
- documented responsibility for OS and dependency updates.

Avoid network filesystems for SQLite unless their locking/durability behavior has been proven for the exact service. Keep the database on persistent local block storage and test restoration into a replacement VM.

## Public-Service Boundary

Do not expose the current app to unknown users by changing a host binding or proxy rule. A public service requires a separate design and security workstream covering at least CSRF, login throttling, secure first administration, authorization/tenant isolation, deletion and recovery, audit logging, encryption and secret management, dependency scanning, concurrency/load, abuse response, privacy/legal obligations, and incident operations.

If the requested audience is untrusted or public, pause deployment and present that expanded scope before making the service reachable.

## Verification and Completion

Use evidence labels precisely:

- **Verified:** performed successfully on the named target from a clean copy, with results recorded.
- **Implemented but unverified:** target-specific code/config exists, but its clean run was not completed.
- **Experimental:** a prototype path exists and further implementation/debugging is expected.
- **Unsupported:** known gaps make the requested exposure unsafe or outside scope.

At completion, record the target brief, source commit, build inputs, artifact hash where applicable, persistent paths, access boundary, backup/restore result, clean-start result, synthetic workflow result, private-artifact scan, and remaining owner-run operations. Reading code is not verification; performing and recording the target check is.
