const byId = (id) => document.getElementById(id);

const ui = {
  merchant: byId("merchant-input"),
  invoice: byId("invoice-input"),
  amount: byId("amount-input"),
  currency: byId("currency-input"),
  submit: byId("submit-proposal"),
  verify: byId("verify-invoice"),
  guided: byId("guided-demo"),
  toast: byId("toast"),
};

const state = {
  audit: [],
  pending: [],
  filter: "all",
  search: "",
  selected: null,
  busy: false,
};

const money = new Intl.NumberFormat("en-IN", {
  style: "currency", currency: "INR", minimumFractionDigits: 2,
});
const dollars = new Intl.NumberFormat("en-US", {
  style: "currency", currency: "USD", minimumFractionDigits: 2,
});
const shortTime = new Intl.DateTimeFormat("en-IN", {
  day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit",
});

function eventId(prefix) {
  const suffix = globalThis.crypto?.randomUUID?.().slice(0, 8)
    ?? Math.random().toString(36).slice(2, 10);
  return `${prefix}-${Date.now().toString(36)}-${suffix}`;
}

function detailText(detail) {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) return detail.map((item) => item.msg ?? "Invalid input").join("; ");
  return "Request failed";
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  let body;
  try { body = await response.json(); } catch { body = {}; }
  if (!response.ok) throw new Error(detailText(body.detail) || `HTTP ${response.status}`);
  return body;
}

let toastTimer;
function toast(message, kind = "info") {
  clearTimeout(toastTimer);
  ui.toast.textContent = message;
  ui.toast.className = `toast ${kind}`;
  ui.toast.hidden = false;
  toastTimer = setTimeout(() => { ui.toast.hidden = true; }, 4300);
}

function setBusy(value) {
  state.busy = value;
  for (const button of [ui.submit, ui.guided]) button.disabled = value;
  ui.currency.disabled = value;
  syncCurrency();
}

function text(id, value) { byId(id).textContent = value; }

function syncCurrency() {
  const usd = ui.currency.value === "USD";
  text("amount-symbol", usd ? "$" : "₹");
  text("amount-limit", usd ? "No USD payout cap in the active mandate" : "Must match invoice total · maximum ₹100 per 24 hours");
  text("currency-note", usd ? "USD proposals are evaluated and blocked by this mandate" : "INR payouts can be approved by this mandate");
  ui.verify.disabled = state.busy || usd;
}

function renderMetrics() {
  const approved = state.audit.filter((item) => item.event.decision === "APPROVED").length;
  const blocked = state.audit.filter((item) => item.event.decision === "REJECTED").length;
  text("metric-total", state.audit.length.toLocaleString("en-IN"));
  text("metric-approved", approved.toLocaleString("en-IN"));
  text("metric-blocked", blocked.toLocaleString("en-IN"));
  text("metric-pending", state.pending.length.toLocaleString("en-IN"));
  text("nav-pending-count", state.pending.length);
  text("pending-count-badge", state.pending.length);
}

function renderMandate(status) {
  const cutoff = Date.now() - 24 * 60 * 60 * 1000;
  const spend = state.audit.reduce((total, item) => {
    const { event } = item;
    if (event.decision !== "APPROVED" || event.action.type !== "payout" || event.action.currency !== "INR") return total;
    if (Date.parse(event.recorded_at) < cutoff) return total;
    return total + Math.round(Number(event.action.amount) * 100);
  }, 0);
  text("mandate-spend", money.format(spend / 100));
  byId("mandate-progress").style.width = `${Math.min(100, spend / 100)}%`;
  text("mandate-confirmed", status.confirmed_count.toLocaleString("en-IN"));
  text("mandate-integrity", status.audit_valid ? "Verified" : "Invalid");
}

function makeCell(row, value, className = "") {
  const cell = document.createElement("td");
  cell.textContent = value;
  if (className) cell.className = className;
  row.append(cell);
  return cell;
}

