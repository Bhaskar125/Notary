"""FastAPI app.

The agent proposes a basket. This module checks it against the spending policy.
PayPal is called only for a basket that passes, and only for the catalog total
stored with that check. A capture is then written to the spend ledger.
"""

from __future__ import annotations

import logging
import os
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
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
# override=True so a restart picks up a new PayPal app even if the process
# already inherited the previous client id from the environment.
load_dotenv(ROOT / ".env", override=True)

# PayPal order ids are uppercase alphanumeric, 1-36 characters.
ORDER_ID_RE = re.compile(r"^[A-Z0-9]{1,36}$")
# PayPal accepted the capture. PENDING is what a US sandbox account often returns for GBP.
_RECORDABLE = {"COMPLETED", "PENDING"}
# One browser, one ledger. A shared file would make judge A's pen purchase
# block judge B's happy path for 24 hours.
SESSION_COOKIE = "notary_session"
_SESSION_RE = re.compile(r"^[a-f0-9]{32}$")
CHECKOUT_TTL = timedelta(minutes=30)
# A public URL can burn the model quota. Ten proposals a minute per caller.
_PROPOSAL_LIMIT = 10
_PROPOSAL_WINDOW_SECONDS = 60.0
_proposal_hits: dict[str, list[float]] = {}

app = FastAPI(title="Notary", version="0.1.0")
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")

_paypal: PayPalClient | None = None
_catalog: Catalog | None = None
_ledger: Ledger | None = None
_checkouts: CheckoutStore | None = None
_data_dir: Path | None = None


# This is the sentence the browser sends. Two thousand characters is as long as a task can be.
class ProposalRequest(BaseModel):
    task: str = Field(min_length=1, max_length=2000)


# This is the only field the browser may send when it wants to pay. The price stays on the server.
class OrderRequest(BaseModel):
    checkout_id: str = Field(pattern=r"^[a-f0-9]{32}$")


class MissingPayPalCredentials(Exception):
    pass


# This builds the PayPal client from .env and keeps it until the keys change.
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


# This loads the product file once and reuses it for every proposal.
def default_catalog() -> Catalog:
    global _catalog
    if _catalog is None:
        _catalog = load_catalog()
    return _catalog


class MissingLLMCredentials(Exception):
    pass


# This builds the model client. The key, the base URL, and the model name all have to be set.
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
    _proposal_hits.clear()


@app.middleware("http")
# This gives each browser its own id. A missing cookie gets a new one, then we set it on the way out.
async def attach_session(request: Request, call_next):
    raw = request.cookies.get(SESSION_COOKIE, "")
    session_id = raw if _SESSION_RE.fullmatch(raw) else uuid.uuid4().hex
    request.state.session_id = session_id
    response = await call_next(request)
    response.set_cookie(
        SESSION_COOKIE,
        session_id,
        max_age=7 * 24 * 60 * 60,
        httponly=True,
        samesite="lax",
        path="/",
    )
    return response


def _caller(request: Request) -> str:
    """The visitor, not Render's proxy.

    With ``--proxy-headers --forwarded-allow-ips=*``, uvicorn sets
    ``request.client`` from the first address in ``X-Forwarded-For``.
    TestClient does not run that middleware, so when the header is present
    we apply the same rule here. The last address is a proxy hop and would
    put every judge in one bucket.
    """
    forwarded = request.headers.get("x-forwarded-for", "")
    first = forwarded.split(",")[0].strip()
    if first:
        if first.startswith("[") and "]" in first:
            first = first[1 : first.index("]")]
        elif first.count(":") == 1:
            first = first.split(":", 1)[0]
        return first[:80]
    if request.client is None or not request.client.host:
        return "unknown"
    return request.client.host[:80]


# This counts proposals for one visitor and refuses the eleventh inside a minute.
def _proposal_allowed(request: Request) -> bool:
    caller = _caller(request)
    now = time.monotonic()
    recent = [stamp for stamp in _proposal_hits.get(caller, []) if now - stamp < _PROPOSAL_WINDOW_SECONDS]
    if len(recent) >= _PROPOSAL_LIMIT:
        _proposal_hits[caller] = recent
        return False
    recent.append(now)
    _proposal_hits[caller] = recent
    return True


# This is the folder the sqlite file lives in. Tests point it at a temporary folder.
def data_directory() -> Path:
    return _data_dir or (ROOT / "data")


# This opens the spend ledger, creating the file the first time someone pays.
def ledger_store() -> Ledger:
    global _ledger
    if _ledger is None:
        _ledger = Ledger(data_directory() / "ledger.sqlite")
    return _ledger


# This opens the checkout table in the same sqlite file as the ledger.
def checkout_store() -> CheckoutStore:
    global _checkouts
    if _checkouts is None:
        _checkouts = CheckoutStore(data_directory() / "ledger.sqlite")
    return _checkouts


