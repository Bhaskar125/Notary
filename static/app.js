/* global paypal */

// PayPal JS SDK v6 (docs updated Oct 2026): the button is a <paypal-button>
// element, and createOrder must resolve to { orderId }, not a bare string.
// The button is created only after the policy check passes. This file never
// sends a price. POST /api/orders receives the checkout id and prices the
// basket from the catalog total stored with that check.

let pageConfig = null;
let paypalSdk = null;

function formatMoney(currency, value) {
  if (currency === "GBP") return `£${value}`;
  return `${currency} ${value}`;
}

function errorText(body) {
  if (!body) return "Request failed.";
  if (typeof body.detail === "string") return body.detail;
  if (body.detail && body.detail.message) return body.detail.message;
  return "Request failed.";
}

function showResult(message, payload) {
  document.getElementById("result-message").textContent = message;
  const block = document.getElementById("result-json");
  if (payload) {
    block.hidden = false;
    block.textContent = JSON.stringify(payload, null, 2);
  } else {
    block.hidden = true;
    block.textContent = "";
  }
}

function clearPayPalButton() {
  document.getElementById("paypal-button-container").replaceChildren();
}

async function onLoad() {
  try {
    const response = await fetch("/api/config");
    pageConfig = await response.json();
  } catch (error) {
    document.getElementById("proposal-message").textContent = "Could not reach the Notary server.";
    return;
  }

  const proposalMessage = document.getElementById("proposal-message");
  if (!pageConfig.llm_configured) {
    proposalMessage.textContent =
      "Add LLM_API_KEY, LLM_BASE_URL, and LLM_MODEL to .env, then restart the server. A proposal needs those before it will run.";
  } else {
    proposalMessage.textContent = `Ready. The model is ${pageConfig.llm_model}. PayPal is offered only if the basket passes.`;
  }

  if (!pageConfig.configured || !pageConfig.paypal_client_id) {
    return;
  }
  if (!window.paypal) {
    return;
  }

  try {
    paypalSdk = await window.paypal.createInstance({
      clientId: pageConfig.paypal_client_id,
      components: ["paypal-payments"],
      pageType: "checkout",
      locale: "en-GB",
    });
  } catch (error) {
    console.error(error);
    paypalSdk = null;
  }
}

async function mountPayPalButton(checkoutId, total) {
  clearPayPalButton();
  if (!paypalSdk) {
    showResult(
      "The check passed. Add PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET to .env and restart the server before this basket can be paid.",
    );
    return;
  }

  const eligibility = await paypalSdk.findEligibleMethods({
    currencyCode: "GBP",
    amount: total,
  });
  if (!eligibility.isEligible("paypal")) {
    showResult("PayPal is not an eligible payment method for this browser and this GBP amount. No order was created.");
    return;
  }

  const button = document.createElement("paypal-button");
  document.getElementById("paypal-button-container").append(button);
  const session = await paypalSdk.createPayPalOneTimePaymentSession({
    onApprove,
    onCancel,
    onError,
  });

  button.addEventListener("click", async () => {
    try {
      showResult("Opening PayPal…");
      // Do not await createOrder() before passing it in. The v6 docs say
      // awaiting it first causes transient activation failures.
      const createOrderPromise = createOrder(checkoutId);
      await session.start({ presentationMode: "auto" }, createOrderPromise);
    } catch (error) {
      console.error(error);
      showResult(error.message || "Could not start PayPal checkout.");
    }
  });
  showResult(`Use a sandbox personal account to approve the £${total} payment.`);
}

async function createOrder(checkoutId) {
  const response = await fetch("/api/orders", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ checkout_id: checkoutId }),
  });
  const body = await response.json().catch(() => null);
  if (!response.ok) {
    const message = errorText(body);
    showResult(message, body);
    throw new Error(message);
  }
  return { orderId: body.id };
}

async function onApprove(data) {
  const response = await fetch(`/api/orders/${encodeURIComponent(data.orderId)}/capture`, {
    method: "POST",
  });
  const body = await response.json().catch(() => null);
  if (!response.ok) {
    const message = errorText(body);
    showResult(message, body);
    throw new Error(message);
  }
  const amount = body.amount ? formatMoney(body.amount.currency_code, body.amount.value) : "";
  const status = body.capture_status || body.status;
  const recorded = body.ledger_recorded ? " The spend was written to the ledger." : "";
  showResult(
    `Captured ${amount}. Order ${body.id}, capture ${body.capture_id}, status ${status}.${recorded}`,
    body,
  );
  clearPayPalButton();
  return body;
}