function selectEntry(item) {
  const event = item.event;
  state.selected = {
    event_id: event.event_id,
    decision: event.decision,
    reason: event.reason,
    code: event.violation_code,
    rule_id: event.rule_id,
    receipt_id: event.receipt_id,
    audit_seq: item.seq,
    action: event.action,
    recorded_at: event.recorded_at,
  };
  renderDecision();
  document.querySelector(".decision-panel").scrollIntoView({ behavior: "smooth", block: "center" });
}

function renderAudit() {
  const body = byId("audit-rows");
  body.replaceChildren();
  const query = state.search.trim().toLowerCase();
  const entries = [...state.audit].reverse().filter((item) => {
    const event = item.event;
    const matchesFilter = state.filter === "all" || event.decision === state.filter;
    const matchesQuery = !query || event.event_id.toLowerCase().includes(query)
      || (event.action.invoice_id ?? "").toLowerCase().includes(query)
      || event.action.merchant.toLowerCase().includes(query);
    return matchesFilter && matchesQuery;
  });

  if (!entries.length) {
    const row = document.createElement("tr");
    makeCell(row, state.audit.length ? "No decisions match this view." :
      "No decisions recorded yet. Run a scenario to start the audit trail.", "table-empty").colSpan = 6;
    body.append(row);
  }

  for (const item of entries) {
    const event = item.event;
    const row = document.createElement("tr");
    row.className = "audit-row";
    row.tabIndex = 0;
    row.setAttribute("role", "button");
    row.setAttribute("aria-label", `View ${event.event_id} decision`);
    row.addEventListener("click", () => selectEntry(item));
    row.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); selectEntry(item); }
    });
    makeCell(row, event.event_id, "event-cell");
    makeCell(row, event.action.type === "invoice_match" ? "Invoice match" : "Payout");
    makeCell(row, event.action.invoice_id ?? "—");
    const formatter = event.action.currency === "USD" ? dollars : money;
    makeCell(row, event.action.type === "invoice_match" ? "—" : formatter.format(Number(event.action.amount)), "amount-cell");
    const decisionCell = makeCell(row, "");
    const badge = document.createElement("span");
    badge.className = `table-status ${event.decision === "APPROVED" ? "approved" : event.decision === "REJECTED" ? "rejected" : "error"}`;
    badge.textContent = event.decision === "REJECTED" ? "Blocked" : event.decision === "APPROVED" ? "Approved" : "Error";
    decisionCell.append(badge);
    makeCell(row, shortTime.format(new Date(event.recorded_at)));
    body.append(row);
  }
  text("audit-count-label", `${entries.length} of ${state.audit.length} decisions`);
}

function check(id, labelId, status) {
  const icon = byId(id);
  icon.className = `check-icon ${status}`;
  icon.textContent = status === "pass" ? "✓" : status === "fail" ? "×" : "·";
  text(labelId, status === "pass" ? "Passed" : status === "fail" ? "Blocked" : "Not evaluated");
}

function renderDecision() {
  const data = state.selected;
  byId("decision-empty").hidden = Boolean(data);
  byId("decision-body").hidden = !data;
  if (!data) return;

  text("decision-reference", `#${String(data.audit_seq).padStart(4, "0")}`);
  const badge = byId("decision-badge");
  badge.className = `status-badge ${data.decision === "APPROVED" ? "approved" : data.decision === "REJECTED" ? "rejected" : "error"}`;
  badge.textContent = data.decision === "REJECTED" ? "BLOCKED" : data.decision;
  text("decision-time", data.recorded_at ? shortTime.format(new Date(data.recorded_at)) : "Just now");
  const invoiceEvidence = data.action?.type === "invoice_match";
  text("decision-headline", invoiceEvidence
    ? (data.decision === "APPROVED" ? "Invoice evidence recorded" : "Invoice match blocked")
    : (data.decision === "APPROVED" ? "Payout approved" : data.decision === "REJECTED" ? "Payout blocked" : "Execution needs attention"));
  text("decision-reason", data.reason);
  text("decision-event", data.event_id);
  text("decision-audit", `Entry #${data.audit_seq}`);
  text("decision-receipt", data.receipt_id ?? "—");
  byId("rule-checks").hidden = invoiceEvidence;

  if (!invoiceEvidence) {
    const vendorFailed = ["denied_merchant", "merchant_not_allowed"].includes(data.code);
    const capFailed = ["over_cap", "unconstrained_money_action"].includes(data.code);
    const invoiceFailed = ["missing_invoice_id", "missing_precondition", "invoice_already_paid", "out_of_sequence"].includes(data.code);
    check("check-vendor", "check-vendor-label", vendorFailed ? "fail" : "pass");
    check("check-cap", "check-cap-label", vendorFailed ? "skip" : capFailed ? "fail" : "pass");
    check("check-invoice", "check-invoice-label", vendorFailed || capFailed ? "skip" : invoiceFailed ? "fail" : "pass");
  }
}

