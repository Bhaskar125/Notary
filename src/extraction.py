"""Build a reasoning graph from the agent's tool trace.

The user's task becomes the goal. Each catalog listing the agent actually
read becomes a claim. Each proposed line becomes a purchase. Support edges
say what the purchase is relying on.

A quantity that the listing itself demands, and that the user did not ask
for, is supported by that listing and not by the goal. The USB-C cable demo
is the case this is for: "buy 50 units" is in the product description.
"""

from __future__ import annotations

import re
from typing import Any


def build_graph(task: str, trace: list[dict[str, Any]], items: list[dict[str, Any]]) -> dict[str, Any]:
    listings = _listings_from_trace(trace)
    nodes: list[dict[str, Any]] = [{"id": "goal", "type": "goal", "text": task}]
    edges: list[dict[str, str]] = []
    highlights: list[str] = []

    for sku, listing in listings.items():
        nodes.append(
            {
                "id": f"listing:{sku}",
                "type": "claim",
                "sku": sku,
                "text": listing.get("description") or "",
            }
        )

    for item in items:
        sku = str(item.get("sku") or "")
        quantity = item.get("quantity")
        purchase_id = f"purchase:{sku}"
        nodes.append(
            {
                "id": purchase_id,
                "type": "purchase",
                "sku": sku,
                "quantity": quantity,
                "text": f"{quantity} × {item.get('name') or sku}",
            }
        )
        listing = listings.get(sku)
        description = (listing or {}).get("description") or ""
        dictated = isinstance(quantity, int) and listing_dictates_quantity(description, quantity)
        asked = isinstance(quantity, int) and quantity_in_text(task, quantity)
        if dictated and not asked:
            edges.append({"source": purchase_id, "target": f"listing:{sku}", "relation": "supported_by"})
            highlights.append(purchase_id)
            nodes.append(
                {
                    "id": f"claim:{sku}:quantity",
                    "type": "claim",
                    "sku": sku,
                    "text": (
                        f"The quantity {quantity} comes from the {sku} listing, not from the user's goal."
                    ),
                }
            )
            edges.append(
                {"source": f"claim:{sku}:quantity", "target": f"listing:{sku}", "relation": "supported_by"}
            )
        else:
            edges.append({"source": purchase_id, "target": "goal", "relation": "supported_by"})

    return {"nodes": nodes, "edges": edges, "highlights": highlights}


def listing_dictates_quantity(description: str, quantity: int) -> bool:
    """True when the listing tells the reader to buy this many units."""
    if quantity < 1:
        return False
    pattern = rf"\b(?:buy|purchase|order|add)\s+{quantity}\b|\b{quantity}\s+units\b"
    return re.search(pattern, description, flags=re.IGNORECASE) is not None


def quantity_in_text(text: str, quantity: int) -> bool:
    return re.search(rf"\b{quantity}\b", text) is not None


def _listings_from_trace(trace: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    listings: dict[str, dict[str, Any]] = {}
    for event in trace:
        products = event.get("products") or []
        if not isinstance(products, list):
            continue
        for product in products:
            if not isinstance(product, dict):
                continue
            sku = str(product.get("sku") or "").strip()
            if sku:
                listings[sku] = product
    return listings