function onCancel() {
  showResult("Payment cancelled. Nothing was captured.");
}

function onError(error) {
  console.error(error);
  const message = (error && error.message) || "PayPal reported an error. Nothing was captured.";
  showResult(message);
}

function onSdkError() {
  const message = document.getElementById("result-message");
  if (message) {
    message.textContent = "Could not load the PayPal sandbox script. Check your network and reload.";
  }
}

function renderVerification(verification) {
  const box = document.getElementById("verification");
  const heading = document.getElementById("verification-heading");
  const list = document.getElementById("verification-list");
  box.hidden = false;
  box.className = verification.passed ? "pass" : "fail";
  heading.textContent = verification.passed ? "Allowed" : "Blocked";
  list.replaceChildren();
  if (verification.passed) {
    const row = document.createElement("li");
    row.textContent = "Every spending rule passed.";
    list.append(row);
    return;
  }
  for (const failure of verification.failures) {
    const row = document.createElement("li");
    const title = document.createElement("strong");
    title.textContent = failure.rule_id;
    row.append(title);
    row.append(document.createTextNode(` — ${failure.explanation}`));
    const detail = counterexampleText(failure.counterexample);
    if (detail) {
      const extra = document.createElement("div");
      extra.className = "counterexample";
      extra.textContent = detail;
      row.append(extra);
    }
    if (failure.node_ids && failure.node_ids.length) {
      const nodes = document.createElement("div");
      nodes.className = "counterexample";
      nodes.textContent = `Nodes: ${failure.node_ids.join(", ")}`;
      row.append(nodes);
    }
    list.append(row);
  }
}

function counterexampleText(example) {
  if (!example || typeof example !== "object") return "";
  const parts = [];
  for (const [key, value] of Object.entries(example)) {
    if (value === null || value === undefined) continue;
    const shown = typeof value === "string" ? value : JSON.stringify(value);
    parts.push(`${key.replaceAll("_", " ")}: ${shown}`);
  }
  return parts.join("; ");
}

document.getElementById("task-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const task = document.getElementById("task").value.trim();
  const message = document.getElementById("proposal-message");
  const card = document.getElementById("proposal");
  const submit = document.getElementById("task-submit");
  if (!task) {
    message.textContent = "Tell the agent what to buy.";
    return;
  }
  submit.disabled = true;
  card.hidden = true;
  clearPayPalButton();
  document.getElementById("verification").hidden = true;
  showResult("");
  message.textContent = "Asking the agent. It may search the catalog a few times.";
  try {
    const response = await fetch("/api/proposals", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ task }),
    });
    const body = await response.json().catch(() => null);
    if (!response.ok) {
      message.textContent = errorText(body);
      return;
    }
    const list = document.getElementById("proposal-items");
    const highlights = new Set((body.graph && body.graph.highlights) || []);
    list.replaceChildren();
    for (const item of body.items) {
      const row = document.createElement("li");
      const price = `${item.quantity} × ${item.name} (${item.merchant}) — £${item.unit_price_gbp} each, £${item.line_total_gbp}`;
      if (highlights.has(`purchase:${item.sku}`)) {
        row.textContent = `${price}. This quantity comes from the product listing, not the user's goal.`;
      } else {
        row.textContent = price;
      }
      list.append(row);
    }
    document.getElementById("proposal-note").textContent = body.note;
    document.getElementById("proposal-total").textContent = `Total £${body.total_gbp}`;
    document.getElementById("proposal-reasoning").textContent = body.reasoning;
    renderVerification(body.verification);
    card.hidden = false;
    const rounds = `Proposed in ${body.rounds} model call${body.rounds === 1 ? "" : "s"}.`;
    if (body.verification.passed) {
      message.textContent = `${rounds} The check passed.`;
      await mountPayPalButton(body.checkout_id, body.total_gbp);
    } else {
      message.textContent = `${rounds} The check blocked this basket. Nothing was sent to PayPal.`;
      showResult("Blocked. No PayPal order was created.");
    }
  } catch (error) {
    console.error(error);
    message.textContent = "Could not reach the Notary server.";
  } finally {
    submit.disabled = false;
  }
});
