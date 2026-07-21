"""Retired database seeder.

This filename is kept only so old notes or agent guesses fail safely. The
former script deleted ledger rows before inserting sample records, which is
not an acceptable entry point for a local-first finance application.
"""

from __future__ import annotations

import sys


MESSAGE = """ERROR: seed_db.py is retired and will not modify any database.

To create a new fictional demo workspace, run this from the current/ directory:

    python scripts/bootstrap_workspace.py --data-dir .local/agent-demo --profile demo

The supported bootstrap is create-only and refuses an existing destination.
Never use a real financial database for demos or tests.
"""


def main() -> int:
    print(MESSAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
