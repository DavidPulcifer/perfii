from __future__ import annotations

import hashlib
import json
from collections import OrderedDict


class SavingsPlannerError(ValueError):
    pass


def basis_points_label(value: int) -> str:
    value = int(value or 0)
    whole, fraction = divmod(value, 100)
    return f"{whole}.{fraction:02d}".rstrip("0").rstrip(".")


def long_term_share_basis_points(
    current_accessible_cents: int,
    accessible_target_cents: int,
    *,
    has_long_term_destination: bool,
) -> int:
    """Return the hard-cutoff share routed to long-term savings.

    The full contribution stays accessible while the selected accessible
    account-and-envelope balance is below target. Once that opening balance is
    at or above target, the full contribution goes to the configured long-term
    destination. A contribution that crosses the target is not split; the next
    preview observes the reached target and switches destinations.
    """
    if not has_long_term_destination:
        return 0
    target = int(accessible_target_cents or 0)
    if target <= 0:
        raise SavingsPlannerError(
            "An accessible savings target greater than $0 is required when a long-term destination is configured."
        )
    current = max(0, int(current_accessible_cents or 0))
    return 10000 if current >= target else 0


def _round_basis_points(cents: int, basis_points: int) -> int:
    return (int(cents) * int(basis_points) + 5000) // 10000


