"""Route tests. The PayPal client is replaced so nothing leaves the machine."""

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from src import main
from src.catalog import load_catalog, order_total, price_lines
from src.extraction import build_graph

client = TestClient(main.app)
CATALOG = load_catalog()


def save_notebook(*, quantity: int = 1) -> tuple[str, dict]:
    lines, errors = price_lines(CATALOG, [{"sku": "NOTEBOOK-PACK", "quantity": quantity}])
    if errors:
        raise AssertionError(errors)
    total = order_total(lines)
    unit = lines[0]["unit_price_gbp"]
    line = lines[0]["line_total_gbp"]
    order = {
        "items": lines,
        "total_gbp": total,
        "reasoning": f"{quantity} * {unit} = {line}\ntotal = {total}",
    }
    task = "Buy a notebook pack."
    trace = [
        {
            "tool": "get_product",
            "products": [
                {
                    "sku": "NOTEBOOK-PACK",
                    "name": lines[0]["name"],
                    "description": "Pack of 5 ruled A4 notebooks.",
                }
            ],
        }
    ]
    checkout_id = main.checkout_store().save(
        task=task,
        order=order,
        graph=build_graph(task, trace, lines),
    )
    return checkout_id, order


class OrdersApiTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        main.set_data_directory(Path(self._tmp.name))

    def tearDown(self):
        main.reset_paypal_client()
        main.set_data_directory(None)
        self._tmp.cleanup()

    def test_health_and_page_offer_paypal_only_after_a_pass(self):
        health = client.get("/api/health")
        self.assertEqual(health.status_code, 200)
        self.assertEqual(health.json(), {"status": "ok"})

        page = client.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertIn("PayPal appears only after every rule passes", page.text)
        self.assertIn("web-sdk/v6/core", page.text)
        self.assertIn("Propose a basket", page.text)
        self.assertNotIn("hardcoded", page.text.casefold())
        self.assertNotIn("PAYPAL_CLIENT_SECRET", page.text)

    def test_config_hides_the_secret_and_reports_missing_credentials(self):
        with patch.dict(os.environ, {"PAYPAL_CLIENT_ID": "", "PAYPAL_CLIENT_SECRET": ""}, clear=False):
            response = client.get("/api/config")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertFalse(body["configured"])
        self.assertIsNone(body["paypal_client_id"])
        self.assertEqual(body["currency"], "GBP")
        self.assertNotIn("client_secret", body)
        self.assertNotIn("item", body)

    def test_create_order_charges_the_checked_basket_and_ignores_a_client_amount(self):
        class FakePayPal:
            def __init__(self):
                self.kwargs = None

            def create_order(self, **kwargs):
                self.kwargs = kwargs
                return {"id": "ORDER1", "status": "CREATED"}

        checkout_id, order = save_notebook()
        fake = FakePayPal()
        env = {"PAYPAL_CLIENT_ID": "public-id", "PAYPAL_CLIENT_SECRET": "secret"}
        with patch.dict(os.environ, env, clear=False):
            with patch.object(main, "paypal_client_from_env", return_value=fake):
                response = client.post(
                    "/api/orders",
                    json={"checkout_id": checkout_id, "value": "99999.00", "currency": "USD"},
                )
                config = client.get("/api/config")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["id"], "ORDER1")
        self.assertEqual(response.json()["total_gbp"], order["total_gbp"])
        self.assertEqual(fake.kwargs["value"], "12.50")
        self.assertEqual(fake.kwargs["currency"], "GBP")
        self.assertEqual(fake.kwargs["custom_id"], checkout_id)
        self.assertEqual(config.json()["paypal_client_id"], "public-id")
        self.assertNotIn("secret", config.json().values())

    def test_create_order_rejects_an_unknown_basket_before_calling_paypal(self):
        called = False

        def explode():
            nonlocal called
            called = True
            raise AssertionError("PayPal should not be called")

        with patch.object(main, "paypal_client_from_env", side_effect=explode):
            missing = client.post("/api/orders", json={"checkout_id": "a" * 32})
            malformed = client.post("/api/orders", json={"value": "12.50"})
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(malformed.status_code, 422)
        self.assertFalse(called)

    def test_create_order_without_credentials_is_503(self):
        checkout_id, _order = save_notebook()
        with patch.dict(os.environ, {"PAYPAL_CLIENT_ID": "", "PAYPAL_CLIENT_SECRET": ""}, clear=False):
            main.reset_paypal_client()
            response = client.post("/api/orders", json={"checkout_id": checkout_id})
        self.assertEqual(response.status_code, 503)

    def test_create_order_blocks_a_basket_once_history_breaks_the_period_cap(self):
        checkout_id, _order = save_notebook()
        main.ledger_store().record(
            sku="LAPTOP-14",
            merchant="Northbeam IT",
            category="electronics",
            amount_gbp="490.00",
            purchased_at=datetime.now(timezone.utc),
        )
        called = False

        def explode():
            nonlocal called
            called = True
            raise AssertionError("PayPal should not be called")

        with patch.object(main, "paypal_client_from_env", side_effect=explode):
            response = client.post("/api/orders", json={"checkout_id": checkout_id})
        self.assertEqual(response.status_code, 409)
        self.assertIn("max_per_period", response.text)
        self.assertFalse(called)

    def test_capture_rejects_a_malformed_order_id_before_calling_paypal(self):
        called = False

        def explode():
            nonlocal called
            called = True
            raise AssertionError("PayPal should not be called")

        with patch.object(main, "paypal_client_from_env", side_effect=explode):
            response = client.post("/api/orders/not-a-valid-id/capture")
        self.assertEqual(response.status_code, 400)
        self.assertFalse(called)

    def test_capture_returns_the_client_summary(self):
        class FakePayPal:
            def capture_order(self, order_id):
                return {
                    "id": order_id,
                    "status": "COMPLETED",
                    "capture_id": "CAP1",
                    "capture_status": "COMPLETED",
                    "amount": {"currency_code": "GBP", "value": "12.50"},
                }

        with patch.object(main, "paypal_client_from_env", return_value=FakePayPal()):
            response = client.post("/api/orders/ORDER1/capture")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["capture_id"], "CAP1")
        self.assertFalse(body["ledger_recorded"])

    def test_capture_writes_each_line_once(self):
        checkout_id, order = save_notebook()
        main.checkout_store().attach_paypal(checkout_id, "ORDER1")

        class FakePayPal:
            def capture_order(self, order_id):
                return {
                    "id": order_id,
                    "status": "COMPLETED",
                    "capture_id": "CAP1",
                    "capture_status": "COMPLETED",
                    "amount": {"currency_code": "GBP", "value": order["total_gbp"]},
                }

        with patch.object(main, "paypal_client_from_env", return_value=FakePayPal()):
            first = client.post("/api/orders/ORDER1/capture")
            second = client.post("/api/orders/ORDER1/capture")
        self.assertTrue(first.json()["ledger_recorded"])
        self.assertFalse(second.json()["ledger_recorded"])
        rows = main.ledger_store().entries()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["sku"], "NOTEBOOK-PACK")
        self.assertEqual(rows[0]["amount_gbp"], "12.50")
        self.assertTrue(main.checkout_store().get(checkout_id)["recorded"])

    def test_a_repeat_of_a_recorded_line_cannot_start_another_payment(self):
        checkout_id, _order = save_notebook()
        main.ledger_store().record(
            sku="NOTEBOOK-PACK",
            merchant="Desk & Co",
            category="stationery",
            amount_gbp="12.50",
            purchased_at=datetime.now(timezone.utc) - timedelta(hours=2),
        )

        def explode():
            raise AssertionError("PayPal should not be called")

        with patch.object(main, "paypal_client_from_env", side_effect=explode):
            response = client.post("/api/orders", json={"checkout_id": checkout_id})
        self.assertEqual(response.status_code, 409)
        self.assertIn("no_duplicates", response.text)


if __name__ == "__main__":
    unittest.main()