function renderPending() {
  const list = byId("pending-list");
  list.replaceChildren();
  if (!state.pending.length) {
    const empty = document.createElement("div");
    empty.className = "pending-empty";
    const symbol = document.createElement("span");
    symbol.textContent = "✓";
    symbol.className = "pending-check";
    const label = document.createElement("span");
    label.textContent = "All clear. No unresolved intents.";
    empty.append(symbol, label);
    list.append(empty);
    return;
  }
  for (const item of state.pending) {
    const row = document.createElement("div");
    row.className = "pending-item";
    const name = document.createElement("strong");
    name.textContent = item.event_id;
    const detail = document.createElement("small");
    detail.textContent = `${item.action.merchant} · ${item.action.type} · ${item.approved_at ? shortTime.format(new Date(item.approved_at)) : "pending"}`;
    row.append(name, detail);
    list.append(row);
  }
}

async function refresh() {
  try {
    const [status, policy, audit, pending] = await Promise.all([
      api("/api/status"), api("/api/policy"), api("/api/audit"), api("/api/pending"),
    ]);
    state.audit = audit.entries;
    state.pending = pending.entries;
    text("mandate-name", policy.name);
    text("mandate-source", policy.mandate);
    text("connection-label", "API connected");
    const integrity = byId("audit-integrity");
    integrity.classList.toggle("invalid", !status.audit_valid);
    integrity.lastChild.textContent = status.audit_valid ? "Chain verified" : "Chain invalid";
    renderMetrics();
    renderMandate(status);
    renderAudit();
    if (!state.selected && state.audit.length) selectEntryWithoutScroll(state.audit.at(-1));
    renderDecision();
    renderPending();
  } catch (error) {
    text("connection-label", "API unavailable");
    toast(`Could not load workspace: ${error.message}`, "error");
  }
}

function selectEntryWithoutScroll(item) {
  const event = item.event;
  state.selected = {
    event_id: event.event_id, decision: event.decision, reason: event.reason,
    code: event.violation_code, rule_id: event.rule_id, receipt_id: event.receipt_id,
    audit_seq: item.seq, action: event.action, recorded_at: event.recorded_at,
  };
}

function formAction() {
  return {
    type: "payout", amount: ui.amount.value, currency: ui.currency.value,
    merchant: ui.merchant.value.trim(), invoice_id: ui.invoice.value.trim(),
  };
}

async function postDecision(path, payload, action) {
  const result = await api(path, { method: "POST", body: JSON.stringify(payload) });
  state.selected = { ...result, action, recorded_at: new Date().toISOString() };
  await refresh();
  return result;
}

async function submitProposal(eventId = eventIdFor("payout"), action = formAction()) {
  return postDecision("/api/proposals", { event_id: eventId, action }, action);
}

function eventIdFor(prefix) { return eventId(prefix); }

async function verifyInvoice(eventId = eventIdFor("invoice"), invoiceId = ui.invoice.value.trim(), merchant = ui.merchant.value.trim()) {
  const action = { type: "invoice_match", amount: "0.00", currency: "INR", merchant, invoice_id: invoiceId };
  return postDecision("/api/demo/invoice-matches", { event_id: eventId, invoice_id: invoiceId, merchant }, action);
}

