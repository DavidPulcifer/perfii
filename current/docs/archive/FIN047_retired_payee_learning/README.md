# FIN-047 retired payee-learning predictor archive

This folder archives the legacy payee-learning import predictor retired by FIN-046/FIN-047.

The archived predictor is no longer live application code:

- `payee_learning_service.py.txt` is a text archive of the old service module, not importable app code.
- Live import review now uses FIN-045 adaptive prefills only.
- Live import commit no longer records payee aliases or envelope stats for this legacy predictor.
- Live schema setup no longer creates `payee_aliases` or `payee_envelope_stats`.

Historical table data is exported by `scripts/archive_retired_payee_learning.py` into a private runtime-data archive before those tables are dropped from active app databases.
