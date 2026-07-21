# Envelope App Modular Refactor

This app keeps the existing Flask, SQLite, and Jinja shape while separating
responsibilities into:

- `app/__init__.py` — application factory, config, blueprint registration
- `app/config.py` — configuration classes
- `app/db.py` — DB path, connection helpers, migrations/init
- `app/repositories/` — raw-SQL wrappers per domain (accounts, envelopes, transactions, etc.)
- `app/services/` — business logic orchestrating repositories (e.g., posting transactions)
- `app/blueprints/` — route handlers per domain
- `app/utils.py` — shared helpers (money formatting, parse utils, decorators)

See `docs/architecture.md` for the cleanup-phase boundaries and maintenance notes.