async function manualSubmit(e) {
  e.preventDefault();
  if (state.busy) return;
  setBusy(true);
  try {
    const result = await submitProposal();
    toast(result.decision === "APPROVED"
      ? "Payout approved and recorded."
      : `${result.reason} (${result.code})`, result.decision === "APPROVED" ? "success" : "info");
  } catch (error) { toast(error.message, "error"); }
  finally { setBusy(false); }
}

async function manualVerify() {
  if (state.busy) return;
  if (ui.currency.value !== "INR") { toast("Invoice evidence in this workspace is INR only. Select INR first.", "error"); return; }
  if (!ui.invoice.value.trim()) { ui.invoice.focus(); toast("Enter an invoice ID first.", "error"); return; }
  setBusy(true);
  try {
    const result = await verifyInvoice();
    toast(result.decision === "APPROVED"
      ? "Trusted demo invoice match recorded."
      : `${result.reason} (${result.code})`, result.decision === "APPROVED" ? "success" : "info");
  } catch (error) { toast(error.message, "error"); }
  finally { setBusy(false); }
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function flowLine(label, decision, expected) {
  const line = document.createElement("li");
  line.className = decision === expected ? "done" : "failed";
  line.textContent = `${label} · ${decision === "REJECTED" ? "BLOCKED" : decision}`;
  byId("guided-progress-list").append(line);
}

async function guidedFlow() {
  if (state.busy) return;
  setBusy(true);
  byId("guided-progress").hidden = false;
  byId("guided-progress-list").replaceChildren();
  text("guided-progress-status", "Running");
  ui.currency.value = "INR";
  syncCurrency();
  ui.merchant.value = "vendor_123";
  try {
    const invoice = await api("/api/demo/invoices/next");
    const invoiceId = invoice.invoice_id;
    ui.invoice.value = invoiceId;
    ui.amount.value = invoice.amount;
    const steps = [
      { label: "Invoice evidence recorded", expected: "APPROVED", run: () => verifyInvoice(eventIdFor("invoice"), invoiceId) },
      { label: "Wrong invoice rejected", expected: "REJECTED", run: () => submitProposal(eventIdFor("wrong"), { ...formAction(), invoice_id: `${invoiceId}-OTHER` }) },
      { label: "Matching payout approved", expected: "APPROVED", run: () => submitProposal(eventIdFor("payout"), formAction()) },
      { label: "Duplicate payout rejected", expected: "REJECTED", run: () => submitProposal(eventIdFor("duplicate"), formAction()) },
    ];
    for (const step of steps) {
      const result = await step.run();
      flowLine(step.label, result.decision, step.expected);
      if (result.decision !== step.expected) throw new Error(`Flow stopped: ${step.label.toLowerCase()} returned ${result.decision}. Use a fresh demo database for a clean run.`);
      await sleep(520);
    }
    text("guided-progress-status", "Complete");
    toast("Guided flow complete. Open the audit trail to inspect all four decisions.", "success");
  } catch (error) {
    text("guided-progress-status", "Stopped");
    toast(error.message, "error");
  } finally { setBusy(false); }
}

const scenarios = {
  valid: { invoice: "INV-001", amount: "40.00" },
  wrong: { invoice: "INV-002", amount: "1.00" },
  over: { invoice: "INV-OVER-CAP", amount: "150.00" },
  duplicate: { invoice: "INV-001", amount: "40.00" },
};

document.querySelectorAll("[data-scenario]").forEach((button) => {
  button.addEventListener("click", () => {
    document.querySelectorAll("[data-scenario]").forEach((item) => item.classList.remove("selected"));
    button.classList.add("selected");
    ui.merchant.value = "vendor_123";
    ui.currency.value = "INR";
    syncCurrency();
    ui.invoice.value = scenarios[button.dataset.scenario].invoice;
    ui.amount.value = scenarios[button.dataset.scenario].amount;
  });
});

document.querySelectorAll("[data-filter]").forEach((button) => {
  button.addEventListener("click", () => {
    document.querySelectorAll("[data-filter]").forEach((item) => item.classList.remove("active"));
    button.classList.add("active");
    state.filter = button.dataset.filter;
    renderAudit();
  });
});

document.querySelectorAll("[data-metric-filter]").forEach((link) => {
  link.addEventListener("click", () => {
    state.filter = link.dataset.metricFilter;
    state.search = "";
    byId("audit-search").value = "";
    byId("topbar-search").value = "";
    document.querySelectorAll("[data-filter]").forEach((button) => {
      button.classList.toggle("active", button.dataset.filter === state.filter);
    });
    renderAudit();
  });
});

byId("audit-search").addEventListener("input", (event) => {
  state.search = event.target.value;
  byId("topbar-search").value = state.search;
  renderAudit();
});

byId("topbar-search").addEventListener("input", (event) => {
  state.search = event.target.value;
  byId("audit-search").value = state.search;
  renderAudit();
});

byId("topbar-search").addEventListener("keydown", (event) => {
  if (event.key === "Enter") byId("activity").scrollIntoView({ behavior: "smooth" });
});

byId("refresh-workspace").addEventListener("click", refresh);

ui.currency.addEventListener("change", syncCurrency);

const workspaceToggle = byId("workspace-toggle");
const workspaceMenu = byId("workspace-menu");

function closeWorkspaceMenu() {
  workspaceMenu.hidden = true;
  workspaceToggle.setAttribute("aria-expanded", "false");
}

workspaceToggle.addEventListener("click", () => {
  workspaceMenu.hidden = !workspaceMenu.hidden;
  workspaceToggle.setAttribute("aria-expanded", String(!workspaceMenu.hidden));
});

workspaceMenu.querySelectorAll("a").forEach((link) => {
  link.addEventListener("click", closeWorkspaceMenu);
});

document.addEventListener("click", (event) => {
  if (!byId("workspace-toggle").parentElement.contains(event.target)) closeWorkspaceMenu();
});

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !workspaceMenu.hidden) {
    closeWorkspaceMenu();
    workspaceToggle.focus();
  }
});

