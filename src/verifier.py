"""Check a proposed order against a spending policy with Z3.

A rule passes when its facts and its policy constraints are satisfiable
together. SAT means the order is allowed. UNSAT means this rule failed, and
the counterexample is the concrete values that cannot all be true.

Money is integer pence inside the solver so a cap of £200.00 is exact.
"""

from __future__ import annotations

import re
import zlib
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

import z3

from src.exprparse import evaluate_calculation, extract_arithmetic
from src.policy import Policy

_TOTAL_LINE = re.compile(r"(?i)\b(?:order\s+)?total\s*=\s*(\d+(?:\.\d{1,2})?)")


def verify(
    policy: Policy,
    order: dict[str, Any],
    graph: dict[str, Any],
    *,
    history: list[dict[str, Any]] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    spent = history or []
    moment = _as_utc(now or datetime.now(timezone.utc))
    failures = []
    for check in (
        _max_per_order,
        _max_per_period,
        _allowed_merchants,
        _blocked_categories,
        _no_duplicates,
        _arithmetic_consistency,
        _goal_grounding,
    ):
        failure = check(policy, order, graph, spent, moment)
        if failure is not None:
            failures.append(failure)
    return {"passed": not failures, "failures": failures}


def _max_per_order(policy: Policy, order: dict, graph: dict, history: list, now: datetime) -> dict | None:
    total = _pence(order["total_gbp"])
    order_total = z3.Int("order_total_pence")
    # The order costs `total`, and the policy says that cost is at most the cap.
    if _holds([order_total == total, order_total <= policy.max_per_order_pence]):
        return None
    cap = _pounds(policy.max_per_order_pence)
    actual = _pounds(total)
    return _failure(
        "max_per_order",
        f"This order is £{actual}, which is above the £{cap} cap.",
        {"order_total_gbp": actual, "cap_gbp": cap},
        _purchase_ids(order),
    )


def _max_per_period(policy: Policy, order: dict, graph: dict, history: list, now: datetime) -> dict | None:
    prior = sum(
        (_pence(row["amount_gbp"]) for row in history if _in_days(row, now, policy.period_days)),
        0,
    )
    order_pence = _pence(order["total_gbp"])
    spent = z3.Int("spent_in_window_pence")
    this_order = z3.Int("this_order_pence")
    if _holds(
        [
            spent == prior,
            this_order == order_pence,
            spent + this_order <= policy.max_per_period_pence,
        ]
    ):
        return None
    combined = prior + order_pence
    return _failure(
        "max_per_period",
        (
            f"Spending in the last {policy.period_days} days would be £{_pounds(combined)}, "
            f"above the £{_pounds(policy.max_per_period_pence)} cap. "
            f"This order is £{_pounds(order_pence)} and earlier spend in the window is £{_pounds(prior)}."
        ),
        {
            "window_days": policy.period_days,
            "prior_gbp": _pounds(prior),
            "order_total_gbp": _pounds(order_pence),
            "combined_gbp": _pounds(combined),
            "cap_gbp": _pounds(policy.max_per_period_pence),
        },
        _purchase_ids(order),
    )


def _allowed_merchants(policy: Policy, order: dict, graph: dict, history: list, now: datetime) -> dict | None:
    if not policy.allowed_merchants:
        return None
    allowed_codes = [_code(name) for name in policy.allowed_merchants]
    bad = []
    for item in order.get("items") or []:
        merchant = str(item.get("merchant") or "")
        code = z3.Int("merchant_code")
        # A fresh solver per line, so the name merchant_code is not reused inside one solver.
        allowed = z3.Or(*(code == value for value in allowed_codes))
        if not _holds([code == _code(merchant), allowed]):
            bad.append(item)
    if not bad:
        return None
    names = sorted({str(item.get("merchant") or "") for item in bad})
    listed = ", ".join(names)
    return _failure(
        "allowed_merchants",
        f"{listed} is not an allowed merchant.",
        {"merchants": names, "allowed_merchants": sorted(policy.allowed_merchants)},
        [_purchase_id(item) for item in bad],
    )


def _blocked_categories(policy: Policy, order: dict, graph: dict, history: list, now: datetime) -> dict | None:
    if not policy.blocked_categories:
        return None
    blocked_codes = [_code(name) for name in policy.blocked_categories]
    bad = []
    for item in order.get("items") or []:
        category = str(item.get("category") or "")
        code = z3.Int("category_code")
        blocked = z3.Or(*(code == value for value in blocked_codes))
        # The line is allowed when its category is not one of the blocked codes.
        if not _holds([code == _code(category), z3.Not(blocked)]):
            bad.append(item)
    if not bad:
        return None
    categories = sorted({str(item.get("category") or "") for item in bad})
    skus = ", ".join(str(item.get("sku") or "") for item in bad)
    return _failure(
        "blocked_categories",
        f"{', '.join(categories)} is a blocked category ({skus}).",
        {"categories": categories, "skus": [str(item.get("sku") or "") for item in bad]},
        [_purchase_id(item) for item in bad],
    )


def _no_duplicates(policy: Policy, order: dict, graph: dict, history: list, now: datetime) -> dict | None:
    bad = []
    examples = []
    for item in order.get("items") or []:
        previous = _matching_purchase(item, history, now, policy.duplicate_window_hours)
        if previous is None:
            continue
        # The row above is the fact that a duplicate exists. The policy says it must not.
        observed = z3.Bool("observed_duplicate")
        if _holds([observed == z3.BoolVal(True), observed == z3.BoolVal(False)]):
            continue
        bad.append(item)
        examples.append(
            {
                "sku": item.get("sku"),
                "merchant": item.get("merchant"),
                "previous_purchased_at": _as_utc(previous["purchased_at"]).isoformat(),
                "window_hours": policy.duplicate_window_hours,
            }
        )
    if not bad:
        return None
    first = examples[0]
    return _failure(
        "no_duplicates",
        (
            f"{first['sku']} from {first['merchant']} was already bought at "
            f"{first['previous_purchased_at']}, inside the {policy.duplicate_window_hours} hour window."
        ),
        {"duplicates": examples},
        [_purchase_id(item) for item in bad],
    )


def _arithmetic_consistency(policy: Policy, order: dict, graph: dict, history: list, now: datetime) -> dict | None:
    reasoning = str(order.get("reasoning") or "")
    for line in reasoning.splitlines():
        parsed = extract_arithmetic(line)
        if parsed is None:
            continue
        pair = evaluate_calculation(parsed)
        if pair is None:
            continue
        computed, claimed = pair
        if _pence_equal(computed, claimed):
            continue
        return _failure(
            "arithmetic_consistency",
            (
                f"The reasoning claims {line.strip()}, but that sum equals "
                f"£{_pounds(_pence(computed))}."
            ),
            {
                "line": line.strip(),
                "claimed_gbp": _pounds(_pence(claimed)),
                "computed_gbp": _pounds(_pence(computed)),
            },
            _nodes_for_line(line, order),
        )

    stated = _stated_total(reasoning)
    order_total = Decimal(str(order["total_gbp"]))
    if stated is None:
        stated_flag = z3.Bool("total_was_stated")
        if not _holds([stated_flag == z3.BoolVal(False), stated_flag == z3.BoolVal(True)]):
            return _failure(
                "arithmetic_consistency",
                "The reasoning does not state an order total.",
                {"order_total_gbp": _pounds(_pence(order_total)), "reasoning_total_gbp": None},
                _purchase_ids(order),
            )
        return None
    if _pence_equal(stated, order_total):
        return None
    return _failure(
        "arithmetic_consistency",
        (
            f"The reasoning total is £{_pounds(_pence(stated))}, "
            f"but the order total is £{_pounds(_pence(order_total))}."
        ),
        {
            "reasoning_total_gbp": _pounds(_pence(stated)),
            "order_total_gbp": _pounds(_pence(order_total)),
        },
        _purchase_ids(order),
    )


def _goal_grounding(policy: Policy, order: dict, graph: dict, history: list, now: datetime) -> dict | None:
    purchase_ids = _purchase_ids(order)
    if not purchase_ids:
        return None
    ungrounded = [purchase_id for purchase_id in purchase_ids if not _reaches_goal(graph, purchase_id)]
    if not ungrounded:
        return None
    listed = ", ".join(ungrounded)
    return _failure(
        "goal_grounding",
        f"{listed} has no support path back to the user's goal.",
        {"purchase_ids": ungrounded},
        ungrounded,
    )


def _reaches_goal(graph: dict, purchase_id: str) -> bool:
    """True when Z3 can put this purchase on a supported_by path that starts at a goal.

    ``reaches`` grows one hop per step. A purchase that only points at a listing,
    and that listing does not point at the goal, stays false. Requiring it to be
    true is then unsatisfiable.
    """
    nodes = [str(node.get("id")) for node in graph.get("nodes") or [] if node.get("id")]
    if purchase_id not in nodes:
        return False
    goals = {str(node.get("id")) for node in graph.get("nodes") or [] if node.get("type") == "goal"}
    edges = [
        (str(edge.get("source")), str(edge.get("target")))
        for edge in graph.get("edges") or []
        if edge.get("relation") == "supported_by" and edge.get("source") and edge.get("target")
    ]
    steps = max(len(nodes), 1)
    reach = [{node_id: z3.Bool(f"r{step}_{_safe(node_id)}") for node_id in nodes} for step in range(steps)]
    constraints = []
    for node_id in nodes:
        constraints.append(reach[0][node_id] == (node_id in goals))
    for step in range(steps - 1):
        for node_id in nodes:
            via = [reach[step][target] for source, target in edges if source == node_id and target in reach[step]]
            if via:
                constraints.append(reach[step + 1][node_id] == z3.Or(reach[step][node_id], *via))
            else:
                constraints.append(reach[step + 1][node_id] == reach[step][node_id])
    constraints.append(reach[-1][purchase_id] == z3.BoolVal(True))
    return _holds(constraints)


def _holds(constraints: list) -> bool:
    solver = z3.Solver()
    for constraint in constraints:
        solver.add(constraint)
    return solver.check() == z3.sat


def _pence_equal(left: Decimal, right: Decimal) -> bool:
    claimed = z3.Int("claimed_pence")
    computed = z3.Int("computed_pence")
    return _holds([claimed == _pence(left), computed == _pence(right), claimed == computed])


def _matching_purchase(item: dict, history: list, now: datetime, hours: int) -> dict | None:
    sku = str(item.get("sku") or "")
    merchant = str(item.get("merchant") or "").casefold()
    for row in history:
        if str(row.get("sku") or "") != sku:
            continue
        if str(row.get("merchant") or "").casefold() != merchant:
            continue
        if _in_hours(row, now, hours):
            return row
    return None


def _in_days(row: dict, now: datetime, days: int) -> bool:
    when = _as_utc(row.get("purchased_at"))
    return now - timedelta(days=days) <= when <= now


def _in_hours(row: dict, now: datetime, hours: int) -> bool:
    when = _as_utc(row.get("purchased_at"))
    return now - timedelta(hours=hours) <= when <= now


def _stated_total(reasoning: str) -> Decimal | None:
    found: Decimal | None = None
    for line in reasoning.splitlines():
        cleaned = line.replace("£", "").replace(",", "").strip()
        if cleaned.endswith("."):
            cleaned = cleaned[:-1].strip()
        match = _TOTAL_LINE.search(cleaned)
        if match:
            found = Decimal(match.group(1))
    return found


def _nodes_for_line(line: str, order: dict) -> list[str]:
    matched = []
    for item in order.get("items") or []:
        sku = str(item.get("sku") or "")
        prices = {str(item.get("unit_price_gbp") or ""), str(item.get("line_total_gbp") or "")}
        if sku and sku in line or any(price and price in line for price in prices):
            matched.append(_purchase_id(item))
    return matched or _purchase_ids(order)


def _purchase_ids(order: dict) -> list[str]:
    return [_purchase_id(item) for item in order.get("items") or [] if item.get("sku")]


def _purchase_id(item: dict) -> str:
    return f"purchase:{item.get('sku')}"


def _failure(rule_id: str, explanation: str, counterexample: dict, node_ids: list[str]) -> dict:
    return {
        "rule_id": rule_id,
        "explanation": explanation,
        "counterexample": counterexample,
        "node_ids": list(dict.fromkeys(node_ids)),
    }


def _pence(value: object) -> int:
    amount = Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return int(amount * 100)


def _pounds(pence: int) -> str:
    sign = "-" if pence < 0 else ""
    absolute = abs(pence)
    return f"{sign}{absolute // 100}.{absolute % 100:02d}"


def _code(text: str) -> int:
    return zlib.crc32(text.casefold().encode()) & 0x7FFFFFFF


def _safe(node_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", node_id)


def _as_utc(value: object) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
