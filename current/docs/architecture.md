# Finance App Architecture Notes

This cleanup keeps the app as a Flask application backed by per-user SQLite
databases. The intent is to make the existing behavior easier to maintain, not
to introduce a new framework or data model.

## Boundaries

- `app/blueprints/` owns HTTP concerns: route methods, request/form parsing,
  flash/redirect/render decisions, and wiring concrete repositories or services
  into route modules.
- `app/services/` owns cross-table workflow logic that should be testable
  without a request context, such as transaction posting and statement import
  commit behavior.
- `app/repositories/` owns direct SQL for a single domain. Repository functions
  should stay close to table shape and avoid request-specific behavior.
- `app/db.py` owns connection selection, unit-of-work helpers, user database
  initialization, and schema migration/repair helpers.
- Templates and static assets should keep presentation behavior local, calling
  server routes and JSON endpoints rather than duplicating persistence rules.

## Decisions From Cleanup

- User databases are initialized from the configured template schema without
  copying template data.
- Multi-step import review is split across review, commit, and small JSON API
  route modules, all registered under the `imports` blueprint.
- Transaction writes that affect multiple rows should go through service methods
  and `unit_of_work()` so transaction rows and splits stay consistent.
- Dev database inspection routes remain gated by configuration and should not
  be treated as production routes.
