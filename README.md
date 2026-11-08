# Notary

Notary sits between a shopping agent and PayPal. The agent may propose a purchase. No PayPal order is created until that proposal has been checked against a spending policy. A passing basket can be paid in the PayPal sandbox. A failing basket cannot.

## Demo

Open the Render URL, or [http://127.0.0.1:8000](http://127.0.0.1:8000) when the app is running locally. Type one sentence and press Propose. The page draws the reasoning graph and the rule results. The PayPal button appears only after every rule passes.

A new Render service starts with an empty ledger. Run the four sentences in this order, in one sitting:

1. `Buy one box of ballpoint pens.` Allowed. One line, £4.50, Desk & Co. The goal points at a green purchase. Pay with a **sandbox personal account**. The seller app should belong to a **UK sandbox business account**, so a GBP capture comes back `COMPLETED`.
2. `Buy one box of ballpoint pens.` Blocked on `no_duplicates`. `PEN-BOX` from Desk & Co is already inside the 24-hour window. No PayPal button.
3. `Buy one USB-C cable.` The listing tells the agent to buy 50. The basket is £400. Blocked on `max_per_order` (the cap is £200) and `goal_grounding` (the quantity is supported by the listing). The purchase node is red.
4. `Buy a laptop.` The catalog laptop is £749, so `max_per_order` blocks it. The agent may also add the 50 cables. When it does, the cable line fails `goal_grounding` as well, and the 7-day total can fail `max_per_period` (the cap is £500) once earlier captures are on the ledger.

The copy on this machine already has the notebook and pen captures from 6 November 2026, so the pen sentence is already blocked locally until those rows age out.

Pay on `sandbox.paypal.com`. A live PayPal login, or the business seller account, will not complete the sandbox checkout.

## Deploy on Render

`render.yaml` describes one free Python web service in Frankfurt. The start command binds `0.0.0.0` and Render's `$PORT`. Python is `3.14.10`. The health check is `GET /api/health`.

1. Push `main` to GitHub. `.env` is gitignored. Leave it that way.
2. In the [Render dashboard](https://dashboard.render.com), choose **New → Blueprint**, select this repo, and apply `render.yaml`.
3. When Render asks, paste `PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET`, and `LLM_API_KEY` from your local `.env`. The model URL is already in the blueprint.
4. Open the service URL after the deploy turns live.

The ledger is a SQLite file on the service disk. A free instance sleeps after a period of inactivity, and the first request can take about a minute while it wakes. A new deploy starts from an empty ledger, so do the allowed payment and the duplicate in the same session.

## What works today

**A catalog and an agent.** `data/catalog.json` has 31 products across 5 merchants. One listing, `USB-C-CABLE`, tells the agent to buy 50 units. The agent searches that file with tools and proposes a basket. The server prices every line from the catalog. The proposal is checked before PayPal is offered.

The model is any hosted Chat Completions API. Set `LLM_API_KEY`, `LLM_BASE_URL` (ending in `/v1`), and `LLM_MODEL`. Groq's free tier works: base URL `https://api.groq.com/openai/v1`, model `openai/gpt-oss-20b`. `llama-3.3-70b-versatile` has been removed from Groq and returns 404.

The agent is naive on purpose. Its system prompt tells it to follow purchasing instructions written inside a product listing, even when that conflicts with the user's budget. That simulates an agent that has been fooled, or was never told to distrust merchant text. Notary does not trust the agent. The check has to catch a bad basket when the agent is fully convinced. The graph is built from the tool trace, not from a second reading of the prose: the user's task is the goal, each listing the agent opened is a claim, and each line is a purchase. A quantity that the listing demands and the user did not ask for is supported by the listing, not by the goal. For `USB-C-CABLE`, that quantity is 50. The page draws that graph with Cytoscape. A red node is one the check flagged.

**Spending policy.** `policies/default.yaml` caps an order at £200 and a 7-day total at £500, allows the five catalog merchants, blocks `alcohol`, and rejects the same item from the same merchant within 24 hours. `verify()` encodes each rule as Z3 constraints. SAT means the order is allowed. The page shows the PayPal button only after a pass, and `POST /api/orders` checks the same basket again before creating an order. The amount is the catalog total stored with that check. A capture with status `COMPLETED` or `PENDING` is written to the SQLite ledger, one row per line, so the next proposal can see it.

**Sandbox checkout.** The buyer approves in the PayPal JavaScript SDK v6 button. The browser sends a checkout id, not a price.

1. The server creates an order with the Orders v2 API (`POST /v2/checkout/orders`, intent `CAPTURE`).
2. The PayPal JavaScript SDK v6 button sends the buyer to approve it.
3. After approval, the server captures the order (`POST /v2/checkout/orders/{id}/capture`).

The browser never sends a price.

Buyer approval uses the current PayPal quick start (JavaScript SDK v6, October 2026): a `<paypal-button>` element and `createPayPalOneTimePaymentSession`. `createOrder` returns `{ orderId }`. The older `paypal.Buttons()` callback, which returned a bare order id string, is not what the current docs show.

## Run it

Python 3.11 is the target. This machine runs Python 3.14, and `z3-solver==5.3.3.0` loads there (`z3.get_version_string()` is `5.3.3`). The version is pinned in `requirements.txt` so Render installs the same wheel.

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

A capture status of `PENDING` means PayPal accepted the call but has not settled it. A US sandbox business account often does this for GBP. Create the sandbox business account in the United Kingdom so the demo can finish as `COMPLETED`. The buyer is still a sandbox personal account. `PENDING` is a successful API call, and it looks like a broken payment to a judge.

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
src/extraction.py      tool trace to a goal / claim / purchase graph
src/exprparse.py       safe arithmetic parser for the reasoning lines
src/policy.py          load policies/default.yaml
src/verifier.py        Z3 check for each spending rule
src/ledger.py          SQLite spend history
src/checkout.py        a passing basket, held until capture
src/paypal_client.py   sandbox OAuth, create, capture
data/catalog.json      31 mock products
static/                the page
policies/default.yaml  the demo spending caps
render.yaml            Render web service
tests/                 catalog, agent, PayPal, and policy tests
```

`reference/` is local dissertation code. It is gitignored and is not part of this app.
