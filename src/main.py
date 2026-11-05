"""FastAPI app.

Two separate actions live here. The agent can propose a basket from the catalog.
That proposal does not create a PayPal order. The sandbox checkout still buys
one hardcoded notebook pack, and only after the buyer approves the button.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from src.agent import AgentError, ChatClient, LLMError, run_agent
from src.catalog import Catalog, load_catalog
from src.paypal_client import PayPalClient, PayPalError

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

# PayPal order ids are uppercase alphanumeric, 1-36 characters.
ORDER_ID_RE = re.compile(r"^[A-Z0-9]{1,36}$")

# Fixed on purpose. Later the agent proposes the line; this step only proves
# that sandbox create + buyer approval + capture works.
DEMO_ITEM = {
    "sku": "NOTEBOOK-PACK",
    "name": "A4 notebook pack",
    "merchant": "Desk & Co",
    "description": "Pack of 5 ruled A4 notebooks. Hardcoded sandbox item, not chosen by an agent.",
    "currency": "GBP",
    "value": "12.50",
}

app = FastAPI(title="Notary", version="0.1.0")
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")

_paypal: PayPalClient | None = None
_catalog: Catalog | None = None


class ProposalRequest(BaseModel):
    task: str = Field(min_length=1, max_length=2000)


class MissingPayPalCredentials(Exception):
    pass


def paypal_client_from_env() -> PayPalClient:
    global _paypal
    client_id = os.environ.get("PAYPAL_CLIENT_ID", "").strip()
    client_secret = os.environ.get("PAYPAL_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        raise MissingPayPalCredentials(
            "Set PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET in .env and restart the server."
        )
    if _paypal is not None and _paypal.matches(client_id, client_secret):
        return _paypal
    _paypal = PayPalClient(client_id, client_secret)
    return _paypal


def default_catalog() -> Catalog:
    global _catalog
    if _catalog is None:
        _catalog = load_catalog()
    return _catalog


class MissingLLMCredentials(Exception):
    pass


def chat_client_from_env() -> ChatClient:
    api_key = os.environ.get("LLM_API_KEY", "").strip()
    base_url = os.environ.get("LLM_BASE_URL", "").strip()
    model = os.environ.get("LLM_MODEL", "").strip()
    if not api_key or not base_url or not model:
        raise MissingLLMCredentials(
            "Set LLM_API_KEY, LLM_BASE_URL, and LLM_MODEL in .env and restart the server."
        )
    return ChatClient(api_key, base_url, model)


def reset_paypal_client() -> None:
    """Drop the cached client. Tests use this so one case cannot leak into the next."""
    global _paypal
    if _paypal is not None:
        _paypal.close()
    _paypal = None


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/config")
def config() -> dict:
    """Public config for the button. The client secret is never included."""
    client_id = os.environ.get("PAYPAL_CLIENT_ID", "").strip()
    model = os.environ.get("LLM_MODEL", "").strip()
    llm_ready = bool(
        os.environ.get("LLM_API_KEY", "").strip()
        and os.environ.get("LLM_BASE_URL", "").strip()
        and model
    )
    return {
        "configured": bool(client_id and os.environ.get("PAYPAL_CLIENT_SECRET", "").strip()),
        "paypal_client_id": client_id or None,
        "llm_configured": llm_ready,
        "llm_model": model or None,
        "item": DEMO_ITEM,
    }


@app.post("/api/orders")
def create_order() -> dict:
    """Create a PayPal order for DEMO_ITEM. No request body: the price is not the client's."""
    client = _client_or_503()
    try:
        return client.create_order(
            currency=DEMO_ITEM["currency"],
            value=DEMO_ITEM["value"],
            description=f"{DEMO_ITEM['name']} ({DEMO_ITEM['sku']})",
            custom_id=DEMO_ITEM["sku"],
        )
    except PayPalError as exc:
        raise HTTPException(status_code=502, detail=exc.detail()) from exc


@app.post("/api/orders/{order_id}/capture")
def capture_order(order_id: str) -> dict:
    """Capture an order after the buyer approves it in the PayPal button."""
    if not ORDER_ID_RE.fullmatch(order_id):
        raise HTTPException(
            status_code=400,
            detail="Order id must be 1-36 uppercase letters or digits.",
        )
    client = _client_or_503()
    try:
        return client.capture_order(order_id)
    except PayPalError as exc:
        raise HTTPException(status_code=502, detail=exc.detail()) from exc


@app.post("/api/proposals")
def create_proposal(body: ProposalRequest) -> dict:
    """Ask the agent for a basket. This never calls PayPal."""
    task = body.task.strip()
    if not task:
        raise HTTPException(status_code=400, detail="Tell the agent what to buy.")
    try:
        client = chat_client_from_env()
    except MissingLLMCredentials as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    try:
        return run_agent(task, default_catalog(), client)
    except LLMError as exc:
        raise HTTPException(status_code=502, detail=exc.message) from exc
    except AgentError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    finally:
        client.close()


@app.get("/")
def index() -> FileResponse:
    return FileResponse(ROOT / "static" / "index.html")


def _client_or_503() -> PayPalClient:
    try:
        return paypal_client_from_env()
    except MissingPayPalCredentials as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
