const state = {
  latest: null,
  config: null,
  auth: { authenticated: false, admin: false, admin_configured: false, user: null },
  account: null,
  logs: [],
  users: [],
  socket: null,
};

const money = new Intl.NumberFormat("en-IN", {
  style: "currency",
  currency: "INR",
  maximumFractionDigits: 0,
});

const number = new Intl.NumberFormat("en-IN", {
  maximumFractionDigits: 2,
});

const compactNumber = new Intl.NumberFormat("en-IN", {
  notation: "compact",
  maximumFractionDigits: 1,
});

const usd = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  minimumFractionDigits: 4,
  maximumFractionDigits: 6,
});

function byId(id) {
  return document.getElementById(id);
}

function fmtMoney(value) {
  return Number.isFinite(Number(value)) ? money.format(Number(value)) : "-";
}

function fmtNumber(value) {
  return Number.isFinite(Number(value)) ? number.format(Number(value)) : "-";
}

function fmtCompact(value) {
  return Number.isFinite(Number(value)) ? compactNumber.format(Number(value)) : "-";
}

function fmtUsd(value) {
  return Number.isFinite(Number(value)) ? usd.format(Number(value)) : "-";
}

function fmtPct(value) {
  return Number.isFinite(Number(value)) ? `${number.format(Number(value))}%` : "-";
}

function fmtTime(value) {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function fmtAge(seconds) {
  const value = Number(seconds);
  if (!Number.isFinite(value)) return "no ticks yet";
  if (value < 60) return `${Math.round(value)}s old`;
  if (value < 3600) return `${Math.round(value / 60)}m old`;
  return `${Math.round(value / 3600)}h old`;
}

function pnlClass(value) {
  const numeric = Number(value);
  if (numeric > 0) return "positive";
  if (numeric < 0) return "negative";
  return "";
}

function render(payload) {
  state.latest = payload;
  const portfolio = payload.portfolio || {};
  const positions = payload.positions || [];
  const quotes = payload.quotes || [];
  const decisions = payload.decisions || [];
  const suggestions = payload.suggestions || [];
  const orders = payload.orders || [];
  const strategies = payload.strategy_metrics || [];
  const sentiment = payload.sentiment || [];

  byId("kpi-equity").textContent = fmtMoney(portfolio.equity);
  byId("kpi-cash").textContent = fmtMoney(portfolio.cash);
  byId("kpi-invested").textContent = fmtMoney(portfolio.invested);
  byId("kpi-unrealized").textContent = fmtMoney(portfolio.unrealized_pnl);
  byId("kpi-unrealized").className = pnlClass(portfolio.unrealized_pnl);
  byId("kpi-positions").textContent = String(positions.length);
  byId("kpi-decisions").textContent = String(decisions.length);
  byId("last-cycle").textContent = payload.last_cycle_at ? `Last cycle ${fmtTime(payload.last_cycle_at)}` : "waiting";

  const pill = byId("status-pill");
  pill.textContent = payload.running ? "running" : "stopped";
  pill.className = `pill ${payload.running ? "running" : "stopped"}`;

  const error = byId("error-box");
  if (payload.last_error) {
    error.hidden = false;
    error.textContent = payload.last_error;
  } else {
    error.hidden = true;
    error.textContent = "";
  }

  byId("position-count").textContent = `${positions.length} open`;
  byId("quote-count").textContent = `${quotes.length} quotes`;
  byId("account-quote-count").textContent = `${quotes.length} quotes`;
  byId("decision-count").textContent = `${decisions.length} decisions`;
  byId("overview-decision-count").textContent = `${decisions.length} decisions`;
  byId("suggestion-count").textContent = `${suggestions.length} candidates`;
  byId("order-count").textContent = `${orders.length} orders`;
  byId("strategy-count").textContent = `${strategies.length} strategies`;
  byId("sentiment-count").textContent = `${sentiment.length} events`;
  byId("nav-positions-badge").textContent = String(positions.length);
  byId("nav-suggestions-badge").textContent = String(suggestions.length);
  byId("nav-decisions-badge").textContent = String(decisions.length);
  byId("nav-orders-badge").textContent = String(orders.length);
  byId("nav-sentiment-badge").textContent = String(sentiment.length);
  byId("nav-logs-badge").textContent = state.auth?.admin ? String(state.logs.length) : "admin";
  byId("nav-overview-badge").textContent = payload.running ? "on" : "off";

  renderPositions(positions);
  renderStrategies(strategies);
  renderSentiment(sentiment);
  renderQuotes(quotes);
  renderSuggestions(suggestions);
  renderDecisions(decisions);
  renderOverviewDecisions(decisions);
  renderOrders(orders);
  renderMarketBreadth(payload.market_breadth || {});
  renderSectorRotation(payload.sector_rotation_context || {});
  renderMacroEvents(payload.upcoming_macro_events || []);
  renderAgentConsole(payload);
  renderShell(payload);
  drawEquity(payload.equity_curve || []);
}

function renderMarketBreadth(breadth) {
  const panel = byId("market-breadth-panel");
  if (!panel) return;
  const regime = breadth.breadth_regime || "neutral";
  byId("breadth-status").textContent = regime;
  const pct50 = Number(breadth.pct_above_50dma || 0);
  panel.innerHTML = `
    <div class="breadth-headline">
      <span class="pill regime ${escapeHtml(regime)}">${escapeHtml(regime)}</span>
      ${breadth.breadth_thrust ? `<strong class="breadth-thrust">BREADTH THRUST DETECTED</strong>` : ""}
    </div>
    <div class="progress-row">
      <span>Above 50 DMA</span>
      <div class="progress-track"><div style="width:${Math.max(0, Math.min(pct50, 100))}%"></div></div>
      <strong>${fmtPct(pct50)}</strong>
    </div>
    <div class="mini-grid">
      <button type="button" data-breadth-detail="adr"><span>A/D Ratio</span><strong>${fmtNumber(breadth.advance_decline_ratio)}</strong></button>
      <button type="button" data-breadth-detail="highs"><span>New Highs</span><strong class="positive">${fmtNumber(breadth.new_highs_count)}</strong></button>
      <button type="button" data-breadth-detail="lows"><span>New Lows</span><strong class="negative">${fmtNumber(breadth.new_lows_count)}</strong></button>
      <button type="button" data-breadth-detail="mcclellan"><span>McClellan</span><strong class="${pnlClass(breadth.mcclellan_proxy)}">${fmtNumber(breadth.mcclellan_proxy)}</strong></button>
    </div>
  `;
  panel.querySelectorAll("button").forEach((button) => button.addEventListener("click", () => showDetails("Market Breadth", breadth)));
}

function renderSectorRotation(context) {
  const panel = byId("sector-rotation-panel");
  if (!panel) return;
  const top = context.leaderboard?.top || [];
  const bottom = context.leaderboard?.bottom || [];
  byId("sector-status").textContent = `${top.length + bottom.length} sectors`;
  const row = (sector, tone) => `<button class="sector-row" type="button">
    <span>${escapeHtml(sector.sector || "-")}</span>
    <strong class="${tone}">${fmtNumber(sector.sector_vs_nifty_rs)}</strong>
    <small>${escapeHtml(`${sector.sector_stage || "-"} · rank ${sector.sector_rank || "-"}`)}</small>
  </button>`;
  panel.innerHTML = `
    <div class="sector-columns">
      <div><h4>Top 3</h4>${top.map((item) => row(item, "positive")).join("") || `<p class="muted">waiting</p>`}</div>
      <div><h4>Bottom 3</h4>${bottom.map((item) => row(item, "negative")).join("") || `<p class="muted">waiting</p>`}</div>
    </div>
  `;
  [...panel.querySelectorAll(".sector-row")].forEach((button, index) => {
    const data = index < top.length ? top[index] : bottom[index - top.length];
    button.addEventListener("click", () => showDetails("Sector Rotation", data));
  });
}

function renderMacroEvents(events) {
  const body = byId("macro-events-body");
  if (!body) return;
  body.innerHTML = events.length
    ? events.slice(0, 10).map((event) => `<tr><td>${escapeHtml(event.date || "-")}</td><td>${escapeHtml(event.type || "-")}</td><td>${escapeHtml(event.scope || (event.symbols || []).join(", ") || "-")}</td></tr>`).join("")
    : `<tr><td colspan="3">No upcoming macro events loaded</td></tr>`;
}

function currentSettings() {
  return state.config?.settings || {};
}

function plainSetting(key, fallback = "-") {
  const value = currentSettings()[key];
  if (value && typeof value === "object" && "saved" in value) return value.saved ? "saved" : "not saved";
  return value ?? fallback;
}

function renderShell(payload = state.latest || {}) {
  const health = payload.market_health || {};
  const macro = payload.macro_context || {};
  const breadth = payload.market_breadth || {};
  const runtime = payload.runtime || {};
  const provider = health.provider || payload.provider || runtime.market_data_provider || "-";
  const mode = health.mode || "unknown";
  const llmProvider = plainSetting("llm_provider", runtime.llm_provider || "offline");
  const llmMode = plainSetting("llm_decision_mode", runtime.llm_decision_mode || "offline");
  const llmModel = llmProvider === "deepseek" ? plainSetting("deepseek_model", "deepseek-v4-pro") : "offline";
  const llmUsage = payload.llm_usage?.today_utc || {};
  const llmUsageText = llmUsage.calls
    ? `${fmtCompact(llmUsage.total_tokens)} tok · ${fmtUsd(llmUsage.cost_usd)} today`
    : `${llmModel || "model unset"}`;

  byId("top-provider").textContent = provider;
  byId("top-llm").textContent = llmProvider === "offline" ? "off" : llmModel;
  byId("top-execution").textContent = plainSetting("execution_mode", runtime.execution_mode || "-");

  byId("feed-pill").textContent = `${mode} feed`;
  byId("feed-pill").className = `pill ${mode === "live" ? "running" : mode === "simulated" ? "stopped" : ""}`;
  byId("ops-feed").textContent = provider;
  byId("ops-feed-meta").textContent = `${health.quote_count || 0} quotes · ${fmtAge(health.latest_quote_age_seconds)}`;
  byId("ops-llm").textContent = llmProvider === "offline" ? "Offline" : llmProvider;
  byId("ops-llm-meta").textContent = `${llmMode} · ${llmUsageText}`;
  byId("ops-risk").textContent = `${plainSetting("max_positions", "-")} slots`;
  byId("ops-risk-meta").textContent = `${fmtPct(Number(plainSetting("max_order_value_pct", 0)) * 100)} max order`;
  byId("ops-macro").textContent = macro.regime || "unknown";
  byId("ops-macro-meta").textContent = `${fmtNumber(macro.risk_score)} risk · breadth ${escapeHtml(breadth.breadth_regime || "neutral")}`;
  byId("ops-cycle").textContent = payload.running ? "Running" : "Stopped";
  byId("ops-cycle-meta").textContent = payload.last_cycle_at ? `${fmtTime(payload.last_cycle_at)} · ${plainSetting("agent_interval_seconds", "-")}s` : "manual run pending";
}

function renderAgentConsole(payload) {
  const portfolio = payload.portfolio || {};
  const health = payload.market_health || {};
  const settings = currentSettings();
  const positions = payload.positions || [];
  const orders = payload.orders || [];
  const decisions = payload.decisions || [];
  const universe = payload.universe || {};
  const latestAction = decisions.find((row) => row.action && row.action !== "HOLD");
  const rows = [
    ["Feed mode", health.mode || "unknown", health.provider || payload.provider || "-"],
    ["Universe", `${universe.enabled ?? "-"} enabled`, `${universe.symbols_per_cycle || "all"} per cycle · ${universe.low_price_enabled ?? 0} <= ₹100 priced`],
    ["Exposure", fmtMoney(portfolio.invested), `${positions.length}/${settings.max_positions ?? "-"} positions`],
    ["Risk", fmtPct(Number(settings.daily_loss_limit_pct || 0) * 100), "daily loss limit"],
    ["Execution", settings.execution_mode || payload.runtime?.execution_mode || "-", settings.live_trading_enabled ? "live switch on" : "live switch off"],
    ["Latest action", latestAction ? `${latestAction.action} ${latestAction.symbol}` : "No trade action", `${orders.length} orders tracked`],
  ];
  byId("agent-console").innerHTML = rows
    .map(
      ([label, value, note]) => `<button class="console-row" type="button">
        <span>${escapeHtml(label)}</span>
        <strong>${escapeHtml(value)}</strong>
        <small>${escapeHtml(note)}</small>
      </button>`,
    )
    .join("");
  [...byId("agent-console").querySelectorAll(".console-row")].forEach((button, index) => {
    button.addEventListener("click", () => showDetails(rows[index][0], rows[index]));
  });
}

function renderAuth(auth) {
  state.auth = auth;
  const authenticated = Boolean(auth.authenticated);
  byId("login-screen").hidden = authenticated;
  byId("app-shell").classList.toggle("app-hidden", !authenticated);
  const pill = byId("admin-pill");
  pill.textContent = auth.admin ? "admin" : "user";
  pill.className = `pill ${auth.admin ? "running" : "stopped"}`;
  const currentUser = auth.user?.username || "signed in";
  byId("current-user-label").textContent = `${currentUser} · ${auth.user?.role || "user"}`;
  byId("logout-btn").hidden = !authenticated;
  for (const item of document.querySelectorAll(".admin-only")) {
    item.hidden = !auth.admin;
  }
  applyAccessMode();
  if (auth.admin) fetchLogs();
  else renderLogs([]);
  if (auth.admin) fetchUsers();
}

function applyAccessMode() {
  const authenticated = Boolean(state.auth && state.auth.authenticated);
  const admin = Boolean(state.auth && state.auth.admin);
  for (const id of [
    "start-btn",
    "stop-btn",
    "run-btn",
    "save-settings-btn",
    "reset-demo-btn",
    "test-llm-btn",
    "refresh-logs-btn",
    "analyze-btn",
    "upstox-auth-url-btn",
    "upstox-connect-btn",
    "nubra-send-otp-btn",
    "nubra-connect-btn",
  ]) {
    const element = byId(id);
    if (element) element.disabled = !admin;
  }
  const analyzeInput = byId("analyze-symbol");
  if (analyzeInput) analyzeInput.disabled = !authenticated;
  const analyzeButton = byId("analyze-btn");
  if (analyzeButton) analyzeButton.disabled = !authenticated;
  const analyzeStatus = byId("analyze-status");
  if (analyzeStatus && !state.latest?.manual_analysis_active) {
    analyzeStatus.textContent = authenticated ? "ready" : "login required";
  }
  const form = byId("settings-form");
  if (form) {
    for (const input of form.querySelectorAll("input, select")) {
      input.disabled = !admin;
    }
  }
  for (const id of [
    "upstox-api-key",
    "upstox-api-secret",
    "upstox-redirect-uri",
    "upstox-auth-code",
    "nubra-base-url",
    "nubra-phone",
    "nubra-device-id",
    "nubra-otp",
    "nubra-mpin",
  ]) {
    const element = byId(id);
    if (element) element.disabled = !admin;
  }
  byId("settings-status").textContent = admin
    ? "admin controls unlocked"
    : authenticated
      ? "user mode: admin required for settings"
      : "login required";
}

async function fetchLogs() {
  if (!(state.auth && state.auth.admin)) {
    renderLogs([]);
    return;
  }
  try {
    const response = await fetch("/api/logs?limit=400");
    const payload = await response.json().catch(() => ({ detail: `HTTP ${response.status}` }));
    if (!response.ok) {
      byId("logs-count").textContent = payload.detail || "logs unavailable";
      return;
    }
    renderLogs(payload.logs || []);
  } catch (error) {
    byId("logs-count").textContent = "logs unavailable";
    showBackendError(networkErrorMessage(error, "logs fetch"), { action: "fetch logs" });
  }
}

function renderLogs(rows) {
  state.logs = rows || [];
  const body = byId("logs-body");
  if (!state.auth?.admin) {
    byId("logs-count").textContent = "admin login required";
    byId("nav-logs-badge").textContent = "admin";
    body.innerHTML = `<tr><td colspan="6">Login as admin to view agent logs.</td></tr>`;
    return;
  }
  byId("logs-count").textContent = `${state.logs.length} logs`;
  byId("nav-logs-badge").textContent = String(state.logs.length);
  if (!state.logs.length) {
    body.innerHTML = `<tr><td colspan="6">No logs yet</td></tr>`;
    return;
  }
  body.innerHTML = state.logs
    .map((row) => {
      const details = parseJsonObject(row.details_json);
      return `<tr>
        <td>${fmtTime(row.ts)}</td>
        <td><span class="log-level ${escapeHtml(String(row.level || "").toLowerCase())}">${escapeHtml(row.level)}</span></td>
        <td>${escapeHtml(row.component || "-")}</td>
        <td>${escapeHtml(row.event || "-")}</td>
        <td class="reason">${escapeHtml(row.message || "-")}</td>
        <td>${escapeHtml(shortValue(details, 110))}</td>
      </tr>`;
    })
    .join("");
  bindRowDetails(body, state.logs, "Agent Log");
}

async function fetchUsers() {
  if (!state.auth?.admin) {
    renderUsers([]);
    return;
  }
  try {
    const response = await fetch("/api/users");
    const payload = await response.json().catch(() => ({ detail: `HTTP ${response.status}` }));
    if (!response.ok) {
      byId("users-count").textContent = payload.detail || "users unavailable";
      return;
    }
    renderUsers(payload.users || []);
  } catch (error) {
    byId("users-count").textContent = "users unavailable";
  }
}

function renderUsers(rows) {
  state.users = rows || [];
  const body = byId("users-body");
  if (!body) return;
  byId("users-count").textContent = `${state.users.length} users`;
  byId("nav-users-badge").textContent = String(state.users.length);
  if (!state.auth?.admin) {
    body.innerHTML = `<tr><td colspan="5">Admin login required</td></tr>`;
    return;
  }
  if (!state.users.length) {
    body.innerHTML = `<tr><td colspan="5">No users yet</td></tr>`;
    return;
  }
  body.innerHTML = state.users
    .map((user) => {
      const active = Boolean(user.active);
      return `<tr data-user-id="${user.id}">
        <td><strong>${escapeHtml(user.username)}</strong></td>
        <td><span class="source ${user.role === "admin" ? "live" : ""}">${escapeHtml(user.role)}</span></td>
        <td><span class="tag ${active ? "open" : "sell"}">${active ? "active" : "disabled"}</span></td>
        <td>${escapeHtml(user.last_login_at ? fmtTime(user.last_login_at) : "-")}</td>
        <td class="row-actions">
          <button type="button" data-user-action="toggle">${active ? "Disable" : "Enable"}</button>
          <button type="button" data-user-action="role">${user.role === "admin" ? "Make User" : "Make Admin"}</button>
        </td>
      </tr>`;
    })
    .join("");
  body.querySelectorAll("button[data-user-action]").forEach((button) => {
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      const row = button.closest("tr");
      const user = state.users.find((item) => String(item.id) === row?.dataset.userId);
      if (!user) return;
      if (button.dataset.userAction === "toggle") {
        updateUser(user.id, { active: !user.active });
      } else {
        updateUser(user.id, { role: user.role === "admin" ? "user" : "admin" });
      }
    });
  });
  bindRowDetails(body, state.users, "User");
}

