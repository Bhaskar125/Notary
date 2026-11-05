/* global paypal */

// PayPal JS SDK v6 (docs updated Oct 2026): the button is a <paypal-button>
// element, and createOrder must resolve to { orderId }, not a bare string.
// The order amount is decided by POST /api/orders. This file never sends a price.

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

async function onLoad() {
  let config;
  try {
    const response = await fetch("/api/config");
    config = await response.json();
  } catch (error) {
    showResult("Could not reach the Notary server.");
    return;
  }

  const proposalMessage = document.getElementById("proposal-message");
  if (!config.llm_configured) {
    proposalMessage.textContent =
      "Add LLM_API_KEY, LLM_BASE_URL, and LLM_MODEL to .env, then restart the server. A proposal needs those before it will run.";
  } else {
    proposalMessage.textContent = `Ready. The model is ${config.llm_model}. A proposal will not create a PayPal order.`;
  }

  const item = config.item;
  document.getElementById("item-merchant").textContent = item.merchant;
  document.getElementById("item-name").textContent = item.name;
  document.getElementById("item-price").textContent = formatMoney(item.currency, item.value);
  document.getElementById("item-description").textContent = item.description;

  if (!config.configured || !config.paypal_client_id) {
    showResult(
      "Add PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET to .env, then restart the server. No order is created until those are set.",
    );
    return;
  }

  if (!window.paypal) {
    showResult("The PayPal sandbox script loaded, but window.paypal is missing.");
    return;
  }

  try {
    const sdkInstance = await window.paypal.createInstance({
      clientId: config.paypal_client_id,
      components: ["paypal-payments"],
      pageType: "checkout",
      locale: "en-GB",
    });

    const eligibility = await sdkInstance.findEligibleMethods({
      currencyCode: item.currency,
      amount: item.value,
    });

    if (!eligibility.isEligible("paypal")) {
      showResult(
        "PayPal is not an eligible payment method for this browser and GBP amount. No order was created.",
      );
      return;
    }

    const button = document.createElement("paypal-button");
    document.getElementById("paypal-button-container").append(button);

    const session = await sdkInstance.createPayPalOneTimePaymentSession({
      onApprove,
      onCancel,
      onError,
    });

    button.addEventListener("click", async () => {
      try {
        showResult("Opening PayPal…");
        // Do not await createOrder() before passing it in. The v6 docs say
        // awaiting it first causes transient activation failures.
        const createOrderPromise = createOrder();
        await session.start({ presentationMode: "auto" }, createOrderPromise);
      } catch (error) {
        console.error(error);
        showResult(error.message || "Could not start PayPal checkout.");
      }
    });

    showResult("Use a sandbox personal account to approve the £12.50 payment.");
  } catch (error) {
    console.error(error);
    showResult(error.message || "Could not start the PayPal button.");
  }
}

async function createOrder() {
  const response = await fetch("/api/orders", { method: "POST" });
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
  showResult(
    `Captured ${amount}. Order ${body.id}, capture ${body.capture_id}, status ${status}.`,
    body,
  );
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
  showResult("Could not load the PayPal sandbox script. Check your network and reload.");
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
    list.replaceChildren();
    for (const item of body.items) {
      const row = document.createElement("li");
      row.textContent = `${item.quantity} × ${item.name} (${item.merchant}) — £${item.unit_price_gbp} each, £${item.line_total_gbp}`;
      list.append(row);
    }
    document.getElementById("proposal-note").textContent = body.note;
    document.getElementById("proposal-total").textContent = `Total £${body.total_gbp}`;
    document.getElementById("proposal-reasoning").textContent = body.reasoning;
    card.hidden = false;
    message.textContent = `Proposed in ${body.rounds} model call${body.rounds === 1 ? "" : "s"}. Nothing was sent to PayPal.`;
  } catch (error) {
    console.error(error);
    message.textContent = "Could not reach the Notary server.";
  } finally {
    submit.disabled = false;
  }
});
