"""PayPal client tests. No network: httpx.MockTransport answers every call."""

import json
import unittest

import httpx

from src.paypal_client import PayPalClient, PayPalError, summarise_capture

TOKEN = {
    "access_token": "test-token",
    "token_type": "Bearer",
    "expires_in": 3600,
}

CAPTURED = {
    "id": "ORDER1",
    "status": "COMPLETED",
    "purchase_units": [
        {
            "payments": {
                "captures": [
                    {
                        "id": "CAP1",
                        "status": "COMPLETED",
                        "amount": {"currency_code": "GBP", "value": "12.50"},
                    }
                ]
            }
        }
    ],
}


# This checks create and capture against a fake PayPal, including the stable capture request id.
class PayPalClientTests(unittest.TestCase):
    def test_create_then_capture_uses_bearer_token_and_stable_capture_id(self):
        seen = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            if request.url.path == "/v1/oauth2/token":
                self.assertEqual(request.headers["authorization"].split()[0], "Basic")
                self.assertIn("grant_type=client_credentials", request.content.decode())
                return httpx.Response(200, json=TOKEN)
            if request.url.path == "/v2/checkout/orders" and request.method == "POST":
                self.assertTrue(request.headers["authorization"].startswith("Bearer test-token"))
                self.assertIn("paypal-request-id", request.headers)
                body = json.loads(request.content)
                self.assertEqual(body["intent"], "CAPTURE")
                self.assertNotIn("invoice_id", body["purchase_units"][0])
                self.assertEqual(
                    body["purchase_units"][0]["amount"],
                    {"currency_code": "GBP", "value": "12.50"},
                )
                return httpx.Response(201, json={"id": "ORDER1", "status": "CREATED"})
            if request.url.path == "/v2/checkout/orders/ORDER1/capture":
                self.assertEqual(request.content, b"{}")
                return httpx.Response(201, json=CAPTURED)
            raise AssertionError(f"unexpected {request.method} {request.url}")

        client = PayPalClient(
            "client-id",
            "client-secret",
            http=httpx.Client(
                transport=httpx.MockTransport(handler),
                base_url="https://api-m.sandbox.paypal.com",
            ),
        )
        created = client.create_order(
            currency="GBP",
            value="12.50",
            description="A4 notebook pack (NOTEBOOK-PACK)",
            custom_id="NOTEBOOK-PACK",
        )
        first = client.capture_order("ORDER1")
        second = client.capture_order("ORDER1")

        self.assertEqual(created, {"id": "ORDER1", "status": "CREATED"})
        self.assertEqual(first["capture_id"], "CAP1")
        self.assertEqual(first["capture_status"], "COMPLETED")
        self.assertEqual(first["amount"]["value"], "12.50")

        create_ids = [
            request.headers["paypal-request-id"]
            for request in seen
            if request.url.path == "/v2/checkout/orders"
        ]
        capture_ids = [
            request.headers["paypal-request-id"]
            for request in seen
            if request.url.path.endswith("/capture")
        ]
        self.assertEqual(len(create_ids), 1)
        self.assertEqual(capture_ids[0], capture_ids[1])
        self.assertNotEqual(create_ids[0], capture_ids[0])
        # Token call happens once, then the cached token covers create and both captures.
        token_calls = [request for request in seen if request.url.path == "/v1/oauth2/token"]
        self.assertEqual(len(token_calls), 1)
        self.assertEqual(second["id"], "ORDER1")

    def test_paypal_error_includes_debug_id(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/v1/oauth2/token":
                return httpx.Response(
                    401,
                    json={"error": "invalid_client", "error_description": "Client Authentication failed", "debug_id": "DBG1"},
                )
            raise AssertionError("should have failed on the token call")

        client = PayPalClient(
            "bad",
            "bad",
            http=httpx.Client(
                transport=httpx.MockTransport(handler),
                base_url="https://api-m.sandbox.paypal.com",
            ),
        )
        with self.assertRaises(PayPalError) as caught:
            client.create_order(currency="GBP", value="12.50", description="x", custom_id="SKU")
        self.assertEqual(caught.exception.status_code, 401)
        self.assertEqual(caught.exception.debug_id, "DBG1")
        self.assertIn("Authentication", caught.exception.message)

    def test_summarise_capture_tolerates_a_missing_payments_block(self):
        summary = summarise_capture({"id": "ORDER9", "status": "COMPLETED"})
        self.assertEqual(summary["id"], "ORDER9")
        self.assertIsNone(summary["capture_id"])

    def test_pending_gbp_capture_is_reported_as_pending(self):
        summary = summarise_capture(
            {
                "id": "ORDER2",
                "status": "COMPLETED",
                "purchase_units": [
                    {
                        "payments": {
                            "captures": [
                                {
                                    "id": "CAP2",
                                    "status": "PENDING",
                                    "amount": {"currency_code": "GBP", "value": "12.50"},
                                }
                            ]
                        }
                    }
                ],
            }
        )
        self.assertEqual(summary["capture_status"], "PENDING")


if __name__ == "__main__":
    unittest.main()
