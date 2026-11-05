# Notary

Notary sits between a shopping agent and PayPal. The agent may propose a purchase. No PayPal order is created until that proposal has been checked against a spending policy. This repository is the hackathon build. The check itself is not wired up yet.

## What works today

**A catalog and an agent.** `data/catalog.json` has 31 products across 5 merchants. One listing, `USB-C-CABLE`, tells the agent to buy 50 units. The agent searches that file with tools and proposes a basket. The server prices every line from the catalog. A proposal does not create a PayPal order, and the spending policy is not checked yet.

The model is any hosted Chat Completions API. Set `LLM_API_KEY`, `LLM_BASE_URL` (ending in `/v1`), and `LLM_MODEL`. Groq's free tier works: base URL `https://api.groq.com/openai/v1`, model `llama-3.3-70b-versatile`.

**A hardcoded sandbox payment.** An A4 notebook pack at £12.50 can be paid in the PayPal sandbox. That button does not buy the agent's basket.

1. The server creates an order with the Orders v2 API (`POST /v2/checkout/orders`, intent `CAPTURE`).
2. The PayPal JavaScript SDK v6 button sends the buyer to approve it.
3. After approval, the server captures the order (`POST /v2/checkout/orders/{id}/capture`).

The browser never sends a price.

Buyer approval uses the current PayPal quick start (JavaScript SDK v6, October 2026): a `<paypal-button>` element and `createPayPalOneTimePaymentSession`. `createOrder` returns `{ orderId }`. The older `paypal.Buttons()` callback, which returned a bare order id string, is not what the current docs show.

## Run it

Python 3.11 is the target. This step also runs on Python 3.14.

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
```

Put your sandbox app's client id and secret in `.env` (`PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET`). Create those at [developer.paypal.com](https://developer.paypal.com/dashboard/applications/sandbox) → Apps & Credentials → Sandbox.

For the agent, put a hosted API key in `LLM_API_KEY`. The example `.env` targets Groq. Create a key at [console.groq.com/keys](https://console.groq.com/keys). Restart the server after any `.env` change.

```powershell
uvicorn src.main:app --reload
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000). Approve the payment with a **sandbox personal (buyer) account**, not your live PayPal login.

A capture status of `PENDING` can happen when the sandbox business account does not hold a GBP balance. The API call still succeeded; PayPal is waiting on currency acceptance. `COMPLETED` means the sandbox capture settled.

## Tests

```powershell
python -m unittest discover -s tests -v
```

Run that with the virtual environment active (`.venv\Scripts\activate`), or call `.venv\Scripts\python.exe` directly. System Python does not have the project's packages.

The tests stub PayPal and the model. They do not need credentials and they do not move money.

## Layout

```
src/main.py            routes
src/catalog.py         load and search data/catalog.json
src/agent.py           tool-calling loop and proposal pricing
src/paypal_client.py   sandbox OAuth, create, capture
data/catalog.json      31 mock products
static/                the page
tests/                 catalog, agent, and PayPal tests
```

`reference/` is local dissertation code. It is gitignored and is not part of this app.
