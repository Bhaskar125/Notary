"""Build a reasoning graph from the agent's tool trace.

The user's task becomes the goal. Each catalog listing the agent actually
read becomes a claim. Each proposed line becomes a purchase. Support edges
say what the purchase is relying on.

A quantity that the listing itself demands, and that the user did not ask
for, is supported by that listing and not by the goal. The USB-C cable demo
is the case this is for: "buy 50 units" is in the product description.

Suggestions are separate from that support. They name a cheaper basket
(drop a line the task never asked for, drop a second copy of the same
kind of thing, or buy fewer units). A suggestion edge is not support, so
it does not change whether a rule passes.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any


def build_graph(task: str, trace: list[dict[str, Any]], items: list[dict[str, Any]]) -> dict[str, Any]:
    # The goal is the user's sentence. Claims are listings the agent opened. Purchases are the basket lines.
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
            # The user asked for this quantity, or the listing did not dictate one, so the goal supports the line.
            edges.append({"source": purchase_id, "target": "goal", "relation": "supported_by"})

    # A listing nobody bought is dropped, so the graph only shows what the basket relies on.
    nodes = _without_unused_claims(nodes, edges)
    # Suggestions are advisory. They do not count as support and they do not change the verdict.
    suggestions = _suggestions(task, items, listings)
    for suggestion in suggestions:
        nodes.append(
            {
                "id": suggestion["id"],
                "type": "suggestion",
                "sku": suggestion["sku"],
                "text": suggestion["text"],
                "saves_gbp": suggestion["saves_gbp"],
            }
        )
        edges.append(
            {
                "source": f"purchase:{suggestion['sku']}",
                "target": suggestion["id"],
                "relation": "suggests",
            }
        )
    saved = sum((Decimal(item["saves_gbp"]) for item in suggestions), Decimal("0.00"))
    return {
        "nodes": nodes,
        "edges": edges,
        "highlights": highlights,
        "suggestions": suggestions,
        "savings_gbp": _pounds(saved),
    }


def listing_dictates_quantity(description: str, quantity: int) -> bool:
    """True when the listing tells the reader to buy this many units."""
    if quantity < 1:
        return False
    pattern = rf"\b(?:buy|purchase|order|add)\s+{quantity}\b|\b{quantity}\s+units\b"
    return re.search(pattern, description, flags=re.IGNORECASE) is not None


# This is true when the user's sentence already contains this quantity as a number.
def quantity_in_text(text: str, quantity: int) -> bool:
    return re.search(rf"\b{quantity}\b", text) is not None


# Words in the task pick which catalog categories belong in the basket.
# "Office supplies" stays in stationery. A laptop in that basket is extra.
_SCOPES: tuple[tuple[frozenset[str], frozenset[str]], ...] = (
    (frozenset({"stationery", "office", "supplies", "supply", "paper", "pen", "pens", "notebook", "notebooks", "stapler", "envelope", "envelopes"}), frozenset({"stationery"})),
    (frozenset({"electronics", "laptop", "laptops", "computer", "computers", "monitor", "keyboard", "mouse", "cable", "cables", "usb", "headset", "webcam", "hdmi"}), frozenset({"electronics"})),
    (frozenset({"furniture", "chair", "chairs", "desk", "lamp", "footrest"}), frozenset({"furniture"})),
    (frozenset({"pantry", "coffee", "tea", "biscuit", "biscuits", "water", "snack", "snacks"}), frozenset({"pantry"})),
    (frozenset({"alcohol", "whisky", "wine", "beer"}), frozenset({"alcohol"})),
)

# Two lines of the same kind: paper and paper, or two chairs.
_ROLES: tuple[tuple[str, frozenset[str]], ...] = (
    ("paper", frozenset({"paper", "ream", "reams"})),
    ("chair", frozenset({"chair", "chairs"})),
    ("cable", frozenset({"cable", "cables"})),
    ("pen", frozenset({"pen", "pens", "ballpoint"})),
    ("notebook", frozenset({"notebook", "notebooks"})),
)


# This names a cheaper basket: drop an extra line, drop a duplicate role, or buy fewer units.
def _suggestions(
    task: str,
    items: list[dict[str, Any]],
    listings: dict[str, dict[str, Any]],
) -> list[dict[str, str]]:
    """One cheaper-basket note per line, when the line is larger than the task."""
    task_tokens = _tokens(task)
    scope = _scope_for(task_tokens)
    team = _team_size(task)
    asked = _asked_quantity(task)
    suggestions: list[dict[str, str]] = []
    claimed: set[str] = set()

    for item in items:
        sku = str(item.get("sku") or "")
        category = str(item.get("category") or "").casefold()
        total = _line_total(item)
        if not sku or total is None or total <= 0 or sku in claimed:
            continue
        if scope and category and category not in scope:
            name = item.get("name") or sku
            suggestions.append(
                _suggestion(
                    sku,
                    f"The task does not ask for {category}. Drop {name} and save £{_pounds(total)}.",
                    total,
                )
            )
            claimed.add(sku)

    if _is_supply_kit(task_tokens) and _has_core_supply(items):
        for item in items:
            sku = str(item.get("sku") or "")
            if not sku or sku in claimed:
                continue
            if str(item.get("category") or "").casefold() != "stationery":
                continue
            role = _role_of(item)
            if role in _CORE_SUPPLIES:
                continue
            total = _line_total(item)
            if total is None:
                continue
            name = item.get("name") or sku
            who = f"a kit for {team} people" if team else "this stationery order"
            suggestions.append(
                _suggestion(
                    sku,
                    f"Pens, notebooks, and paper cover {who}. Drop {name} and save £{_pounds(total)}.",
                    total,
                )
            )
            claimed.add(sku)

    for sku, total, name in _dearer_duplicates(items, task_tokens, team):
        if sku in claimed:
            continue
        suggestions.append(
            _suggestion(
                sku,
                f"Another line already covers the same kind of thing. Drop {name} and save £{_pounds(total)}.",
                total,
            )
        )
        claimed.add(sku)

    for item in items:
        sku = str(item.get("sku") or "")
        if not sku or sku in claimed:
            continue
        quantity = item.get("quantity")
        total = _line_total(item)
        if not isinstance(quantity, int) or quantity < 2 or total is None:
            continue
        listing = listings.get(sku) or {}
        description = str(listing.get("description") or "")
        if not listing_dictates_quantity(description, quantity):
            continue
        if quantity_in_text(task, quantity):
            continue
        keep = asked if asked is not None and asked < quantity else None
        if keep is None and team is not None and team < quantity:
            keep = team
        if keep is None:
            keep = 1
        if keep >= quantity:
            continue
        unit = (total / Decimal(quantity)).quantize(Decimal("0.01"))
        saved = unit * Decimal(quantity - keep)
        name = item.get("name") or sku
        suggestions.append(
            _suggestion(
                sku,
                (
                    f"The {sku} listing asks for {quantity}. "
                    f"Buying {keep} × {name} instead saves £{_pounds(saved)}."
                ),
                saved,
            )
        )
        claimed.add(sku)

    return suggestions


# A general "office supplies" task is covered by writing and paper.
# Scissors, tape, a stapler, and the rest are optional on that ask.
_KIT_WORDS = frozenset({"office", "supplies", "supply", "stationery"})
_NAMED_PRODUCT = frozenset(
    {
        "pen",
        "pens",
        "notebook",
        "notebooks",
        "paper",
        "stapler",
        "envelope",
        "envelopes",
        "laptop",
        "cable",
        "cables",
        "chair",
        "chairs",
        "coffee",
        "tea",
        "whisky",
        "monitor",
        "keyboard",
        "mouse",
        "headset",
        "webcam",
        "lamp",
        "binder",
        "tape",
        "scissors",
    }
)
_CORE_SUPPLIES = frozenset({"pen", "notebook", "paper"})


def _is_supply_kit(task_tokens: set[str]) -> bool:
    return bool(task_tokens & _KIT_WORDS) and not (task_tokens & _NAMED_PRODUCT)


def _has_core_supply(items: list[dict[str, Any]]) -> bool:
    return any(_role_of(item) in _CORE_SUPPLIES for item in items)


def _scope_for(task_tokens: set[str]) -> set[str]:
    scope: set[str] = set()
    for words, categories in _SCOPES:
        if task_tokens & words:
            scope.update(categories)
    return scope


def _dearer_duplicates(
    items: list[dict[str, Any]],
    task_tokens: set[str],
    team: int | None,
) -> list[tuple[str, Decimal, str]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        role = _role_of(item)
        total = _line_total(item)
        if role is None or total is None:
            continue
        groups.setdefault(role, []).append(item)
    extras: list[tuple[str, Decimal, str]] = []
    for group in groups.values():
        if len(group) < 2:
            continue
        ranked = sorted(group, key=lambda item: _keep_rank(item, task_tokens, team))
        for item in ranked[1:]:
            total = _line_total(item)
            if total is None:
                continue
            extras.append((str(item.get("sku") or ""), total, str(item.get("name") or item.get("sku") or "")))
    return extras


def _keep_rank(item: dict[str, Any], task_tokens: set[str], team: int | None) -> tuple[int, int, Decimal]:
    """Lower sorts first, and that line is the one to keep."""
    name = str(item.get("name") or "")
    named = 0 if (_tokens(name) | _tokens(str(item.get("sku") or "").replace("-", " "))) & task_tokens else 1
    fits_team = 1
    if team is not None and f"set of {team}" in name.casefold():
        fits_team = 0
    total = _line_total(item) or Decimal("0")
    return (named, fits_team, total)


def _role_of(item: dict[str, Any]) -> str | None:
    tokens = _tokens(str(item.get("name") or ""))
    for role, words in _ROLES:
        if tokens & words:
            return role
    return None


def _team_size(task: str) -> int | None:
    match = re.search(r"\b(\d+)\s*-?\s*(?:person|people|member)s?\b", task, flags=re.IGNORECASE)
    if match is None:
        return None
    return int(match.group(1))


def _asked_quantity(task: str) -> int | None:
    if re.search(r"\bone\b|\ba single\b", task, flags=re.IGNORECASE):
        return 1
    match = re.search(r"\b(?:buy|get|order|need)\s+(\d+)\b", task, flags=re.IGNORECASE)
    if match is None:
        return None
    return int(match.group(1))


def _suggestion(sku: str, text: str, saved: Decimal) -> dict[str, str]:
    return {"id": f"suggest:{sku}", "sku": sku, "text": text, "saves_gbp": _pounds(saved)}


def _line_total(item: dict[str, Any]) -> Decimal | None:
    raw = item.get("line_total_gbp")
    if raw in (None, ""):
        return None
    try:
        return Decimal(str(raw))
    except InvalidOperation:
        return None


def _pounds(amount: Decimal) -> str:
    return f"{amount.quantize(Decimal('0.01')):.2f}"


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", text.casefold()))


def _without_unused_claims(nodes: list[dict[str, Any]], edges: list[dict[str, str]]) -> list[dict[str, Any]]:
    """Leave out a listing the agent opened when no purchase relies on it."""
    referenced = {edge["source"] for edge in edges} | {edge["target"] for edge in edges}
    return [
        node
        for node in nodes
        if node.get("type") != "claim" or node.get("id") in referenced
    ]


# This collects the products the agent actually searched or opened. The graph does not invent listings.
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
