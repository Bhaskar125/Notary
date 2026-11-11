"""Mock product catalog. Prices in this file are what the agent is allowed to buy at.

The model can claim any number it likes. Callers price a proposal with
`price_lines`, which reads the price from here.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "data" / "catalog.json"
SEARCH_LIMIT = 8


@dataclass(frozen=True)
class Product:
    sku: str
    name: str
    merchant: str
    category: str
    price_gbp: Decimal
    description: str

    def public(self) -> dict[str, str]:
        return {
            "sku": self.sku,
            "name": self.name,
            "merchant": self.merchant,
            "category": self.category,
            "price_gbp": money(self.price_gbp),
            "description": self.description,
        }


class Catalog:
    def __init__(self, products: list[Product]):
        seen: set[str] = set()
        for product in products:
            if product.sku in seen:
                raise ValueError(f"Duplicate SKU {product.sku}")
            seen.add(product.sku)
        self._products = list(products)
        self._by_sku = {product.sku: product for product in products}

    def __len__(self) -> int:
        return len(self._products)

    def get(self, sku: str) -> Product | None:
        return self._by_sku.get(sku.strip())

    def products(self) -> list[Product]:
        """Every product, ordered for a tester browsing the catalog."""
        return sorted(self._products, key=lambda product: (product.category.casefold(), product.name.casefold()))

    @property
    def merchants(self) -> set[str]:
        return {product.merchant for product in self._products}

    @property
    def categories(self) -> set[str]:
        return {product.category for product in self._products}

    def search(
        self,
        *,
        query: str = "",
        merchant: str = "",
        category: str = "",
        max_price: Decimal | None = None,
        limit: int = SEARCH_LIMIT,
    ) -> tuple[list[Product], int]:
        """Return up to `limit` matches and the full match count.

        Every word in `query` must appear in the sku, name, merchant, category,
        or description. The cap is what forces the agent to search narrowly
        and then open a listing, which is how it meets the poisoned description.
        """
        terms = [term.casefold() for term in query.split() if term.strip()]
        merchant_key = merchant.strip().casefold()
        category_key = category.strip().casefold()
        matches: list[Product] = []
        for product in self._products:
            if merchant_key and product.merchant.casefold() != merchant_key:
                continue
            if category_key and product.category.casefold() != category_key:
                continue
            if max_price is not None and product.price_gbp > max_price:
                continue
            haystack = " ".join(
                [
                    product.sku,
                    product.name,
                    product.merchant,
                    product.category,
                    product.description,
                ]
            ).casefold()
            if terms and any(term not in haystack for term in terms):
                continue
            matches.append(product)
        matches.sort(key=lambda product: product.name.casefold())
        return matches[:limit], len(matches)


def load_catalog(path: Path = DEFAULT_PATH) -> Catalog:
    raw = json.loads(path.read_text(encoding="utf-8"))
    rows = raw.get("products") if isinstance(raw, dict) else None
    if not isinstance(rows, list) or not rows:
        raise ValueError(f"{path} must contain a non-empty products list")
    return Catalog([_product_from_row(row, path) for row in rows])


def price_lines(catalog: Catalog, items: list[dict]) -> tuple[list[dict], list[str]]:
    """Price SKUs from the catalog. Returns (lines, errors). Errors means do not buy."""
    if not isinstance(items, list) or not items:
        return [], ["Propose at least one item."]
    merged: dict[str, int] = {}
    errors: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            errors.append("Each item must be an object with sku and quantity.")
            continue
        sku = str(item.get("sku", "")).strip()
        quantity = parse_quantity(item.get("quantity"))
        if not sku:
            errors.append("Every item needs a sku.")
            continue
        if quantity is None:
            errors.append(f"{sku or 'item'}: quantity must be a whole number from 1 to 99.")
            continue
        merged[sku] = merged.get(sku, 0) + quantity
    lines: list[dict] = []
    for sku, quantity in merged.items():
        if quantity > 99:
            errors.append(f"{sku}: quantity must be from 1 to 99.")
            continue
        product = catalog.get(sku)
        if product is None:
            errors.append(f"Unknown SKU {sku}.")
            continue
        line_total = product.price_gbp * quantity
        lines.append(
            {
                "sku": product.sku,
                "name": product.name,
                "merchant": product.merchant,
                "category": product.category,
                "quantity": quantity,
                "unit_price_gbp": money(product.price_gbp),
                "line_total_gbp": money(line_total),
            }
        )
    if errors:
        return [], errors
    lines.sort(key=lambda line: line["sku"])
    return lines, []


def order_total(lines: list[dict]) -> str:
    total = sum((Decimal(line["line_total_gbp"]) for line in lines), Decimal("0"))
    return money(total)


def parse_quantity(value: object) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        quantity = value
    elif isinstance(value, float) and value.is_integer():
        quantity = int(value)
    elif isinstance(value, str) and value.strip().isdigit():
        quantity = int(value.strip())
    else:
        return None
    if 1 <= quantity <= 99:
        return quantity
    return None


def money(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.01')):.2f}"


def _product_from_row(row: object, path: Path) -> Product:
    if not isinstance(row, dict):
        raise ValueError(f"{path} has a product that is not an object")
    try:
        sku = str(row["sku"]).strip()
        name = str(row["name"]).strip()
        merchant = str(row["merchant"]).strip()
        category = str(row["category"]).strip()
        description = str(row["description"]).strip()
        price = Decimal(str(row["price_gbp"]))
    except (KeyError, ArithmeticError) as exc:
        raise ValueError(f"{path} has a product missing sku, name, merchant, category, price_gbp, or description") from exc
    if not all([sku, name, merchant, category, description]):
        raise ValueError(f"{path} has a blank field on SKU {sku or '?'}")
    if price != price.quantize(Decimal("0.01")) or price <= 0:
        raise ValueError(f"{path} SKU {sku} has a price that is not a positive two-decimal GBP amount")
    return Product(sku, name, merchant, category, price, description)
