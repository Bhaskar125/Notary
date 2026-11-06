"""FastAPI app.

The agent proposes a basket. This module checks it against the spending policy.
PayPal is called only for a basket that passes, and only for the catalog total
stored with that check. A capture is then written to the spend ledger.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from src.agent import AgentError, ChatClient, LLMError, run_agent
from src.catalog import Catalog, load_catalog
from src.checkout import CheckoutStore
from src.ledger import Ledger
from src.paypal_client import PayPalClient, PayPalError
from src.policy import load_policy
from src.verifier import verify

log = logging.getLogger("notary.proposals")

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

# PayPal order ids are uppercase alphanumeric, 1-36 characters.
ORDER_ID_RE = re.compile(r"^[A-Z0-9]{1,36}$")
# PayPal accepted the capture. PENDING is what a US sandbox account often returns for GBP.
_RECORDABLE = {"COMPLETED", "PENDING"}

app = FastAPI(title="Notary", version="0.1.0")
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")

_paypal: PayPalClient | None = None
_catalog: Catalog | None = None
_ledger: Ledger | None = None
_checkouts: CheckoutStore | None = None
_data_dir: Path | None = None


class ProposalRequest(BaseModel):
    task: str = Field(min_length=1, max_length=2000)


class OrderRequest(BaseModel):
    checkout_id: str = Field(pattern=r"^[a-f0-9]{32}$")


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


def set_data_directory(path: Path | None) -> None:
    """Point the ledger and checkout store at a directory. Tests pass a temp folder."""
    global _data_dir, _ledger, _checkouts
    _data_dir = path
    _ledger = None
    _checkouts = None


def data_directory() -> Path:
    return _data_dir or (ROOT / "data")


def ledger_store() -> Ledger:
    global _ledger
    if _ledger is None:
        _ledger = Ledger(data_directory() / "ledger.sqlite")
    return _ledger


def checkout_store() -> CheckoutStore:
    global _checkouts
    if _checkouts is None:
        _checkouts = CheckoutStore(data_directory() / "ledger.sqlite")
    return _checkouts


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
        "currency": "GBP",
    }


@app.post("/api/orders")
def create_order(body: OrderRequest) -> dict:
    """Create a PayPal order for a basket that still passes the policy.

    The amount is the catalog total stored at check time. A price in the
    request body is not read.
    """
    saved = _payable_checkout(body.checkout_id)
    client = _client_or_503()
    order = saved["order"]
    try:
        created = client.create_order(
            currency="GBP",
            value=str(order["total_gbp"]),
            description=_basket_description(order),
            custom_id=saved["id"],
        )
    except PayPalError as exc:
        raise HTTPException(status_code=502, detail=exc.detail()) from exc
    checkout_store().attach_paypal(saved["id"], created["id"])
    return {
        "id": created["id"],
        "status": created.get("status"),
        "checkout_id": saved["id"],
        "total_gbp": order["total_gbp"],
    }


@app.post("/api/orders/{order_id}/capture")
def capture_order(order_id: str) -> dict:
    """Capture an order after the buyer approves it, then record the spend."""
    if not ORDER_ID_RE.fullmatch(order_id):
        raise HTTPException(
            status_code=400,
            detail="Order id must be 1-36 uppercase letters or digits.",
        )
    client = _client_or_503()
    try:
        summary = client.capture_order(order_id)
    except PayPalError as exc:
        raise HTTPException(status_code=502, detail=exc.detail()) from exc
    summary["ledger_recorded"] = _record_capture(order_id, summary)
    return summary


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
        proposal = run_agent(task, default_catalog(), client)
    except LLMError as exc:
        log.warning(
            "proposal failed upstream_status=%s code=%s body=%s",
            exc.status_code,
            exc.code,
            exc.body_text,
        )
        raise HTTPException(
            status_code=502,
            detail={
                "message": exc.message,
                "upstream_status": exc.status_code,
                "upstream_code": exc.code,
            },
        ) from exc
    except AgentError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    finally:
        client.close()
    return _with_verification(task, proposal)


@app.get("/")
def index() -> FileResponse:
    return FileResponse(ROOT / "static" / "index.html")


def _client_or_503() -> PayPalClient:
    try:
        return paypal_client_from_env()
    except MissingPayPalCredentials as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


def _with_verification(task: str, proposal: dict) -> dict:
    order = {
        "items": proposal["items"],
        "total_gbp": proposal["total_gbp"],
        "reasoning": proposal["reasoning"],
    }
    graph = proposal.get("graph") or {"nodes": [], "edges": [], "highlights": []}
    verification = verify(load_policy(), order, graph, history=ledger_store().entries())
    proposal["verification"] = verification
    proposal["paypal_order_created"] = False
    if verification["passed"]:
        proposal["checkout_id"] = checkout_store().save(task=task, order=order, graph=graph)
        proposal["note"] = "Every rule passed. PayPal can take this basket. No order has been created yet."
    else:
        proposal["checkout_id"] = None
        proposal["note"] = "A spending rule failed. PayPal will not be offered for this basket."
    return proposal


def _payable_checkout(checkout_id: str) -> dict:
    saved = checkout_store().get(checkout_id)
    if saved is None:
        raise HTTPException(status_code=404, detail="That basket is not waiting for payment.")
    if saved["recorded"]:
        raise HTTPException(status_code=409, detail="This basket was already captured.")
    verification = verify(
        load_policy(),
        saved["order"],
        saved["graph"],
        history=ledger_store().entries(),
    )
    if not verification["passed"]:
        raise HTTPException(
            status_code=409,
            detail={
                "message": verification["failures"][0]["explanation"],
                "verification": verification,
            },
        )
    return saved


def _basket_description(order: dict) -> str:
    parts = [
        f"{item.get('quantity')} x {item.get('name') or item.get('sku')}"
        for item in order.get("items") or []
    ]
    text = ", ".join(parts) or "Catalog basket"
    if len(text) <= 127:
        return text
    return text[:124] + "..."


def _record_capture(order_id: str, summary: dict) -> bool:
    if summary.get("capture_status") not in _RECORDABLE:
        return False
    saved = checkout_store().find_by_paypal(order_id)
    if saved is None or saved["recorded"]:
        return False
    purchased_at = datetime.now(timezone.utc)
    for item in saved["order"].get("items") or []:
        ledger_store().record(
            sku=str(item.get("sku") or ""),
            merchant=str(item.get("merchant") or ""),
            category=str(item.get("category") or ""),
            amount_gbp=str(item.get("line_total_gbp") or "0.00"),
            purchased_at=purchased_at,
            paypal_order_id=order_id,
        )
    checkout_store().mark_recorded(saved["id"])
    return True
