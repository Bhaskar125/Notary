/* global paypal */

// PayPal JS SDK v6 (docs updated Oct 2026): the button is a <paypal-button>
// element, and createOrder must resolve to { orderId }, not a bare string.
// The button is created only after the policy check passes. This file never
// sends a price. POST /api/orders receives the checkout id and prices the
// basket from the catalog total stored with that check.

let pageConfig = null;
let paypalSdk = null;
let graphView = null;

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

function setProposalVisible(visible) {
  document.getElementById("proposal").hidden = !visible;
  const empty = document.getElementById("result-empty");
  if (empty) empty.hidden = visible;
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

function clearGraph() {
  if (graphView) {
    graphView.destroy();
    graphView = null;
  }
  const detail = document.getElementById("graph-detail");
  if (detail) detail.textContent = "";
  const suggestions = document.getElementById("suggestions");
  const suggestionList = document.getElementById("suggestion-list");
  if (suggestionList) suggestionList.replaceChildren();
  if (suggestions) suggestions.hidden = true;
}

function clip(text, max) {
  const value = String(text || "").replace(/\s+/g, " ").trim();
  if (value.length <= max) return value;
  return `${value.slice(0, max - 1)}…`;
}

function nodeLabel(node) {
  const id = String(node.id || "");
  if (node.type === "goal") return clip(node.text || "Goal", 80);
  if (id.endsWith(":quantity")) return "Quantity from the listing";
  if (node.type === "suggestion") return node.saves_gbp ? `Save £${node.saves_gbp}` : "Save";
  if (id.startsWith("listing:")) return node.sku || id;
  return clip(node.text || id, 48);
}

function renderGraph(graph, verification) {
  clearGraph();
  const container = document.getElementById("graph");
  const detail = document.getElementById("graph-detail");
  if (!graph || !Array.isArray(graph.nodes) || graph.nodes.length === 0) {
    detail.textContent = "This proposal has no graph.";
    return;
  }
  if (!window.cytoscape) {
    detail.textContent = "The graph library did not load. Refresh the page.";
    return;
  }

  const flagged = new Set(graph.highlights || []);
  for (const failure of (verification && verification.failures) || []) {
    for (const id of failure.node_ids || []) flagged.add(id);
  }

  const elements = [];
  for (const node of graph.nodes) {
    elements.push({
      data: {
        id: node.id,
        label: nodeLabel(node),
        full: node.text || node.id,
        kind: node.type || "claim",
        flagged: flagged.has(node.id) ? "yes" : "no",
      },
    });
  }
  for (const edge of graph.edges || []) {
    if (!edge.source || !edge.target) continue;
    // Support is stored as "this line is supported by that node".
    // Draw the arrow from the support toward the line that relies on it.
    // A suggestion hangs off the purchase, so that arrow stays as stored.
    const advice = edge.relation === "suggests";
    elements.push({
      data: {
        id: `${edge.source}__${edge.target}`,
        source: advice ? edge.source : edge.target,
        target: advice ? edge.target : edge.source,
        relation: edge.relation || "supported_by",
      },
    });
  }

  graphView = window.cytoscape({
    container,
    elements,
    layout: {
      name: "breadthfirst",
      directed: true,
      padding: 28,
      spacingFactor: 1.25,
    },
    minZoom: 0.35,
    maxZoom: 2,
    wheelSensitivity: 0.25,
    style: [
      {
        selector: "node",
        style: {
          label: "data(label)",
          "text-wrap": "wrap",
          "text-max-width": "130px",
          "font-size": "12px",
          "font-family": "Outfit, Segoe UI, sans-serif",
          color: "#1c1915",
          "text-valign": "center",
          "text-halign": "center",
          "background-color": "#fffdf8",
          "border-width": 2,
          "border-color": "#d9d0c1",
          width: 150,
          height: 52,
          shape: "round-rectangle",
        },
      },
      {
        selector: 'node[kind = "goal"]',
        style: {
          "background-color": "#1c1915",
          color: "#f3efe6",
          "border-color": "#1c1915",
          width: 210,
          height: 68,
          "text-max-width": "190px",
        },
      },
      {
        selector: 'node[kind = "purchase"]',
        style: { "border-color": "#2f6b45", width: 170 },
      },
      {
        selector: 'node[kind = "suggestion"]',
        style: {
          "background-color": "#f8edd6",
          "border-color": "#8a5a12",
          "border-style": "dashed",
          width: 128,
          height: 46,
          "font-size": "12px",
        },
      },
      {
        selector: 'node[flagged = "yes"]',
        style: {
          "background-color": "#f8efe9",
          "border-color": "#8c2f2f",
          "border-width": 3,
          color: "#1c1915",
        },
      },
      {
        selector: "edge",
        style: {
          width: 1.5,
          "line-color": "#5c564c",
          "target-arrow-color": "#5c564c",
          "target-arrow-shape": "triangle",
          "curve-style": "bezier",
          "arrow-scale": 0.85,
        },
      },
      {
        selector: 'edge[relation = "suggests"]',
        style: {
          "line-style": "dashed",
          "line-color": "#8a5a12",
          "target-arrow-color": "#8a5a12",
        },
      },
      {
        selector: "node:selected",
        style: { "overlay-opacity": 0.12, "overlay-color": "#1c1915" },
      },
    ],
  });

  graphView.on("tap", "node", (event) => {
    detail.textContent = event.target.data("full");
  });

  const prefer = (graph.highlights && graph.highlights[0]) || [...flagged][0];
  let chosen = prefer ? graphView.getElementById(prefer) : graphView.nodes().first();
  if (!chosen || chosen.empty()) chosen = graphView.nodes().first();
  if (chosen && chosen.nonempty()) {
    chosen.select();
    detail.textContent = chosen.data("full");
  }
  requestAnimationFrame(() => {
    if (!graphView) return;
    graphView.resize();
    graphView.fit(undefined, 28);
  });
  renderSuggestions(graph);
}

function renderSuggestions(graph) {
  const box = document.getElementById("suggestions");
  const list = document.getElementById("suggestion-list");
  const notes = (graph && graph.suggestions) || [];
  if (!box || !list || notes.length === 0) return;
  const saved = document.createElement("li");
  saved.className = "suggestion-total";
  saved.textContent = `A cheaper basket would cost £${graph.savings_gbp} less. PayPal still charges the basket the agent proposed.`;
  list.append(saved);
  for (const note of notes) {
    const row = document.createElement("li");
    row.textContent = note.text;
    list.append(row);
  }
  box.hidden = false;
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

document.querySelectorAll("[data-example]").forEach((button) => {
  button.addEventListener("click", () => {
    const field = document.getElementById("task");
    field.value = button.dataset.example;
    field.focus();
  });
});

document.getElementById("task-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const task = document.getElementById("task").value.trim();
  const message = document.getElementById("proposal-message");
  const submit = document.getElementById("task-submit");
  if (!task) {
    message.textContent = "Tell the agent what to buy.";
    return;
  }
  submit.disabled = true;
  setProposalVisible(false);
  clearPayPalButton();
  clearGraph();
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
      const flagged = highlights.has(`purchase:${item.sku}`);
      row.className = flagged ? "line flagged" : "line";
      const name = document.createElement("span");
      name.className = "line-name";
      name.textContent = `${item.quantity} × ${item.name}`;
      const meta = document.createElement("span");
      meta.className = "line-meta";
      meta.textContent = `${item.merchant} · £${item.unit_price_gbp} each`;
      const amount = document.createElement("span");
      amount.className = "line-amount";
      amount.textContent = `£${item.line_total_gbp}`;
      row.append(name, meta, amount);
      if (flagged) {
        const note = document.createElement("span");
        note.className = "line-note";
        note.textContent = "This quantity comes from the product listing, not the user's goal.";
        row.append(note);
      }
      list.append(row);
    }
    document.getElementById("proposal-note").textContent = body.note;
    document.getElementById("proposal-total").textContent = `Total £${body.total_gbp}`;
    document.getElementById("proposal-reasoning").textContent = body.reasoning;
    setProposalVisible(true);
    renderGraph(body.graph, body.verification);
    renderVerification(body.verification);
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