async function createUser(event) {
  event.preventDefault();
  const status = byId("user-create-status");
  const payload = {
    username: byId("new-user-username").value.trim(),
    password: byId("new-user-password").value,
    role: byId("new-user-role").value,
    active: true,
  };
  status.textContent = "creating";
  status.className = "settings-inline-status";
  try {
    const response = await fetch("/api/users", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await response.json().catch(() => ({ detail: `HTTP ${response.status}` }));
    if (!response.ok) {
      status.textContent = data.detail || "create failed";
      status.className = "settings-inline-status negative";
      return;
    }
    byId("new-user-username").value = "";
    byId("new-user-password").value = "";
    byId("new-user-role").value = "user";
    renderUsers(data.users || []);
    status.textContent = "user created";
    status.className = "settings-inline-status positive";
    fetchLogs();
  } catch (error) {
    status.textContent = "create failed: backend unreachable";
    status.className = "settings-inline-status negative";
  }
}

async function updateUser(userId, patch) {
  try {
    const response = await fetch(`/api/users/${userId}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch),
    });
    const data = await response.json().catch(() => ({ detail: `HTTP ${response.status}` }));
    if (!response.ok) {
      showDetails("User Update", data);
      return;
    }
    renderUsers(data.users || []);
    fetchLogs();
  } catch (error) {
    showBackendError(networkErrorMessage(error, "user update"), { action: "user update" });
  }
}

function renderAccount(account) {
  state.account = account;
  const paper = account.paper || {};
  const upstox = account.upstox || {};
  const portfolio = paper.portfolio || {};
  byId("account-status").textContent = upstox.connected ? "paper + upstox" : "paper demo";
  byId("account-body").innerHTML = `
    <div class="account-metrics">
      <div><span>Mode</span><strong>${paper.mode || "-"}</strong></div>
      <div><span>Paper Cash</span><strong>${fmtMoney(paper.cash)}</strong></div>
      <div><span>Paper Equity</span><strong>${fmtMoney(portfolio.equity ?? paper.cash)}</strong></div>
      <div><span>Upstox</span><strong>${upstox.connected ? "connected" : "not connected"}</strong></div>
    </div>
    <div class="account-note">
      <strong>Runtime ledger</strong>
      <span>Paper execution stays internal unless live protection is explicitly enabled.</span>
    </div>
    <pre>${escapeHtml(JSON.stringify({ upstox }, null, 2))}</pre>
  `;
}

function renderSentiment(rows) {
  const body = byId("sentiment-body");
  if (!rows.length) {
    body.innerHTML = `<tr><td colspan="5">No sentiment events yet</td></tr>`;
    return;
  }
  body.innerHTML = rows
    .slice(0, 40)
    .map((row) => {
      let headlines = [];
      try {
        headlines = JSON.parse(row.headlines_json || "[]");
      } catch {
        headlines = [];
      }
      return `<tr>
        <td><strong>${row.symbol}</strong></td>
        <td class="num ${pnlClass(row.score)}">${fmtNumber(row.score)}</td>
        <td class="num">${fmtNumber(row.confidence)}</td>
        <td class="num">${row.headline_count || 0}</td>
        <td class="reason">${headlines.slice(0, 3).map(escapeHtml).join("<br>")}</td>
      </tr>`;
    })
    .join("");
  bindRowDetails(body, rows.slice(0, 40), "Sentiment Event");
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function renderSettings(config) {
  state.config = config;
  const form = byId("settings-form");
  const groups = new Map();
  for (const item of config.schema) {
    const group = groups.get(item.category) || [];
    group.push(item);
    groups.set(item.category, group);
  }

  form.innerHTML = [...groups.entries()]
    .map(([category, items]) => {
      const fields = items.map((item) => renderField(item, config.settings[item.key])).join("");
      return `<section class="settings-group">
        <h3>${category}</h3>
        ${fields}
      </section>`;
    })
    .join("");
  renderUpstoxConnect(config.settings || {});
  renderNubraConnect(config.settings || {});
  applyAccessMode();
  renderShell();
}

function renderUpstoxConnect(settings) {
  const apiKey = byId("upstox-api-key");
  const apiSecret = byId("upstox-api-secret");
  const redirectUri = byId("upstox-redirect-uri");
  if (apiKey && !apiKey.value) apiKey.placeholder = settings.upstox_api_key?.saved ? "API key saved" : "API Key";
  if (apiSecret && !apiSecret.value) apiSecret.placeholder = settings.upstox_api_secret?.saved ? "API secret saved" : "API Secret";
  if (redirectUri) redirectUri.value = settings.upstox_redirect_uri || `${window.location.origin}/upstox/callback`;
}

function renderNubraConnect(settings) {
  const baseUrl = byId("nubra-base-url");
  const phone = byId("nubra-phone");
  const deviceId = byId("nubra-device-id");
  if (baseUrl) baseUrl.value = settings.nubra_api_base_url || "https://uatapi.nubra.io";
  if (phone && !phone.value) phone.placeholder = settings.nubra_phone?.saved ? "phone saved" : "Phone";
  if (deviceId) deviceId.value = settings.nubra_device_id || "opentrade-local-001";
}

function renderField(item, stored) {
  const savedSecret = item.type === "secret" && stored && stored.saved;
  const value = item.type === "secret" ? "" : stored;
  const common = `id="setting-${item.key}" name="${item.key}" data-setting-type="${item.type}"`;
  let control = "";
  if (item.type === "select") {
    control = `<select ${common}>${item.choices
      .map((choice) => `<option value="${choice}" ${choice === value ? "selected" : ""}>${choice}</option>`)
      .join("")}</select>`;
  } else if (item.type === "boolean") {
    control = `<input ${common} type="checkbox" ${value ? "checked" : ""} />`;
  } else if (item.type === "number") {
    const min = item.min ?? "";
    const max = item.max ?? "";
    const step = item.step ?? "any";
    control = `<input ${common} type="number" value="${value}" min="${min}" max="${max}" step="${step}" />`;
  } else if (item.type === "secret") {
    const placeholder = savedSecret ? "saved" : "";
    control = `<input ${common} type="password" value="" placeholder="${placeholder}" autocomplete="off" />`;
  } else {
    control = `<input ${common} type="text" value="${value ?? ""}" />`;
  }
  return `<div class="field">
    <label for="setting-${item.key}">${item.label}</label>
    ${control}
  </div>`;
}

function collectSettings() {
  const values = {};
  for (const input of byId("settings-form").querySelectorAll("[name]")) {
    const type = input.dataset.settingType;
    if (type === "boolean") {
      values[input.name] = input.checked;
    } else if (type === "number") {
      values[input.name] = input.value === "" ? 0 : Number(input.value);
    } else if (type === "secret" && input.value === "") {
      continue;
    } else {
      values[input.name] = input.value;
    }
  }
  return values;
}

function networkErrorMessage(error, action = "request") {
  const reason = error && error.message ? error.message : "network unavailable";
  return `Backend ${action} failed: ${reason}. Check that trading-agent is running and port 8000 is reachable.`;
}

function showBackendError(message, detail = {}) {
  const error = byId("error-box");
  error.hidden = false;
  error.textContent = message;
  showDetails("Backend Connection", { message, ...detail });
}

async function saveSettings() {
  const status = byId("settings-status");
  status.textContent = "saving";
  status.className = "settings-inline-status";
  try {
    const response = await fetch("/api/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ settings: collectSettings() }),
    });
    const payload = await response.json().catch(() => ({ detail: `HTTP ${response.status}` }));
    if (!response.ok) {
      status.textContent = payload.detail || "save failed";
      status.className = "settings-inline-status negative";
      return;
    }
    renderSettings(payload.config);
    render(payload.status);
    fetchLogs();
    const savedAt = new Date().toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
    byId("settings-status").textContent = `saved at ${savedAt}`;
    byId("settings-status").className = "settings-inline-status positive";
  } catch (error) {
    const message = networkErrorMessage(error, "settings save");
    status.textContent = "save failed: backend unreachable";
    status.className = "settings-inline-status negative";
    showBackendError(message, { action: "save settings" });
  }
}

async function resetDemo() {
  byId("settings-status").textContent = "resetting demo account";
  try {
    const response = await fetch("/api/control/reset-demo", { method: "POST" });
    render(await response.json());
    byId("settings-status").textContent = "demo account reset";
    fetchLogs();
  } catch (error) {
    byId("settings-status").textContent = "reset failed: backend unreachable";
    showBackendError(networkErrorMessage(error, "demo reset"), { action: "reset demo" });
  }
}

async function testLlm() {
  const status = byId("llm-test-status");
  const button = byId("test-llm-btn");
  const configuredTimeout = Number(state.config?.settings?.llm_timeout_seconds || 45);
  const healthTimeout = Math.min(Math.max(configuredTimeout, 5), 180);
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), (healthTimeout + 5) * 1000);
  const started = Date.now();
  const progress = window.setInterval(() => {
    const elapsed = Math.floor((Date.now() - started) / 1000);
    status.textContent = `testing LLM, ${elapsed}s / ${healthTimeout}s`;
  }, 1000);
  status.textContent = `testing LLM, 0s / ${healthTimeout}s`;
  status.className = "settings-inline-status";
  button.disabled = true;
  try {
    const response = await fetch("/api/llm/test", { method: "POST", signal: controller.signal });
    const payload = await response.json();
    if (!response.ok || !payload.ok) {
      const reason = payload.reason || payload.detail || `HTTP ${payload.status_code || response.status}`;
      status.textContent = `LLM failed: ${reason}`;
      status.className = "settings-inline-status negative";
      showDetails("LLM Test", payload);
      return;
    }
    status.textContent = `LLM ready: ${payload.model} · ${payload.latency_ms} ms`;
    status.className = "settings-inline-status positive";
    showDetails("LLM Test", payload);
  } catch (error) {
    const reason = error.name === "AbortError" ? `browser timed out after ${healthTimeout + 5}s` : error.message;
    status.textContent = `LLM failed: ${reason}`;
    status.className = "settings-inline-status negative";
    showDetails("LLM Test", { ok: false, reason });
  } finally {
    window.clearTimeout(timer);
    window.clearInterval(progress);
    button.disabled = !(state.auth && state.auth.admin);
  }
}

function upstoxConnectPayload() {
  return {
    api_key: byId("upstox-api-key")?.value?.trim() || byId("setting-upstox_api_key")?.value?.trim(),
    api_secret: byId("upstox-api-secret")?.value?.trim() || byId("setting-upstox_api_secret")?.value?.trim(),
    redirect_uri: byId("upstox-redirect-uri")?.value?.trim() || byId("setting-upstox_redirect_uri")?.value?.trim(),
    base_url: byId("setting-upstox_api_base_url")?.value?.trim(),
    code: byId("upstox-auth-code")?.value?.trim(),
  };
}

async function openUpstoxLogin() {
  const status = byId("upstox-connect-status");
  const button = byId("upstox-auth-url-btn");
  status.textContent = "building Upstox login URL";
  status.className = "settings-inline-status";
  button.disabled = true;
  try {
    const response = await fetch("/api/upstox/auth-url", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(upstoxConnectPayload()),
    });
    const payload = await response.json().catch(() => ({ detail: `HTTP ${response.status}` }));
    if (!response.ok || !payload.ok) {
      status.textContent = payload.detail || "Upstox login URL failed";
      status.className = "settings-inline-status negative";
      showDetails("Upstox Login", payload);
      return;
    }
    status.textContent = "Upstox login opened. Paste returned code here after login.";
    status.className = "settings-inline-status positive";
    window.open(payload.auth_url, "_blank", "noopener");
    showDetails("Upstox Login", payload);
  } catch (error) {
    status.textContent = "Upstox login failed: backend unreachable";
    status.className = "settings-inline-status negative";
    showBackendError(networkErrorMessage(error, "Upstox login"), { action: "upstox login" });
  } finally {
    button.disabled = !(state.auth && state.auth.admin);
  }
}

async function connectUpstox() {
  const status = byId("upstox-connect-status");
  const button = byId("upstox-connect-btn");
  status.textContent = "exchanging Upstox code for access token";
  status.className = "settings-inline-status";
  button.disabled = true;
  try {
    const response = await fetch("/api/upstox/connect", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(upstoxConnectPayload()),
    });
    const payload = await response.json().catch(() => ({ detail: `HTTP ${response.status}` }));
    if (!response.ok || !payload.ok) {
      status.textContent = payload.detail || "Upstox connect failed";
      status.className = "settings-inline-status negative";
      showDetails("Upstox Connect", payload);
      return;
    }
    if (byId("upstox-auth-code")) byId("upstox-auth-code").value = "";
    if (byId("upstox-api-secret")) byId("upstox-api-secret").value = "";
    status.textContent = `Upstox connected · ${payload.provider || "provider ready"}`;
    status.className = "settings-inline-status positive";
    if (payload.config) renderSettings(payload.config);
    if (payload.status) render(payload.status);
    fetchLogs();
    showDetails("Upstox Connect", {
      ok: payload.ok,
      message: payload.message,
      provider: payload.provider,
      token_type: payload.token_type,
      user_id: payload.user_id,
    });
  } catch (error) {
    status.textContent = "Upstox connect failed: backend unreachable";
    status.className = "settings-inline-status negative";
    showBackendError(networkErrorMessage(error, "Upstox connect"), { action: "upstox connect" });
  } finally {
    button.disabled = !(state.auth && state.auth.admin);
  }
}

function nubraConnectPayload() {
  return {
    base_url: byId("nubra-base-url")?.value?.trim(),
    phone: byId("nubra-phone")?.value?.trim(),
    device_id: byId("nubra-device-id")?.value?.trim(),
    otp: byId("nubra-otp")?.value?.trim(),
    mpin: byId("nubra-mpin")?.value?.trim(),
  };
}

async function sendNubraOtp() {
  const status = byId("nubra-connect-status");
  const button = byId("nubra-send-otp-btn");
  status.textContent = "sending Nubra OTP";
  status.className = "settings-inline-status";
  button.disabled = true;
  try {
    const response = await fetch("/api/nubra/send-otp", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(nubraConnectPayload()),
    });
    const payload = await response.json().catch(() => ({ detail: `HTTP ${response.status}` }));
    if (!response.ok || !payload.ok) {
      status.textContent = payload.detail || "OTP failed";
      status.className = "settings-inline-status negative";
      showDetails("Nubra OTP", payload);
      return;
    }
    status.textContent = `OTP sent to ******${payload.phone_suffix || ""}${payload.expiry ? ` · expires in ${payload.expiry}m` : ""}`;
    status.className = "settings-inline-status positive";
    showDetails("Nubra OTP", payload);
  } catch (error) {
    status.textContent = "Nubra OTP failed: backend unreachable";
    status.className = "settings-inline-status negative";
    showBackendError(networkErrorMessage(error, "Nubra OTP"), { action: "nubra send otp" });
  } finally {
    button.disabled = !(state.auth && state.auth.admin);
  }
}

async function connectNubra() {
  const status = byId("nubra-connect-status");
  const button = byId("nubra-connect-btn");
  status.textContent = "verifying Nubra OTP and MPIN";
  status.className = "settings-inline-status";
  button.disabled = true;
  try {
    const response = await fetch("/api/nubra/verify", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(nubraConnectPayload()),
    });
    const payload = await response.json().catch(() => ({ detail: `HTTP ${response.status}` }));
    if (!response.ok || !payload.ok) {
      status.textContent = payload.detail || "Nubra connect failed";
      status.className = "settings-inline-status negative";
      showDetails("Nubra Connect", payload);
      return;
    }
    if (byId("nubra-otp")) byId("nubra-otp").value = "";
    if (byId("nubra-mpin")) byId("nubra-mpin").value = "";
    status.textContent = `Nubra connected · ${payload.provider || "provider ready"}`;
    status.className = "settings-inline-status positive";
    if (payload.config) renderSettings(payload.config);
    if (payload.status) render(payload.status);
    fetchLogs();
    showDetails("Nubra Connect", {
      ok: payload.ok,
      message: payload.message,
      client_code: payload.client_code,
      name: payload.name,
      email: payload.email,
      phone_suffix: payload.phone_suffix,
      device_id: payload.device_id,
      provider: payload.provider,
    });
  } catch (error) {
    status.textContent = "Nubra connect failed: backend unreachable";
    status.className = "settings-inline-status negative";
    showBackendError(networkErrorMessage(error, "Nubra connect"), { action: "nubra connect" });
  } finally {
    button.disabled = !(state.auth && state.auth.admin);
  }
}

async function login(event) {
  if (event) event.preventDefault();
  const username = byId("login-username").value;
  const password = byId("login-password").value;
  const status = byId("login-status");
  status.textContent = "Signing in";
  status.className = "settings-inline-status";
  try {
    const response = await fetch("/api/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password }),
    });
    const payload = await response.json().catch(() => ({ detail: `HTTP ${response.status}` }));
    if (!response.ok) {
      status.textContent = payload.detail || "login failed";
      status.className = "settings-inline-status negative";
      return;
    }
    byId("login-password").value = "";
    renderAuth(payload);
    await loadAuthenticatedData();
    openSocket();
  } catch (error) {
    status.textContent = "login failed: backend unreachable";
    status.className = "settings-inline-status negative";
  }
}

async function logout() {
  try {
    await fetch("/api/auth/logout", { method: "POST" });
    if (state.socket) state.socket.close();
    state.socket = null;
    renderAuth({ authenticated: false, admin: false, admin_configured: state.auth.admin_configured, user: null });
    byId("login-status").textContent = "Signed out.";
    byId("login-status").className = "settings-inline-status";
  } catch (error) {
    showBackendError(networkErrorMessage(error, "logout"), { action: "logout" });
  }
}

function renderStrategies(rows) {
  const body = byId("strategies-body");
  if (!rows.length) {
    body.innerHTML = `<tr><td colspan="6">No strategy activity yet</td></tr>`;
    return;
  }
  body.innerHTML = rows
    .slice(0, 80)
    .map(
      (row) => `<tr>
        <td><strong>${escapeHtml(row.strategy)}</strong></td>
        <td class="num">${row.open_positions}</td>
        <td class="num">${fmtMoney(row.exposure)}</td>
        <td class="num ${pnlClass(row.unrealized_pnl)}">${fmtMoney(row.unrealized_pnl)}</td>
        <td class="num ${pnlClass(row.realized_pnl)}">${fmtMoney(row.realized_pnl)}</td>
        <td class="num">${row.filled_orders}</td>
      </tr>`,
    )
    .join("");
  bindRowDetails(body, rows.slice(0, 80), "Strategy");
}

function renderPositions(rows) {
  const body = byId("positions-body");
  if (!rows.length) {
    body.innerHTML = `<tr><td colspan="8">No open positions</td></tr>`;
    return;
  }
  body.innerHTML = rows
    .map((row) => {
      const pnl = (Number(row.market_price) - Number(row.avg_price)) * Number(row.qty);
      const marketValue = Number(row.market_price) * Number(row.qty);
      const exit = row.exit_plan || {};
      return `<tr>
        <td><strong>${escapeHtml(row.symbol)}</strong></td>
        <td>${escapeHtml(row.strategy || "-")}</td>
        <td class="num">${row.qty}</td>
        <td class="num">${fmtMoney(row.avg_price)}</td>
        <td class="num">${fmtMoney(row.market_price)}</td>
        <td class="num">${fmtMoney(marketValue)}</td>
        <td class="num ${pnlClass(pnl)}">${fmtMoney(pnl)}</td>
        <td>${exitPlanMini(exit)}</td>
      </tr>`;
    })
    .join("");
  bindRowDetails(body, rows, "Position");
}

function renderSuggestions(rows) {
  const body = byId("suggestions-body");
  if (!rows.length) {
    body.innerHTML = `<div class="empty-state">Run a cycle to generate ranked suggestions.</div>`;
    return;
  }
  body.innerHTML = rows
    .slice(0, 5)
    .map((row, index) => {
      const action = String(row.suggestion || "WATCH").toLowerCase();
      const t1 = (row.targets || [])[0] || {};
      return `<button class="suggestion-card" type="button" data-index="${index}">
        <div class="suggestion-top">
          <span class="rank">#${index + 1}</span>
          <strong>${escapeHtml(row.symbol)}</strong>
          <span class="tag ${action}">${escapeHtml(row.suggestion)}</span>
        </div>
        <div class="suggestion-score">
          <div><span>Confluence</span><strong>${escapeHtml(row.confluence ?? "-")}/26</strong><small>${escapeHtml(row.tier || "-")}</small></div>
          <div><span>Combined</span><strong class="${pnlClass(row.combined_score)}">${fmtNumber(row.combined_score)}</strong><small>${escapeHtml(row.decision_readiness || "-")}</small></div>
          <div><span>Inst.</span><strong class="${pnlClass(row.institutional_bias)}">${fmtNumber(row.institutional_bias)}</strong><small>${escapeHtml(shortValue(row.institutional_flags || {}, 46))}</small></div>
        </div>
        <div class="suggestion-plan">
          <span>Entry ${formatZone(row.entry_zone)}</span>
          <span>SL ${fmtMoney(row.stop_loss)}</span>
          <span>T1 ${fmtMoney(t1.price)}</span>
        </div>
        <p>${escapeHtml(shortValue(row.reason || "-", 190))}</p>
      </button>`;
    })
    .join("");
  [...body.querySelectorAll(".suggestion-card")].forEach((button) => {
    const row = rows[Number(button.dataset.index)];
    button.addEventListener("click", () => showDetails("Suggestion", row));
  });
}

function renderQuotes(rows) {
  const accountBody = byId("quotes-body");
  const overviewBody = byId("overview-quotes-body");
  const markup = rows
    .slice(0, 160)
    .map((row) => quoteRow(row))
    .join("");
  accountBody.innerHTML = markup || `<tr><td colspan="6">No quotes yet</td></tr>`;
  overviewBody.innerHTML =
    rows
      .slice(0, 12)
      .map((row) => quoteRow(row))
      .join("") || `<tr><td colspan="6">No quotes yet</td></tr>`;
  bindRowDetails(accountBody, rows.slice(0, 160), "Quote");
  bindRowDetails(overviewBody, rows.slice(0, 12), "Quote");
}

function quoteRow(row) {
  const dayPct = quoteDayPct(row);
  return `<tr>
        <td><strong>${escapeHtml(row.symbol)}</strong></td>
        <td class="num">${fmtMoney(row.price)}</td>
        <td class="num ${pnlClass(dayPct)}">${fmtPct(dayPct)}</td>
        <td class="num">${fmtCompact(row.volume)}</td>
        <td><span class="source ${sourceClass(row.source)}">${escapeHtml(row.source)}</span></td>
        <td>${fmtTime(row.ts)}</td>
      </tr>`;
}

function quoteDayPct(row) {
  const price = Number(row.price);
  const close = Number(row.close);
  if (!Number.isFinite(price) || !Number.isFinite(close) || close === 0) return NaN;
  return ((price - close) / close) * 100;
}

function sourceClass(source) {
  const value = String(source || "");
  if (value.includes("live")) return "live";
  if (value.includes("delayed")) return "delayed";
  if (value.includes("simulated")) return "simulated";
  return "";
}

function renderDecisions(rows) {
  const body = byId("decisions-body");
  body.innerHTML = rows.length
    ? rows
    .slice(0, 120)
    .map((row) => {
      const action = String(row.action || "HOLD").toLowerCase();
      return `<tr>
        <td>${fmtTime(row.ts)}</td>
        <td><strong>${escapeHtml(row.symbol)}</strong></td>
        <td>${escapeHtml(row.strategy || "-")}</td>
        <td><span class="tag ${action}">${escapeHtml(row.action)}</span></td>
        <td class="num">${fmtNumber(Number(row.confidence) * 100)}%</td>
        <td class="num">${fmtMoney(row.price)}</td>
        <td class="num ${pnlClass(row.technical_score)}">${fmtNumber(row.technical_score)}</td>
        <td class="num ${pnlClass(row.sentiment_score)}">${fmtNumber(row.sentiment_score)}</td>
        <td class="reason">${escapeHtml(row.reason)}</td>
      </tr>`;
    })
    .join("")
    : `<tr><td colspan="9">No decisions yet</td></tr>`;
  bindRowDetails(body, rows.slice(0, 120), "Decision");
}

function renderOverviewDecisions(rows) {
  const body = byId("overview-decisions-body");
  body.innerHTML = rows.length
    ? rows
        .slice(0, 10)
        .map((row) => {
          const action = String(row.action || "HOLD").toLowerCase();
          return `<tr>
            <td><strong>${escapeHtml(row.symbol)}</strong></td>
            <td><span class="tag ${action}">${escapeHtml(row.action)}</span></td>
            <td class="num">${fmtNumber(Number(row.confidence) * 100)}%</td>
            <td class="num ${pnlClass(row.technical_score)}">${fmtNumber(row.technical_score)}</td>
            <td class="num ${pnlClass(row.sentiment_score)}">${fmtNumber(row.sentiment_score)}</td>
          </tr>`;
        })
        .join("")
    : `<tr><td colspan="5">No decisions yet</td></tr>`;
  bindRowDetails(body, rows.slice(0, 10), "Decision");
}

function renderOrders(rows) {
  const body = byId("orders-body");
  if (!rows.length) {
    body.innerHTML = `<tr><td colspan="9">No orders yet</td></tr>`;
    return;
  }
  body.innerHTML = rows
    .slice(0, 120)
    .map((row) => {
      const side = String(row.side || "").toLowerCase();
      const exit = exitPlanFromOrder(row);
      return `<tr>
        <td>${fmtTime(row.ts)}</td>
        <td><span class="tag ${side}">${escapeHtml(row.side)}</span></td>
        <td><strong>${escapeHtml(row.symbol)}</strong></td>
        <td>${escapeHtml(row.strategy || "-")}</td>
        <td class="num">${row.qty}</td>
        <td class="num">${fmtMoney(row.price)}</td>
        <td class="num">${fmtMoney(row.notional)}</td>
        <td>${escapeHtml(row.status)}</td>
        <td>${exitPlanMini(exit)}</td>
      </tr>`;
    })
    .join("");
  bindRowDetails(body, rows.slice(0, 120), "Order");
}

function bindRowDetails(body, rows, title) {
  [...body.querySelectorAll("tr")].forEach((tr, index) => {
    const row = rows[index];
    if (!row) return;
    tr.addEventListener("click", () => showDetails(title, row));
  });
}

async function showDetails(title, value) {
  byId("drawer-title").textContent = title;
  byId("detail-drawer").classList.add("open");
  byId("drawer-body").innerHTML = `<div class="empty-state">Loading details...</div>`;
  let detailValue = value;
  if (value?.detail_url && !value.details_json) {
    try {
      const response = await fetch(value.detail_url);
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      detailValue = { ...value, ...(await response.json()) };
    } catch (error) {
      detailValue = {
        ...value,
        detail_error: error.message || String(error),
      };
    }
  }
  byId("drawer-body").innerHTML = detailHtml(detailValue);
}

function detailHtml(value) {
  if (!value || typeof value !== "object") {
    return `<pre>${escapeHtml(value)}</pre>`;
  }
  if (value.suggestion) {
    return suggestionDetailHtml(value);
  }
  if (value.exit_plan) {
    return positionDetailHtml(value);
  }
  if (value.details_json && value.action) {
    return decisionDetailHtml(value);
  }
  if (value.details_json && value.side) {
    return orderDetailHtml(value);
  }
  const rows = Object.entries(value)
    .filter(([key]) => !key.endsWith("_json"))
    .map(([key, item]) => `<div><span>${escapeHtml(key)}</span><strong>${escapeHtml(formatDetailValue(item))}</strong></div>`)
    .join("");
  const jsonBlocks = Object.entries(value)
    .filter(([key]) => key.endsWith("_json"))
    .map(([key, item]) => `<h4>${escapeHtml(key)}</h4><pre>${escapeHtml(prettyJson(item))}</pre>`)
    .join("");
  return `<div class="detail-list">${rows}</div>${jsonBlocks}<pre>${escapeHtml(JSON.stringify(value, null, 2))}</pre>`;
}

function suggestionDetailHtml(row) {
  const audit = parseJsonObject(row.details_json);
  const context = audit.context || {};
  return `
    ${auditHero({
      label: "Suggestion",
      symbol: row.symbol,
      action: row.suggestion,
      status: `${row.confluence}/26 ${row.tier || ""}`,
      meta: `${fmtMoney(row.price)} · combined ${fmtNumber(row.combined_score)}`,
    })}
    <section class="audit-section">
      <h4>Why Suggested</h4>
      <p>${escapeHtml(row.reason || "-")}</p>
      <div class="audit-chips">
        <span>Readiness: ${escapeHtml(row.decision_readiness || "-")}</span>
        <span>Strategy: ${escapeHtml(row.strategy || "-")}</span>
        <span>Institutional: ${fmtNumber(row.institutional_bias)}</span>
      </div>
    </section>
    ${exitPlanHtml(row.exit_plan)}
    ${scoreBreakdownHtml(audit.score_breakdown)}
    ${marketContextHtml(context)}
    ${fullSpectrumHtml(context.full_spectrum_analysis)}
  `;
}

function positionDetailHtml(row) {
  const pnl = (Number(row.market_price) - Number(row.avg_price)) * Number(row.qty);
  return `
    ${auditHero({
      label: "Position",
      symbol: row.symbol,
      action: pnl >= 0 ? "OPEN" : "WATCH",
      status: row.strategy || "-",
      meta: `${row.qty} qty · ${fmtMoney(pnl)} unrealized`,
    })}
    ${objectCardsHtml("Position", {
      qty: row.qty,
      avg_price: fmtMoney(row.avg_price),
      market_price: fmtMoney(row.market_price),
      market_value: fmtMoney(Number(row.market_price) * Number(row.qty)),
      unrealized_pnl: fmtMoney(pnl),
    })}
    ${exitPlanHtml(row.exit_plan)}
    <section class="audit-section">
      <h4>Full Position JSON</h4>
      <pre>${escapeHtml(JSON.stringify(row, null, 2))}</pre>
    </section>
  `;
}

function decisionDetailHtml(row) {
  const audit = parseJsonObject(row.details_json);
  const context = audit.context || {};
  const llm = audit.llm_output || null;
  const exit = exitPlanFromAudit(audit);
  return `
    ${auditHero({
      label: "Decision audit",
      symbol: row.symbol,
      action: row.action,
      status: audit.decision_path || row.strategy || "-",
      meta: `${fmtNumber(Number(row.confidence) * 100)}% confidence · ${fmtMoney(row.price)}`,
    })}
    <section class="audit-section">
      <h4>Why ${escapeHtml(row.action)}</h4>
      <p>${escapeHtml(audit.action_reason || row.reason || "-")}</p>
      <div class="audit-chips">
        <span>Strategy: ${escapeHtml(row.strategy || context.best_strategy?.name || "-")}</span>
        <span>Path: ${escapeHtml(audit.decision_path || "-")}</span>
        <span>At: ${escapeHtml(fmtTime(row.ts))}</span>
      </div>
    </section>
    ${preFilterHtml(audit, context)}
    ${exitPlanHtml(exit)}
    ${scoreBreakdownHtml(audit.score_breakdown)}
    ${llm ? llmOutputHtml(llm, audit) : ""}
    ${riskGateHtml(audit)}
    ${marketContextHtml(context)}
    ${fullSpectrumHtml(context.full_spectrum_analysis)}
    ${strategySignalsHtml(context.strategy_signals || [])}
    <section class="audit-section">
      <h4>Full Audit JSON</h4>
      <pre>${escapeHtml(JSON.stringify(audit, null, 2))}</pre>
    </section>
  `;
}

function orderDetailHtml(row) {
  const audit = parseJsonObject(row.details_json);
  const execution = audit.execution || {};
  const route = audit.route || {};
  const exit = exitPlanFromOrder(row);
  return `
    ${auditHero({
      label: "Order audit",
      symbol: row.symbol,
      action: row.side,
      status: row.status,
      meta: `${row.qty} qty · ${fmtMoney(row.notional)}`,
    })}
    <section class="audit-section">
      <h4>Why Order ${escapeHtml(row.status)}</h4>
      <p>${escapeHtml(row.reason || "-")}</p>
      <div class="audit-chips">
        <span>Strategy: ${escapeHtml(row.strategy || "-")}</span>
        <span>Price: ${fmtMoney(row.price)}</span>
        <span>Time: ${escapeHtml(fmtTime(row.ts))}</span>
      </div>
    </section>
    ${objectCardsHtml("Execution Sizing", execution.sizing)}
    ${objectCardsHtml("Execution Risk Checks", execution.risk_checks || execution.daily_loss)}
    ${exitPlanHtml(exit)}
    ${objectCardsHtml("Broker / Route", route)}
    ${audit.decision ? nestedDecisionHtml(audit.decision) : ""}
    <section class="audit-section">
      <h4>Full Audit JSON</h4>
      <pre>${escapeHtml(JSON.stringify(audit, null, 2))}</pre>
    </section>
  `;
}

function exitPlanFromOrder(row) {
  const audit = parseJsonObject(row.details_json);
  return exitPlanFromAudit(audit);
}

function exitPlanFromAudit(audit) {
  const details = audit.decision?.details || {};
  const full = details.context?.full_spectrum_analysis || audit.context?.full_spectrum_analysis || {};
  const tradePlan = full.trade_plan || {};
  if (!Object.keys(tradePlan).length) return {};
  const targets = normalizedTargets(tradePlan.targets || []);
  return {
    horizon: tradePlan.horizon,
    entry_zone: tradePlan.entry_zone,
    stop_loss: tradePlan.stop_loss,
    target_1: targets[0],
    target_2: targets[1],
    target_3: targets[2],
    invalidation: tradePlan.invalidation,
    monitoring_checklist: full.monitoring_checklist || [],
    plan: "Exit on stop or invalidation. Take partial profit or tighten stop near T1, trail after T1, then reassess at T2/T3 or if news/global risk turns negative.",
  };
}

function normalizedTargets(targets) {
  const normalized = Array.isArray(targets) ? targets.map((target) => ({ ...(target || {}) })) : [];
  if (normalized.length < 3) return normalized;
  const t1 = Number(normalized[0]?.price);
  const t2 = Number(normalized[1]?.price);
  const t3 = Number(normalized[2]?.price);
  if (!Number.isFinite(t2) || !Number.isFinite(t3) || t3 > t2) return normalized;
  const riskStep = Number.isFinite(t1) && t2 > t1 ? t2 - t1 : Math.max(t2 * 0.01, 0.01);
  normalized[2] = {
    ...normalized[2],
    price: t2 + riskStep,
    rr: normalized[2].rr === "structure" ? "3.5_or_structure" : normalized[2].rr,
    structure_reference: normalized[2].structure_reference ?? t3,
    note: "Normalized above T2; original structure target is kept as reference.",
  };
  return normalized;
}

function exitPlanMini(exit) {
  if (!exit || !Object.keys(exit).length) return `<span class="muted">pending</span>`;
  const t1 = exit.target_1 || {};
  return `<span class="exit-mini">SL ${fmtMoney(exit.stop_loss)} · T1 ${fmtMoney(t1.price)}</span>`;
}

function exitPlanHtml(exit) {
  if (!exit || !Object.keys(exit).length) return "";
  return `<section class="audit-section exit-plan">
    <h4>Exit Plan</h4>
    <div class="audit-cards">
      <div class="audit-card"><span>When</span><strong>${escapeHtml(exit.horizon || "swing_3_to_7_days")}</strong><small>review every cycle</small></div>
      <div class="audit-card"><span>Entry Zone</span><strong>${escapeHtml(formatZone(exit.entry_zone))}</strong><small>avoid chasing outside plan</small></div>
      <div class="audit-card"><span>Hard Stop</span><strong class="negative">${fmtMoney(exit.stop_loss)}</strong><small>exit if invalidated</small></div>
      <div class="audit-card"><span>Target 1</span><strong class="positive">${fmtMoney(exit.target_1?.price)}</strong><small>R:R ${escapeHtml(exit.target_1?.rr ?? "-")}</small></div>
      <div class="audit-card"><span>Target 2</span><strong class="positive">${fmtMoney(exit.target_2?.price)}</strong><small>R:R ${escapeHtml(exit.target_2?.rr ?? "-")}</small></div>
      <div class="audit-card"><span>Target 3</span><strong class="positive">${fmtMoney(exit.target_3?.price)}</strong><small>${escapeHtml(exit.target_3?.rr ?? "-")}</small></div>
    </div>
    <p>${escapeHtml(exit.plan || "-")}</p>
    ${objectCardsHtml("Invalidation", exit.invalidation)}
    ${auditList("Exit Monitoring", exit.monitoring_checklist)}
  </section>`;
}

function formatZone(zone) {
  if (!Array.isArray(zone) || !zone.length) return "-";
  return zone.length === 1 ? fmtMoney(zone[0]) : `${fmtMoney(zone[0])} - ${fmtMoney(zone[zone.length - 1])}`;
}

function auditHero({ label, symbol, action, status, meta }) {
  const tagClass = String(action || "").toLowerCase();
  return `<section class="audit-hero">
    <span>${escapeHtml(label)}</span>
    <div>
      <strong>${escapeHtml(symbol || "-")}</strong>
      <span class="tag ${tagClass}">${escapeHtml(action || "-")}</span>
    </div>
    <p>${escapeHtml(status || "-")} · ${escapeHtml(meta || "-")}</p>
  </section>`;
}

function scoreBreakdownHtml(score) {
  if (!score || !Array.isArray(score.components)) return "";
  return `<section class="audit-section">
    <h4>Score Breakdown</h4>
    <div class="audit-cards">
      ${score.components
        .map(
          (component) => `<div class="audit-card">
            <span>${labelize(component.name)}</span>
            <strong class="${pnlClass(component.score)}">${fmtNumber(component.score)}</strong>
            <small>weight ${fmtNumber(Number(component.weight) * 100)}% · contribution ${fmtNumber(component.contribution)}</small>
          </div>`,
        )
        .join("")}
    </div>
    <p class="audit-formula">${escapeHtml(score.formula || "")} = <strong>${fmtNumber(score.combined)}</strong></p>
  </section>`;
}

function preFilterHtml(audit, context) {
  const pre = audit.pre_filter || context.pre_filter || audit.risk_gates?.pre_filter || {};
  const gateContext = audit.risk_gates?.decision_gate_context || {};
  const gates = gateContext.evaluated_gates || pre.gates || gateContext.failed_gates || [];
  if (!gates.length) return "";
  return `<section class="audit-section">
    <h4>Pre-Filter Gates</h4>
    <div class="audit-cards">
      ${gates.map((gate) => `<div class="audit-card">
        <span>${escapeHtml(labelize(gate.gate || "gate"))}</span>
        <strong class="${gate.passed === false ? "negative" : "positive"}">${gate.passed === false ? "fail" : "pass"}</strong>
        <small>${escapeHtml(shortValue(gate.value ?? gate.reason ?? "-", 160))}</small>
      </div>`).join("")}
    </div>
    ${pre.elimination_reason ? `<p class="negative">${escapeHtml(pre.elimination_reason)}</p>` : ""}
  </section>`;
}

function llmOutputHtml(llm, audit) {
  return `<section class="audit-section">
    <h4>LLM Evidence</h4>
    <div class="audit-cards two">
      <div class="audit-card">
        <span>Requested Action</span>
        <strong>${escapeHtml(audit.requested_action || audit.final_action || "-")}</strong>
        <small>model risk: ${escapeHtml(llm.risk || "-")}</small>
      </div>
      <div class="audit-card">
        <span>Analysed By</span>
        <strong>${escapeHtml(audit.model || llm.model || "-")}</strong>
        <small>${escapeHtml(audit.provider || llm.provider || "-")} · ${escapeHtml(audit.analysis_mode || llm.analysis_mode || "single_context")}</small>
      </div>
      <div class="audit-card">
        <span>Confidence Gate</span>
        <strong>${audit.confidence_gate?.passed ? "passed" : "not passed"}</strong>
        <small>minimum ${fmtNumber(Number(audit.confidence_gate?.minimum_required || 0) * 100)}%</small>
      </div>
    </div>
    ${objectCardsHtml("LLM Routing", {
      configured_provider: audit.configured_provider,
      configured_model: audit.configured_model,
      selected_provider: audit.provider,
      selected_model: audit.model,
      analysis_mode: audit.analysis_mode,
      rolling_context: audit.rolling_context,
    })}
    ${auditList("Model Attempts", (audit.model_attempts || []).map((item) => `${item.status}: ${item.provider}/${item.model} ${item.latency_ms || 0}ms ${item.error || ""}`))}
    ${auditList("Evidence", llm.evidence)}
    ${auditList("Checklist", llm.checklist)}
    ${auditList("Risk Checks", llm.risk_checks)}
    ${auditList("Invalidators", llm.invalidators)}
    ${objectCardsHtml("LLM Signal Plan", llm.signal_plan)}
    ${objectCardsHtml("LLM Trade Plan", llm.trade_plan)}
    ${auditList("LLM Monitoring Checklist", llm.monitoring_checklist)}
    ${auditList("LLM Data Gaps", llm.data_gaps)}
  </section>`;
}

function riskGateHtml(audit) {
  const gates = audit.risk_gates || {};
  const confidence = audit.confidence_gate ? { confidence_gate: audit.confidence_gate } : {};
  return objectCardsHtml("Risk Gates", { ...confidence, ...gates });
}

function marketContextHtml(context) {
  if (!context || !Object.keys(context).length) return "";
  return `<section class="audit-section">
    <h4>Market Context Used</h4>
    <div class="audit-cards">
      <div class="audit-card"><span>Quote</span><strong>${fmtMoney(context.quote?.price)}</strong><small>${escapeHtml(context.quote?.source || "-")}</small></div>
      <div class="audit-card"><span>Technical</span><strong class="${pnlClass(context.technical_math?.score)}">${fmtNumber(context.technical_math?.score)}</strong><small>${escapeHtml(context.technical_math?.trend || "-")}</small></div>
      <div class="audit-card"><span>Candles</span><strong class="${pnlClass(context.candlestick_analysis?.score)}">${fmtNumber(context.candlestick_analysis?.score)}</strong><small>${escapeHtml((context.candlestick_analysis?.patterns || []).join(", "))}</small></div>
      <div class="audit-card"><span>Sentiment</span><strong class="${pnlClass(context.sentiment?.score)}">${fmtNumber(context.sentiment?.score)}</strong><small>news sentiment score</small></div>
      <div class="audit-card"><span>Global Risk</span><strong class="${pnlClass(context.global_market_context?.risk_score)}">${fmtNumber(context.global_market_context?.risk_score)}</strong><small>${escapeHtml(context.global_market_context?.regime || "-")}</small></div>
      <div class="audit-card"><span>Free Institutional</span><strong class="${pnlClass(context.institutional_context?.market_bias?.score)}">${fmtNumber(context.institutional_context?.market_bias?.score)}</strong><small>${escapeHtml(context.institutional_context?.source_quality || "-")}</small></div>
      <div class="audit-card"><span>Universe Rank</span><strong>${escapeHtml(context.universe_scan?.rank || "-")}</strong><small>${escapeHtml(shortValue(context.universe_scan?.selection_basis || "-", 110))}</small></div>
    </div>
    <pre>${escapeHtml(JSON.stringify({
      position: context.position,
      technical_math: context.technical_math,
      candlestick_analysis: context.candlestick_analysis,
      best_strategy: context.best_strategy,
      global_market_context: context.global_market_context,
      institutional_context: context.institutional_context,
      universe_scan: context.universe_scan,
      recent_candle_count: context.recent_candle_count,
      recent_candles_tail: context.recent_candles_tail,
    }, null, 2))}</pre>
  </section>`;
}

function fullSpectrumHtml(analysis) {
  if (!analysis || typeof analysis !== "object") return "";
  const confluence = analysis.confluence_score || {};
  const trend = analysis.trend_context || {};
  const tradePlan = analysis.trade_plan || {};
  const risk = analysis.risk_overrides || {};
  const liquidity = analysis.liquidity_profile || {};
  const conflicts = analysis.signal_conflicts || {};
  const scorecard = analysis.institutional_scorecard || {};
  const stage = analysis.stage_analysis || {};
  const entry = analysis.entry_quality || {};
  const breakout = analysis.breakout_quality || {};
  const divergence = analysis.price_volume_divergence || {};
  const alignment = trend.timeframe_alignment || {};
  const sector = analysis.sector_rotation || {};
  return `<section class="audit-section">
    <h4>Full-Spectrum v2 Analysis</h4>
    <div class="audit-cards">
      <div class="audit-card"><span>Stage</span><strong class="${stage.buy_permitted ? "positive" : "negative"}">${escapeHtml(stage.stage || "-")}</strong><small>${escapeHtml(`${stage.stage_confidence || "-"} · buy ${stage.buy_permitted ? "permitted" : "blocked"}`)}</small></div>
      <div class="audit-card"><span>Entry Grade</span><strong class="${entry.entry_grade === "D" ? "negative" : "positive"}">${escapeHtml(entry.entry_grade || "-")}</strong><small>${fmtPct(entry.distance_from_pivot_pct)} from pivot</small></div>
      <div class="audit-card"><span>Breakout</span><strong class="${breakout.two_day_rule_failed ? "negative" : ""}">${escapeHtml(breakout.breakout_quality || "-")}</strong><small>2-day fail: ${escapeHtml(String(Boolean(breakout.two_day_rule_failed)))}</small></div>
      <div class="audit-card"><span>PV Divergence</span><strong class="${pnlClass(divergence.divergence_score)}">${fmtNumber(divergence.divergence_score)}</strong><small>climax ${escapeHtml(String(Boolean(divergence.climax_volume_top)))}</small></div>
      <div class="audit-card"><span>MTF Alignment</span><strong class="grade-${escapeHtml(alignment.alignment_grade || "D")}">${escapeHtml(alignment.alignment_grade || "-")}</strong><small>${escapeHtml(shortValue(alignment.timeframes || {}, 120))}</small></div>
      <div class="audit-card"><span>Sector</span><strong class="${sector.sector_tailwind ? "positive" : sector.sector_headwind ? "negative" : ""}">${escapeHtml(sector.sector_tier || "-")}</strong><small>${escapeHtml(`${sector.sector_stage || "-"} · rank ${sector.sector_rank || "-"}`)}</small></div>
      <div class="audit-card"><span>Confluence</span><strong>${escapeHtml(confluence.total ?? "-")}/26</strong><small>${escapeHtml(confluence.tier || "-")}</small></div>
      <div class="audit-card"><span>Institutional Score</span><strong>${escapeHtml(scorecard.total_score ?? "-")}/100</strong><small>${escapeHtml(`${scorecard.grade || "-"} · ${scorecard.buy_ready ? "buy ready" : "not ready"}`)}</small></div>
      <div class="audit-card"><span>Daily Trend</span><strong>${escapeHtml(trend.daily || "-")}</strong><small>${escapeHtml(trend.structure || "-")}</small></div>
      <div class="audit-card"><span>Signal Direction</span><strong>${escapeHtml(tradePlan.direction || "-")}</strong><small>${escapeHtml(tradePlan.horizon || "-")}</small></div>
      <div class="audit-card"><span>Risk Overrides</span><strong>${escapeHtml(risk.no_new_longs ? "no new longs" : "clear")}</strong><small>${escapeHtml((risk.flags || []).join(", ") || "-")}</small></div>
      <div class="audit-card"><span>Liquidity</span><strong>${escapeHtml(liquidity.liquidity_tier || "-")}</strong><small>${fmtMoney(liquidity.avg_traded_value_20)} avg value</small></div>
      <div class="audit-card"><span>Conflicts</span><strong>${escapeHtml(conflicts.severity || "-")}</strong><small>${escapeHtml((conflicts.conflicts || []).join(", ") || "-")}</small></div>
    </div>
    ${objectCardsHtml("Stage Analysis", stage)}
    ${objectCardsHtml("Entry Quality", entry)}
    ${objectCardsHtml("Breakout Quality", breakout)}
    ${objectCardsHtml("Price-Volume Divergence", divergence)}
    ${objectCardsHtml("Multi-Timeframe Alignment", alignment)}
    ${objectCardsHtml("Sector Rotation", sector)}
    ${objectCardsHtml("Confluence Breakdown", confluence.breakdown)}
    ${objectCardsHtml("Prompt v2 Requirement Coverage", analysis.requirement_coverage)}
    ${scorecardHtml(scorecard)}
    ${objectCardsHtml("Signal Plan", analysis.signal_plan)}
    ${objectCardsHtml("News Sentiment", analysis.news_sentiment)}
    ${objectCardsHtml("Liquidity Profile", analysis.liquidity_profile)}
    ${objectCardsHtml("Relative Strength", analysis.relative_strength)}
    ${objectCardsHtml("Fundamental Quality", analysis.fundamental_quality)}
    ${objectCardsHtml("Corporate Event Risk", analysis.corporate_event_risk)}
    ${objectCardsHtml("Delivery / Accumulation", analysis.delivery_accumulation)}
    ${objectCardsHtml("Options / OI", analysis.options_oi)}
    ${objectCardsHtml("Backtest Snapshot", analysis.backtest_snapshot)}
    ${objectCardsHtml("Signal Conflicts", analysis.signal_conflicts)}
    ${objectCardsHtml("Institutional Flow", analysis.institutional_flow)}
    ${objectCardsHtml("Key Levels", analysis.key_levels)}
    ${objectCardsHtml("Institutional Structure", analysis.institutional_structure)}
    ${auditList("Monitoring Checklist", analysis.monitoring_checklist)}
    ${auditList("Data Gaps", analysis.data_gaps)}
    <pre>${escapeHtml(JSON.stringify({
      primary_filters: analysis.primary_filters,
      fibonacci: analysis.fibonacci,
      indicator_suite: analysis.indicator_suite,
      candlestick_v2: analysis.candlestick_v2,
      chart_patterns: analysis.chart_patterns,
      liquidity_profile: analysis.liquidity_profile,
      relative_strength: analysis.relative_strength,
      fundamental_quality: analysis.fundamental_quality,
      corporate_event_risk: analysis.corporate_event_risk,
      delivery_accumulation: analysis.delivery_accumulation,
      options_oi: analysis.options_oi,
      backtest_snapshot: analysis.backtest_snapshot,
      signal_conflicts: analysis.signal_conflicts,
      institutional_scorecard: analysis.institutional_scorecard,
      trade_plan: analysis.trade_plan,
    }, null, 2))}</pre>
  </section>`;
}

function scorecardHtml(scorecard) {
  if (!scorecard || typeof scorecard !== "object" || !Object.keys(scorecard).length) return "";
  const sections = scorecard.sections || {};
  return `<section class="audit-section">
    <h4>Institutional Scorecard</h4>
    <div class="audit-cards">
      <div class="audit-card"><span>Score</span><strong>${escapeHtml(scorecard.total_score ?? "-")}/100</strong><small>${escapeHtml(scorecard.grade || "-")}</small></div>
      <div class="audit-card"><span>Buy Ready</span><strong>${escapeHtml(scorecard.buy_ready ? "yes" : "no")}</strong><small>${escapeHtml((scorecard.must_pass_failed || []).join(", ") || "all must-pass clear")}</small></div>
      <div class="audit-card"><span>Hard Veto</span><strong>${escapeHtml(scorecard.hard_veto?.passed ? "clear" : "blocked")}</strong><small>${escapeHtml((scorecard.hard_veto?.failed || []).join(", ") || "-")}</small></div>
      <div class="audit-card"><span>Warnings</span><strong>${escapeHtml((scorecard.warnings || []).length)}</strong><small>${escapeHtml((scorecard.warnings || []).join(", ") || "-")}</small></div>
    </div>
    <div class="audit-cards">
      ${Object.values(sections)
        .map(
          (section) => `<div class="audit-card">
            <span>${escapeHtml(section.label || labelize(section.key || ""))}</span>
            <strong>${escapeHtml(section.score ?? "-")}/${escapeHtml(section.max ?? "-")}</strong>
            <small>${escapeHtml(`${section.status || "-"} · ${(section.evidence || []).join(", ") || "-"}`)}</small>
          </div>`,
        )
        .join("")}
    </div>
  </section>`;
}

function strategySignalsHtml(signals) {
  if (!Array.isArray(signals) || !signals.length) return "";
  return `<section class="audit-section">
    <h4>Strategy Signals</h4>
    <div class="audit-table-wrap">
      <table class="audit-table">
        <thead><tr><th>Name</th><th>Direction</th><th>Score</th><th>Confidence</th><th>Notes</th></tr></thead>
        <tbody>
          ${signals
            .map(
              (signal) => `<tr>
                <td>${escapeHtml(signal.name || "-")}</td>
                <td>${escapeHtml(signal.direction || "-")}</td>
                <td class="num ${pnlClass(signal.score)}">${fmtNumber(signal.score)}</td>
                <td class="num">${fmtNumber(Number(signal.confidence) * 100)}%</td>
                <td>${escapeHtml((signal.notes || []).join(", ") || "-")}</td>
              </tr>`,
            )
            .join("")}
        </tbody>
      </table>
    </div>
  </section>`;
}

function nestedDecisionHtml(decision) {
  return `<section class="audit-section">
    <h4>Linked Decision</h4>
    <p>${escapeHtml(decision.reason || decision.action_reason || "-")}</p>
    <pre>${escapeHtml(JSON.stringify(decision, null, 2))}</pre>
  </section>`;
}

function objectCardsHtml(title, object) {
  if (!object || typeof object !== "object" || !Object.keys(object).length) return "";
  return `<section class="audit-section">
    <h4>${escapeHtml(title)}</h4>
    <div class="audit-cards">
      ${Object.entries(object)
        .map(
          ([key, value]) => `<div class="audit-card">
            <span>${labelize(key)}</span>
            <strong>${escapeHtml(shortValue(value))}</strong>
          </div>`,
        )
        .join("")}
    </div>
  </section>`;
}

function auditList(title, items) {
  if (!items || (Array.isArray(items) && !items.length)) return "";
  const list = Array.isArray(items) ? items : [items];
  return `<div class="audit-list">
    <strong>${escapeHtml(title)}</strong>
    <ul>${list.map((item) => `<li>${escapeHtml(shortValue(item, 220))}</li>`).join("")}</ul>
  </div>`;
}

function parseJsonObject(value) {
  try {
    const parsed = typeof value === "string" ? JSON.parse(value || "{}") : value;
    return parsed && typeof parsed === "object" ? parsed : {};
  } catch {
    return {};
  }
}

function labelize(value) {
  return escapeHtml(String(value || "-").replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase()));
}

function shortValue(value, max = 90) {
  if (value === null || value === undefined) return "-";
  const text = typeof value === "object" ? JSON.stringify(value) : String(value);
  return text.length > max ? `${text.slice(0, max)}...` : text;
}

function formatDetailValue(value) {
  if (value === null || value === undefined) return "-";
  if (typeof value === "object") return JSON.stringify(value);
  return value;
}

function prettyJson(value) {
  try {
    return JSON.stringify(JSON.parse(value), null, 2);
  } catch {
    return value;
  }
}

function drawEquity(rows) {
  const canvas = byId("equity-chart");
  const ctx = canvas.getContext("2d");
  const dpr = window.devicePixelRatio || 1;
  const width = canvas.clientWidth;
  const height = canvas.clientHeight;
  canvas.width = width * dpr;
  canvas.height = height * dpr;
  ctx.scale(dpr, dpr);
  ctx.clearRect(0, 0, width, height);

  const pad = 24;
  ctx.strokeStyle = "#d9e0e8";
  ctx.lineWidth = 1;
  for (let i = 0; i < 4; i += 1) {
    const y = pad + ((height - pad * 2) * i) / 3;
    ctx.beginPath();
    ctx.moveTo(pad, y);
    ctx.lineTo(width - pad, y);
    ctx.stroke();
  }

  if (rows.length < 2) {
    ctx.fillStyle = "#667085";
    ctx.font = "13px system-ui";
    ctx.fillText("Waiting for portfolio snapshots", pad, height / 2);
    return;
  }

  const values = rows.map((row) => Number(row.equity)).filter(Number.isFinite);
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min || 1;
  const xStep = (width - pad * 2) / Math.max(values.length - 1, 1);

  ctx.beginPath();
  values.forEach((value, index) => {
    const x = pad + index * xStep;
    const y = height - pad - ((value - min) / span) * (height - pad * 2);
    if (index === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  });
  ctx.strokeStyle = values[values.length - 1] >= values[0] ? "#15803d" : "#b42318";
  ctx.lineWidth = 2;
  ctx.stroke();

  ctx.fillStyle = "#667085";
  ctx.font = "12px system-ui";
  ctx.fillText(fmtMoney(max), pad, pad - 6);
  ctx.fillText(fmtMoney(min), pad, height - 8);
}

async function postControl(path) {
  try {
    const response = await fetch(path, { method: "POST" });
    const payload = await response.json().catch(() => ({ detail: `HTTP ${response.status}` }));
    if (!response.ok) {
      showDetails("Control Error", payload);
      const error = byId("error-box");
      error.hidden = false;
      error.textContent = payload.detail || `Control request failed: ${response.status}`;
      return;
    }
    render(payload);
    fetchLogs();
  } catch (error) {
    showBackendError(networkErrorMessage(error, "control request"), { path });
  }
}

async function analyzeSymbol(event) {
  event.preventDefault();
  const input = byId("analyze-symbol");
  const button = byId("analyze-btn");
  const symbol = input.value.trim().toUpperCase();
  if (!symbol) {
    byId("analyze-status").textContent = "enter a symbol";
    return;
  }
  const configuredTimeout = Number(state.config?.settings?.llm_timeout_seconds || 60);
  const cycleTimeout = Number(state.config?.settings?.cycle_timeout_seconds || 180);
  const timeoutMs = Math.max(120000, Math.min(240000, Math.max(configuredTimeout, cycleTimeout) * 1000 + 30000));
  const controller = new AbortController();
  const started = Date.now();
  const timer = window.setTimeout(() => controller.abort(), timeoutMs);
  const ticker = window.setInterval(() => {
    const elapsed = Math.round((Date.now() - started) / 1000);
    byId("analyze-status").textContent = `analyzing ${symbol} · ${elapsed}s`;
  }, 1000);
  button.disabled = true;
  byId("analyze-status").textContent = `analyzing ${symbol}...`;
  byId("analyze-result").innerHTML = `<div class="empty-state">Running live quote, candles, strategy, sentiment, risk gates, and LLM if enabled...</div>`;
  try {
    const response = await fetch("/api/analyze-symbol", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ symbol }),
      signal: controller.signal,
    });
    const payload = await response.json().catch(() => ({ detail: `HTTP ${response.status}` }));
    if (!response.ok) {
      byId("analyze-status").textContent = "analysis failed";
      byId("analyze-result").innerHTML = `<div class="error-box">${escapeHtml(payload.detail || "Analysis failed")}</div>`;
      return;
    }
    byId("analyze-status").textContent = `${payload.symbol} analyzed`;
    renderManualAnalysis(payload);
    fetchLogs();
  } catch (error) {
    const timeout = error?.name === "AbortError";
    byId("analyze-status").textContent = timeout ? "analysis timed out" : "network error";
    const message = timeout
      ? "Symbol analysis is still taking longer than the UI wait budget. Try again when the cycle is idle, or check Logs for a completed manual analysis entry."
      : networkErrorMessage(error, "symbol analysis");
    byId("analyze-result").innerHTML = `<div class="error-box">${escapeHtml(message)}</div>`;
  } finally {
    window.clearTimeout(timer);
    window.clearInterval(ticker);
    button.disabled = false;
  }
}

function renderManualAnalysis(payload) {
  const decision = payload.decision || {};
  const action = String(decision.action || "HOLD").toLowerCase();
  const details = decision.details || parseJsonObject(decision.details_json);
  const path = details.decision_path || decision.strategy || "-";
  const news = payload.news || {};
  const headlines = news.headlines || [];
  byId("analyze-result").innerHTML = `
    <div class="manual-analysis-card">
      <div>
        <span>Symbol</span>
        <strong>${escapeHtml(payload.symbol || decision.symbol || "-")}</strong>
        <small>${escapeHtml(payload.provider || "-")} · ${payload.candle_count || 0} candles</small>
      </div>
      <div>
        <span>Decision</span>
        <strong><span class="tag ${action}">${escapeHtml(decision.action || "-")}</span></strong>
        <small>${escapeHtml(path)}</small>
      </div>
      <div>
        <span>Confidence</span>
        <strong>${fmtNumber(Number(decision.confidence || 0) * 100)}%</strong>
        <small>policy gates still apply</small>
      </div>
      <div>
        <span>Price</span>
        <strong>${fmtMoney(decision.price || payload.quote?.price)}</strong>
        <small>${escapeHtml(fmtTime(payload.quote?.asof))}</small>
      </div>
      <div>
        <span>News Sentiment</span>
        <strong class="${pnlClass(news.score)}">${fmtNumber(news.score)}</strong>
        <small>${headlines.length} latest items · ${fmtNumber(Number(news.confidence || 0) * 100)}% conf</small>
      </div>
    </div>
    <section class="audit-section manual-summary">
      <h4>Reason</h4>
      <p>${escapeHtml(decision.reason || "-")}</p>
      ${auditList("Latest News", headlines.slice(0, 6))}
      ${payload.provider_error ? `<p class="negative">${escapeHtml(payload.provider_error)}</p>` : ""}
      <button id="manual-detail-btn" type="button">Open Full Analysis</button>
    </section>
  `;
  byId("manual-detail-btn").addEventListener("click", () => showDetails("Manual Analysis", decision));
}

function bindControls() {
  byId("start-btn").addEventListener("click", () => postControl("/api/control/start"));
  byId("stop-btn").addEventListener("click", () => postControl("/api/control/stop"));
  byId("run-btn").addEventListener("click", () => postControl("/api/control/run-once"));
  byId("analyze-form").addEventListener("submit", analyzeSymbol);
  byId("login-form").addEventListener("submit", login);
  byId("user-create-form").addEventListener("submit", createUser);
  byId("save-settings-btn").addEventListener("click", saveSettings);
  byId("reset-demo-btn").addEventListener("click", resetDemo);
  byId("test-llm-btn").addEventListener("click", testLlm);
  byId("upstox-auth-url-btn").addEventListener("click", openUpstoxLogin);
  byId("upstox-connect-btn").addEventListener("click", connectUpstox);
  byId("nubra-send-otp-btn").addEventListener("click", sendNubraOtp);
  byId("nubra-connect-btn").addEventListener("click", connectNubra);
  byId("refresh-logs-btn").addEventListener("click", fetchLogs);
  byId("drawer-close").addEventListener("click", () => byId("detail-drawer").classList.remove("open"));
  for (const button of document.querySelectorAll(".nav-item")) {
    button.addEventListener("click", () => setView(button.dataset.view));
  }
  for (const tile of document.querySelectorAll(".kpi")) {
    tile.addEventListener("click", () => {
      const portfolio = state.latest?.portfolio || {};
      const map = {
        portfolio,
        cash: { cash: portfolio.cash, equity: portfolio.equity },
        invested: { invested: portfolio.invested, market_value: portfolio.market_value },
        pnl: { unrealized_pnl: portfolio.unrealized_pnl, realized_pnl: portfolio.realized_pnl },
        "positions-summary": state.latest?.positions || [],
        "decision-summary": state.latest?.decisions || [],
      };
      showDetails(tile.dataset.detailType, map[tile.dataset.detailType]);
    });
  }
  for (const tile of document.querySelectorAll(".ops-card")) {
    tile.addEventListener("click", () => {
      const settings = currentSettings();
      const map = {
        "feed-health": state.latest?.market_health || {},
        "llm-health": {
          provider: settings.llm_provider,
          mode: settings.llm_decision_mode,
          model: settings.deepseek_model,
          base_url: settings.deepseek_base_url,
          api_key_saved: Boolean(settings.deepseek_api_key?.saved),
          reasoning_effort: settings.llm_reasoning_effort,
          thinking_enabled: settings.llm_thinking_enabled,
          rolling_context_enabled: settings.llm_rolling_context_enabled,
          timeout_seconds: settings.llm_timeout_seconds,
          usage: state.latest?.llm_usage || {},
        },
        "risk-health": {
          max_positions: settings.max_positions,
          max_position_pct: settings.max_position_pct,
          max_order_value_pct: settings.max_order_value_pct,
          stop_loss_pct: settings.stop_loss_pct,
          take_profit_pct: settings.take_profit_pct,
          daily_loss_limit_pct: settings.daily_loss_limit_pct,
        },
        "macro-health": {
          global: state.latest?.macro_context || {},
          institutional: state.latest?.institutional_context || {},
          market_breadth: state.latest?.market_breadth || {},
          sector_rotation: state.latest?.sector_rotation_context || {},
          upcoming_macro_events: state.latest?.upcoming_macro_events || [],
        },
        "cycle-health": {
          running: state.latest?.running,
          last_cycle_at: state.latest?.last_cycle_at,
          cycle: state.latest?.cycle,
          interval_seconds: settings.agent_interval_seconds,
          last_error: state.latest?.last_error,
        },
      };
      showDetails(tile.dataset.detailType, map[tile.dataset.detailType]);
    });
  }
  byId("logout-btn").addEventListener("click", logout);
}

function setView(view) {
  for (const item of document.querySelectorAll(".nav-item")) {
    item.classList.toggle("active", item.dataset.view === view);
  }
  for (const section of document.querySelectorAll(".view")) {
    section.classList.toggle("active", section.id === `${view}-view`);
  }
  const label = document.querySelector(`.nav-item[data-view="${view}"] span`)?.textContent || "Overview";
  byId("view-title").textContent = label;
}

async function loadInitial() {
  try {
    const authResponse = await fetch("/api/auth/me");
    const auth = await authResponse.json();
    renderAuth(auth);
    if (!auth.authenticated) {
      return;
    }
    await loadAuthenticatedData();
    openSocket();
  } catch (error) {
    byId("login-status").textContent = "Backend unavailable. Start OpenTrade and refresh.";
    byId("login-status").className = "settings-inline-status negative";
  }
}

async function loadAuthenticatedData() {
  try {
    const [statusResponse, configResponse, accountResponse] = await Promise.all([
      fetch("/api/status"),
      fetch("/api/config"),
      fetch("/api/account"),
    ]);
    render(await statusResponse.json());
    renderSettings(await configResponse.json());
    renderAccount(await accountResponse.json());
    if (state.auth?.admin) fetchUsers();
  } catch (error) {
    showBackendError(networkErrorMessage(error, "initial load"), { action: "initial load" });
  }
}

function openSocket() {
  if (!state.auth?.authenticated || state.socket) return;
  const protocol = window.location.protocol === "https:" ? "wss" : "ws";
  const socket = new WebSocket(`${protocol}://${window.location.host}/ws`);
  socket.addEventListener("message", (event) => render(JSON.parse(event.data)));
  socket.addEventListener("close", () => {
    state.socket = null;
    if (state.auth?.authenticated) setTimeout(openSocket, 2000);
  });
  state.socket = socket;
}

bindControls();
loadInitial();
window.addEventListener("resize", () => {
  if (state.latest) drawEquity(state.latest.equity_curve || []);
});
