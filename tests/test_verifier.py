"""One passing case and one failing case for every spending rule.

The checks are not wired to PayPal. These tests call verify() directly.
"""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.catalog import load_catalog
from src.extraction import build_graph
from src.ledger import Ledger
from src.policy import load_policy
from src.verifier import verify

NOW = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)
POLICY = load_policy()
CATALOG = load_catalog()


def _order(item: dict, *, total: str, reasoning: str) -> dict:
    return {"items": [item], "total_gbp": total, "reasoning": reasoning}


def _item(sku: str, *, quantity: int = 1, merchant: str | None = None, category: str | None = None, unit: str | None = None) -> dict:
    from decimal import Decimal

    product = CATALOG.get(sku)
    unit_price = Decimal(unit) if unit else product.price_gbp
    line_total = unit_price * quantity
    return {
        "sku": sku,
        "name": product.name if product else sku,
        "merchant": merchant or (product.merchant if product else ""),
        "category": category or (product.category if product else ""),
        "quantity": quantity,
        "unit_price_gbp": f"{unit_price:.2f}",
        "line_total_gbp": f"{line_total:.2f}",
    }


def _graph_for(task: str, item: dict, description: str | None = None) -> dict:
    product = CATALOG.get(item["sku"])
    text = description if description is not None else (product.description if product else "")
    trace = [
        {
            "tool": "get_product",
            "products": [{"sku": item["sku"], "name": item["name"], "description": text}],
        }
    ]
    return build_graph(task, trace, [item])


def _ids(result: dict) -> set[str]:
    return {failure["rule_id"] for failure in result["failures"]}


def _failure(result: dict, rule_id: str) -> dict:
    matches = [failure for failure in result["failures"] if failure["rule_id"] == rule_id]
    if len(matches) != 1:
        raise AssertionError(result)
    return matches[0]


class PolicyFileTests(unittest.TestCase):
    def test_default_policy_has_the_demo_caps(self):
        self.assertEqual(POLICY.max_per_order_pence, 20000)
        self.assertEqual(POLICY.max_per_period_pence, 50000)
        self.assertEqual(POLICY.period_days, 7)
        self.assertEqual(POLICY.duplicate_window_hours, 24)
        self.assertIn("alcohol", {name.casefold() for name in POLICY.blocked_categories})
        self.assertEqual(len(POLICY.allowed_merchants), 5)


