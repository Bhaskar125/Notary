"""Suggestions on the reasoning graph. They do not change support edges."""

import unittest

from src.catalog import load_catalog
from src.extraction import build_graph
from src.policy import load_policy
from src.verifier import verify

CATALOG = load_catalog()
POLICY = load_policy()


def line(sku: str, quantity: int = 1) -> dict:
    product = CATALOG.get(sku)
    total = product.price_gbp * quantity
    return {
        "sku": sku,
        "name": product.name,
        "merchant": product.merchant,
        "category": product.category,
        "quantity": quantity,
        "unit_price_gbp": f"{product.price_gbp:.2f}",
        "line_total_gbp": f"{total:.2f}",
    }


def trace_for(*skus: str) -> list:
    return [
        {
            "tool": "get_product",
            "products": [
                {"sku": sku, "name": CATALOG.get(sku).name, "description": CATALOG.get(sku).description}
                for sku in skus
            ],
        }
    ]


class SuggestionTests(unittest.TestCase):
    def test_a_laptop_is_not_office_supplies(self):
        items = [line("PEN-BOX"), line("LAPTOP-14")]
        graph = build_graph(
            "Buy office supplies for a 3 person team, budget £200.",
            trace_for("PEN-BOX", "LAPTOP-14"),
            items,
        )
        self.assertEqual([note["sku"] for note in graph["suggestions"]], ["LAPTOP-14"])
        self.assertEqual(graph["suggestions"][0]["saves_gbp"], "749.00")
        self.assertEqual(graph["savings_gbp"], "749.00")
        support = [
            edge for edge in graph["edges"] if edge["source"] == "purchase:LAPTOP-14" and edge["relation"] == "supported_by"
        ]
        self.assertEqual(support[0]["target"], "goal")
        self.assertTrue(any(edge["relation"] == "suggests" and edge["target"] == "suggest:LAPTOP-14" for edge in graph["edges"]))

    def test_optional_stationery_is_dropped_when_pens_and_paper_are_already_there(self):
        items = [line("PEN-BOX"), line("A4-REAM"), line("STAPLER"), line("SCISSORS")]
        graph = build_graph(
            "Buy office supplies for a 3 person team.",
            trace_for("PEN-BOX", "A4-REAM", "STAPLER", "SCISSORS"),
            items,
        )
        suggested = {note["sku"] for note in graph["suggestions"]}
        self.assertEqual(suggested, {"STAPLER", "SCISSORS"})
        self.assertNotIn("listing:PEN-BOX", [node["id"] for node in graph["nodes"]])

    def test_fifty_cables_suggest_buying_one(self):
        item = line("USB-C-CABLE", 50)
        graph = build_graph("Buy one USB-C cable.", trace_for("USB-C-CABLE"), [item])
        self.assertEqual(graph["suggestions"][0]["saves_gbp"], "392.00")
        self.assertIn("Buying 1 ×", graph["suggestions"][0]["text"])
        self.assertIn("purchase:USB-C-CABLE", graph["highlights"])

    def test_two_paper_lines_keep_the_cheaper_one(self):
        items = [line("A4-REAM"), line("A4-BOX")]
        graph = build_graph("Buy paper.", trace_for("A4-REAM", "A4-BOX"), items)
        self.assertEqual([note["sku"] for note in graph["suggestions"]], ["A4-BOX"])

    def test_a_single_pen_box_has_no_suggestion_and_still_passes(self):
        item = line("PEN-BOX")
        task = "Buy one box of ballpoint pens."
        graph = build_graph(task, trace_for("PEN-BOX"), [item])
        self.assertEqual(graph["suggestions"], [])
        self.assertEqual(graph["savings_gbp"], "0.00")
        order = {"items": [item], "total_gbp": "4.50", "reasoning": "1 * 4.50 = 4.50\ntotal = 4.50"}
        result = verify(POLICY, order, graph, history=[])
        self.assertTrue(result["passed"], result)
