"""SQLite spend history. The verifier does not open the database itself.

Callers ask the ledger for rows and pass that list into ``verify``. Tests of
the rules can pass the same list shape without a file.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_LEDGER_PATH = Path(__file__).resolve().parent.parent / "data" / "ledger.sqlite"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS spends (
    id INTEGER PRIMARY KEY,
    sku TEXT NOT NULL,
    merchant TEXT NOT NULL,
    category TEXT NOT NULL,
    amount_gbp TEXT NOT NULL,
    purchased_at TEXT NOT NULL
)
"""


class Ledger:
    def __init__(self, path: Path = DEFAULT_LEDGER_PATH):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(_SCHEMA)

    def record(
        self,
        *,
        sku: str,
        merchant: str,
        category: str,
        amount_gbp: str,
        purchased_at: datetime,
    ) -> None:
        when = _as_utc(purchased_at)
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO spends (sku, merchant, category, amount_gbp, purchased_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (sku, merchant, category, amount_gbp, when.isoformat()),
            )

    def entries(self) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT sku, merchant, category, amount_gbp, purchased_at
                FROM spends
                ORDER BY purchased_at, id
                """
            ).fetchall()
        return [
            {
                "sku": row["sku"],
                "merchant": row["merchant"],
                "category": row["category"],
                "amount_gbp": row["amount_gbp"],
                "purchased_at": row["purchased_at"],
            }
            for row in rows
        ]

    @contextmanager
    def _connect(self):
        # sqlite3's own context manager commits, but it leaves the file open.
        # On Windows that lock stops a test from deleting the temp database.
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