@app.get("/api/health")
# This is the Render health check. It does not call PayPal or the model.
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/catalog")
def catalog() -> dict:
    """Names and prices a tester can ask for. Listing text stays with the agent."""
    return {
        "products": [
            {
                "sku": product.sku,
                "name": product.name,
                "merchant": product.merchant,
                "category": product.category,
                "price_gbp": product.public()["price_gbp"],
            }
            for product in default_catalog().products()
        ]
    }


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
def create_order(body: OrderRequest, request: Request) -> dict:
    """Create a PayPal order for a basket that still passes the policy.

    The amount is the catalog total stored at check time. A price in the
    request body is not read.
    """
    saved = _payable_checkout(body.checkout_id, request.state.session_id)
    client = _client_or_503()
    order = saved["order"]
    try:
        created = client.create_order(
            currency="GBP",
            value=str(order["total_gbp"]),
            description=_basket_description(order),
            # The sandbox order carries the checkout id: the check that allowed it.
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
def capture_order(order_id: str, request: Request) -> dict:
    """Capture an order after the buyer approves it, then record the spend."""
    if not ORDER_ID_RE.fullmatch(order_id):
        raise HTTPException(
            status_code=400,
            detail="Order id must be 1-36 uppercase letters or digits.",
        )
    saved = _guard_checkout(checkout_store().find_by_paypal(order_id), request.state.session_id)
    client = _client_or_503()
    try:
        summary = client.capture_order(order_id)
    except PayPalError as exc:
        raise HTTPException(status_code=502, detail=exc.detail()) from exc
    summary["ledger_recorded"] = _record_capture(order_id, summary, request.state.session_id)
    return summary


@app.post("/api/proposals")
def create_proposal(body: ProposalRequest, request: Request) -> dict:
    """Ask the agent for a basket. This never calls PayPal."""
    task = body.task.strip()
    if not task:
        raise HTTPException(status_code=400, detail="Tell the agent what to buy.")
    if not _proposal_allowed(request):
        raise HTTPException(status_code=429, detail="Too many proposals. Wait a minute and try again.")
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
    return _with_verification(task, proposal, request.state.session_id)


@app.get("/")
# This serves the page. The browser then loads the CSS, the script, and the PayPal button.
def index() -> FileResponse:
    return FileResponse(ROOT / "static" / "index.html")


# This refuses to call PayPal when the sandbox keys are missing.
def _client_or_503() -> PayPalClient:
    try:
        return paypal_client_from_env()
    except MissingPayPalCredentials as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


# This runs the policy check, saves a checkout only when every rule passes, and stamps the receipt.
def _with_verification(task: str, proposal: dict, session_id: str) -> dict:
    order = {
        "items": proposal["items"],
        "total_gbp": proposal["total_gbp"],
        "reasoning": proposal["reasoning"],
    }
    graph = proposal.get("graph") or {"nodes": [], "edges": [], "highlights": []}
    policy = load_policy()
    verification = verify(
        policy,
        order,
        graph,
        history=ledger_store().entries(session_id=session_id),
    )
    proposal["verification"] = verification
    proposal["paypal_order_created"] = False
    if verification["passed"]:
        proposal["checkout_id"] = checkout_store().save(
            task=task,
            order=order,
            graph=graph,
            session_id=session_id,
        )
        proposal["note"] = "Every rule passed. PayPal can take this basket. No order has been created yet."
    else:
        proposal["checkout_id"] = None
        proposal["note"] = "A spending rule failed. PayPal will not be offered for this basket."
    proposal["receipt"] = _receipt(policy, verification)
    return proposal


def _receipt(policy, verification: dict) -> dict:
    """A read-only stamp of the check. It does not decide whether PayPal is called."""
    failed = {item["rule_id"] for item in verification["failures"]}
    labels = (
        ("max_per_order", f"No order over £{_gbp(policy.max_per_order_pence)}"),
        (
            "max_per_period",
            f"No more than £{_gbp(policy.max_per_period_pence)} in {policy.period_days} days",
        ),
        ("allowed_merchants", "Merchant is on the allowed list"),
        ("blocked_categories", "Category is not blocked"),
        (
            "no_duplicates",
            f"No repeat from the same merchant within {policy.duplicate_window_hours} hours",
        ),
        ("arithmetic_consistency", "The stated total matches the basket"),
        ("goal_grounding", "Every purchase is supported by the user's goal"),
    )
    return {
        "policy": "default",
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "rules": [
            {"rule_id": rule_id, "label": label, "passed": rule_id not in failed}
            for rule_id, label in labels
        ],
    }


# This turns a cap stored in pence into the pounds string printed on the receipt.
def _gbp(pence: int) -> str:
    return f"{pence / 100:.2f}"


# This loads the saved basket and checks it again, so a pass cannot go stale before PayPal is called.
def _payable_checkout(checkout_id: str, session_id: str) -> dict:
    saved = _guard_checkout(checkout_store().get(checkout_id), session_id)
    verification = verify(
        load_policy(),
        saved["order"],
        saved["graph"],
        history=ledger_store().entries(session_id=session_id),
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


# This is the short text PayPal shows on the order. PayPal only keeps the first 127 characters.
def _basket_description(order: dict) -> str:
    parts = [
        f"{item.get('quantity')} x {item.get('name') or item.get('sku')}"
        for item in order.get("items") or []
    ]
    text = ", ".join(parts) or "Catalog basket"
    if len(text) <= 127:
        return text
    return text[:124] + "..."


def _guard_checkout(saved: dict | None, session_id: str) -> dict:
    """A capture or payment must be one we stored, for this browser, and still fresh."""
    if saved is None or (saved.get("session_id") or "") != session_id:
        raise HTTPException(status_code=404, detail="That basket is not waiting for payment.")
    if saved["recorded"]:
        raise HTTPException(status_code=409, detail="This basket was already captured.")
    created = datetime.fromisoformat(saved["created_at"])
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) - created > CHECKOUT_TTL:
        raise HTTPException(status_code=409, detail="This check expired. Propose the basket again.")
    return saved


# This writes each basket line to the ledger after a capture, and skips a line that was already written.
def _record_capture(order_id: str, summary: dict, session_id: str) -> bool:
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
            session_id=session_id,
        )
    checkout_store().mark_recorded(saved["id"])
    return True
