"""PayPal Orders v2 sandbox client: OAuth, create order, capture order.

Endpoints, from the current PayPal docs (not guessed):

- POST https://api-m.sandbox.paypal.com/v1/oauth2/token
- POST https://api-m.sandbox.paypal.com/v2/checkout/orders
- POST https://api-m.sandbox.paypal.com/v2/checkout/orders/{id}/capture

The buyer must approve the order between create and capture. This module
does not approve anything; the JS SDK button does that in the browser.
"""

from __future__ import annotations

import threading
import uuid
from typing import Any

import httpx

SANDBOX_API = "https://api-m.sandbox.paypal.com"

# Same id on a retry must replay the original capture, not take the money twice.
# A fresh UUID per click would defeat that. uuid5 makes the capture id stable
# for a given order id without storing anything.
_REQUEST_NAMESPACE = uuid.UUID("6f1c0c3e-7c1a-4f0e-9b3a-0c8e5a1d4b77")


class PayPalError(Exception):
    """PayPal returned a non-success status, or the response was not JSON."""

    def __init__(self, message: str, status_code: int, body: dict[str, Any]):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.body = body

    @property
    def debug_id(self) -> str | None:
        value = self.body.get("debug_id")
        return value if isinstance(value, str) else None

    def detail(self) -> dict[str, Any]:
        return {
            "message": self.message,
            "paypal_status": self.status_code,
            "debug_id": self.debug_id,
        }


class PayPalClient:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        *,
        base_url: str = SANDBOX_API,
        http: httpx.Client | None = None,
    ):
        self._client_id = client_id
        self._client_secret = client_secret
        self._http = http or httpx.Client(base_url=base_url, timeout=30.0)
        self._owns_http = http is None
        self._token: str | None = None
        self._token_deadline: float = 0.0
        self._lock = threading.Lock()

    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    def matches(self, client_id: str, client_secret: str) -> bool:
        # This tells the app whether the cached client still belongs to the keys in .env.
        return self._client_id == client_id and self._client_secret == client_secret

    def create_order(self, *, currency: str, value: str, description: str, custom_id: str) -> dict[str, Any]:
        """Create an order. Does not capture. `value` is a decimal string, e.g. '12.50'."""
        if len(currency) != 3 or not currency.isalpha():
            raise ValueError("currency must be a 3-letter ISO code")
        # GBP has two decimal places. A fixed invoice_id is intentionally absent:
        # PayPal rejects a second order that reuses one (DUPLICATE_INVOICE_ID),
        # which would break the second demo click.
        payload = {
            "intent": "CAPTURE",
            "purchase_units": [
                {
                    "description": description[:127],
                    "custom_id": custom_id[:127],
                    "amount": {
                        "currency_code": currency.upper(),
                        "value": value,
                    },
                }
            ],
        }
        body = self._post("/v2/checkout/orders", payload, request_id=str(uuid.uuid4()))
        order_id = body.get("id")
        if not isinstance(order_id, str) or not order_id:
            raise PayPalError("PayPal create-order response had no id", 502, body)
        return {"id": order_id, "status": body.get("status")}

    def capture_order(self, order_id: str) -> dict[str, Any]:
        """Capture an order the buyer has already approved."""
        request_id = str(uuid.uuid5(_REQUEST_NAMESPACE, f"capture:{order_id}"))
        body = self._post(
            f"/v2/checkout/orders/{order_id}/capture",
            {},
            request_id=request_id,
        )
        return summarise_capture(body)

    # This sends one authenticated PayPal call. The request id is how a retry stays the same call.
    def _post(self, path: str, payload: dict[str, Any], *, request_id: str) -> dict[str, Any]:
        response = self._http.post(
            path,
            json=payload,
            headers={
                "Authorization": f"Bearer {self._access_token()}",
                "PayPal-Request-Id": request_id,
                # Ask for the full order resource so status and captures are present.
                "Prefer": "return=representation",
            },
        )
        return self._json_or_raise(response)

    # This gets a sandbox access token and reuses it until a minute before it expires.
    def _access_token(self) -> str:
        # PayPal tokens last for expires_in seconds (often hours). Refresh a minute
        # early. Cached on this instance; main.py reuses one instance per process.
        now = time_monotonic()
        with self._lock:
            if self._token and now < self._token_deadline:
                return self._token
            response = self._http.post(
                "/v1/oauth2/token",
                data={"grant_type": "client_credentials"},
                auth=httpx.BasicAuth(self._client_id, self._client_secret),
                headers={"Accept": "application/json"},
            )
            body = self._json_or_raise(response)
            token = body.get("access_token")
            if not isinstance(token, str) or not token:
                raise PayPalError("PayPal token response had no access_token", 502, body)
            expires_in = body.get("expires_in", 0)
            try:
                lifetime = float(expires_in)
            except (TypeError, ValueError):
                lifetime = 0
            self._token = token
            self._token_deadline = time_monotonic() + max(0.0, lifetime - 60.0)
            return token

    @staticmethod
    def _json_or_raise(response: httpx.Response) -> dict[str, Any]:
        try:
            body = response.json()
        except ValueError:
            body = {"raw": response.text}
        if not isinstance(body, dict):
            body = {"raw": body}
        if response.status_code >= 400:
            message = body.get("message") or body.get("error_description") or response.reason_phrase
            raise PayPalError(str(message), response.status_code, body)
        return body


def summarise_capture(payload: dict[str, Any]) -> dict[str, Any]:
    """Pull the fields the page needs out of a capture response.

    A GBP capture can come back PENDING when the sandbox business account
    does not hold GBP. That is still a completed API call; the status is
    returned as PayPal sent it.
    """
    capture: dict[str, Any] | None = None
    units = payload.get("purchase_units") or []
    if units and isinstance(units[0], dict):
        payments = units[0].get("payments") or {}
        captures = payments.get("captures") or []
        if captures and isinstance(captures[0], dict):
            capture = captures[0]
    return {
        "id": payload.get("id"),
        "status": payload.get("status"),
        "capture_id": None if capture is None else capture.get("id"),
        "capture_status": None if capture is None else capture.get("status"),
        "amount": None if capture is None else capture.get("amount"),
    }


# This is a clock the token cache can call. Tests replace it so they do not wait out a real hour.
def time_monotonic() -> float:
    import time

    return time.monotonic()