byId("menu-toggle").addEventListener("click", () => {
  const open = byId("sidebar").classList.toggle("open");
  byId("menu-toggle").setAttribute("aria-expanded", String(open));
});

document.querySelectorAll("[data-nav]").forEach((link) => {
  link.addEventListener("click", () => {
    document.querySelectorAll("[data-nav]").forEach((item) => item.classList.remove("active"));
    link.classList.add("active");
    byId("sidebar").classList.remove("open");
    byId("menu-toggle").setAttribute("aria-expanded", "false");
  });
});

byId("proposal-form").addEventListener("submit", manualSubmit);
ui.verify.addEventListener("click", manualVerify);
ui.guided.addEventListener("click", guidedFlow);
window.addEventListener("focus", refresh);

const welcomeScreen = byId("welcome-screen");
const appShell = byId("app-shell");
const enterDemo = byId("enter-demo");
let alreadyEntered = false;
try { alreadyEntered = sessionStorage.getItem("veto-dashboard-entered") === "1"; } catch { /* keep the intro available when storage is blocked */ }

if (alreadyEntered) {
  welcomeScreen.remove();
  document.body.classList.remove("welcome-active");
  appShell.inert = false;
  appShell.removeAttribute("aria-hidden");
} else {
  enterDemo.focus();
  enterDemo.addEventListener("click", () => {
    enterDemo.disabled = true;
    welcomeScreen.classList.add("is-entering");
    const delay = window.matchMedia("(prefers-reduced-motion: reduce)").matches ? 140 : 510;
    window.setTimeout(() => {
      try { sessionStorage.setItem("veto-dashboard-entered", "1"); } catch { /* the intro still dismisses for this page */ }
      appShell.inert = false;
      appShell.removeAttribute("aria-hidden");
      welcomeScreen.remove();
      document.body.classList.remove("welcome-active");
      byId("overview").scrollIntoView({ behavior: "auto", block: "start" });
    }, delay);
  });
}

syncCurrency();
refresh();
