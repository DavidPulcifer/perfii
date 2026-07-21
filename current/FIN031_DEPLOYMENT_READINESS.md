# FIN-031 Deployment Readiness

## Scope

- Adds bank and credit-card reconciliation workflow only.
- Reconciliation state remains in `reconciliation_sessions` and `reconciliation_items`.
- Close, reopen, and void do not mutate transaction amounts, dates, splits, FITIDs, or transfer links.
- Loan and investment accounts are display-safe but do not get a reconciliation workflow.
- Credit-card owed statement balances must be entered using the app signed convention: negative amounts.

## Validation Commands

```bash
python -m unittest tests.test_reconciliation_service tests.test_reconciliation_ui
python -m compileall app tests
```

## Manual Smoke Plan

1. Open a bank account page and start reconciliation from the account actions.
2. Enter statement date and statement balance, save with no selected transactions, and confirm validation/flash behavior.
3. Select transactions, save progress, leave the page, resume from the account page, and confirm selections persist.
4. Close a balanced reconciliation and confirm it appears in account reconciliation history.
5. Attempt to close an unbalanced reconciliation and confirm it remains editable with items unchanged.
6. Open history detail, confirm reconciled transaction rows are visible, then reopen and confirm rows become cleared/editable.
7. Void a session from detail/history and confirm the audit row remains visible as void.
8. On the transaction list, filter all/unreconciled/reconciled and confirm pagination links preserve the filter.
9. Try normal edit/delete on a reconciled transaction and on a transfer whose paired leg is reconciled; both should be blocked with a reopen message.
10. Confirm import review/manual matching flows still load and are only blocked if they try to mutate a reconciled transaction.

## Rollback Notes

- No deploy or service restart was performed for this phase.
- To roll back before deployment, revert the FIN-031 route/template/service/repository changes in the application branch.
- If already deployed, first stop user traffic, restore the previous application revision, then restart using the normal production procedure.
- Reconciliation audit tables can remain in place after rollback; older code does not depend on them. Do not delete audit rows unless a separate data-retention decision is made.
