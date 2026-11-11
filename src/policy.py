"""Load a spending policy from YAML."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import yaml

DEFAULT_POLICY_PATH = Path(__file__).resolve().parent.parent / "policies" / "default.yaml"


# This is the loaded policy. Caps are in pence so the checker can compare whole numbers.
@dataclass(frozen=True)
class Policy:
    max_per_order_pence: int
    max_per_period_pence: int
    period_days: int
    allowed_merchants: frozenset[str]
    blocked_categories: frozenset[str]
    duplicate_window_hours: int

    def merchant_allowed(self, merchant: str) -> bool:
        # An empty list allows every merchant. Otherwise the name has to match, ignoring case.
        if not self.allowed_merchants:
            return True
        return merchant.casefold() in {name.casefold() for name in self.allowed_merchants}

    def category_blocked(self, category: str) -> bool:
        # Alcohol is the blocked category in the demo policy. An empty list blocks nothing.
        return category.casefold() in {name.casefold() for name in self.blocked_categories}


# This reads policies/default.yaml and refuses a missing or non-positive cap.
def load_policy(path: Path = DEFAULT_POLICY_PATH) -> Policy:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{path} must be a mapping")
    try:
        period_days = int(raw["period_days"])
        window_hours = int(raw["duplicate_window_hours"])
        order_cap = _pence(raw["max_per_order_gbp"])
        period_cap = _pence(raw["max_per_period_gbp"])
    except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
        raise ValueError(f"{path} is missing a cap, period_days, or duplicate_window_hours") from exc
    if period_days < 1 or window_hours < 1 or order_cap < 1 or period_cap < 1:
        raise ValueError(f"{path} caps and windows must be positive")
    merchants = raw.get("allowed_merchants") or []
    categories = raw.get("blocked_categories") or []
    if not isinstance(merchants, list) or not isinstance(categories, list):
        raise ValueError(f"{path} merchant and category lists must be lists")
    return Policy(
        max_per_order_pence=order_cap,
        max_per_period_pence=period_cap,
        period_days=period_days,
        allowed_merchants=frozenset(str(item).strip() for item in merchants if str(item).strip()),
        blocked_categories=frozenset(str(item).strip() for item in categories if str(item).strip()),
        duplicate_window_hours=window_hours,
    )


# This turns a pounds string such as "200.00" into 20000 pence.
def _pence(value: object) -> int:
    amount = Decimal(str(value)).quantize(Decimal("0.01"))
    return int(amount * 100)
