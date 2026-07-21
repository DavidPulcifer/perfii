# Historical Deployment Pointer

This project ships as a web application. This file intentionally no longer
contains the former owner-specific server commands because they included local
paths, service names, and database-copy steps that are unsafe to reuse for a
different person.

Coding agents adapting the project for local web, a private server, a private
cloud VM, or a desktop shell must begin with
[`docs/agent/DEPLOYMENT.md`](../docs/agent/DEPLOYMENT.md). That guide describes
the questions to ask, the supported web starting point, and the work and
verification required to convert the app for another hosting method.

Do not copy a database out of the source tree. New installations start from the
tracked `app/base_schema.sql` through the create-only workspace bootstrap.
Migration of an existing ledger is a separate, explicitly authorized data
operation with backup and recovery requirements.