class VerifierTests(unittest.TestCase):
    def test_a_notebook_within_the_caps_passes_every_rule(self):
        item = _item("NOTEBOOK-PACK")
        order = _order(item, total="12.50", reasoning="1 * 12.50 = 12.50\ntotal = 12.50")
        graph = _graph_for("Buy a notebook pack.", item)
        result = verify(POLICY, order, graph, history=[], now=NOW)
        self.assertTrue(result["passed"], result)
        self.assertEqual(result["failures"], [])

    def test_max_per_order_blocks_the_fifty_cable_basket(self):
        item = _item("USB-C-CABLE", quantity=50)
        order = _order(item, total="400.00", reasoning="50 * 8.00 = 400.00\ntotal = 400.00")
        graph = _graph_for("We need a USB-C cable for the new laptops.", item)
        result = verify(POLICY, order, graph, history=[], now=NOW)
        failure = _failure(result, "max_per_order")
        self.assertEqual(failure["counterexample"]["order_total_gbp"], "400.00")
        self.assertEqual(failure["counterexample"]["cap_gbp"], "200.00")
        self.assertEqual(failure["node_ids"], ["purchase:USB-C-CABLE"])
        self.assertIn("£400.00", failure["explanation"])

    def test_max_per_order_allows_a_basket_on_the_cap(self):
        item = _item("NOTEBOOK-PACK", quantity=16)
        # 16 * 12.50 = 200.00, which is not above the cap.
        order = _order(item, total="200.00", reasoning="16 * 12.50 = 200.00\ntotal = 200.00")
        graph = _graph_for("Buy notebook packs.", item, description="Pack of notebooks.")
        result = verify(POLICY, order, graph, history=[], now=NOW)
        self.assertNotIn("max_per_order", _ids(result))

    def test_max_per_period_counts_spend_inside_the_window_only(self):
        item = _item("CHAIR-TASK")
        order = _order(item, total="89.00", reasoning="1 * 89.00 = 89.00\ntotal = 89.00")
        graph = _graph_for("Buy a task chair.", item, description="One chair.")
        recent = {
            "sku": "LAPTOP-14",
            "merchant": "Northbeam IT",
            "amount_gbp": "450.00",
            "purchased_at": NOW - timedelta(days=6),
        }
        old = {
            "sku": "LAPTOP-14",
            "merchant": "Northbeam IT",
            "amount_gbp": "450.00",
            "purchased_at": NOW - timedelta(days=8),
        }
        over = verify(POLICY, order, graph, history=[recent], now=NOW)
        failure = _failure(over, "max_per_period")
        self.assertEqual(failure["counterexample"]["prior_gbp"], "450.00")
        self.assertEqual(failure["counterexample"]["combined_gbp"], "539.00")
        self.assertEqual(failure["counterexample"]["cap_gbp"], "500.00")
        self.assertIn("purchase:CHAIR-TASK", failure["node_ids"])

        under = verify(POLICY, order, graph, history=[old], now=NOW)
        self.assertNotIn("max_per_period", _ids(under))

    def test_allowed_merchants_rejects_a_shop_outside_the_list(self):
        item = _item("NOTEBOOK-PACK", merchant="Rogue Shop")
        order = _order(item, total="12.50", reasoning="1 * 12.50 = 12.50\ntotal = 12.50")
        graph = _graph_for("Buy a notebook pack.", item, description="A notebook.")
        result = verify(POLICY, order, graph, history=[], now=NOW)
        failure = _failure(result, "allowed_merchants")
        self.assertEqual(failure["counterexample"]["merchants"], ["Rogue Shop"])
        self.assertEqual(failure["node_ids"], ["purchase:NOTEBOOK-PACK"])

    def test_blocked_categories_rejects_alcohol(self):
        item = _item("WHISKY-GIFT")
        order = _order(item, total="48.00", reasoning="1 * 48.00 = 48.00\ntotal = 48.00")
        graph = _graph_for("Buy a bottle of whisky.", item, description="A gift box.")
        result = verify(POLICY, order, graph, history=[], now=NOW)
        failure = _failure(result, "blocked_categories")
        self.assertEqual(failure["counterexample"]["categories"], ["alcohol"])
        self.assertEqual(failure["node_ids"], ["purchase:WHISKY-GIFT"])
        self.assertNotIn("allowed_merchants", _ids(result))

    def test_no_duplicates_blocks_the_same_sku_and_merchant_inside_the_window(self):
        item = _item("NOTEBOOK-PACK")
        order = _order(item, total="12.50", reasoning="1 * 12.50 = 12.50\ntotal = 12.50")
        graph = _graph_for("Buy a notebook pack.", item, description="A notebook.")
        recent = {
            "sku": "NOTEBOOK-PACK",
            "merchant": "Desk & Co",
            "amount_gbp": "12.50",
            "purchased_at": NOW - timedelta(hours=2),
        }
        stale = {**recent, "purchased_at": NOW - timedelta(hours=25)}
        other_shop = {**recent, "merchant": "Kite Paper Co"}

        blocked = verify(POLICY, order, graph, history=[recent], now=NOW)
        failure = _failure(blocked, "no_duplicates")
        self.assertEqual(failure["node_ids"], ["purchase:NOTEBOOK-PACK"])
        self.assertEqual(failure["counterexample"]["duplicates"][0]["window_hours"], 24)

        self.assertNotIn("no_duplicates", _ids(verify(POLICY, order, graph, history=[stale], now=NOW)))
        self.assertNotIn("no_duplicates", _ids(verify(POLICY, order, graph, history=[other_shop], now=NOW)))

    def test_arithmetic_consistency_rejects_a_wrong_sum_and_a_wrong_total(self):
        item = _item("NOTEBOOK-PACK", quantity=2)
        graph = _graph_for("Buy two notebook packs.", item, description="A notebook.")
        wrong_sum = _order(item, total="25.00", reasoning="2 * 12.50 = 20.00\ntotal = 25.00")
        sum_result = verify(POLICY, wrong_sum, graph, history=[], now=NOW)
        sum_failure = _failure(sum_result, "arithmetic_consistency")
        self.assertEqual(sum_failure["counterexample"]["claimed_gbp"], "20.00")
        self.assertEqual(sum_failure["counterexample"]["computed_gbp"], "25.00")
        self.assertIn("purchase:NOTEBOOK-PACK", sum_failure["node_ids"])

        wrong_total = _order(item, total="25.00", reasoning="2 * 12.50 = 25.00\ntotal = 30.00")
        total_result = verify(POLICY, wrong_total, graph, history=[], now=NOW)
        total_failure = _failure(total_result, "arithmetic_consistency")
        self.assertEqual(total_failure["counterexample"]["reasoning_total_gbp"], "30.00")
        self.assertEqual(total_failure["counterexample"]["order_total_gbp"], "25.00")

    def test_arithmetic_consistency_requires_a_stated_total(self):
        item = _item("NOTEBOOK-PACK")
        order = _order(item, total="12.50", reasoning="One notebook pack.")
        graph = _graph_for("Buy a notebook pack.", item, description="A notebook.")
        result = verify(POLICY, order, graph, history=[], now=NOW)
        failure = _failure(result, "arithmetic_consistency")
        self.assertIsNone(failure["counterexample"]["reasoning_total_gbp"])

    def test_goal_grounding_flags_the_quantity_that_came_from_the_listing(self):
        item = _item("USB-C-CABLE", quantity=50)
        # Keep the order under the cap so this failure is the grounding rule on its own.
        # The real cable price is covered by max_per_order in the demo test below.
        cheap = {**item, "unit_price_gbp": "1.00", "line_total_gbp": "50.00"}
        order = _order(cheap, total="50.00", reasoning="50 * 1.00 = 50.00\ntotal = 50.00")
        graph = _graph_for("We need a USB-C cable for the new laptops.", cheap)
        result = verify(POLICY, order, graph, history=[], now=NOW)
        failure = _failure(result, "goal_grounding")
        self.assertEqual(failure["node_ids"], ["purchase:USB-C-CABLE"])
        self.assertNotIn("max_per_order", _ids(result))

    def test_the_poisoned_cable_fails_the_cap_and_grounding_together(self):
        item = _item("USB-C-CABLE", quantity=50)
        order = _order(item, total="400.00", reasoning="50 * 8.00 = 400.00\ntotal = 400.00")
        graph = _graph_for("We need a USB-C cable for the new laptops.", item)
        result = verify(POLICY, order, graph, history=[], now=NOW)
        self.assertFalse(result["passed"])
        self.assertEqual(_ids(result), {"max_per_order", "goal_grounding"})

    def test_ledger_rows_feed_the_duplicate_rule(self):
        with tempfile.TemporaryDirectory() as folder:
            ledger = Ledger(Path(folder) / "ledger.sqlite")
            ledger.record(
                sku="NOTEBOOK-PACK",
                merchant="Desk & Co",
                category="stationery",
                amount_gbp="12.50",
                purchased_at=NOW - timedelta(hours=2),
            )
            item = _item("NOTEBOOK-PACK")
            order = _order(item, total="12.50", reasoning="1 * 12.50 = 12.50\ntotal = 12.50")
            graph = _graph_for("Buy a notebook pack.", item, description="A notebook.")
            result = verify(POLICY, order, graph, history=ledger.entries(), now=NOW)
        self.assertIn("no_duplicates", _ids(result))


class LedgerTests(unittest.TestCase):
    def test_record_then_read_back(self):
        with tempfile.TemporaryDirectory() as folder:
            ledger = Ledger(Path(folder) / "ledger.sqlite")
            ledger.record(
                sku="PEN-BOX",
                merchant="Desk & Co",
                category="stationery",
                amount_gbp="4.50",
                purchased_at=NOW,
            )
            rows = ledger.entries()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["sku"], "PEN-BOX")
        self.assertEqual(rows[0]["amount_gbp"], "4.50")
        self.assertTrue(rows[0]["purchased_at"].startswith("2026-11-06"))
