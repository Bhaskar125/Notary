"""Remember a basket that passed the policy, until PayPal captures it.

The browser is only given an id. Prices, reasoning, and the graph stay here,
so a later create-order call cannot substitute a different amount.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.ledger import DEFAULT_LEDGER_PATH

_SCHEMA = """
CREATE TABLE IF NOT EXISTS checkouts (
    id TEXT PRIMARY KEY,
    task TEXT NOT NULL,
    order_json TEXT NOT NULL,
    graph_json TEXT NOT NULL,
    paypal_order_id TEXT,
    recorded INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    session_id TEXT NOT NULL DEFAULT ''
)
"""


class CheckoutStore:
    def __init__(self, path: Path = DEFAULT_LEDGER_PATH):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(_SCHEMA)
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(checkouts)")}
        if "session_id" not in columns:
            connection.execute("ALTER TABLE checkouts ADD COLUMN session_id TEXT NOT NULL DEFAULT ''")

    # This stores a basket that passed. The id is what the browser is allowed to send back.
    def save(
        self,
        *,
        task: str,
        order: dict[str, Any],
        graph: dict[str, Any],
        session_id: str,
    ) -> str:
        checkout_id = uuid.uuid4().hex
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO checkouts (
                    id, task, order_json, graph_json, paypal_order_id, recorded, created_at, session_id
                )
                VALUES (?, ?, ?, ?, NULL, 0, ?, ?)
                """,
                (
                    checkout_id,
                    task,
                    json.dumps(order),
                    json.dumps(graph),
                    datetime.now(timezone.utc).isoformat(),
                    session_id,
                ),
            )
        return checkout_id

    # This loads one checkout, including the prices the browser is not allowed to change.
    def get(self, checkout_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM checkouts WHERE id = ?", (checkout_id,)).fetchone()
        return None if row is None else _row_to_dict(row)

    # This remembers the PayPal order id on the checkout that created it.
    def attach_paypal(self, checkout_id: str, paypal_order_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE checkouts SET paypal_order_id = ? WHERE id = ?",
                (paypal_order_id, checkout_id),
            )

    # This finds the checkout from the PayPal order id, which is what capture sends us.
    def find_by_paypal(self, paypal_order_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM checkouts WHERE paypal_order_id = ?",
                (paypal_order_id,),
            ).fetchone()
        return None if row is None else _row_to_dict(row)

    # This marks the checkout as written to the ledger so a second capture does nothing.
    def mark_recorded(self, checkout_id: str) -> None:
        with self._connect() as connection:
            connection.execute("UPDATE checkouts SET recorded = 1 WHERE id = ?", (checkout_id,))

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()


# This unpacks a row. The order and the graph were stored as JSON.
def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "task": row["task"],
        "order": json.loads(row["order_json"]),
        "graph": json.loads(row["graph_json"]),
        "paypal_order_id": row["paypal_order_id"],
        "recorded": bool(row["recorded"]),
        "created_at": row["created_at"],
        "session_id": row["session_id"] or "",
    }
