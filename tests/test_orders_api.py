"""Route tests. The PayPal client is replaced so nothing leaves the machine."""

import os
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from src import main
from src.main import DEMO_ITEM

client = TestClient(main.app)


class OrdersApiTests(unittest.TestCase):
    def tearDown(self):
        main.reset_paypal_client()

    def test_health_and_page_render_the_hardcoded_item(self):
        health = client.get("/api/health")
        self.assertEqual(health.status_code, 200)
        self.assertEqual(health.json(), {"status": "ok"})

        page = client.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertIn(DEMO_ITEM["name"], page.text)
        self.assertIn("web-sdk/v6/core", page.text)
        self.assertIn("Propose a basket", page.text)
        self.assertNotIn("PAYPAL_CLIENT_SECRET", page.text)

    def test_config_hides_the_secret_and_reports_missing_credentials(self):
        with patch.dict(os.environ, {"PAYPAL_CLIENT_ID": "", "PAYPAL_CLIENT_SECRET": ""}, clear=False):
            response = client.get("/api/config")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertFalse(body["configured"])
        self.assertIsNone(body["paypal_client_id"])
        self.assertEqual(body["item"]["value"], "12.50")
        self.assertNotIn("client_secret", body)

    def test_create_order_uses_server_price_and_ignores_a_client_amount(self):
        class FakePayPal:
            def __init__(self):
                self.kwargs = None

            def create_order(self, **kwargs):
                self.kwargs = kwargs
                return {"id": "ORDER1", "status": "CREATED"}

        fake = FakePayPal()
        env = {"PAYPAL_CLIENT_ID": "public-id", "PAYPAL_CLIENT_SECRET": "secret"}
        with patch.dict(os.environ, env, clear=False):
            with patch.object(main, "paypal_client_from_env", return_value=fake):
                response = client.post("/api/orders", json={"value": "99999.00", "currency": "USD"})
                config = client.get("/api/config")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["id"], "ORDER1")
        self.assertEqual(fake.kwargs["value"], "12.50")
        self.assertEqual(fake.kwargs["currency"], "GBP")
        self.assertEqual(config.json()["paypal_client_id"], "public-id")
        self.assertNotIn("secret", config.json().values())

    def test_create_order_without_credentials_is_503(self):
        with patch.dict(os.environ, {"PAYPAL_CLIENT_ID": "", "PAYPAL_CLIENT_SECRET": ""}, clear=False):
            response = client.post("/api/orders", json={"value": "1.00"})
        self.assertEqual(response.status_code, 503)

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
        self.assertEqual(response.json()["capture_id"], "CAP1")


if __name__ == "__main__":
    unittest.main()
