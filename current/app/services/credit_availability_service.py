def get_credit_budget_metrics(
    *,
    credit_limit_cents: int,
    account_total_cents: int,
    envelope_balance_cents: int,
) -> dict[str, int]:
    """
    Return the dashboard metrics used for a credit-card account.

    Credit-card account totals are negative when money is owed and positive
    when the card has an overpaid credit balance. Available-to-allocate keeps
    the existing FIN-028 behavior: limit minus owed balance minus card envelope
    balances, floored at zero.
    """
    account_total_cents = int(account_total_cents or 0)
    credit_limit_cents = int(credit_limit_cents or 0)
    envelope_balance_cents = int(envelope_balance_cents or 0)

    owed_total_cents = -account_total_cents if account_total_cents < 0 else 0
    credit_balance_cents = account_total_cents if account_total_cents > 0 else 0
    available_to_allocate_cents = max(
        credit_limit_cents - (owed_total_cents + envelope_balance_cents),
        0,
    )

    return {
        "owed_total_cents": owed_total_cents,
        "credit_balance_cents": credit_balance_cents,
        "available_to_allocate_cents": available_to_allocate_cents,
    }