def _allocate_contributions(take_home_cents: int, rules: list[dict]) -> list[int]:
    """Allocate percentage contributions without letting row rounding overspend pay."""
    take_home_cents = int(take_home_cents)
    total_basis_points = sum(int(rule["contribution_basis_points"]) for rule in rules)
    target_total = _round_basis_points(take_home_cents, total_basis_points)

    allocations: list[int] = []
    remainders: list[tuple[int, int]] = []
    for index, rule in enumerate(rules):
        numerator = take_home_cents * int(rule["contribution_basis_points"])
        allocations.append(numerator // 10000)
        remainders.append((numerator % 10000, index))

    cents_to_distribute = target_total - sum(allocations)
    for _remainder, index in sorted(remainders, key=lambda item: (-item[0], item[1]))[:cents_to_distribute]:
        allocations[index] += 1
    return allocations


def _require_account(accounts_by_id: dict[int, dict], account_id: int, label: str) -> dict:
    account = accounts_by_id.get(int(account_id))
    if not account:
        raise SavingsPlannerError(f"Choose a valid {label} account.")
    if account.get("account_type") != "bank":
        raise SavingsPlannerError(f"{label.capitalize()} must be a cash or savings account.")
    return account


def _require_compatible_envelope(
    envelopes_by_id: dict[int, dict],
    envelope_id: int,
    account_id: int,
    label: str,
) -> dict:
    envelope = envelopes_by_id.get(int(envelope_id))
    if not envelope or envelope.get("archived_at"):
        raise SavingsPlannerError(f"Choose an active {label} envelope.")
    locked_account_id = envelope.get("locked_account_id")
    if locked_account_id is not None and int(locked_account_id) != int(account_id):
        raise SavingsPlannerError(
            f"The {label} envelope is locked to a different account."
        )
    return envelope


def validate_plan(
    plan: dict | None,
    *,
    accounts_by_id: dict[int, dict],
    envelopes_by_id: dict[int, dict],
) -> None:
    if not plan:
        raise SavingsPlannerError("Set up the paycheck source before creating a savings preview.")
    source_account_id = plan.get("source_account_id")
    source_envelope_id = plan.get("source_envelope_id")
    if not source_account_id or not source_envelope_id:
        raise SavingsPlannerError("Choose both a paycheck source account and source envelope.")
    _require_account(accounts_by_id, int(source_account_id), "paycheck source")
    _require_compatible_envelope(
        envelopes_by_id,
        int(source_envelope_id),
        int(source_account_id),
        "paycheck source",
    )


def validate_rule(
    rule: dict,
    *,
    source_account_id: int | None,
    accounts_by_id: dict[int, dict],
    envelopes_by_id: dict[int, dict],
) -> None:
    name = str(rule.get("name") or "").strip()
    if not name:
        raise SavingsPlannerError("Each savings rule needs a name.")

    rate = int(rule.get("contribution_basis_points") or 0)
    if rate <= 0 or rate > 10000:
        raise SavingsPlannerError("Each savings percentage must be greater than 0% and no more than 100%.")

    accessible_account_id = int(rule.get("accessible_account_id") or 0)
    accessible_envelope_id = int(rule.get("accessible_envelope_id") or 0)
    _require_account(accounts_by_id, accessible_account_id, "accessible savings")
    _require_compatible_envelope(
        envelopes_by_id,
        accessible_envelope_id,
        accessible_account_id,
        "accessible savings",
    )
    if source_account_id and accessible_account_id == int(source_account_id):
        raise SavingsPlannerError("The accessible savings account must differ from the paycheck source account.")

    long_account = rule.get("long_term_account_id")
    long_envelope = rule.get("long_term_envelope_id")
    if bool(long_account) != bool(long_envelope):
        raise SavingsPlannerError("Choose both a long-term account and envelope, or leave both blank.")
    if long_account and long_envelope:
        long_account_id = int(long_account)
        _require_account(accounts_by_id, long_account_id, "long-term savings")
        _require_compatible_envelope(
            envelopes_by_id,
            int(long_envelope),
            long_account_id,
            "long-term savings",
        )
        if source_account_id and long_account_id == int(source_account_id):
            raise SavingsPlannerError("The long-term savings account must differ from the paycheck source account.")
        if long_account_id == accessible_account_id:
            raise SavingsPlannerError("Accessible and long-term savings must use different accounts.")
        if int(rule.get("accessible_target_cents") or 0) <= 0:
            raise SavingsPlannerError(
                "Enter an accessible savings target greater than $0 when using a long-term destination."
            )
    elif int(rule.get("accessible_target_cents") or 0) < 0:
        raise SavingsPlannerError("The accessible savings target cannot be negative.")


def validate_configuration(
    plan: dict | None,
    rules: list[dict],
    *,
    accounts_by_id: dict[int, dict],
    envelopes_by_id: dict[int, dict],
) -> list[dict]:
    validate_plan(plan, accounts_by_id=accounts_by_id, envelopes_by_id=envelopes_by_id)
    enabled_rules = [rule for rule in rules if int(rule.get("enabled", 1)) == 1]
    if not enabled_rules:
        raise SavingsPlannerError("Create and enable at least one savings rule.")
    total_basis_points = sum(int(rule.get("contribution_basis_points") or 0) for rule in enabled_rules)
    if total_basis_points > 10000:
        raise SavingsPlannerError("Enabled savings rules cannot total more than 100% of take-home pay.")
    for rule in enabled_rules:
        validate_rule(
            rule,
            source_account_id=int(plan["source_account_id"]),
            accounts_by_id=accounts_by_id,
            envelopes_by_id=envelopes_by_id,
        )
    return enabled_rules


def validate_enabled_rule_total(rules: list[dict]) -> None:
    enabled_rules = [rule for rule in rules if int(rule.get("enabled", 1)) == 1]
    total_basis_points = sum(int(rule.get("contribution_basis_points") or 0) for rule in enabled_rules)
    if total_basis_points > 10000:
        raise SavingsPlannerError("Enabled savings rules cannot total more than 100% of take-home pay.")


def validate_recording_recommendation(
    recommendation: dict,
    *,
    plan: dict,
    accounts_by_id: dict[int, dict],
    envelopes_by_id: dict[int, dict],
) -> None:
    """Fail closed before turning a signed preview group into a transfer."""
    source_account_id = int(recommendation.get("source_account_id") or 0)
    source_envelope_id = int(recommendation.get("source_envelope_id") or 0)
    destination_account_id = int(recommendation.get("destination_account_id") or 0)
    if source_account_id != int(plan.get("source_account_id") or 0):
        raise SavingsPlannerError("The paycheck source changed after this preview was created.")
    if source_envelope_id != int(plan.get("source_envelope_id") or 0):
        raise SavingsPlannerError("The paycheck source envelope changed after this preview was created.")
    if source_account_id == destination_account_id:
        raise SavingsPlannerError("A savings transfer cannot use the same source and destination account.")
    _require_account(accounts_by_id, source_account_id, "paycheck source")
    _require_account(accounts_by_id, destination_account_id, "savings destination")
    _require_compatible_envelope(
        envelopes_by_id,
        source_envelope_id,
        source_account_id,
        "paycheck source",
    )

    amount_cents = int(recommendation.get("amount_cents") or 0)
    if amount_cents <= 0:
        raise SavingsPlannerError("The reviewed savings transfer amount must be greater than $0.")
    destination_total = 0
    for split in recommendation.get("destination_splits") or []:
        envelope_id = int(split.get("envelope_id") or 0)
        split_cents = int(split.get("amount_cents") or 0)
        if split_cents <= 0:
            raise SavingsPlannerError("Reviewed destination splits must be positive.")
        _require_compatible_envelope(
            envelopes_by_id,
            envelope_id,
            destination_account_id,
            "savings destination",
        )
        destination_total += split_cents
    if destination_total != amount_cents:
        raise SavingsPlannerError("Reviewed destination splits no longer match the transfer total.")


def _recording_signature(recommendation: dict) -> tuple:
    destination_splits = tuple(
        sorted(
            (
                int(split.get("envelope_id") or 0),
                int(split.get("amount_cents") or 0),
            )
            for split in recommendation.get("destination_splits") or []
        )
    )
    return (
        int(recommendation.get("source_account_id") or 0),
        int(recommendation.get("source_envelope_id") or 0),
        int(recommendation.get("destination_account_id") or 0),
        int(recommendation.get("amount_cents") or 0),
        destination_splits,
        tuple(sorted(str(value) for value in recommendation.get("destination_kinds") or [])),
        tuple(sorted(str(value) for value in recommendation.get("goal_names") or [])),
    )


def validate_recommendation_freshness(
    reviewed_recommendation: dict,
    *,
    current_preview: dict,
) -> None:
    """Reject a signed recommendation when current balances change its transfer."""
    destination_account_id = int(reviewed_recommendation.get("destination_account_id") or 0)
    current_recommendation = next(
        (
            recommendation
            for recommendation in current_preview.get("recommendations") or []
            if int(recommendation.get("destination_account_id") or 0) == destination_account_id
        ),
        None,
    )
    if (
        current_recommendation is None
        or _recording_signature(current_recommendation)
        != _recording_signature(reviewed_recommendation)
    ):
        raise SavingsPlannerError(
            "Savings balances changed after this preview, so the reviewed transfer is no longer current. "
            "Create a fresh preview before recording."
        )


def configuration_fingerprint(plan: dict, rules: list[dict]) -> str:
    plan_payload = {
        "name": plan.get("name"),
        "source_account_id": plan.get("source_account_id"),
        "source_envelope_id": plan.get("source_envelope_id"),
    }
    rule_fields = (
        "id",
        "name",
        "contribution_basis_points",
        "accessible_account_id",
        "accessible_envelope_id",
        "long_term_account_id",
        "long_term_envelope_id",
        "accessible_target_cents",
        "enabled",
        "display_order",
    )
    rules_payload = [
        {field: rule.get(field) for field in rule_fields}
        for rule in sorted(rules, key=lambda item: (int(item.get("display_order") or 0), int(item.get("id") or 0)))
    ]
    raw = json.dumps(
        {"plan": plan_payload, "rules": rules_payload},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def calculate_preview(
    *,
    take_home_cents: int,
    posted_at: str,
    plan: dict | None,
    rules: list[dict],
    accounts_by_id: dict[int, dict],
    envelopes_by_id: dict[int, dict],
    account_envelope_balances: dict[tuple[int, int], int],
) -> dict:
    take_home_cents = int(take_home_cents)
    if take_home_cents <= 0:
        raise SavingsPlannerError("Take-home pay must be greater than $0.")

    enabled_rules = validate_configuration(
        plan,
        rules,
        accounts_by_id=accounts_by_id,
        envelopes_by_id=envelopes_by_id,
    )
    assert plan is not None
    contributions = _allocate_contributions(take_home_cents, enabled_rules)
    contribution_rows: list[dict] = []
    grouped: OrderedDict[int, dict] = OrderedDict()

    def add_destination(
        *,
        account_id: int,
        envelope_id: int,
        amount_cents: int,
        goal_name: str,
        destination_kind: str,
    ) -> None:
        if amount_cents <= 0:
            return
        account = accounts_by_id[account_id]
        envelope = envelopes_by_id[envelope_id]
        group = grouped.setdefault(
            account_id,
            {
                "destination_account_id": account_id,
                "destination_account_name": account["name"],
                "amount_cents": 0,
                "splits_by_envelope": OrderedDict(),
                "goal_names": [],
                "destination_kinds": [],
            },
        )
        group["amount_cents"] += int(amount_cents)
        split = group["splits_by_envelope"].setdefault(
            envelope_id,
            {
                "envelope_id": envelope_id,
                "envelope_name": envelope["name"],
                "amount_cents": 0,
            },
        )
        split["amount_cents"] += int(amount_cents)
        if goal_name not in group["goal_names"]:
            group["goal_names"].append(goal_name)
        if destination_kind not in group["destination_kinds"]:
            group["destination_kinds"].append(destination_kind)

    for rule, contribution_cents in zip(enabled_rules, contributions):
        accessible_account_id = int(rule["accessible_account_id"])
        accessible_envelope_id = int(rule["accessible_envelope_id"])
        long_account_id = int(rule["long_term_account_id"]) if rule.get("long_term_account_id") else None
        long_envelope_id = int(rule["long_term_envelope_id"]) if rule.get("long_term_envelope_id") else None
        has_long_term = bool(long_account_id and long_envelope_id)
        current_accessible_cents = int(
            account_envelope_balances.get((accessible_account_id, accessible_envelope_id), 0) or 0
        )
        target_cents = int(rule.get("accessible_target_cents") or 0)
        long_share = long_term_share_basis_points(
            current_accessible_cents,
            target_cents,
            has_long_term_destination=has_long_term,
        )
        long_term_cents = _round_basis_points(contribution_cents, long_share)
        accessible_cents = contribution_cents - long_term_cents
        progress_basis_points = (
            min(10000, max(0, (max(0, current_accessible_cents) * 10000) // target_cents))
            if target_cents > 0
            else 0
        )

        row = {
            "rule_id": int(rule["id"]),
            "name": rule["name"],
            "contribution_basis_points": int(rule["contribution_basis_points"]),
            "contribution_percent": basis_points_label(int(rule["contribution_basis_points"])),
            "contribution_cents": contribution_cents,
            "current_accessible_cents": current_accessible_cents,
            "accessible_target_cents": target_cents,
            "target_progress_basis_points": progress_basis_points,
            "target_progress_percent": basis_points_label(progress_basis_points),
            "long_term_share_basis_points": long_share,
            "long_term_share_percent": basis_points_label(long_share),
            "accessible_cents": accessible_cents,
            "accessible_account_id": accessible_account_id,
            "accessible_account_name": accounts_by_id[accessible_account_id]["name"],
            "accessible_envelope_id": accessible_envelope_id,
            "accessible_envelope_name": envelopes_by_id[accessible_envelope_id]["name"],
            "long_term_cents": long_term_cents,
            "long_term_account_id": long_account_id,
            "long_term_account_name": accounts_by_id[long_account_id]["name"] if long_account_id else None,
            "long_term_envelope_id": long_envelope_id,
            "long_term_envelope_name": envelopes_by_id[long_envelope_id]["name"] if long_envelope_id else None,
        }
        contribution_rows.append(row)
        add_destination(
            account_id=accessible_account_id,
            envelope_id=accessible_envelope_id,
            amount_cents=accessible_cents,
            goal_name=rule["name"],
            destination_kind="Accessible savings",
        )
        if long_account_id and long_envelope_id:
            add_destination(
                account_id=long_account_id,
                envelope_id=long_envelope_id,
                amount_cents=long_term_cents,
                goal_name=rule["name"],
                destination_kind="Long-term savings",
            )

    source_account_id = int(plan["source_account_id"])
    source_envelope_id = int(plan["source_envelope_id"])
    recommendations: list[dict] = []
    for group_index, group in enumerate(grouped.values()):
        goal_summary = ", ".join(group["goal_names"])
        recommendations.append(
            {
                "group_index": group_index,
                "source_account_id": source_account_id,
                "source_account_name": accounts_by_id[source_account_id]["name"],
                "source_envelope_id": source_envelope_id,
                "source_envelope_name": envelopes_by_id[source_envelope_id]["name"],
                "destination_account_id": int(group["destination_account_id"]),
                "destination_account_name": group["destination_account_name"],
                "amount_cents": int(group["amount_cents"]),
                "destination_splits": list(group["splits_by_envelope"].values()),
                "goal_names": group["goal_names"],
                "destination_kinds": group["destination_kinds"],
                "memo": f"Pay Yourself First: {goal_summary}"[:250],
            }
        )

    total_contribution_cents = sum(contributions)
    total_basis_points = sum(int(rule["contribution_basis_points"]) for rule in enabled_rules)
    return {
        "plan_name": plan.get("name") or "Pay Yourself First",
        "take_home_cents": take_home_cents,
        "posted_at": posted_at,
        "total_contribution_cents": total_contribution_cents,
        "remaining_pay_cents": take_home_cents - total_contribution_cents,
        "total_basis_points": total_basis_points,
        "total_percent": basis_points_label(total_basis_points),
        "source_account_id": source_account_id,
        "source_account_name": accounts_by_id[source_account_id]["name"],
        "source_envelope_id": source_envelope_id,
        "source_envelope_name": envelopes_by_id[source_envelope_id]["name"],
        "contributions": contribution_rows,
        "recommendations": recommendations,
    }
