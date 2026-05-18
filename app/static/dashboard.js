const state = {
  latest: null,
  config: null,
  auth: { authenticated: false, admin: false, admin_configured: false, user: null },
  account: null,
  credits: null,
  adminCredits: null,
  logs: [],
  users: [],
  socket: null,
  socketReconnectTimer: null,
  statusRefreshInFlight: false,
  quoteFilter: "",
  activeMarket: "IN",
  activeSettingsTab: "broker",
  pageFilters: {
    suggestions: "all",
    decisions: "all",
    sentiment: "all",
    orders: "all",
  },
};

const MARKET_LABELS = {
  IN: "India",
  US: "US",
};

const SETTINGS_TABS = ["broker", "markets", "runtime", "ai", "risk", "users", "calendar", "advanced"];

const SETTINGS_TAB_CATEGORIES = {
  broker: new Set(["Live Protection"]),
  markets: new Set(["Market Data"]),
  runtime: new Set(["Runtime", "Agent Cycle"]),
  ai: new Set(["LLM Brain", "Sentiment"]),
  risk: new Set(["Risk"]),
  users: new Set(["Access Control", "User Credits"]),
  calendar: new Set(["Macro Calendar"]),
  advanced: new Set(["Global Intelligence", "Institutional Feeds"]),
};

const money = new Intl.NumberFormat("en-IN", {
  style: "currency",
  currency: "INR",
  minimumFractionDigits: 2,
  maximumFractionDigits: 6,
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

const usdPrice = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  minimumFractionDigits: 2,
  maximumFractionDigits: 6,
});

const creditsFmt = new Intl.NumberFormat("en-IN", {
  maximumFractionDigits: 4,
});

function byId(id) {
  return document.getElementById(id);
}

function fmtMoney(value) {
  return Number.isFinite(Number(value)) ? money.format(Number(value)) : "-";
}

function normalizeUiMarket(value) {
  return String(value || "IN").toUpperCase() === "US" ? "US" : "IN";
}

function activeMarketLabel() {
  return MARKET_LABELS[normalizeUiMarket(state.activeMarket)] || "India";
}

function rowMarket(row = {}) {
  const explicit = normalizeUiMarket(row.market_region || row.market || row.marketRegion);
  if (row.market_region || row.market || row.marketRegion) return explicit;
  const exchange = String(row.exchange || "").toUpperCase();
  if (["NASDAQ", "NYSE", "AMEX", "ARCA", "NYSEARCA", "BATS", "OTC"].includes(exchange)) return "US";
  if (String(row.source || "").toLowerCase().includes("yahoo")) return "US";
  return "IN";
}

function filterRowsByMarket(rows = [], market = state.activeMarket) {
  const region = normalizeUiMarket(market);
  return (rows || []).filter((row) => rowMarket(row) === region);
}

function payloadRowsForMarket(payload = {}, key, market = state.activeMarket) {
  const region = normalizeUiMarket(market);
  const byMarket = payload[`${key}_by_market`] || payload[`${key}ByMarket`];
  if (byMarket && Array.isArray(byMarket[region])) return byMarket[region];
  return filterRowsByMarket(payload[key] || [], region);
}

function scopedMarketContext(context = {}, market = state.activeMarket) {
  const region = normalizeUiMarket(market);
  const byMarket = context.by_market || context.byMarket;
  return byMarket?.[region] || context;
}

function fmtMarketMoney(value, market = "IN") {
  if (!Number.isFinite(Number(value))) return "-";
  return normalizeUiMarket(market) === "US" ? usdPrice.format(Number(value)) : money.format(Number(value));
}

function marketCurrencyLabel(market = state.activeMarket) {
  return normalizeUiMarket(market) === "US" ? "USD" : "INR";
}

function marketPortfolioFromPayload(payload = {}, market = "IN") {
  const region = normalizeUiMarket(market);
  const byMarket = payload.portfolio_by_market || payload.portfolioByMarket || payload.portfolio?.portfolio_by_market;
  if (byMarket && byMarket[region]) return byMarket[region];
  return portfolioMetricsForMarket(payload.portfolio || {}, filterRowsByMarket(payload.positions || [], region), region);
}

function portfolioMetricsForMarket(portfolio = {}, positions = [], market = "IN") {
  const region = normalizeUiMarket(market);
  const nested = portfolio.portfolio_by_market || portfolio.portfolioByMarket;
  if (nested && nested[region]) return nested[region];
  const invested = positions.reduce((sum, row) => sum + (Number(row.avg_price) || 0) * (Number(row.qty) || 0), 0);
  const marketValue = positions.reduce((sum, row) => sum + (Number(row.market_price) || 0) * (Number(row.qty) || 0), 0);
  const unrealized = positions.reduce(
    (sum, row) => sum + ((Number(row.market_price) || 0) - (Number(row.avg_price) || 0)) * (Number(row.qty) || 0),
    0,
  );
  const configuredCash = Number(currentSettings().initial_cash_inr || 0);
  const marketCash = Number(portfolio.cash_by_market?.[region] ?? portfolio.cashByMarket?.[region]);
  const totalCapital = Number.isFinite(marketCash)
    ? marketCash + invested
    : configuredCash > 0
      ? configuredCash
      : Number(portfolio.cash || 0) + Number(portfolio.invested || 0);
  const cash = Math.max(totalCapital - invested, 0);
  return {
    market_region: region,
    currency: region === "US" ? "USD" : "INR",
    cash,
    invested,
    market_value: marketValue,
    equity: cash + marketValue,
    unrealized_pnl: unrealized,
  };
}

function fmtNumber(value) {
  return Number.isFinite(Number(value)) ? number.format(Number(value)) : "-";
}

function firstFinite(...values) {
  for (const value of values) {
    const numberValue = Number(value);
    if (Number.isFinite(numberValue)) return numberValue;
  }
  return null;
}

function firstPositiveFinite(...values) {
  for (const value of values) {
    const numberValue = Number(value);
    if (Number.isFinite(numberValue) && numberValue > 0) return numberValue;
  }
  return null;
}

function fmtCompact(value) {
  return Number.isFinite(Number(value)) ? compactNumber.format(Number(value)) : "-";
}

function fmtUsd(value) {
  return Number.isFinite(Number(value)) ? usd.format(Number(value)) : "-";
}

function fmtCredits(value) {
  return Number.isFinite(Number(value)) ? creditsFmt.format(Number(value)) : "-";
}

function fmtPct(value) {
  return Number.isFinite(Number(value)) ? `${number.format(Number(value))}%` : "-";
}

function displayValue(value, fallback = "Not available") {
  const text = String(value ?? "").trim();
  if (!text || text === "-" || text === "--" || text.toUpperCase() === "DATA_MISSING") return fallback;
  return text;
}

function symbolInitials(value) {
  const text = String(value || "OS").replace(/[^a-z0-9]/gi, "").toUpperCase();
  return (text || "OS").slice(0, 2);
}

function fmtTime(value) {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function getNSESession(now = new Date()) {
  const ist = new Date(now.toLocaleString("en-US", { timeZone: "Asia/Kolkata" }));
  const day = ist.getDay();
  if (day === 0 || day === 6) return { state: "closed", label: "NSE Closed" };
  const mins = ist.getHours() * 60 + ist.getMinutes();
  if (mins >= 9 * 60 && mins < 9 * 60 + 15) return { state: "pre-open", label: "Pre-open" };
  if (mins >= 9 * 60 + 15 && mins < 15 * 60 + 30) return { state: "open", label: "NSE Open" };
  if (mins >= 15 * 60 + 30 && mins < 16 * 60) return { state: "post-close", label: "Post-close" };
  return { state: "closed", label: "NSE Closed" };
}

function updateSessionPill() {
  const pill = byId("session-pill");
  if (!pill) return;
  const session = getNSESession();
  pill.textContent = session.state === "closed" ? `${session.label} · Opens ${timeUntilNextNseOpen()}` : session.label;
  pill.className = `session-pill ${session.state}`;
}

function timeUntilNextNseOpen(now = new Date()) {
  const ist = new Date(now.toLocaleString("en-US", { timeZone: "Asia/Kolkata" }));
  const target = new Date(ist);
  target.setHours(9, 15, 0, 0);
  if (ist.getDay() === 6) target.setDate(target.getDate() + 2);
  else if (ist.getDay() === 0) target.setDate(target.getDate() + 1);
  else if (ist >= target) target.setDate(target.getDate() + (ist.getDay() === 5 ? 3 : 1));
  while (target.getDay() === 0 || target.getDay() === 6) target.setDate(target.getDate() + 1);
  const diff = Math.max(0, target.getTime() - ist.getTime());
  const hours = Math.floor(diff / 3600000);
  const minutes = Math.floor((diff % 3600000) / 60000);
  return `in ${hours}h ${minutes}m`;
}

function applyTheme(theme) {
  const next = theme === "dark" ? "dark" : "light";
  document.body.dataset.theme = next;
  try {
    window.localStorage.setItem("openstocks-theme", next);
  } catch {
    /* ignore storage failures */
  }
  const button = byId("theme-toggle-btn");
  if (button) {
    button.textContent = next === "dark" ? "☀" : "☾";
    button.setAttribute("aria-label", `Switch to ${next === "dark" ? "light" : "dark"} theme`);
  }
}

function initTheme() {
  let saved = "light";
  try {
    saved = window.localStorage.getItem("openstocks-theme") || "light";
  } catch {
    saved = "light";
  }
  applyTheme(saved);
}

function fmtDate(value) {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value).slice(0, 10);
  return date.toLocaleDateString("en-IN", { day: "2-digit", month: "short", year: "numeric" });
}

function fmtAge(seconds) {
  const value = Number(seconds);
  if (!Number.isFinite(value)) return "no ticks yet";
  if (value < 60) return `${Math.round(value)}s old`;
  if (value < 3600) return `${Math.round(value / 60)}m old`;
  return `${Math.round(value / 3600)}h old`;
}

function getUSSession(now = new Date()) {
  const ny = new Date(now.toLocaleString("en-US", { timeZone: "America/New_York" }));
  const day = ny.getDay();
  if (day === 0 || day === 6) return { state: "closed", label: "US Closed", venue: "US" };
  const mins = ny.getHours() * 60 + ny.getMinutes();
  if (mins >= 4 * 60 && mins < 9 * 60 + 30) return { state: "pre-open", label: "US Pre-market", venue: "US" };
  if (mins >= 9 * 60 + 30 && mins < 16 * 60) return { state: "open", label: "US Open", venue: "US" };
  if (mins >= 16 * 60 && mins < 20 * 60) return { state: "post-close", label: "US After-hours", venue: "US" };
  return { state: "closed", label: "US Closed", venue: "US" };
}

function marketSessionFor(market = state.activeMarket) {
  return normalizeUiMarket(market) === "US" ? getUSSession() : getNSESession();
}

function quoteDisplayMode(payload = state.latest || {}, market = state.activeMarket) {
  const region = normalizeUiMarket(market);
  const session = marketSessionFor(region);
  const health = payload.market_health || {};
  const mode = String(health.mode || "").toLowerCase();
  const quotes = filterRowsByMarket(payload.quotes || [], region);
  const age = Number(health.latest_quote_age_seconds);
  const hasQuotes = Boolean(quotes.length);
  const isOpen = session.state === "open" || Boolean(health.is_market_open);
  if (!hasQuotes) {
    return { label: "Waiting for prices", tone: "stopped", session, hasQuotes: false, age };
  }
  if (isOpen && mode !== "stale") {
    return { label: "Last traded", tone: Number.isFinite(age) && age > 600 ? "warning" : "running", session, hasQuotes: true, age };
  }
  if (mode === "stale") {
    return { label: "Stale prices", tone: "warning", session, hasQuotes: true, age };
  }
  return { label: "Last close", tone: "warning", session, hasQuotes: true, age };
}

function marketDataLabel(payload = state.latest || {}, market = state.activeMarket) {
  const display = quoteDisplayMode(payload, market);
  const region = normalizeUiMarket(market);
  const quotes = filterRowsByMarket(payload.quotes || [], region);
  const count = quotes.length;
  const age = fmtAge(display.age);
  const sessionLabel = display.session?.label || `${MARKET_LABELS[region]} closed`;
  return {
    ...display,
    count,
    title: display.hasQuotes ? display.label : "Connect feed",
    meta: display.hasQuotes
      ? `${count} ${MARKET_LABELS[region]} prices · last tick ${age} · ${sessionLabel}`
      : `${MARKET_LABELS[region]} prices are not connected yet`,
  };
}

function scoreToProductLabel(score) {
  const value = Number(score);
  if (!Number.isFinite(value)) return { label: "Not scored", tone: "neutral" };
  if (value >= 80) return { label: "Strong", tone: "positive" };
  if (value >= 65) return { label: "Healthy", tone: "positive" };
  if (value >= 50) return { label: "Watch", tone: "warning" };
  return { label: "Weak", tone: "negative" };
}

function marketStanceText(breadth = {}) {
  const regime = String(breadth.breadth_regime || "neutral");
  const labels = {
    bull_confirmed: "Broad Bull Market",
    bull_weakening: "Selective Bull Market",
    neutral: "Neutral",
    bear_warning: "Defensive Market",
    bear_confirmed: "Risk-Off Market",
  };
  return labels[regime] || humanLabel(regime);
}

function marketStanceHelp(breadth = {}) {
  const regime = String(breadth.breadth_regime || "neutral");
  const labels = {
    bull_confirmed: "Most stocks are participating. Strong long setups can use normal risk.",
    bull_weakening: "The market is still positive, but participation is narrowing. Take only the cleanest longs and avoid weak sectors.",
    neutral: "Participation is mixed. New trades need stronger stock-specific evidence.",
    bear_warning: "Participation is weak. Reduce size and avoid marginal long setups.",
    bear_confirmed: "Broad participation is bearish. Fresh long entries are blocked.",
  };
  return labels[regime] || "Breadth measures how many stocks are participating in the move.";
}

function actionableIdeaRows(rows = []) {
  return (rows || []).filter((row) => {
    const action = String(row.suggestion || row.action || row.signal_type || "").toUpperCase();
    const readiness = String(row.decision_readiness || "").toLowerCase();
    return ["BUY", "PAPER", "LIVE", "TRADE"].includes(action) || readiness.includes("trade");
  });
}

function pageFilter(group) {
  return state.pageFilters?.[group] || "all";
}

function confidencePercent(row = {}) {
  const values = [
    row.confidence,
    row.overall_score_pct,
    row.score_percent,
    row.score,
  ]
    .map(Number)
    .filter(Number.isFinite)
    .map((value) => (value > 0 && value <= 1 ? value * 100 : value));
  return values.length ? Math.max(...values) : 0;
}

function decisionScorePercent(row = {}) {
  const audit = decisionAudit(row);
  const score = audit.score_breakdown || {};
  const values = [
    row.confidence,
    row.overall_score_pct,
    row.score_percent,
    score.score_percent,
    row.score,
  ]
    .map(Number)
    .filter(Number.isFinite)
    .map((value) => (value > 0 && value <= 1 ? value * 100 : value));
  const combined = Number(score.combined ?? row.combined_score);
  if (Number.isFinite(combined)) values.push(combined >= -1 && combined <= 1 ? (combined + 1) * 50 : combined);
  return values.length ? Math.max(...values) : 0;
}

function sortDecisionRows(rows = []) {
  return (rows || []).slice().sort((a, b) => {
    const scoreDelta = decisionScorePercent(b) - decisionScorePercent(a);
    if (scoreDelta !== 0) return scoreDelta;
    const timeDelta = (rowTimestamp(b)?.getTime() || 0) - (rowTimestamp(a)?.getTime() || 0);
    if (timeDelta !== 0) return timeDelta;
    return String(a.symbol || "").localeCompare(String(b.symbol || ""));
  });
}

function rowActionText(row = {}) {
  return String(row.action || row.suggestion || row.signal_type || "").toUpperCase();
}

function rowTimestamp(row = {}) {
  const raw =
    row.ts ||
    row.last_seen_at ||
    row.first_seen_at ||
    row.created_at ||
    row.updated_at ||
    row.asof ||
    row.recommended_at;
  const date = raw ? new Date(raw) : null;
  return date && Number.isFinite(date.getTime()) ? date : null;
}

function isTodayRow(row = {}) {
  const date = rowTimestamp(row);
  if (!date) return false;
  const now = new Date();
  return date.getFullYear() === now.getFullYear() && date.getMonth() === now.getMonth() && date.getDate() === now.getDate();
}

function isThisWeekRow(row = {}) {
  const date = rowTimestamp(row);
  if (!date) return false;
  return Date.now() - date.getTime() <= 7 * 24 * 60 * 60 * 1000;
}

function latestCycleCutoff(rows = []) {
  const times = rows
    .map((row) => rowTimestamp(row)?.getTime())
    .filter(Number.isFinite);
  if (!times.length) return null;
  return Math.max(...times) - 20 * 60 * 1000;
}

function applySuggestionFilter(rows = []) {
  const filter = pageFilter("suggestions");
  if (filter === "all") return rows;
  return (rows || []).filter((row) => ideaMatchesFilter(row, filter));
}

function ideaMatchesFilter(row = {}, filter = pageFilter("suggestions")) {
  const action = rowActionText(row);
  if (filter === "buy") return action === "BUY" || String(row.decision_readiness || "").toLowerCase().includes("trade");
  if (filter === "high") return confidencePercent(row) >= 65 || Number(row.overall_score_pct || 0) >= 70;
  if (filter === "today") return isTodayRow(row);
  return true;
}

function applyStrategyPlanFilter(rows = []) {
  const filter = pageFilter("suggestions");
  if (filter === "all") return rows;
  const market = normalizeUiMarket(state.activeMarket);
  return (rows || []).filter((plan) => {
    const ideas = Array.isArray(plan.constituents) ? plan.constituents : [];
    return ideas.some((idea) => rowMarket(idea) === market && ideaMatchesFilter(idea, filter));
  });
}

function applyDecisionFilter(rows = []) {
  const filter = pageFilter("decisions");
  if (filter === "all") return rows;
  const cutoff = filter === "cycle" ? latestCycleCutoff(rows) : null;
  return (rows || []).filter((row) => {
    const action = rowActionText(row);
    if (filter === "buy") return action === "BUY";
    if (filter === "sell") return action === "SELL";
    if (filter === "hold") return action === "HOLD";
    if (filter === "exit") return action === "EXIT" || action === "SELL";
    if (filter === "high") return decisionScorePercent(row) >= 65;
    if (filter === "cycle") {
      const ts = rowTimestamp(row)?.getTime();
      return cutoff !== null && Number.isFinite(ts) && ts >= cutoff;
    }
    return true;
  });
}

function applySentimentFilter(rows = []) {
  const filter = pageFilter("sentiment");
  if (filter === "all") return rows;
  return (rows || []).filter((row) => {
    const score = Number(row.score);
    if (filter === "positive") return Number.isFinite(score) && score > 0.05;
    if (filter === "negative") return Number.isFinite(score) && score < -0.05;
    if (filter === "today") return isTodayRow(row);
    if (filter === "week") return isThisWeekRow(row);
    return true;
  });
}

function applyOrderFilter(rows = []) {
  const filter = pageFilter("orders");
  if (filter === "all") return rows;
  return (rows || []).filter((row) => {
    const status = String(row.status || "").toLowerCase();
    if (filter === "open") return ["open", "pending", "submitted", "working"].includes(status);
    if (filter === "filled") return status === "filled";
    if (filter === "rejected") return status === "rejected" || status === "vetoed" || status === "cancelled";
    return true;
  });
}

function filteredCountLabel(visible, total, singular, plural = `${singular}s`) {
  const label = total === 1 ? singular : plural;
  return visible === total ? `${total} ${label}` : `${visible}/${total} ${label}`;
}

function setPageFilter(group, value) {
  if (!group) return;
  state.pageFilters[group] = value || "all";
  updatePageFilterButtons(group);
  if (state.latest) render(state.latest);
}

function updatePageFilterButtons(group = "") {
  const selector = group ? `[data-filter-group="${CSS.escape(group)}"]` : "[data-filter-group]";
  document.querySelectorAll(selector).forEach((bar) => {
    const active = pageFilter(bar.dataset.filterGroup);
    bar.querySelectorAll("[data-filter-value]").forEach((button) => {
      button.classList.toggle("active", button.dataset.filterValue === active);
    });
  });
}

function pnlClass(value) {
  const numeric = Number(value);
  if (numeric > 0) return "positive";
  if (numeric < 0) return "negative";
  return "";
}

function flowBiasText(value) {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return "flow unknown";
  if (numeric >= 0.25) return `strong positive flow (${fmtNumber(numeric)})`;
  if (numeric > 0.05) return `mild positive flow (${fmtNumber(numeric)})`;
  if (numeric <= -0.25) return `strong negative flow (${fmtNumber(numeric)})`;
  if (numeric < -0.05) return `mild negative flow (${fmtNumber(numeric)})`;
  return `neutral flow (${fmtNumber(numeric)})`;
}

function humanLabel(value) {
  return String(value || "-")
    .replaceAll("_", " ")
    .replaceAll("-", " ")
    .replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function cssToken(value, fallback = "neutral") {
  const token = String(value || fallback)
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
  return token || fallback;
}

function compactSentence(text, fallback = "-") {
  const value = String(text || "").trim();
  if (!value) return fallback;
  return value.endsWith(".") || value.endsWith("!") ? value : `${value}.`;
}

function reasonFromSnakeCase(value, fallback = "-") {
  const text = String(value || "").trim();
  if (!text) return fallback;
  const mapped = {
    pre_filter_stage2_distribution: "Delivery data shows distribution, so OpenStocks is avoiding a fresh BUY.",
    market_breadth_bear_confirmed_no_new_longs: "The broader market is in a confirmed bearish breadth regime, so fresh BUY signals are blocked.",
    expiry_day_no_new_longs: "It is expiry day, so OpenStocks is waiting instead of opening a fresh long.",
    monthly_expiry_no_new_longs: "Monthly expiry risk is active, so OpenStocks is waiting for cleaner confirmation.",
    monthly_expiry_eve_no_new_longs: "Monthly expiry is close, so OpenStocks is reducing event risk and avoiding fresh longs.",
    earnings_lockout: "Earnings are too close, so OpenStocks is waiting for clarity.",
    extended_entry_no_new_longs: "The entry is extended from the ideal breakout zone, so OpenStocks is waiting for a better price.",
    false_breakout_two_day_rule_failed: "The breakout failed confirmation and closed back below resistance.",
    stage_analysis_not_stage2_markup: "The stock is not in a clean Stage 2 markup trend, so fresh BUY is blocked.",
    climax_top_detected_no_new_longs: "Price-volume action looks like a possible climax top, so OpenStocks is not buying.",
    timeframe_alignment_conflict: "Weekly, daily, and short-term trends are not aligned enough for a BUY.",
    options_max_pain_8pct_below_no_new_longs: "Options Max Pain is far below the current price, so upside risk/reward is weak for a new BUY.",
    risk_override_no_new_longs: "Risk overrides are active, so OpenStocks is not opening a new long.",
    portfolio_concentration_correlation_too_high: "The portfolio already has too much correlated exposure, so this BUY is blocked.",
    bottom_quartile_distribution: "The sector is weak and in distribution, so the stock needs exceptional confirmation before buying.",
    llm_primary_failed_safe_hold: "The LLM did not return a clean answer in time, so OpenStocks forced HOLD and did not trade.",
    llm_buy_blocked_by_system_rules: "The LLM wanted to buy, but hard system rules blocked the trade.",
    llm_primary_unavailable_no_trade: "Primary LLM approval is required, but the LLM is unavailable, so OpenStocks forced HOLD.",
    llm_primary_required_no_unreviewed_trade: "The symbol was not reviewed by the primary LLM in this cycle, so OpenStocks forced HOLD.",
    llm_failed_or_timed_out_deterministic_trade_preserved: "The LLM did not return a clean answer in time, so OpenStocks forced HOLD and did not trade.",
    llm_not_selected_due_candidate_limit_deterministic_action_allowed: "The symbol was outside the current LLM review limit; new trades now require LLM approval, so this should be HOLD.",
    time_stop_no_progress_15_sessions: "The position has not moved enough after 15 sessions, so OpenStocks is exiting dead capital.",
    overall_score_below_55_no_new_longs: "The production-readiness score is below 55%, so OpenStocks is not opening a fresh long.",
    fundamentals_unknown_needs_news_or_delivery_confirmation: "Fundamentals are still unknown, news/sentiment is missing, and delivery accumulation is not confirmed.",
    watch_entry_needs_exceptional_confirmation: "The setup is only WATCH grade, so it needs exceptional confirmation before a BUY.",
    delivery_distribution_no_new_longs: "Delivery data shows distribution, so OpenStocks is avoiding a fresh BUY.",
  };
  if (mapped[text]) return mapped[text];
  return compactSentence(humanLabel(text).toLowerCase());
}

function gateValueText(gateName, value, market = "IN") {
  if (value === null || value === undefined || value === "") return "";
  if (typeof value !== "object") return String(value);
  if (gateName === "stage_gate") {
    return `price ${fmtMarketMoney(value.price, market)}, 30-period SMA ${fmtMarketMoney(value.sma30, market)}, slope ${fmtNumber(value.slope)}`;
  }
  if (gateName === "sector_gate" || gateName === "sector_rotation_gate") {
    return `${value.sector_tier || "sector tier unknown"} · ${value.sector_stage || "stage unknown"}`;
  }
  if (gateName === "fundamental_confirmation_gate") {
    const sentiment = Number(value.sentiment_confidence || 0);
    const delivery = value.delivery_fingerprint ? "delivery accumulation confirmed" : "no delivery fingerprint";
    const alignment = value.alignment || "unknown";
    return `fundamentals unknown · sentiment/news confidence ${fmtNumber(sentiment)} · ${delivery} · alignment ${alignment}`;
  }
  if (gateName === "delivery_distribution_gate") {
    const bias = value.bias || value.net_bias || "unknown";
    const source = value.source || "delivery data";
    return `${bias} bias from ${source} · score ${fmtNumber(value.delivery_score)}`;
  }
  if (gateName === "options_max_pain_gate") {
    return `Max Pain ${fmtMarketMoney(value.max_pain, market)}, distance ${fmtPct(value.max_pain_distance_pct)}, source ${value.source || "-"}`;
  }
  if (gateName === "macro_calendar_gate" || gateName === "earnings_gate") {
    const event = value.recommended_action || value.expiry_type || value.event_type || value.type || "event risk";
    const risk = value.event_risk_score !== undefined ? ` · risk ${fmtNumber(value.event_risk_score)}` : "";
    return `${event}${risk}`;
  }
  if (gateName === "breakout_gate" || gateName === "breakout_quality_gate") {
    return `${value.breakout_quality || "breakout quality unknown"} · two-day failed ${Boolean(value.two_day_rule_failed)}`;
  }
  if (gateName === "divergence_gate" || gateName === "climax_volume_gate") {
    return `divergence ${fmtNumber(value.divergence_score)} · climax top ${Boolean(value.climax_volume_top)}`;
  }
  if (gateName === "portfolio_correlation_gate") {
    const symbols = (value.correlated_positions || []).map((item) => item.symbol).filter(Boolean);
    return symbols.length ? `correlated with ${symbols.join(", ")}` : shortValue(value, 120);
  }
  if (gateName === "overall_quality_gate") {
    return `overall score ${fmtPct(value.overall_score_pct || 0)} · grade ${value.overall_grade || "-"}`;
  }
  if (gateName === "system_rule_gates" || gateName.startsWith("system_rule_")) {
    const blocks = Array.isArray(value) ? value : value ? [value] : [];
    return blocks
      .map((block) => `${block.flag || "hard block"}: ${block.reason || "-"}`)
      .join("; ");
  }
  return shortValue(value, 120);
}

function humanizeGateFailure(gate, market = "IN") {
  const gateName = String(gate?.gate || gate?.name || "gate");
  const reason = String(gate?.reason || "");
  const value = gateValueText(gateName, gate?.value, market);
  const messages = {
    stage_gate: "Trend gate is weak or bearish",
    delivery_gate: "Delivery data points to distribution",
    breadth_gate: "Market breadth does not support new longs",
    earnings_gate: "Event or earnings risk is too close",
    macro_calendar_gate: "Macro calendar risk is elevated",
    sector_gate: "Sector rotation is not supportive",
    sector_rotation_gate: "Sector rotation is not supportive",
    entry_grade_gate: "Entry grade is too extended",
    breakout_gate: "Breakout confirmation failed",
    breakout_quality_gate: "Breakout confirmation failed",
    stage_buy_permitted: "Stage analysis does not allow a fresh BUY",
    divergence_gate: "Price-volume divergence is warning against a BUY",
    climax_volume_gate: "Climax-volume risk is warning against a BUY",
    alignment_gate: "Timeframes are not aligned",
    timeframe_alignment_gate: "Timeframes are not aligned",
    options_max_pain_gate: "Options Max Pain is against a fresh BUY",
    risk_overrides: "Risk overrides blocked new longs",
    portfolio_correlation_gate: "Portfolio correlation risk is too high",
    overall_quality_gate: "Overall production-readiness score is too low",
    fundamental_confirmation_gate: "Fundamental confirmation is missing",
    delivery_distribution_gate: "Delivery bias is not supportive",
    system_rule_gates: "System trading rules blocked the action",
    pre_filter: "Pre-filter blocked the setup",
  };
  const base = messages[gateName] || (gateName.startsWith("system_rule_") ? "System trading rule failed" : humanLabel(gateName));
  const reasonText = reason ? reasonFromSnakeCase(reason) : "";
  const parts = [base];
  if (value && value !== "-") parts.push(value);
  if (reasonText && !parts.join(" ").toLowerCase().includes(reasonText.toLowerCase().replace(/\.$/, ""))) {
    parts.push(reasonText);
  }
  return compactSentence(parts.filter(Boolean).join(": "));
}

function failedGatesFromAudit(audit = {}, context = {}) {
  const gateContext = audit.risk_gates?.decision_gate_context || context.decision_gate_context || {};
  const failed = gateContext.failed_gates || [];
  const evaluated = gateContext.evaluated_gates || [];
  const explicit = Array.isArray(failed) ? failed : [];
  const evaluatedFails = Array.isArray(evaluated) ? evaluated.filter((gate) => gate && gate.passed === false) : [];
  return [...explicit, ...evaluatedFails].filter((gate, index, list) => {
    const key = `${gate?.gate || gate?.name}-${JSON.stringify(gate?.value ?? "")}`;
    return list.findIndex((item) => `${item?.gate || item?.name}-${JSON.stringify(item?.value ?? "")}` === key) === index;
  });
}

function deterministicReasonFromText(text, action = "HOLD") {
  const technical = text.match(/technical=([-0-9.]+)\s*\(([^)]*)\)/i);
  const confluence = text.match(/confluence=([^,]+)/i);
  const combined = text.match(/combined=([-0-9.]+)/i);
  const sentiment = text.match(/sentiment=([-0-9.]+)/i);
  const global = text.match(/global=([-0-9.]+)\s*\(([^)]*)\)/i);
  const rank = text.match(/universe_rank=([^,\s]+)/i);
  const gateMatch = text.match(/failed_gates=\[([^\]]*)\]/i);
  const gates = gateMatch
    ? gateMatch[1]
        .split(",")
        .map((item) => item.replaceAll("'", "").replaceAll('"', "").trim())
        .filter(Boolean)
        .map((gate) => humanizeGateFailure({ gate }))
    : [];
  const actionText = String(action || "HOLD").toUpperCase();
  const lead =
    actionText === "BUY"
      ? "OpenStocks found a BUY setup that passed the main score and risk checks"
      : actionText === "SELL"
        ? "OpenStocks found exit pressure on an existing position"
        : "OpenStocks held because the setup did not pass every BUY requirement";
  const facts = [];
  if (combined) facts.push(`combined score ${fmtNumber(combined[1])}`);
  if (confluence) facts.push(`confluence ${confluence[1].trim()}`);
  if (technical) facts.push(`technical trend ${technical[2]} (${fmtNumber(technical[1])})`);
  if (sentiment) facts.push(`sentiment ${fmtNumber(sentiment[1])}`);
  if (global) facts.push(`global risk ${fmtNumber(global[1])} (${global[2]})`);
  if (rank) facts.push(`universe rank ${rank[1]}`);
  const blocker = gates.length ? ` Main blocker: ${gates.slice(0, 2).join(" ")}` : "";
  return compactSentence(`${lead}. ${facts.length ? facts.join(", ") : "No score details were available"}.${blocker}`);
}

function humanizeReasonText(text, action = "HOLD") {
  const value = String(text || "").trim();
  if (!value || value === "-") return "Decision narrative is still being built from the next scan.";
  if (/tools\s+technical=/i.test(value)) return deterministicReasonFromText(value, action);
  if (/^[a-z0-9_]+$/i.test(value)) return reasonFromSnakeCase(value);
  if (/Fundamental Confirmation Gate:\s*\{/i.test(value)) {
    const jsonMatch = value.match(/Fundamental Confirmation Gate:\s*(\{.*?\})(?::|$)/i);
    const gateValue = jsonMatch ? parseJsonObject(jsonMatch[1]) : {};
    return humanizeGateFailure({
      gate: "fundamental_confirmation_gate",
      value: gateValue,
      reason: "fundamentals_unknown_needs_news_or_delivery_confirmation",
    });
  }
  return value
    .replace(/llm_primary_required_no_unreviewed_trade/g, "LLM approval was required before trading, so OpenStocks held")
    .replace(/llm_primary_failed_safe_hold/g, "LLM failed, so OpenStocks held safely")
    .replace(/llm_primary_unavailable_no_trade/g, "LLM was unavailable, so OpenStocks held safely")
    .replace(/llm_failed_deterministic_action_preserved/g, "LLM failed, so OpenStocks held safely")
    .replace(/failed_gates=\[([^\]]*)\]/gi, (_, gates) => {
      const readable = gates
        .split(",")
        .map((item) => item.replaceAll("'", "").replaceAll('"', "").trim())
        .filter(Boolean)
        .map((gate) => humanLabel(gate).toLowerCase())
        .join(", ");
      return readable ? `blocked by ${readable}` : "";
    });
}

function decisionAudit(row = {}) {
  if (row.details && typeof row.details === "object") return row.details;
  return parseJsonObject(row.details_json);
}

function decisionFullSpectrum(audit = {}) {
  return audit.context?.full_spectrum_analysis || audit.decision?.details?.context?.full_spectrum_analysis || {};
}

function readableDecisionReason(row = {}) {
  const audit = decisionAudit(row);
  const context = audit.context || {};
  const action = String(audit.final_action || row.action || row.suggestion || "HOLD").toUpperCase();
  const failed = failedGatesFromAudit(audit, context);
  const pre = audit.pre_filter || context.pre_filter || {};
  const score = audit.score_breakdown || {};
  const full = decisionFullSpectrum(audit);
  const confluence = full.confluence_score || {};
  const scorecard = full.institutional_scorecard || {};
  const threshold = audit.risk_gates?.decision_gate_context?.buy_threshold || pre.buy_threshold || 0.35;
  const combinedValue = score.combined ?? row.combined_score;
  const confluenceValue = confluence.total ?? row.confluence;
  const confluenceTier = confluence.tier || row.tier || "tier pending";
  if (failed.length) {
    const market = rowMarket(row);
    return `No fresh BUY: ${failed.slice(0, 2).map((gate) => humanizeGateFailure(gate, market)).join(" ")}`;
  }
  if (pre.elimination_reason) {
    return reasonFromSnakeCase(pre.elimination_reason);
  }
  if (audit.llm_error) {
    return `LLM did not return a usable decision, so OpenStocks used the safe ${action} result.`;
  }
  if (action === "BUY") {
    return `BUY candidate: combined score ${fmtNumber(combinedValue)} vs ${fmtNumber(threshold)} required, confluence ${confluenceValue ?? "-"}/26 (${confluenceTier}), and institutional readiness is ${scorecard.buy_ready ? "clear" : "being monitored"}.`;
  }
  if (action === "SELL") {
    return humanizeReasonText(audit.action_reason || row.reason || "Exit rule triggered.", action);
  }
  if (combinedValue !== undefined || confluenceValue !== undefined || scorecard.buy_ready !== undefined) {
    const blockers = [];
    if (combinedValue !== undefined) blockers.push(`combined score ${fmtNumber(combinedValue)} vs BUY threshold ${fmtNumber(threshold)}`);
    if (confluenceValue !== undefined) blockers.push(`confluence ${confluenceValue}/26`);
    if (scorecard.buy_ready === false) blockers.push(`institutional scorecard not buy-ready`);
    return compactSentence(`HOLD because the setup is not strong enough yet: ${blockers.join(", ") || "BUY requirements were not met"}`);
  }
  return humanizeReasonText(audit.action_reason || row.reason || "-", action);
}

function decisionReasonHighlights(row = {}) {
  const audit = decisionAudit(row);
  const context = audit.context || {};
  const full = decisionFullSpectrum(audit);
  const gateHighlights = failedGatesFromAudit(audit, context).slice(0, 6).map(humanizeGateFailure);
  if (gateHighlights.length) return gateHighlights;
  const highlights = [];
  const score = audit.score_breakdown || {};
  const confluence = full.confluence_score || {};
  const scorecard = full.institutional_scorecard || {};
  const stage = full.stage_analysis || {};
  const entry = full.entry_quality || {};
  const alignment = (full.trend_context || {}).timeframe_alignment || {};
  const combinedValue = score.combined ?? row.combined_score;
  const confluenceValue = confluence.total ?? row.confluence;
  if (combinedValue !== undefined) highlights.push(`Combined score: ${fmtNumber(combinedValue)}`);
  if (confluenceValue !== undefined) highlights.push(`Confluence: ${confluenceValue}/26 (${confluence.tier || row.tier || "tier pending"})`);
  if (scorecard.total_score !== undefined) {
    highlights.push(`Institutional scorecard: ${scorecard.total_score}/100, ${scorecard.buy_ready ? "buy-ready" : "not buy-ready"}`);
  }
  if (stage.stage) highlights.push(`Stage analysis: ${stage.stage} (${stage.buy_permitted ? "BUY permitted" : "BUY blocked"})`);
  if (entry.entry_grade) highlights.push(`Entry quality: grade ${entry.entry_grade}, ${fmtPct(entry.distance_from_pivot_pct)} from pivot`);
  if (alignment.alignment_grade) highlights.push(`Timeframe alignment: grade ${alignment.alignment_grade}`);
  return highlights.length ? highlights : [readableDecisionReason(row)];
}

function readableOrderReason(row = {}) {
  const raw = String(row.reason || "").trim();
  const stop = raw.match(/risk exit: price ([0-9.]+) <= stop ([0-9.]+)/i);
  const market = rowMarket(row);
  if (stop) return `Sold for risk control: price ${fmtMarketMoney(stop[1], market)} reached the stop level ${fmtMarketMoney(stop[2], market)}.`;
  const tier2 = raw.match(/profit tier2: price ([0-9.]+) >= target2 ([0-9.]+)/i);
  if (tier2) return `Booked profit at Target 2: price ${fmtMarketMoney(tier2[1], market)} reached ${fmtMarketMoney(tier2[2], market)}.`;
  const tier1 = raw.match(/profit tier1: price ([0-9.]+) >= target1 ([0-9.]+)/i);
  if (tier1) return `Booked partial profit at Target 1: price ${fmtMarketMoney(tier1[1], market)} reached ${fmtMarketMoney(tier1[2], market)}, and the stop should tighten toward break-even.`;
  return humanizeReasonText(raw, row.side);
}

function render(payload) {
  if (!payload.user_signal_session && !state.auth?.admin && state.latest?.user_signal_session) {
    payload = { ...payload, user_signal_session: state.latest.user_signal_session };
  }
  state.latest = payload;
  const activeMarket = normalizeUiMarket(state.activeMarket);
  const userSession = payload.user_signal_session || {};
  const controlRunning = state.auth?.admin ? Boolean(payload.running) : Boolean(userSession.running);
  const portfolio = payload.portfolio || {};
  const allPositions = payload.positions || [];
  const allQuotes = payload.quotes || [];
  const allDecisions = payload.decisions || [];
  const allOrders = payload.orders || [];
  const allSentiment = payload.sentiment || [];
  const positions = filterRowsByMarket(allPositions, activeMarket);
  const quotes = filterRowsByMarket(allQuotes, activeMarket);
  const decisions = sortDecisionRows(payloadRowsForMarket(payload, "decisions", activeMarket));
  const suggestions = payloadRowsForMarket(payload, "suggestions", activeMarket);
  const trackedIdeas = payloadRowsForMarket(payload, "tracked_ideas", activeMarket);
  const orders = filterRowsByMarket(allOrders, activeMarket);
  const strategies = payload.strategy_metrics || [];
  const strategyPlans = payload.strategy_plans || [];
  const sentiment = filterRowsByMarket(allSentiment, activeMarket);
  const visibleDecisions = applyDecisionFilter(decisions);
  const visibleSuggestions = applySuggestionFilter(suggestions);
  const visibleTrackedIdeas = applySuggestionFilter(trackedIdeas);
  const visibleOrders = applyOrderFilter(orders);
  const visibleSentiment = applySentimentFilter(sentiment);
  const visibleStrategyPlans = applyStrategyPlanFilter(strategyPlans);

  const scopedPortfolio = marketPortfolioFromPayload(payload, activeMarket);
  const unrealizedPct = Number(scopedPortfolio.invested) > 0
    ? (Number(scopedPortfolio.unrealized_pnl || 0) / Number(scopedPortfolio.invested)) * 100
    : 0;
  byId("kpi-equity").textContent = fmtMarketMoney(scopedPortfolio.equity, activeMarket);
  byId("kpi-cash").textContent = fmtMarketMoney(scopedPortfolio.cash, activeMarket);
  byId("kpi-unrealized").textContent = fmtMarketMoney(scopedPortfolio.unrealized_pnl, activeMarket);
  byId("kpi-unrealized").className = pnlClass(scopedPortfolio.unrealized_pnl);
  const equityDelta = byId("kpi-equity-delta");
  if (equityDelta) equityDelta.textContent = `${fmtMarketMoney(scopedPortfolio.unrealized_pnl, activeMarket)} today`;
  const unrealizedPctEl = byId("kpi-unrealized-pct");
  if (unrealizedPctEl) {
    unrealizedPctEl.textContent = fmtPct(unrealizedPct);
    unrealizedPctEl.className = pnlClass(unrealizedPct);
  }
  byId("kpi-positions").textContent = String(positions.length);
  const kpiOrders = byId("kpi-orders");
  if (kpiOrders) kpiOrders.textContent = String(orders.length);
  const currency = byId("kpi-currency");
  if (currency) currency.textContent = marketCurrencyLabel(activeMarket);
  byId("last-cycle").textContent = state.auth?.admin
    ? (payload.last_cycle_at ? `Last cycle ${fmtTime(payload.last_cycle_at)}` : "waiting")
    : userSession.shared_backend
      ? (payload.last_cycle_at ? `Shared backend ${fmtTime(payload.last_cycle_at)}` : "shared backend waiting")
      : (userSession.last_cycle_at ? `Your signal cycle ${fmtTime(userSession.last_cycle_at)}` : "signals waiting");

  const pill = byId("status-pill");
  pill.textContent = controlRunning ? "Agent running" : "Agent idle";
  pill.className = `pill ${controlRunning ? "running" : "stopped"}`;

  const error = byId("error-box");
  const displayError = state.auth?.admin ? payload.last_error : userSession.last_error;
  if (displayError) {
    error.hidden = false;
    const feedPending = isFeedPending(payload);
    error.className = `error-box ${feedPending ? "warning" : ""}`;
    error.textContent = feedPending
      ? "Market data connection pending. Connect/refresh the selected market feed when ready; the terminal remains available for account, settings, and audit review."
      : displayError;
  } else {
    error.hidden = true;
    error.textContent = "";
    error.className = "error-box";
  }

  byId("position-count").textContent = `${positions.length}/${allPositions.length} open`;
  byId("quote-count").textContent = `${quotes.length}/${allQuotes.length} quotes`;
  byId("account-quote-count").textContent = `${quotes.length} ${activeMarketLabel()} quotes`;
  byId("decision-count").textContent = `${activeMarketLabel()} · ${filteredCountLabel(visibleDecisions.length, decisions.length, "decision")}`;
  byId("overview-decision-count").textContent = `${activeMarketLabel()} · ${decisions.length} decisions`;
  byId("suggestion-count").textContent = visibleSuggestions.length
    ? `${filteredCountLabel(visibleSuggestions.length, suggestions.length, "full-audit idea", "full-audit ideas")}`
    : suggestions.length
      ? `0/${suggestions.length} ideas`
      : "0 ideas";
  byId("order-count").textContent = `${filteredCountLabel(visibleOrders.length, orders.length, "order")}`;
  byId("strategy-count").textContent = `${strategies.length} strategies`;
  const planCount = byId("strategy-plan-count");
  if (planCount) planCount.textContent = `${filteredCountLabel(visibleStrategyPlans.length, strategyPlans.length, "plan")}`;
  const trackedCount = byId("tracked-count");
  if (trackedCount) trackedCount.textContent = visibleTrackedIdeas.length ? `${filteredCountLabel(visibleTrackedIdeas.length, trackedIdeas.length, "active idea", "active ideas")}` : "0 active";
  byId("sentiment-count").textContent = `${filteredCountLabel(visibleSentiment.length, sentiment.length, "event")}`;
  byId("nav-positions-badge").textContent = String(positions.length);
  byId("nav-suggestions-badge").textContent = String(suggestions.length);
  byId("nav-decisions-badge").textContent = String(decisions.length);
  byId("nav-orders-badge").textContent = String(orders.length);
  byId("nav-sentiment-badge").textContent = String(sentiment.length);
  byId("nav-logs-badge").textContent = state.auth?.admin ? String(state.logs.length) : "admin";
  byId("nav-overview-badge").textContent = controlRunning ? "on" : "off";
  updateMarketWorkspaceLabels(payload);

  renderPositions(positions);
  renderStrategies(strategies);
  updatePageFilterButtons();
  renderStrategyPlans(visibleStrategyPlans);
  renderTrackedIdeas(visibleTrackedIdeas);
  renderSentiment(visibleSentiment);
  renderQuotes(quotes);
  renderMarketTape(quotes, activeMarket);
  renderProductActionPanel(payload, suggestions, trackedIdeas, positions, decisions, scopedPortfolio);
  renderProductTrackingPanel(trackedIdeas, positions, suggestions);
  renderSuggestions(visibleSuggestions);
  renderDecisions(visibleDecisions, { controlRunning });
  renderOverviewDecisions(decisions, { controlRunning });
  renderOverviewPositions(positions);
  renderOrders(visibleOrders);
  renderMarketBreadth(scopedMarketContext(payload.market_breadth || {}, activeMarket));
  renderSectorRotation(scopedMarketContext(payload.sector_rotation_context || {}, activeMarket));
  renderPerformance(payload.performance || {});
  renderMacroEvents(payload.upcoming_macro_events || []);
  renderAgentConsole(payload);
  renderSelfAudit(payload.self_audit || {});
  renderShell(payload);
}

function renderProductActionPanel(payload, suggestions, trackedIdeas, positions, decisions, portfolio) {
  const panel = byId("product-action-panel");
  if (!panel) return;
  const market = normalizeUiMarket(state.activeMarket);
  const breadth = scopedMarketContext(payload.market_breadth || {}, market);
  const feed = marketDataLabel(payload, market);
  const readyIdeas = actionableIdeaRows(suggestions);
  const reviewPositions = (positions || []).filter((row) => {
    const summary = row.position_summary || {};
    const action = String(summary.recommended_action || "").toUpperCase();
    const flags = summary.active_flags || [];
    return ["EXIT", "REVIEW", "TRAIL STOP"].includes(action) || flags.length;
  });
  const pnl = Number(portfolio.unrealized_pnl || 0);
  const pnlPct = Number(portfolio.invested) > 0 ? (pnl / Number(portfolio.invested)) * 100 : 0;
  const credits = state.credits || {};
  const creditText = state.auth?.admin
    ? "Admin view"
    : `${fmtCredits(credits.daily_credits_remaining ?? state.auth?.user?.credit_balance ?? 0)} credits left today`;
  const lastDecision = decisions?.[0];
  const lastReason = lastDecision ? readableDecisionReason(lastDecision) : "";
  let headline = "Waiting for the first scan";
  let note = "OpenStocks will publish a tracked idea only after price, trend, risk, news, and decision gates are clear.";
  let cta = { label: "Analyze Symbol", view: "analyze" };
  let tone = "neutral";
  if (reviewPositions.length) {
    headline = `${reviewPositions.length} position${reviewPositions.length === 1 ? "" : "s"} need review`;
    note = "Risk, stop, or target rules are asking for attention before adding fresh exposure.";
    cta = { label: "Open Positions", view: "positions" };
    tone = "warning";
  } else if (readyIdeas.length) {
    headline = `${readyIdeas.length} trade idea${readyIdeas.length === 1 ? "" : "s"} ready to review`;
    note = "These ideas cleared the main gates. Check entry, stop, targets, and expiry before following.";
    cta = { label: "Review Ideas", view: "suggestions" };
    tone = "positive";
  } else if (trackedIdeas.length) {
    headline = `${trackedIdeas.length} idea${trackedIdeas.length === 1 ? "" : "s"} being tracked`;
    note = "No fresh buy has cleared all gates yet. Continue tracking active ideas against targets and stops.";
    cta = { label: "Track Ideas", view: "suggestions" };
    tone = "open";
  } else if (decisions.length) {
    headline = "No fresh buys yet";
    note = lastReason || "The latest scan did not find a setup strong enough for a new trade.";
    cta = { label: "See Scan Log", view: "decisions" };
    tone = "neutral";
  }
  const stance = marketStanceText(breadth);
  const score = scoreToProductLabel(payload.self_audit?.overall_score_pct);
  panel.innerHTML = `
    <article class="product-action-card ${escapeHtml(tone)}">
      <div class="product-action-primary">
        <span class="product-eyebrow">${escapeHtml(MARKET_LABELS[market] || market)} workspace</span>
        <h3>${escapeHtml(headline)}</h3>
        <p>${escapeHtml(shortValue(note, 190))}</p>
        <div class="product-action-buttons">
          <button class="primary" type="button" data-view-jump="${escapeHtml(cta.view)}">${escapeHtml(cta.label)}</button>
          <button type="button" data-view-jump="analyze">Run Stock Check</button>
        </div>
      </div>
      <div class="product-action-metrics">
        <button type="button" data-view-jump="account">
          <span>Prices</span>
          <strong class="${escapeHtml(feed.tone)}">${escapeHtml(feed.title)}</strong>
          <small>${escapeHtml(feed.meta)}</small>
        </button>
        <button type="button" data-view-jump="positions">
          <span>Portfolio P&amp;L</span>
          <strong class="${pnlClass(pnl)}">${fmtMarketMoney(pnl, market)}</strong>
          <small>${Number(portfolio.invested) > 0 ? `${fmtPct(pnlPct)} on deployed capital` : "no deployed capital yet"}</small>
        </button>
        <button type="button" data-view-jump="decisions">
          <span>Market Stance</span>
          <strong class="${escapeHtml(cssToken(breadth.breadth_regime || "neutral"))}">${escapeHtml(stance)}</strong>
          <small>${Number(breadth.symbols_checked || 0) ? `${fmtPct(breadth.pct_above_50dma || 0)} above 50-DMA` : "Breadth history building"}</small>
        </button>
        <button type="button" data-view-jump="${state.auth?.admin ? "users" : "account"}">
          <span>${state.auth?.admin ? "Access" : "Credits"}</span>
          <strong>${escapeHtml(creditText)}</strong>
          <small>${escapeHtml(score.label)} trade-safety score</small>
        </button>
      </div>
    </article>
  `;
  panel.querySelectorAll("[data-view-jump]").forEach((button) => {
    button.addEventListener("click", () => setView(button.dataset.viewJump));
  });
}

function renderProductTrackingPanel(trackedIdeas = [], positions = [], suggestions = []) {
  const panel = byId("product-tracking-panel");
  const status = byId("product-track-status");
  if (!panel) return;
  const market = normalizeUiMarket(state.activeMarket);
  const rows = [
    ...positions.slice(0, 3).map((row) => ({ type: "position", row })),
    ...trackedIdeas.slice(0, 4).map((row) => ({ type: "tracked", row })),
    ...actionableIdeaRows(suggestions).slice(0, 3).map((row) => ({ type: "idea", row })),
  ].slice(0, 6);
  if (status) status.textContent = rows.length ? `${rows.length} active` : "no active tracks";
  if (!rows.length) {
    panel.innerHTML = emptyBlock(
      "No active ideas yet",
      "When a signal clears every gate, it will appear here with entry, stop, targets, expiry, and return from recommendation.",
      "Open Ideas",
      "suggestions",
    );
    return;
  }
  panel.innerHTML = rows.map((item, index) => {
    const row = item.row || {};
    const itemMarket = rowMarket(row) || market;
    const lifecycle = ideaLifecycle(row);
    if (item.type === "position") {
      const pnl = (Number(row.market_price) - Number(row.avg_price)) * Number(row.qty);
      const summary = row.position_summary || {};
      return `<article class="product-track-item" role="button" tabindex="0" data-track-index="${index}">
        <div>
          <span class="product-track-type">Position</span>
          <strong>${escapeHtml(displayValue(row.symbol, "Symbol"))}</strong>
          <small>${escapeHtml(summary.recommended_action || "HOLD")} · ${escapeHtml(shortValue(summary.reason || row.strategy || "Position is being monitored.", 90))}</small>
        </div>
        <div class="product-track-values">
          <strong class="${pnlClass(pnl)}">${fmtMarketMoney(pnl, itemMarket)}</strong>
          <small>${fmtNumber(row.qty)} qty · ${fmtMarketMoney(row.market_price, itemMarket)}</small>
        </div>
      </article>`;
    }
    const latest = Number(row.follow_latest_price || row.latest_price || row.price || 0);
    const entry = Number(row.follow_entry_price || row.entry_price || row.price || 0);
    const returnPct = Number(row.return_pct || row.current_return_pct || (entry ? ((latest - entry) / entry) * 100 : 0));
    return `<article class="product-track-item" role="button" tabindex="0" data-track-index="${index}">
      <div>
        <span class="product-track-type">${escapeHtml(item.type === "idea" ? "Ready idea" : "Tracked idea")}</span>
        <strong>${escapeHtml(displayValue(row.symbol, "Symbol"))}</strong>
        <small>${escapeHtml(lifecycle.label)} · ${escapeHtml(ideaTimelineText(row))}</small>
      </div>
      <div class="product-track-values">
        <strong class="${pnlClass(returnPct)}">${fmtPct(returnPct)}</strong>
        <small>${fmtMarketMoney(latest || row.price, itemMarket)} · ${escapeHtml(row.suggestion || row.strategy || "WATCH")}</small>
      </div>
    </article>`;
  }).join("");
  panel.querySelectorAll(".product-track-item").forEach((card) => {
    const item = rows[Number(card.dataset.trackIndex)];
    card.addEventListener("click", () => {
      const title = item.type === "position" ? "Position" : item.type === "idea" ? "Suggestion" : "Tracked Idea";
      showDetails(title, item.row);
    });
  });
}

function renderSelfAudit(audit = {}) {
  const panel = byId("self-audit-panel");
  if (!panel) return;
  const ok = audit.capital_pool_within_position_count_rule !== false && !Number(audit.price_mismatch_count || 0);
  byId("self-audit-status").textContent = audit.updated_at ? `${fmtPct(audit.overall_score_pct ?? 0)} · ${audit.overall_grade || (ok ? "clear" : "flags")}` : "pending";
  const items = [
    { label: "Trade Safety", value: `${fmtPct(audit.overall_score_pct ?? 0)}`, note: ok ? "rules clear" : "needs review" },
    { label: "Grade Violations", value: audit.grade_violation_count ?? 0, note: "WATCH/undefined entries" },
    { label: "Delivery Conflicts", value: audit.delivery_conflict_count ?? 0, note: "distribution vs long" },
    { label: "Price Mismatch", value: audit.price_mismatch_count ?? 0, note: ">1% source gap" },
    { label: "Earnings Calendar", value: audit.earnings_calendar_last_updated ? fmtDate(audit.earnings_calendar_last_updated) : "missing", note: "last updated" },
  ];
  panel.innerHTML = items
    .map((item) => `<button type="button" data-detail-type="self-audit"><span>${escapeHtml(item.label)}</span><strong>${escapeHtml(item.value)}</strong><small>${escapeHtml(item.note)}</small></button>`)
    .join("");
  for (const button of panel.querySelectorAll("[data-detail-type='self-audit']")) {
    button.addEventListener("click", () => showDetails("Self Audit", audit));
  }
}

function renderPerformance(performance) {
  const panel = byId("performance-panel");
  if (!panel) return;
  const orders = performance.orders || {};
  const pnl = performance.pnl || {};
  const positions = performance.positions || {};
  const learning = performance.post_trade_learning || {};
  const learningSummary = learning.summary || {};
  byId("performance-status").textContent = `${orders.filled || 0} fills · ${learningSummary.ideas_analyzed || 0} ideas`;
  const market = normalizeUiMarket(state.activeMarket);
  panel.innerHTML = `
    <button type="button" data-performance-detail="fills"><span>Filled</span><strong>${fmtNumber(orders.filled)}</strong><small>${fmtNumber(orders.vetoed)} vetoed</small></button>
    <button type="button" data-performance-detail="win"><span>Win Rate</span><strong>${fmtPct(Number(pnl.win_rate || 0) * 100)}</strong><small>${fmtNumber(positions.closed)} closed</small></button>
    <button type="button" data-performance-detail="realized"><span>Realized</span><strong class="${pnlClass(pnl.realized)}">${fmtMarketMoney(pnl.realized, market)}</strong><small>${fmtMarketMoney(pnl.expectancy_per_closed_trade, market)} expectancy</small></button>
    <button type="button" data-performance-detail="dd"><span>Max DD</span><strong class="${pnlClass(pnl.max_drawdown_pct)}">${fmtPct(pnl.max_drawdown_pct)}</strong><small>equity curve</small></button>
    <button type="button" data-performance-detail="quick-red"><span>Quick Red</span><strong class="${pnlClass(-Number(learningSummary.quick_red || 0))}">${fmtNumber(learningSummary.quick_red || 0)}</strong><small>${escapeHtml(learningSummary.evidence_quality || "thin")} evidence</small></button>
    <button type="button" data-performance-detail="t1"><span>T1 Hits</span><strong class="positive">${fmtNumber(learningSummary.hit_t1 || 0)}</strong><small>${fmtPct(Number(learningSummary.avg_mfe_pct || 0))} avg MFE</small></button>
  `;
  panel.querySelectorAll("button").forEach((button) => button.addEventListener("click", () => showDetails("Trade Scoreboard", performance)));
}

function renderMarketBreadth(breadth) {
  const panel = byId("market-breadth-panel");
  if (!panel) return;
  const regime = breadth.breadth_regime || "neutral";
  const checked = Number(breadth.symbols_checked || 0);
  const hasGap = Boolean(breadth.data_gap) || checked <= 0;
  byId("breadth-status").textContent = hasGap ? "building history" : marketStanceText(breadth);
  const pct50 = Number(breadth.pct_above_50dma || 0);
  const checkedLabel = checked ? `${checked} symbols` : "this market";
  const helpText = hasGap
    ? `History is still loading for ${checkedLabel}. New BUY calls stay conservative until enough candles are available.`
    : `${marketStanceHelp(breadth)} Checked across ${checkedLabel}.`;
  panel.innerHTML = `
    <div class="breadth-headline">
      <span class="pill regime ${escapeHtml(regime)}">${escapeHtml(hasGap ? "Waiting for candles" : marketStanceText(breadth))}</span>
      ${breadth.breadth_thrust ? `<strong class="breadth-thrust">BREADTH THRUST DETECTED</strong>` : ""}
      <small>${escapeHtml(helpText)}</small>
    </div>
    <div class="progress-row">
      <span>Stocks above 50-DMA</span>
      <div class="progress-track"><div style="width:${Math.max(0, Math.min(pct50, 100))}%"></div></div>
      <strong>${checked ? fmtPct(pct50) : "Awaiting data"}</strong>
    </div>
    <div class="mini-grid">
      <button type="button" data-breadth-detail="adr"><span>Advancers / Decliners</span><strong>${fmtNumber(breadth.advance_decline_ratio)}</strong></button>
      <button type="button" data-breadth-detail="highs"><span>New Highs</span><strong class="positive">${fmtNumber(breadth.new_highs_count)}</strong></button>
      <button type="button" data-breadth-detail="lows"><span>New Lows</span><strong class="negative">${fmtNumber(breadth.new_lows_count)}</strong></button>
      <button type="button" data-breadth-detail="mcclellan"><span>Breadth Pulse</span><strong class="${pnlClass(breadth.mcclellan_proxy)}">${fmtNumber(breadth.mcclellan_proxy)}</strong></button>
    </div>
  `;
  panel.querySelectorAll("button").forEach((button) => button.addEventListener("click", () => showDetails("Market Health", breadth)));
}

function renderSectorRotation(context) {
  const panel = byId("sector-rotation-panel");
  if (!panel) return;
  const top = context.leaderboard?.top || [];
  const bottom = context.leaderboard?.bottom || [];
  const seen = new Set();
  const sectors = [...top, ...bottom].filter((item) => {
    const key = String(item.sector || "").toLowerCase();
    if (!key || seen.has(key)) return false;
    seen.add(key);
    return true;
  }).slice(0, 9);
  byId("sector-status").textContent = sectors.length ? `${sectors.length} sectors` : "building";
  const tile = (sector) => {
    const rs = Number(sector.sector_vs_nifty_rs ?? sector.sector_return_5d ?? 0);
    const clamped = Math.max(-2, Math.min(2, rs));
    const heat = ((clamped + 2) / 4) * 100;
    const tone = pnlClass(rs) || "flat";
    return `<button class="sector-tile sector-${tone}" type="button" style="--heat:${heat}%">
      <span>${escapeHtml(sector.sector || "-")}</span>
      <strong class="${pnlClass(rs)}">${fmtPct(rs)}</strong>
      <small>${escapeHtml(`${humanLabel(sector.sector_stage || "neutral")} · rank ${sector.sector_rank || "-"}`)}</small>
    </button>`;
  };
  panel.innerHTML = sectors.length
    ? sectors.map(tile).join("")
    : `<div class="empty-state product-empty">
      <strong>Sector heatmap is building</strong>
      <span>Sector momentum appears after the next scan has enough quote and candle data.</span>
      <button type="button" data-view-jump="analyze">Analyze a stock</button>
    </div>`;
  [...panel.querySelectorAll(".sector-tile")].forEach((button, index) => {
    button.addEventListener("click", () => showDetails("Sector Rotation", sectors[index]));
  });
  panel.querySelectorAll("[data-view-jump]").forEach((button) => {
    button.addEventListener("click", () => setView(button.dataset.viewJump));
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
  const opportunity = payload.opportunity_scan || {};
  const runtime = payload.runtime || {};
  const userSession = payload.user_signal_session || {};
  const activeMarket = normalizeUiMarket(state.activeMarket);
  const activeQuotes = filterRowsByMarket(payload.quotes || [], activeMarket);
  const controlRunning = state.auth?.admin ? Boolean(payload.running) : Boolean(userSession.running);
  const provider = health.provider || payload.provider || runtime.market_data_provider || "-";
  const feed = marketDataLabel(payload, activeMarket);
  const llmProvider = plainSetting("llm_provider", runtime.llm_provider || "offline");
  const llmMode = plainSetting("llm_decision_mode", runtime.llm_decision_mode || "offline");
  const llmModel = llmProvider === "deepseek"
    ? plainSetting("deepseek_model", "deepseek-v4-pro")
    : llmProvider === "groq"
      ? plainSetting("groq_model", "qwen/qwen3-32b")
      : llmProvider === "assigned"
        ? "OpenStocks Brain"
      : "offline";
  const llmDisplay = llmProvider === "assigned" ? "OpenStocks Brain" : llmProvider;
  const llmUsage = payload.llm_usage?.today_utc || {};
  const llmActivity = userSession.last_llm_activity || {};
  const llmUsageText = llmUsage.calls
    ? `${fmtCompact(llmUsage.total_tokens)} tok · ${fmtUsd(llmUsage.cost_usd)} today`
    : `${llmModel || "model unset"}`;

  byId("top-provider").textContent = state.auth?.admin ? provider : feed.title;
  byId("top-llm").textContent = state.auth?.admin ? (llmProvider === "offline" ? "off" : llmModel) : "OpenStocks Brain";
  byId("top-execution").textContent = state.auth?.admin ? plainSetting("execution_mode", runtime.execution_mode || "-") : marketCurrencyLabel(activeMarket);

  const feedPending = isFeedPending(payload);
  const feedConnected = !feedPending && feed.hasQuotes;
  const paperBanner = byId("paper-mode-banner");
  if (paperBanner) {
    const executionMode = String(plainSetting("execution_mode", runtime.execution_mode || "paper")).toLowerCase();
    let dismissed = false;
    try {
      dismissed = window.sessionStorage.getItem("openstocks-paper-banner-dismissed") === "1";
    } catch {
      dismissed = false;
    }
    paperBanner.hidden = executionMode !== "paper" || dismissed;
  }
  byId("feed-pill").textContent = feedConnected ? feed.title : "Feed pending";
  byId("feed-pill").className = `pill ${feedConnected ? feed.tone : "stopped"}`;
  const modePill = byId("mode-pill");
  if (modePill) {
    const executionMode = String(plainSetting("execution_mode", runtime.execution_mode || "paper")).toLowerCase();
    const live = executionMode === "live" || executionMode === "live_trading";
    modePill.textContent = live ? "LIVE" : "PAPER";
    modePill.className = `mode-pill ${live ? "live" : "paper"}`;
  }
  byId("ops-feed").textContent = feedConnected ? feed.title : "Connect feed";
  byId("ops-feed-meta").textContent = feedPending
    ? "quotes paused until token/feed is ready"
    : feed.meta;
  byId("ops-llm").textContent = state.auth?.admin ? (llmProvider === "offline" ? "Offline" : llmDisplay) : "OpenStocks Brain";
  const userCreditMeta = state.credits
    ? `${fmtCredits(state.credits.credits_used_today || 0)} credits used today · ${fmtCredits(state.credits.daily_credits_remaining || 0)} available`
    : `${fmtCredits(llmActivity.credits_charged || 0)} credits last cycle`;
  byId("ops-llm-meta").textContent = !state.auth?.admin
    ? userCreditMeta
    : `${llmMode} · ${llmUsageText}`;
  byId("ops-risk").textContent = `${plainSetting("max_positions", "-")} slots`;
  byId("ops-risk-meta").textContent = `${fmtPct(Number(plainSetting("max_order_value_pct", 0)) * 100)} max order`;
  const rawSymbols = Number(opportunity.raw_symbols || 0);
  const selectedSymbols = Number(opportunity.selected_symbols || 0);
  const newsCandidates = Number(opportunity.positive_news_candidates || 0);
  byId("ops-opportunity").textContent = opportunity.enabled ? `${fmtNumber(selectedSymbols)} picked` : "Static";
  byId("ops-opportunity-meta").textContent = opportunity.enabled
    ? `${fmtNumber(rawSymbols)} raw · ${fmtNumber(newsCandidates)} news · ${(opportunity.top_candidates || []).slice(0, 3).map((item) => item.symbol).filter(Boolean).join(", ") || "building"}`
    : "dynamic scan off";
  byId("ops-macro").textContent = macro.regime || marketStanceText(breadth);
  const macroRiskText = Number.isFinite(Number(macro.risk_score)) ? `${fmtNumber(macro.risk_score)} risk` : "risk pending";
  byId("ops-macro-meta").textContent = `${macroRiskText} · ${marketStanceText(breadth)}`;
  byId("ops-cycle").textContent = controlRunning ? (state.auth?.admin ? "Running" : "Scanning") : "Paused";
  byId("ops-cycle-meta").textContent = state.auth?.admin
    ? (payload.last_cycle_at ? `${fmtTime(payload.last_cycle_at)} · ${plainSetting("agent_interval_seconds", "-")}s` : "manual run pending")
    : (userSession.shared_backend
      ? (payload.last_cycle_at ? `${fmtTime(payload.last_cycle_at)} · background scan` : "background scan waiting")
      : userSession.last_cycle_at
      ? `${fmtTime(userSession.last_cycle_at)} · ${fmtCredits(userSession.last_credit_charge || 0)} credits`
      : `${userSession.monitor_scope === "CUSTOM" ? `${fmtNumber(userSession.monitor_symbols_count || 0)} custom` : (userSession.symbols_per_cycle || plainSetting("universe_symbols_per_cycle", 30) || 30)} symbols per cycle`);
  const phase = String(state.auth?.admin ? payload.cycle?.phase || "" : userSession.phase || payload.cycle?.phase || "").toLowerCase();
  const scanBusy = controlRunning && phase && !["idle", "sleep", "shared_backend"].includes(phase);
  const runButton = byId("dashboard-run-btn");
  if (runButton) {
    runButton.disabled = scanBusy;
    runButton.textContent = scanBusy ? "Scanning..." : "Run Now";
    runButton.title = scanBusy ? "A scan is already running. Wait for it to finish before starting another." : "Start a fresh scan.";
  }
}

function isFeedPending(payload = state.latest || {}) {
  const provider = String(payload.market_health?.provider || payload.provider || payload.runtime?.market_data_provider || "");
  const error = String(payload.last_error || "");
  if (normalizeUiMarket(state.activeMarket) === "US" && filterRowsByMarket(payload.quotes || [], "US").length) return false;
  return provider.includes("upstox-not-connected")
    || provider.includes("indstocks-not-connected")
    || /(upstox|indstocks|marketdataerror|no quotes|access token)/i.test(error);
}

function updateMarketWorkspaceLabels(payload = state.latest || {}) {
  const market = normalizeUiMarket(state.activeMarket);
  const label = activeMarketLabel();
  const quotes = filterRowsByMarket(payload.quotes || [], market);
  const decisions = payloadRowsForMarket(payload, "decisions", market);
  const positions = filterRowsByMarket(payload.positions || [], market);
  for (const button of document.querySelectorAll(".market-workspace-tab")) {
    const active = button.dataset.marketWorkspace === market;
    button.classList.toggle("active", active);
    button.setAttribute("aria-selected", active ? "true" : "false");
  }
  const scope = byId("market-scope-label");
  if (scope) scope.textContent = `${label} workspace`;
  const currentView = currentViewName();
  const title = byId("view-title");
  const navLabel = document.querySelector(`.nav-item[data-view="${currentView}"] span:not(.nav-icon)`)?.textContent || "Dashboard";
  if (title) {
    title.textContent = ["overview", "suggestions", "analyze", "positions", "orders", "decisions", "sentiment"].includes(currentView)
      ? `${label} ${navLabel}`
      : navLabel;
  }
  const subtitle = byId("view-subtitle");
  if (subtitle) {
    subtitle.textContent = ["overview", "suggestions", "analyze", "positions", "orders", "decisions", "sentiment"].includes(currentView)
      ? `${label} market only · ${quotes.length} quotes · ${positions.length} positions · ${decisions.length} decisions`
      : "Admin controls · split-market routing · runtime settings";
  }
  const railLabel = byId("rail-market-label");
  if (railLabel) railLabel.textContent = `${label} universe`;
  const ideasTitle = byId("ideas-market-title");
  if (ideasTitle) ideasTitle.textContent = `${label} Ideas`;
  const trackedTitle = byId("tracked-market-title");
  if (trackedTitle) trackedTitle.textContent = `${label} Tracks`;
  const signalTitle = byId("signals-market-title");
  if (signalTitle) signalTitle.textContent = `${label} Signal History`;
}

function renderAgentConsole(payload) {
  const portfolio = payload.portfolio || {};
  const health = payload.market_health || {};
  const settings = currentSettings();
  const positions = filterRowsByMarket(payload.positions || [], state.activeMarket);
  const orders = payload.orders || [];
  const decisions = payload.decisions || [];
  const universe = payload.universe || {};
  const latestAction = decisions.find((row) => row.action && row.action !== "HOLD");
  const rows = [
    {
      label: "Feed mode",
      value: health.mode || "unknown",
      note: health.provider || payload.provider || "-",
      onClick: () => openSettingsTab("broker"),
    },
    {
      label: "Universe",
      value: `${universe.enabled ?? "-"} enabled`,
      note: `${universe.symbols_per_cycle || "all"} per cycle · ${universe.india_enabled ?? 0} IN · ${universe.us_enabled ?? 0} US`,
      onClick: () => setView("account"),
    },
    {
      label: "Exposure",
      value: fmtMarketMoney(marketPortfolioFromPayload(payload, state.activeMarket).invested, state.activeMarket),
      note: `${positions.length}/${settings.max_positions ?? "-"} positions`,
      onClick: () => setView("positions"),
    },
    {
      label: "Risk",
      value: fmtPct(Number(settings.daily_loss_limit_pct || 0) * 100),
      note: "daily loss limit",
      onClick: () => openSettingsTab("risk"),
    },
    {
      label: "Execution",
      value: settings.execution_mode || payload.runtime?.execution_mode || "-",
      note: settings.live_trading_enabled ? "live switch on" : "live switch off",
      onClick: () => openSettingsTab("broker"),
    },
    {
      label: "Latest action",
      value: latestAction ? `${latestAction.action} ${latestAction.symbol}` : "No trade action",
      note: `${orders.length} orders tracked`,
      onClick: () => (latestAction ? showDetails("Decision", latestAction) : setView("decisions")),
    },
  ];
  byId("agent-console").innerHTML = rows
    .map(
      (row) => `<button class="console-row" type="button">
        <span>${escapeHtml(row.label)}</span>
        <strong>${escapeHtml(row.value)}</strong>
        <small>${escapeHtml(row.note)}</small>
      </button>`,
    )
    .join("");
  [...byId("agent-console").querySelectorAll(".console-row")].forEach((button, index) => {
    button.addEventListener("click", rows[index].onClick);
  });
}

function renderAuth(auth) {
  state.auth = auth;
  const authenticated = Boolean(auth.authenticated);
  byId("login-screen").hidden = authenticated;
  byId("app-shell").classList.toggle("app-hidden", !authenticated);
  document.body.classList.toggle("is-admin", authenticated && Boolean(auth.admin));
  document.body.classList.toggle("is-user", authenticated && !auth.admin);
  const pill = byId("admin-pill");
  pill.textContent = auth.admin ? "admin" : "user";
  pill.className = `pill ${auth.admin ? "running" : "stopped"}`;
  pill.hidden = true;
  const currentUser = auth.user?.username || "signed in";
  byId("current-user-label").textContent = currentUser;
  byId("credit-pill").hidden = Boolean(auth.admin);
  byId("credit-pill").textContent = auth.user && !auth.admin ? `${fmtCredits(auth.user.credit_balance || 0)} credits` : "";
  byId("start-btn").textContent = auth.admin ? "Start" : "Refresh Signals";
  byId("stop-btn").textContent = auth.admin ? "Stop" : "Managed";
  byId("logout-btn").hidden = !authenticated;
  renderUserBrokerStatus();
  for (const item of document.querySelectorAll(".admin-only")) {
    item.hidden = !auth.admin;
  }
  if (authenticated && !auth.admin && ["logs", "users", "settings"].includes(currentViewName())) {
    setView("overview");
  }
  applyAccessMode();
  if (auth.admin) fetchLogs();
  else renderLogs([]);
  if (auth.admin) {
    fetchUsers();
    fetchAdminCredits();
  }
}

function handleUnauthorized(message = "Session expired. Sign in again.") {
  if (state.socketReconnectTimer) clearTimeout(state.socketReconnectTimer);
  state.socketReconnectTimer = null;
  if (state.socket) state.socket.close();
  state.socket = null;
  renderAuth({
    authenticated: false,
    admin: false,
    admin_configured: state.auth?.admin_configured,
    user: null,
  });
  const status = byId("login-status");
  if (status) {
    status.textContent = message;
    status.className = "settings-inline-status negative";
  }
}

function applyAccessMode() {
  const authenticated = Boolean(state.auth && state.auth.authenticated);
  const admin = Boolean(state.auth && state.auth.admin);
  for (const id of ["start-btn", "stop-btn"]) {
    const element = byId(id);
    if (element) element.disabled = !authenticated || (!admin && id === "stop-btn");
  }
  for (const id of [
    "run-btn",
    "save-settings-btn",
    "reset-demo-btn",
    "test-llm-btn",
    "refresh-logs-btn",
    "analyze-btn",
    "upstox-connect-btn",
  ]) {
    const element = byId(id);
    if (element) element.disabled = !admin;
  }
  const analyzeInput = byId("analyze-symbol");
  if (analyzeInput) analyzeInput.disabled = !authenticated || admin;
  for (const tab of document.querySelectorAll(".market-tab")) {
    tab.disabled = !authenticated || admin;
  }
  const analyzeButton = byId("analyze-btn");
  if (analyzeButton) analyzeButton.disabled = !authenticated || admin;
  const analyzeStatus = byId("analyze-status");
  if (analyzeStatus && !state.latest?.manual_analysis_active) {
    analyzeStatus.textContent = authenticated
      ? (admin ? "user-only signal tool" : "ready")
      : "login required";
  }
  const form = byId("settings-form");
  if (form) {
    for (const input of form.querySelectorAll("input, select")) {
      input.disabled = !admin;
    }
  }
  for (const id of [
    "upstox-access-token",
  ]) {
    const element = byId(id);
    if (element) element.disabled = !admin;
  }
  for (const id of [
    "my-upstox-access-token",
    "my-upstox-token-save-btn",
    "my-kite-api-key",
    "my-kite-access-token",
    "my-kite-connect-btn",
    "daily-credit-limit-input",
    "save-daily-credit-limit-btn",
    "signal-execution-mode",
    "save-signal-execution-mode-btn",
  ]) {
    const element = byId(id);
    if (element) element.disabled = !authenticated || admin;
  }
  byId("settings-status").textContent = admin
    ? "admin controls unlocked"
    : authenticated
      ? "user mode: admin required for settings"
      : "login required";
}

async function fetchCredits() {
  if (!state.auth?.authenticated) {
    renderCreditSummary(null);
    return;
  }
  try {
    const response = await fetch("/api/me/credits");
    const payload = await response.json().catch(() => ({ detail: `HTTP ${response.status}` }));
    if (!response.ok) {
      byId("credits-status").textContent = payload.detail || "credits unavailable";
      return;
    }
    renderCreditSummary(payload.credits || null, payload.usage_policy || {});
  } catch (error) {
    byId("credits-status").textContent = "credits unavailable";
  }
}

function renderCreditSummary(credits, policy = {}) {
  state.credits = credits;
  const body = byId("credits-body");
  if (!body) return;
  if (!state.auth?.authenticated) {
    byId("credits-status").textContent = "login required";
    body.innerHTML = `<div class="empty-state">Login to view credit usage.</div>`;
    renderCreditPopoverBody(null, policy);
    return;
  }
  if (state.auth?.admin) {
    byId("credits-status").textContent = "admin view";
    body.innerHTML = `<div class="account-note"><strong>Admin mode</strong><span>Admins allocate credits and monitor usage from the Users tab. Signals run from user accounts only.</span></div>`;
    byId("credit-pill").hidden = true;
    renderCreditPopoverBody(null, policy);
    return;
  }
  const balance = Number(credits?.credit_balance || 0);
  const dailyLimit = Number(credits?.daily_credit_limit || 0);
  const usedToday = Number(credits?.credits_used_today || 0);
  const remaining = Number(credits?.daily_credits_remaining || 0);
  const llmActivity = state.latest?.user_signal_session?.last_llm_activity || {};
  const autoTrade = state.latest?.user_signal_session?.auto_trade || {};
  byId("credits-status").textContent = `${fmtCredits(remaining)} left today`;
  byId("credit-pill").hidden = false;
  byId("credit-pill").textContent = `⚡ ${fmtCredits(balance)} credits`;
  renderCreditPopoverBody(credits, policy);
  body.innerHTML = `
    <div class="account-metrics">
      <div><span>Balance</span><strong>${fmtCredits(balance)}</strong></div>
      <div><span>Daily Budget</span><strong>${dailyLimit > 0 ? fmtCredits(dailyLimit) : "No cap"}</strong></div>
      <div><span>Used Today</span><strong>${fmtCredits(usedToday)}</strong></div>
      <div><span>Available Today</span><strong>${fmtCredits(remaining)}</strong></div>
    </div>
    <form id="daily-credit-limit-form" class="symbol-search">
      <input id="daily-credit-limit-input" type="number" min="0" step="1" value="${dailyLimit || ""}" placeholder="Daily credits to spend" />
      <button id="save-daily-credit-limit-btn" type="submit">Save Budget</button>
    </form>
    <div class="account-note">
      <strong>Credit transparency</strong>
      <span>Every completed brain review deducts credits from the daily budget and account balance.</span>
      <span>OpenStocks automatically uses a leaner analysis path when today's remaining credits are tight.</span>
      <span>Estimated signal cost: ${fmtCredits(policy.estimated_signal_credit || 0)} credits.</span>
      ${llmActivity.message ? `<span>Last cycle: ${escapeHtml(llmActivity.message)}${llmActivity.latest_failure ? ` ${escapeHtml(llmActivity.latest_failure)}` : ""}</span>` : ""}
      ${autoTrade.mode ? `<span>Auto action: ${escapeHtml(signalModeLabel(autoTrade.mode))} · followed ${fmtNumber(autoTrade.followed || 0)} BUY ideas${(autoTrade.skipped || []).length ? ` · skipped ${(autoTrade.skipped || []).length}` : ""}</span>` : ""}
    </div>
    <div class="table-wrap compact credit-ledger">
      <table>
        <thead><tr><th>Time</th><th>Type</th><th>Credits</th><th>Balance</th><th>Description</th></tr></thead>
        <tbody>${(credits?.ledger || []).slice(0, 12).map((row) => `<tr>
          <td>${fmtTime(row.ts)}</td>
          <td>${escapeHtml(humanLabel(row.entry_type))}</td>
          <td class="num ${pnlClass(row.amount)}">${fmtCredits(row.amount)}</td>
          <td class="num">${fmtCredits(row.balance_after)}</td>
          <td>${escapeHtml(row.description || "-")}</td>
        </tr>`).join("") || `<tr><td colspan="5">No credit activity yet</td></tr>`}</tbody>
      </table>
    </div>
  `;
  byId("daily-credit-limit-form").addEventListener("submit", saveDailyCreditLimit);
  applyAccessMode();
}

function renderCreditPopoverBody(credits, policy = {}) {
  const body = byId("credit-popover-body");
  if (!body) return;
  if (!credits || state.auth?.admin) {
    body.innerHTML = `<div class="empty-state product-empty"><strong>No user credit ledger</strong><span>User credit usage appears here after sign in.</span></div>`;
    return;
  }
  const balance = Number(credits.credit_balance || 0);
  const dailyLimit = Number(credits.daily_credit_limit || 0);
  const usedToday = Number(credits.credits_used_today || 0);
  const remaining = Number(credits.daily_credits_remaining || 0);
  const pct = dailyLimit > 0 ? Math.max(0, Math.min(100, (usedToday / dailyLimit) * 100)) : 0;
  const ledger = credits.ledger || [];
  body.innerHTML = `
    <div class="credit-popover-summary">
      <div><span>Balance</span><strong>${fmtCredits(balance)}</strong></div>
      <div><span>Used today</span><strong>${fmtCredits(usedToday)}</strong></div>
      <div><span>Remaining today</span><strong>${dailyLimit > 0 ? fmtCredits(remaining) : "No cap"}</strong></div>
    </div>
    <div class="progress-row credit-progress">
      <span>Daily budget</span>
      <div class="progress-track"><div style="width:${pct}%"></div></div>
      <strong>${fmtPct(pct)}</strong>
    </div>
    <div class="credit-popover-ledger">
      ${(ledger || []).slice(0, 5).map((row) => `<button type="button">
        <span>${escapeHtml(humanLabel(row.entry_type || "usage"))}</span>
        <strong class="${pnlClass(row.amount)}">${fmtCredits(row.amount)}</strong>
        <small>${escapeHtml(row.description || "OpenStocks Brain review")} · ${fmtTime(row.ts)}</small>
      </button>`).join("") || `<div class="empty-state product-empty"><strong>No LLM calls yet</strong><span>Last five credit events will appear here.</span></div>`}
    </div>
    <small class="credit-policy-note">${fmtCredits(policy.estimated_signal_credit || 0)} estimated credits per signal review.</small>
  `;
}

async function saveDailyCreditLimit(event) {
  event.preventDefault();
  const value = Number(byId("daily-credit-limit-input")?.value || 0);
  byId("credits-status").textContent = "saving";
  try {
    const response = await fetch("/api/me/credits/daily-limit", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ daily_credit_limit: value }),
    });
    const payload = await response.json().catch(() => ({ detail: `HTTP ${response.status}` }));
    if (!response.ok) {
      byId("credits-status").textContent = payload.detail || "save failed";
      return;
    }
    renderCreditSummary(payload.credits || null);
  } catch (error) {
    byId("credits-status").textContent = "save failed";
  }
}

async function fetchAdminCredits() {
  if (!state.auth?.admin) {
    renderAdminCredits(null);
    return;
  }
  try {
    const response = await fetch("/api/admin/credits");
    const payload = await response.json().catch(() => ({ detail: `HTTP ${response.status}` }));
    if (!response.ok) {
      byId("admin-credits-status").textContent = payload.detail || "usage unavailable";
      return;
    }
    renderAdminCredits(payload);
  } catch (error) {
    byId("admin-credits-status").textContent = "usage unavailable";
  }
}

function renderAdminCredits(summary) {
  state.adminCredits = summary;
  const body = byId("admin-credits-body");
  if (!body) return;
  if (!summary) {
    body.innerHTML = `<div class="empty-state">Admin credit usage will appear here.</div>`;
    return;
  }
  const users = summary.users || [];
  const policy = summary.credit_policy || {};
  const totals = users.reduce(
    (acc, user) => {
      acc.today += Number(user.today_credits_used || 0);
      acc.margin += Number(user.today_platform_margin || 0);
      acc.all += Number(user.all_time_credits_used || 0);
      return acc;
    },
    { today: 0, margin: 0, all: 0 },
  );
  byId("admin-credits-status").textContent = `${users.length} users`;
  body.innerHTML = `
    <button type="button"><span>Users</span><strong>${users.length}</strong></button>
    <button type="button"><span>Credits Today</span><strong>${fmtCredits(totals.today)}</strong></button>
    <button type="button"><span>Platform Margin Today</span><strong>${fmtCredits(totals.margin)}</strong></button>
    <button type="button"><span>All-Time Usage</span><strong>${fmtCredits(totals.all)}</strong></button>
    <button type="button"><span>Credit Rule</span><strong>${fmtCredits(policy.tokens_per_credit || 10)} tokens</strong></button>
  `;
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
    body.innerHTML = emptyTableRow(6, "Admin logs are protected", "Sign in as admin to inspect backend cycle, feed, LLM, and execution logs.");
    return;
  }
  byId("logs-count").textContent = `${state.logs.length} logs`;
  byId("nav-logs-badge").textContent = String(state.logs.length);
  if (!state.logs.length) {
    body.innerHTML = emptyTableRow(6, "No logs yet", "Cycle, feed, LLM, and order events will appear here once the backend starts.");
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
        <td class="reason">${escapeHtml(shortValue(prettyAgentDetails(details), 140))}</td>
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
    body.innerHTML = emptyTableRow(8, "Admin access required", "Only admins can create users, allocate credits, and assign broker feeds.");
    return;
  }
  if (!state.users.length) {
    body.innerHTML = emptyTableRow(8, "No users yet", "Create the first user to run signals with separate credits, cash, and broker feed settings.");
    return;
  }
  body.innerHTML = state.users
    .map((user) => {
      const active = Boolean(user.active);
      const credits = user.credit_usage || {};
      const upstox = user.broker_accounts?.upstox || {};
      const sharedUpstox = state.account?.upstox || {};
      const kite = user.broker_accounts?.kite || {};
      const assigned = user.assigned_llm || {};
      const signalMode = String(user.signal_execution_mode || "SIGNAL_ONLY").toUpperCase();
      const upstoxEffective = Boolean(upstox.connected || sharedUpstox.connected);
      const upstoxLabel = upstox.connected
        ? (upstox.scope === "user" ? "personal" : "shared data")
        : sharedUpstox.connected
          ? "shared data"
          : "off";
      const kiteLabel = kite.connected ? (kite.scope === "user" ? "personal" : "saved") : "off";
      const brokerSubLabel = upstoxEffective && upstox.scope !== "user"
        ? `analytics only${kite.connected ? ` · Kite ${kiteLabel}` : ""}`
        : `Kite ${kiteLabel}`;
      return `<tr data-user-id="${user.id}">
        <td><strong>${escapeHtml(user.username)}</strong></td>
        <td><span class="source ${user.role === "admin" ? "live" : ""}">${escapeHtml(user.role)}</span><br><small>${escapeHtml(assigned.provider || "default")} · ${escapeHtml(assigned.model || "default")}</small></td>
        <td><strong>${fmtCredits(user.credit_balance || credits.credit_balance || 0)}</strong><br><small>daily ${fmtCredits(user.daily_credit_limit || credits.daily_credit_limit || 0)}</small></td>
        <td><strong>${fmtCredits(credits.credits_used_today || 0)}</strong><br><small>left ${fmtCredits(credits.daily_credits_remaining || 0)}</small></td>
        <td><span class="tag ${signalModeClass(signalMode)}">${escapeHtml(signalModeLabel(signalMode))}</span></td>
        <td><span class="tag ${upstoxEffective ? "open" : "watch"}">Upstox ${upstoxLabel}</span><br><small>${escapeHtml(brokerSubLabel)}</small></td>
        <td><span class="tag ${active ? "open" : "sell"}">${active ? "active" : "disabled"}</span></td>
        <td class="row-actions">
          <button type="button" data-user-action="toggle">${active ? "Disable" : "Enable"}</button>
          <button type="button" data-user-action="role" title="${user.role === "admin" ? "Change to user role" : "Change to admin role"}">Role</button>
          <button type="button" data-user-action="model">Model</button>
          <button type="button" data-user-action="signal-mode">Mode</button>
          <button type="button" data-user-action="credits">Credits</button>
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
      } else if (button.dataset.userAction === "role") {
        updateUser(user.id, { role: user.role === "admin" ? "user" : "admin" });
      } else if (button.dataset.userAction === "model") {
        openModelAssign(user);
      } else if (button.dataset.userAction === "signal-mode") {
        openSignalModeAssign(user);
      } else if (button.dataset.userAction === "credits") {
        openCreditAdjust(user);
      }
    });
  });
  bindRowDetails(body, state.users, "User");
}

function signalModeLabel(mode) {
  const normalized = String(mode || "SIGNAL_ONLY").toUpperCase();
  if (normalized === "AUTO_PAPER") return "Auto paper";
  if (normalized === "AUTO_LIVE") return "Auto live";
  return "Signal only";
}

function signalModeClass(mode) {
  const normalized = String(mode || "SIGNAL_ONLY").toUpperCase();
  if (normalized === "AUTO_PAPER") return "open";
  if (normalized === "AUTO_LIVE") return "sell";
  return "watch";
}

function openModelAssign(user) {
  const current = user.assigned_llm || {};
  const raw = window.prompt(
    `Assign LLM for ${user.username}\nUse provider:model`,
    `${current.provider || "groq"}:${current.model || "qwen/qwen3-32b"}`,
  );
  if (raw === null) return;
  const [providerRaw, ...modelParts] = raw.split(":");
  const provider = (providerRaw || "").trim().toLowerCase();
  const model = modelParts.join(":").trim();
  if (!["groq", "deepseek", "offline"].includes(provider)) {
    showDetails("Assign Model", { detail: "Provider must be groq, deepseek, or offline." });
    return;
  }
  updateUser(user.id, { assigned_llm_provider: provider, assigned_llm_model: model || "offline" });
}

function openSignalModeAssign(user) {
  const current = String(user.signal_execution_mode || "SIGNAL_ONLY").toUpperCase();
  const raw = window.prompt(
    `Signal execution mode for ${user.username}\nUse SIGNAL_ONLY, AUTO_PAPER, or AUTO_LIVE`,
    current,
  );
  if (raw === null) return;
  updateUser(user.id, { signal_execution_mode: raw });
}

function openCreditAdjust(user) {
  const amountRaw = window.prompt(`Credits to add/remove for ${user.username}`, "100000");
  if (amountRaw === null) return;
  const amount = Number(amountRaw);
  if (!Number.isFinite(amount) || amount === 0) return;
  adjustUserCredits(user.id, amount);
}

async function adjustUserCredits(userId, amount) {
  try {
    const response = await fetch(`/api/users/${userId}/credits`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ amount, description: "Admin credit allocation" }),
    });
    const payload = await response.json().catch(() => ({ detail: `HTTP ${response.status}` }));
    if (!response.ok) {
      showDetails("Credit Adjustment", payload);
      return;
    }
    renderUsers(payload.users || []);
    renderAdminCredits(payload.admin || null);
    fetchLogs();
  } catch (error) {
    showBackendError(networkErrorMessage(error, "credit adjustment"), { action: "credit adjustment" });
  }
}

async function createUser(event) {
  event.preventDefault();
  const status = byId("user-create-status");
  const payload = {
    username: byId("new-user-username").value.trim(),
    password: byId("new-user-password").value,
    role: byId("new-user-role").value,
    ...llmAssignmentFromSelect(byId("new-user-llm").value),
    signal_execution_mode: byId("new-user-signal-mode")?.value || "SIGNAL_ONLY",
    active: true,
    starting_credits: Number(byId("new-user-credits").value || 0),
    daily_credit_limit: Number(byId("new-user-daily-limit").value || 0),
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
    byId("new-user-llm").value = "groq:qwen/qwen3-32b";
    if (byId("new-user-signal-mode")) byId("new-user-signal-mode").value = "SIGNAL_ONLY";
    byId("new-user-credits").value = "";
    byId("new-user-daily-limit").value = "";
    renderUsers(data.users || []);
    fetchAdminCredits();
    status.textContent = "user created";
    status.className = "settings-inline-status positive";
    fetchLogs();
  } catch (error) {
    status.textContent = "create failed: backend unreachable";
    status.className = "settings-inline-status negative";
  }
}

function llmAssignmentFromSelect(value) {
  const [providerRaw, ...modelParts] = String(value || "groq:qwen/qwen3-32b").split(":");
  return {
    assigned_llm_provider: providerRaw || "groq",
    assigned_llm_model: modelParts.join(":") || "qwen/qwen3-32b",
  };
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
  const brokerSync = account.broker_sync || {};
  const brokerReconcile = brokerSync.reconciliation || {};
  const portfolio = paper.portfolio || {};
  const portfolioByMarket = paper.portfolio_by_market || portfolio.portfolio_by_market || {};
  const trackedIdeas = account.tracked_ideas || [];
  const userUpstox = state.auth?.user?.broker_accounts?.upstox || {};
  const userKite = state.auth?.user?.broker_accounts?.kite || {};
  const indiaPaper = portfolioByMarket.IN || portfolioMetricsForMarket(portfolio, filterRowsByMarket(paper.positions || [], "IN"), "IN");
  const usPaper = portfolioByMarket.US || portfolioMetricsForMarket(portfolio, filterRowsByMarket(paper.positions || [], "US"), "US");
  const cashPool = paper.cash_pool_by_market || state.auth?.user?.paper_cash_by_market || {};
  const indiaCashPool = Number(cashPool.IN ?? (Number(indiaPaper.cash || 0) + Number(indiaPaper.invested || 0)));
  const usCashPool = Number(cashPool.US ?? (Number(usPaper.cash || 0) + Number(usPaper.invested || 0)));
  const monitorSymbols = account.monitor_symbols || state.auth?.user?.monitor_symbols || [];
  const cashEditor = state.auth?.admin ? "" : `
    <form id="paper-cash-form" class="paper-cash-form">
      <label>
        <span>India Capital</span>
        <input id="paper-cash-in" type="number" min="0" step="0.01" value="${Number.isFinite(indiaCashPool) ? indiaCashPool : ""}" />
      </label>
      <label>
        <span>US Capital</span>
        <input id="paper-cash-us" type="number" min="0" step="0.01" value="${Number.isFinite(usCashPool) ? usCashPool : ""}" />
      </label>
      <button id="save-paper-cash-btn" type="submit">Save Cash</button>
      <small id="paper-cash-status">Starting paper cash per market; active paper ideas reduce available cash.</small>
    </form>
  `;
  const signalExecutionMode = String(account.signal_execution_mode || state.auth?.user?.signal_execution_mode || "SIGNAL_ONLY").toUpperCase();
  const signalModeEditor = state.auth?.admin ? "" : `
    <form id="signal-mode-form" class="paper-cash-form">
      <label>
        <span>Signal Action</span>
        <select id="signal-execution-mode">
          <option value="SIGNAL_ONLY" ${signalExecutionMode === "SIGNAL_ONLY" ? "selected" : ""}>Signal only</option>
          <option value="AUTO_PAPER" ${signalExecutionMode === "AUTO_PAPER" ? "selected" : ""}>Auto paper</option>
          <option value="AUTO_LIVE" ${signalExecutionMode === "AUTO_LIVE" ? "selected" : ""}>Auto live guarded</option>
        </select>
      </label>
      <button id="save-signal-execution-mode-btn" type="submit">Save Mode</button>
      <small id="signal-mode-status">${escapeHtml(account.signal_execution_mode_message || "Signals are saved only until you enable auto paper or guarded live mode.")}</small>
    </form>
  `;
  const monitorEditor = state.auth?.admin ? "" : `
    <form id="monitor-symbols-form" class="paper-cash-form monitor-symbols-form">
      <label class="wide">
        <span>Monitor Only These Stocks</span>
        <textarea id="monitor-symbols-input" rows="3" placeholder="IDEA, RELIANCE, TCS">${escapeHtml((monitorSymbols || []).join(", "))}</textarea>
      </label>
      <button id="save-monitor-symbols-btn" type="submit">Save List</button>
      <button id="clear-monitor-symbols-btn" type="button">Use Dynamic Scan</button>
      <small id="monitor-symbols-status">${monitorSymbols.length ? `${fmtNumber(monitorSymbols.length)} custom symbol(s) active` : "Empty list uses the dynamic opportunity scan."}</small>
    </form>
  `;
  const userUpstoxPersonal = userUpstox.connected && userUpstox.scope === "user";
  const userKitePersonal = userKite.connected && userKite.scope === "user";
  const userFeedLabel = userUpstoxPersonal
    ? "Upstox connected"
    : userKitePersonal
      ? "Kite connected"
      : upstox.connected
        ? "shared Upstox analytics"
        : "not connected";
  byId("account-status").textContent = userFeedLabel;
  byId("account-body").innerHTML = `
    <div class="account-metrics">
      <div><span>Mode</span><strong>${paper.mode || "-"}</strong></div>
      <div><span>India Cash</span><strong>${fmtMarketMoney(indiaPaper.cash, "IN")}</strong></div>
      <div><span>India Equity</span><strong>${fmtMarketMoney(indiaPaper.equity, "IN")}</strong></div>
      <div><span>US Cash</span><strong>${fmtMarketMoney(usPaper.cash, "US")}</strong></div>
      <div><span>US Equity</span><strong>${fmtMarketMoney(usPaper.equity, "US")}</strong></div>
      <div><span>User Feed</span><strong>${userFeedLabel}</strong></div>
      <div><span>Signal Action</span><strong>${escapeHtml(signalModeLabel(signalExecutionMode))}</strong></div>
      <div><span>Monitor Scope</span><strong>${monitorSymbols.length ? `${fmtNumber(monitorSymbols.length)} custom` : "Dynamic"}</strong></div>
      <div><span>Broker Sync</span><strong>${escapeHtml(brokerSync.status_label || brokerSync.status || "Not Connected")}</strong></div>
      <div><span>Tracked Ideas</span><strong>${fmtNumber(trackedIdeas.length)}</strong></div>
      <div><span>Paper Positions</span><strong>${fmtNumber((paper.positions || []).length)}</strong></div>
      <div><span>Broker Positions</span><strong>${fmtNumber(brokerReconcile.broker_position_symbols || brokerSync.positions_count || 0)}</strong></div>
    </div>
    ${cashEditor}
    ${signalModeEditor}
    ${monitorEditor}
    <div class="account-note">
      <strong>${state.auth?.admin ? "Admin mode" : "User trading mode"}</strong>
      <span>${state.auth?.admin ? "Admins manage users, credits, and runtime broker connections. Signals are run from user accounts." : "Signals and symbol analysis consume this user's credits and use this user's broker feed when connected."}</span>
    </div>
    <div class="account-note">
      <strong>Broker sync</strong>
      <span>${escapeHtml(brokerSync.note || "Connect a personal broker token to reconcile live requests with broker positions.")}</span>
      ${(brokerReconcile.unmatched_live_requests || []).length ? `<span>${fmtNumber((brokerReconcile.unmatched_live_requests || []).length)} live request(s) are not matched to a broker position.</span>` : ""}
    </div>
  `;
  const cashForm = byId("paper-cash-form");
  if (cashForm) cashForm.addEventListener("submit", savePaperCash);
  const signalModeForm = byId("signal-mode-form");
  if (signalModeForm) signalModeForm.addEventListener("submit", saveSignalExecutionMode);
  const monitorForm = byId("monitor-symbols-form");
  if (monitorForm) monitorForm.addEventListener("submit", saveMonitorSymbols);
  const clearMonitorButton = byId("clear-monitor-symbols-btn");
  if (clearMonitorButton) clearMonitorButton.addEventListener("click", clearMonitorSymbols);
  renderUserBrokerStatus();
}

async function refreshAccountAndUsers() {
  try {
    const accountResponse = await fetch("/api/account");
    if (accountResponse.ok) {
      renderAccount(await accountResponse.json());
    }
    if (state.auth?.admin) {
      fetchUsers();
    }
  } catch {
    /* Keep the current screen usable; the regular status refresh will retry. */
  }
}

async function saveMonitorSymbols(event) {
  event.preventDefault();
  const status = byId("monitor-symbols-status");
  const symbols = byId("monitor-symbols-input")?.value || "";
  if (status) status.textContent = "saving";
  try {
    const response = await fetch("/api/me/monitor-symbols", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ symbols }),
    });
    const payload = await response.json().catch(() => ({ detail: `HTTP ${response.status}` }));
    if (!response.ok) {
      if (status) status.textContent = payload.detail || "save failed";
      return;
    }
    if (state.auth?.user) {
      state.auth.user.monitor_symbols = payload.monitor_symbols || [];
      state.auth.user.monitor_symbols_count = payload.monitor_symbols_count || 0;
      state.auth.user.monitor_scope = payload.monitor_scope;
    }
    const invalid = payload.invalid_symbols || [];
    if (status) status.textContent = invalid.length
      ? `saved ${fmtNumber(payload.monitor_symbols_count || 0)} · ignored ${invalid.slice(0, 4).join(", ")}`
      : payload.message || "saved";
    await loadAuthenticatedData();
  } catch (error) {
    if (status) status.textContent = "save failed";
  }
}

async function clearMonitorSymbols() {
  const input = byId("monitor-symbols-input");
  if (input) input.value = "";
  await saveMonitorSymbols({ preventDefault() {} });
}

async function saveSignalExecutionMode(event) {
  event.preventDefault();
  const status = byId("signal-mode-status");
  const mode = byId("signal-execution-mode")?.value || "SIGNAL_ONLY";
  if (status) status.textContent = "saving";
  try {
    const response = await fetch("/api/me/signal-execution-mode", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ signal_execution_mode: mode }),
    });
    const payload = await response.json().catch(() => ({ detail: `HTTP ${response.status}` }));
    if (!response.ok) {
      if (status) status.textContent = payload.detail || "save failed";
      return;
    }
    if (state.auth?.user) state.auth.user.signal_execution_mode = payload.signal_execution_mode || mode;
    if (status) status.textContent = payload.message || "saved";
    await loadAuthenticatedData();
  } catch (error) {
    if (status) status.textContent = "save failed";
  }
}

async function savePaperCash(event) {
  event.preventDefault();
  const status = byId("paper-cash-status");
  const indiaCash = Number(byId("paper-cash-in")?.value || 0);
  const usCash = Number(byId("paper-cash-us")?.value || 0);
  if (!Number.isFinite(indiaCash) || !Number.isFinite(usCash) || indiaCash < 0 || usCash < 0) {
    if (status) status.textContent = "Enter valid non-negative cash amounts.";
    return;
  }
  if (status) status.textContent = "saving";
  try {
    const response = await fetch("/api/me/paper-cash", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ india_cash: indiaCash, us_cash: usCash }),
    });
    const payload = await response.json().catch(() => ({ detail: `HTTP ${response.status}` }));
    if (!response.ok) {
      if (status) status.textContent = payload.detail || "save failed";
      return;
    }
    state.account = { ...(state.account || {}), paper: payload.paper || state.account?.paper || {} };
    if (state.auth?.user) {
      state.auth.user.paper_cash_by_market = payload.paper_cash_by_market || state.auth.user.paper_cash_by_market;
    }
    if (status) status.textContent = "saved";
    await loadAuthenticatedData();
  } catch (error) {
    if (status) status.textContent = "save failed";
  }
}

function renderSentiment(rows) {
  const body = byId("sentiment-body");
  const mood = byId("market-mood-panel");
  const scoredRows = rows.filter((row) => Number(row.headline_count || 0) > 0 && Number.isFinite(Number(row.score)));
  const avg = scoredRows.length
    ? scoredRows.reduce((sum, row) => sum + Number(row.score || 0), 0) / scoredRows.length
    : 0;
  const moodScore = scoredRows.length ? Math.max(0, Math.min(100, 50 + avg * 50)) : 0;
  const moodLabel = !scoredRows.length
    ? "Awaiting news"
    : moodScore >= 80
      ? "Extreme Greed"
      : moodScore >= 62
        ? "Greed"
        : moodScore >= 40
          ? "Neutral"
          : moodScore >= 20
            ? "Fear"
            : "Extreme Fear";
  if (mood) {
    mood.innerHTML = `
      <div class="mood-gauge" style="--mood:${moodScore}">
        <strong>${scoredRows.length ? fmtNumber(moodScore) : "-"}</strong>
        <span>${escapeHtml(moodLabel)}</span>
      </div>
      <div>
        <h4>Market Mood Index</h4>
        <p>${scoredRows.length ? `${scoredRows.length} scored news events are contributing to this market mood.` : "No verified sentiment data is available yet for this market."}</p>
      </div>
    `;
  }
  if (!rows.length) {
    body.innerHTML = emptyBlock(
      `No ${activeMarketLabel()} sentiment events yet`,
      "Run symbol analysis or wait for the next cycle to attach verified headlines. Missing sentiment is marked as unavailable, never treated as neutral.",
      "Analyze Symbol",
      "analyze",
    );
    return;
  }
  body.innerHTML = rows
    .slice(0, 40)
    .map((row, index) => {
      let headlines = [];
      try {
        headlines = JSON.parse(row.headlines_json || "[]");
      } catch {
        headlines = [];
      }
      const hasNews = Number(row.headline_count || 0) > 0;
      const tone = hasNews ? (Number(row.score) > 0.1 ? "positive" : Number(row.score) < -0.1 ? "negative" : "watch") : "watch";
      const confidence = Math.max(0, Math.min(100, Number(row.confidence || 0) * 100));
      return `<article class="sentiment-card" role="button" tabindex="0" data-index="${index}">
        <div class="sentiment-card-head">
          <strong>${escapeHtml(displayValue(row.symbol, "Symbol"))}</strong>
          <span class="tag ${tone}">${hasNews ? fmtNumber(row.score) : "No news"}</span>
        </div>
        <div class="score-ring small" style="--score:${confidence}">
          <strong>${hasNews ? fmtNumber(confidence) : "-"}</strong>
          <small>%</small>
        </div>
        <p>${hasNews ? `${row.headline_count || 0} verified items analysed` : "No recent verified headlines from connected news feeds."}</p>
        <div class="sentiment-headlines">${headlines.length ? headlines.slice(0, 3).map((headline) => `<span>${escapeHtml(headline)}</span>`).join("") : `<span>No verified headlines yet</span>`}</div>
      </article>`;
    })
    .join("");
  [...body.querySelectorAll(".sentiment-card")].forEach((card) => {
    const row = rows[Number(card.dataset.index)];
    if (row) card.addEventListener("click", () => showDetails("Sentiment Event", row));
  });
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
  for (const tabName of SETTINGS_TABS) {
    const target = byId(`settings-fields-${tabName}`);
    if (target) target.innerHTML = "";
  }
  const groups = new Map();
  for (const item of config.schema) {
    const group = groups.get(item.category) || [];
    group.push(item);
    groups.set(item.category, group);
  }

  for (const [category, items] of groups.entries()) {
    const tabName = settingsTabForCategory(category);
    const target = byId(`settings-fields-${tabName}`);
    if (!target) continue;
    const fields = items.map((item) => renderField(item, config.settings[item.key])).join("");
    target.insertAdjacentHTML(
      "beforeend",
      `<section class="settings-group">
        <h3>${category}</h3>
        ${fields}
      </section>`,
    );
  }
  for (const tabName of SETTINGS_TABS) {
    const target = byId(`settings-fields-${tabName}`);
    if (target && !target.innerHTML.trim()) {
      target.innerHTML = `<div class="empty-state">No settings in this tab.</div>`;
    }
  }
  renderProviderKeysPanel(config.settings || {});
  renderUpstoxConnect(config.settings || {});
  const configuredRegion = String(config.settings?.market_region || "IN").toUpperCase();
  setActiveMarket(configuredRegion === "US" ? "US" : "IN", { rerender: false });
  setAnalyzeMarket(configuredRegion === "US" ? "US" : "IN");
  setSettingsTab(state.activeSettingsTab || "broker");
  applyAccessMode();
  renderShell();
}

function renderProviderKeysPanel(settings) {
  const deepseekSaved = Boolean(settings.deepseek_api_key?.saved);
  const groqSaved = Boolean(settings.groq_api_key?.saved);
  const deepseekState = byId("deepseek-key-state");
  const groqState = byId("groq-key-state");
  const status = byId("llm-keys-status");
  if (deepseekState) deepseekState.textContent = deepseekSaved ? "saved" : "not saved";
  if (groqState) groqState.textContent = groqSaved ? "saved" : "not saved";
  if (status) {
    status.textContent = `${deepseekSaved ? "DeepSeek saved" : "DeepSeek missing"} · ${groqSaved ? "Groq saved" : "Groq missing"}`;
    status.className = `settings-inline-status ${deepseekSaved || groqSaved ? "positive" : ""}`;
  }
  if (byId("admin-deepseek-key")) byId("admin-deepseek-key").placeholder = deepseekSaved ? "DeepSeek key saved" : "DeepSeek API Key";
  if (byId("admin-groq-key")) byId("admin-groq-key").placeholder = groqSaved ? "Groq key saved" : "Groq API Key";
  if (byId("admin-default-user-provider")) byId("admin-default-user-provider").value = plainSetting("user_default_llm_provider", "groq");
  if (byId("admin-default-user-model")) byId("admin-default-user-model").value = plainSetting("user_default_llm_model", "qwen/qwen3-32b");
  if (byId("admin-runtime-provider")) byId("admin-runtime-provider").value = plainSetting("llm_provider", "deepseek");
}

function settingsTabForCategory(category) {
  for (const [tabName, categories] of Object.entries(SETTINGS_TAB_CATEGORIES)) {
    if (categories.has(category)) return tabName;
  }
  return "ai";
}

function setSettingsTab(tabName) {
  const aliases = { access: "users", data: "advanced" };
  const next = aliases[tabName] || tabName || "broker";
  state.activeSettingsTab = next;
  for (const button of document.querySelectorAll(".settings-tab")) {
    button.classList.toggle("active", button.dataset.settingsTab === next);
  }
  for (const panel of document.querySelectorAll(".settings-tab-panel")) {
    panel.classList.toggle("active", panel.dataset.settingsPanel === next);
  }
}

function renderUpstoxConnect(settings) {
  const token = byId("upstox-access-token");
  const status = byId("upstox-connect-status");
  const saved = Boolean(settings.upstox_access_token?.saved);
  if (token && !token.value) token.placeholder = saved ? "Upstox token saved" : "Paste Upstox access token";
  if (status) {
    status.textContent = saved ? "connected" : "not connected";
    status.className = `settings-inline-status ${saved ? "positive" : ""}`;
  }
}

function renderUserBrokerStatus() {
  const user = state.auth?.user || {};
  const upstox = user.broker_accounts?.upstox || {};
  const kite = user.broker_accounts?.kite || {};
  const sharedUpstox = state.account?.upstox || {};
  const upstoxStatus = byId("my-upstox-status");
  const kiteStatus = byId("my-kite-status");
  const brokerStatus = byId("user-broker-status");
  const upstoxPersonal = upstox.connected && upstox.scope === "user";
  const upstoxShared = !upstoxPersonal && Boolean(upstox.connected || sharedUpstox.connected);
  const kitePersonal = kite.connected && kite.scope === "user";
  if (upstoxStatus) {
    upstoxStatus.textContent = upstoxPersonal ? "personal connected" : upstoxShared ? "using shared analytics" : "not connected";
    upstoxStatus.className = `settings-inline-status ${upstoxPersonal || upstoxShared ? "positive" : ""}`;
  }
  if (kiteStatus) {
    kiteStatus.textContent = kitePersonal ? "personal connected" : kite.api_key_saved ? "key saved" : "not connected";
    kiteStatus.className = `settings-inline-status ${kitePersonal ? "positive" : ""}`;
  }
  if (brokerStatus) {
    brokerStatus.textContent = upstoxPersonal
      ? "Upstox connected"
      : kitePersonal
        ? "Kite connected"
        : sharedUpstox.connected
          ? "shared Upstox analytics"
          : "connect broker feed";
  }
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
  mergeProviderKeyPanelSettings(values);
  return values;
}

function mergeProviderKeyPanelSettings(values) {
  const deepseekKey = byId("admin-deepseek-key")?.value?.trim();
  const groqKey = byId("admin-groq-key")?.value?.trim();
  const defaultProvider = byId("admin-default-user-provider")?.value;
  const defaultModel = byId("admin-default-user-model")?.value?.trim();
  const runtimeProvider = byId("admin-runtime-provider")?.value;
  if (deepseekKey) values.deepseek_api_key = deepseekKey;
  if (groqKey) values.groq_api_key = groqKey;
  if (defaultProvider) values.user_default_llm_provider = defaultProvider;
  if (defaultModel) values.user_default_llm_model = defaultModel;
  if (runtimeProvider) values.llm_provider = runtimeProvider;
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
    await refreshAccountAndUsers();
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
    access_token: byId("upstox-access-token")?.value?.trim() || byId("setting-upstox_access_token")?.value?.trim(),
    base_url: byId("setting-upstox_api_base_url")?.value?.trim() || "https://api.upstox.com/v2",
  };
}

function myUpstoxConnectPayload() {
  return {
    access_token: byId("my-upstox-access-token")?.value?.trim(),
    base_url: state.config?.settings?.upstox_api_base_url || "https://api.upstox.com/v2",
  };
}

async function saveMyUpstoxToken() {
  const status = byId("my-upstox-status");
  const button = byId("my-upstox-token-save-btn");
  const token = byId("my-upstox-access-token")?.value?.trim();
  if (!token) {
    if (status) {
      status.textContent = "paste token first";
      status.className = "settings-inline-status negative";
    }
    return;
  }
  if (status) {
    status.textContent = "saving token";
    status.className = "settings-inline-status";
  }
  if (button) button.disabled = true;
  try {
    const response = await fetch("/api/me/upstox/connect", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(myUpstoxConnectPayload()),
    });
    const payload = await response.json().catch(() => ({ detail: `HTTP ${response.status}` }));
    if (!response.ok || !payload.ok) {
      if (status) {
        status.textContent = payload.detail || "token save failed";
        status.className = "settings-inline-status negative";
      }
      showDetails("User Upstox Token", payload);
      return;
    }
    if (byId("my-upstox-access-token")) byId("my-upstox-access-token").value = "";
    state.auth.user = payload.user || state.auth.user;
    renderUserBrokerStatus();
    if (status) {
      status.textContent = "token saved";
      status.className = "settings-inline-status positive";
    }
  } catch (error) {
    if (status) {
      status.textContent = "token save failed";
      status.className = "settings-inline-status negative";
    }
    showBackendError(networkErrorMessage(error, "user Upstox token save"), { action: "user Upstox token save" });
  } finally {
    if (button) button.disabled = !(state.auth?.authenticated && !state.auth?.admin);
  }
}

async function connectUpstox() {
  const status = byId("upstox-connect-status");
  const button = byId("upstox-connect-btn");
  if (status) {
    status.textContent = "connecting Upstox";
    status.className = "settings-inline-status";
  }
  if (button) button.disabled = true;
  try {
    const response = await fetch("/api/upstox/connect", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(upstoxConnectPayload()),
    });
    const payload = await response.json().catch(() => ({ detail: `HTTP ${response.status}` }));
    if (!response.ok || !payload.ok) {
      if (status) {
        status.textContent = payload.detail || "Upstox connect failed";
        status.className = "settings-inline-status negative";
      }
      showDetails("Upstox Connect", payload);
      return;
    }
    if (byId("upstox-access-token")) byId("upstox-access-token").value = "";
    if (status) {
      status.textContent = `Upstox connected · ${payload.provider || "provider ready"}`;
      status.className = "settings-inline-status positive";
    }
    if (payload.config) renderSettings(payload.config);
    if (payload.status) render(payload.status);
    await refreshAccountAndUsers();
    fetchLogs();
    showDetails("Upstox Connect", {
      ok: payload.ok,
      message: payload.message,
      provider: payload.provider,
    });
  } catch (error) {
    if (status) {
      status.textContent = "Upstox connect failed: backend unreachable";
      status.className = "settings-inline-status negative";
    }
    showBackendError(networkErrorMessage(error, "Upstox connect"), { action: "Upstox connect" });
  } finally {
    if (button) button.disabled = !(state.auth && state.auth.admin);
  }
}

async function connectMyKite() {
  const status = byId("my-kite-status");
  const button = byId("my-kite-connect-btn");
  status.textContent = "saving";
  button.disabled = true;
  try {
    const response = await fetch("/api/me/kite/connect", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        api_key: byId("my-kite-api-key")?.value?.trim(),
        access_token: byId("my-kite-access-token")?.value?.trim(),
      }),
    });
    const payload = await response.json().catch(() => ({ detail: `HTTP ${response.status}` }));
    if (!response.ok || !payload.ok) {
      status.textContent = payload.detail || "save failed";
      status.className = "settings-inline-status negative";
      showDetails("User Kite Connect", payload);
      return;
    }
    if (byId("my-kite-access-token")) byId("my-kite-access-token").value = "";
    state.auth.user = payload.user || state.auth.user;
    renderUserBrokerStatus();
    status.textContent = "saved";
    status.className = "settings-inline-status positive";
  } catch (error) {
    status.textContent = "save failed";
    status.className = "settings-inline-status negative";
  } finally {
    button.disabled = !(state.auth?.authenticated && !state.auth?.admin);
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
    if (state.socketReconnectTimer) clearTimeout(state.socketReconnectTimer);
    state.socketReconnectTimer = null;
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
    body.innerHTML = emptyTableRow(
      6,
      "No realized strategy P&L yet",
      "Strategy performance starts filling after paper or live orders are opened and tracked through exits.",
      "Review Ideas",
      "suggestions",
    );
    return;
  }
  const market = normalizeUiMarket(state.activeMarket);
  body.innerHTML = rows
    .slice(0, 80)
    .map(
      (row) => `<tr>
        <td><strong>${escapeHtml(row.strategy)}</strong></td>
        <td class="num">${row.open_positions}</td>
        <td class="num">${fmtMarketMoney(row.exposure, market)}</td>
        <td class="num ${pnlClass(row.unrealized_pnl)}">${fmtMarketMoney(row.unrealized_pnl, market)}</td>
        <td class="num ${pnlClass(row.realized_pnl)}">${fmtMarketMoney(row.realized_pnl, market)}</td>
        <td class="num">${row.filled_orders}</td>
      </tr>`,
    )
    .join("");
  bindRowDetails(body, rows.slice(0, 80), "Strategy");
}

function renderStrategyPlans(rows) {
  const body = byId("strategy-plans-body");
  if (!body) return;
  if (!rows.length) {
    const filtered = pageFilter("suggestions") !== "all";
    body.innerHTML = emptyBlock(
      filtered ? "No plans match this filter" : "No strategy plans loaded",
      filtered
        ? "Switch back to All, or wait for new ideas to be assigned to strategy plans."
        : "Plans define how ideas are grouped, budgeted, followed, and measured after recommendation.",
    );
    return;
  }
  const market = normalizeUiMarket(state.activeMarket);
  body.innerHTML = rows
    .slice(0, 8)
    .map((row) => {
      const risk = humanLabel(row.risk_level || "Medium");
      const ideas = (row.constituents || []).filter((idea) => rowMarket(idea) === market).slice(0, 4);
      const symbolList = ideas.length
        ? ideas.map((idea) => {
            const life = ideaLifecycle(idea);
            return `<span class="plan-symbol ${escapeHtml(life.className)}"><strong>${escapeHtml(displayValue(idea.symbol, "Symbol"))}</strong><small>${escapeHtml(life.label)} · ${fmtPct(idea.current_return_pct || 0)}</small></span>`;
          }).join("")
        : `<span class="plan-symbol empty">No ${escapeHtml(activeMarketLabel())} stocks in this plan yet</span>`;
      const followRow = state.auth?.admin
        ? `<div class="plan-admin-note compact">Admin managed · users follow with their own budget and credits</div>`
        : `<div class="plan-follow-row">
            <input type="number" min="0" step="100" inputmode="decimal" placeholder="Budget" data-plan-budget="${escapeHtml(row.code)}" />
            <button type="button" class="primary" data-plan-action="paper" data-plan-code="${escapeHtml(row.code)}">Follow Paper</button>
            <button type="button" data-plan-action="track" data-plan-code="${escapeHtml(row.code)}">Track</button>
          </div>`;
      return `<article class="strategy-plan-card risk-${escapeHtml(cssToken(row.risk_level))}" role="button" tabindex="0" data-plan="${escapeHtml(row.code)}">
      <div class="strategy-plan-top">
        <span class="strategy-risk-pill">${escapeHtml(risk)}</span>
        <strong>${escapeHtml(row.name || row.code || "-")}</strong>
      </div>
      <p>${escapeHtml(shortValue(row.description || "-", 150))}</p>
      <div class="strategy-plan-stats">
        <span><small>Holding</small><strong>${escapeHtml(row.holding_period || "-")}</strong></span>
        <span><small>${escapeHtml(activeMarketLabel())} Stocks</small><strong>${fmtNumber(ideas.length)}</strong></span>
        <span><small>Since Signals</small><strong class="${pnlClass(row.avg_return_pct)}">${fmtPct(row.avg_return_pct || 0)}</strong></span>
      </div>
      <div class="plan-symbol-list">${symbolList}</div>
      ${followRow}
      <div class="strategy-capital-rule">${escapeHtml(shortValue(row.capital_rule || "", 120))}</div>
    </article>`;
    })
    .join("");
  [...body.querySelectorAll(".strategy-plan-card")].forEach((button, index) => {
    button.addEventListener("click", (event) => {
      if (event.target.closest("[data-plan-action]") || event.target.closest("[data-plan-budget]")) return;
      showDetails("Strategy Plan", rows[index]);
    });
  });
  [...body.querySelectorAll("[data-plan-action]")].forEach((button) => {
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      const code = button.dataset.planCode;
      const input = body.querySelector(`[data-plan-budget="${CSS.escape(code)}"]`);
      followPlan(code, button.dataset.planAction, Number(input?.value || 0));
    });
  });
}

async function followPlan(planCode, action, amount = 0) {
  if (!planCode) return;
  const mode = action === "paper" ? "PAPER" : action === "live" ? "LIVE" : "TRACK";
  try {
    const response = await fetch(`/api/plans/${encodeURIComponent(planCode)}/follow`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mode, amount, market: state.activeMarket, max_symbols: 5 }),
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      alert(payload.detail || "Could not follow plan");
      return;
    }
    if (Array.isArray(payload.ideas)) {
      state.latest = {
        ...(state.latest || {}),
        suggestions: payload.ideas,
        signal_ideas: payload.ideas,
        tracked_ideas: payload.tracked_ideas || state.latest?.tracked_ideas || [],
        positions: payload.positions || state.latest?.positions || [],
        strategy_plans: payload.strategy_plans || state.latest?.strategy_plans || [],
      };
      render(state.latest);
    } else {
      fetchStatus();
    }
  } catch (error) {
    alert(networkErrorMessage(error, "plan follow"));
  }
}

function renderPositions(rows) {
  const body = byId("positions-body");
  const summary = byId("positions-summary-strip");
  if (summary) {
    const winners = rows.filter((row) => (Number(row.market_price) - Number(row.avg_price)) * Number(row.qty) > 0).length;
    const losers = rows.filter((row) => (Number(row.market_price) - Number(row.avg_price)) * Number(row.qty) < 0).length;
    const deployed = rows.reduce((sum, row) => sum + Number(row.avg_price || 0) * Number(row.qty || 0), 0);
    const pnl = rows.reduce((sum, row) => sum + (Number(row.market_price || 0) - Number(row.avg_price || 0)) * Number(row.qty || 0), 0);
    const market = normalizeUiMarket(state.activeMarket);
    summary.innerHTML = `
      <button type="button"><span>Deployed</span><strong>${fmtMarketMoney(deployed, market)}</strong></button>
      <button type="button"><span>Unrealised P&L</span><strong class="${pnlClass(pnl)}">${fmtMarketMoney(pnl, market)}</strong></button>
      <button type="button"><span>Winners</span><strong class="positive">${fmtNumber(winners)}</strong></button>
      <button type="button"><span>Losers</span><strong class="negative">${fmtNumber(losers)}</strong></button>
    `;
  }
  if (!rows.length) {
    body.innerHTML = emptyTableRow(
      11,
      `No open ${activeMarketLabel()} positions`,
      "The agent will open positions when it finds qualifying opportunities.",
      "Run agent cycle",
      "decisions",
    );
    return;
  }
  body.innerHTML = rows
    .map((row) => positionRowHtml(row))
    .join("");
  bindRowDetails(body, rows, "Position");
  bindPositionExitButtons(body, rows);
}

function renderOverviewPositions(rows) {
  const body = byId("overview-positions-body");
  if (!body) return;
  const sorted = [...rows].sort((a, b) => {
    const pnlA = (Number(a.market_price) - Number(a.avg_price)) * Number(a.qty);
    const pnlB = (Number(b.market_price) - Number(b.avg_price)) * Number(b.qty);
    return pnlB - pnlA;
  });
  if (!sorted.length) {
    body.innerHTML = emptyTableRow(
      9,
      `No open ${activeMarketLabel()} positions`,
      "The agent will open positions when qualifying opportunities are found.",
      "View decisions",
      "decisions",
    );
    return;
  }
  body.innerHTML = sorted.slice(0, 5).map((row) => positionRowHtml(row, true)).join("");
  bindRowDetails(body, sorted.slice(0, 5), "Position");
}

function positionRowHtml(row, compact = false) {
  const pnl = (Number(row.market_price) - Number(row.avg_price)) * Number(row.qty);
  const pnlPct = Number(row.avg_price) > 0 ? ((Number(row.market_price) - Number(row.avg_price)) / Number(row.avg_price)) * 100 : 0;
  const marketValue = Number(row.market_price) * Number(row.qty);
  const summary = row.position_summary || {};
  const flags = summary.active_flags || [];
  const market = rowMarket(row);
  const dayPct = quoteDayPct(row);
  const gates = flags.length
    ? flags.slice(0, 3).map((flag) => `<span class="gate-pill warning">${escapeHtml(humanLabel(flag))}</span>`).join("")
    : `<span class="gate-pill positive">Clear</span>`;
  const symbolCell = `<div class="symbol-cell"><span class="symbol-logo">${escapeHtml(symbolInitials(row.symbol))}</span><div><strong>${escapeHtml(displayValue(row.symbol, "Symbol"))}</strong><small>${escapeHtml(displayValue(row.company_name || row.strategy, "Position"))}</small></div></div>`;
  if (compact) {
    return `<tr>
      <td>${symbolCell}</td>
      <td class="num"><strong>${fmtPct(summary.overall_score_pct ?? 0)}</strong></td>
      <td class="num quantity">${fmtNumber(row.qty)}</td>
      <td class="num price" aria-live="polite">${fmtMarketMoney(row.market_price, market)}</td>
      <td class="num"><span class="day-badge ${pnlClass(dayPct)}">${fmtPct(dayPct)}</span></td>
      <td class="num price">${fmtMarketMoney(marketValue, market)}</td>
      <td class="num pnl ${pnlClass(pnl)}"><strong>${fmtMarketMoney(pnl, market)}</strong></td>
      <td>${gates}</td>
      <td><button type="button" class="row-link">Details →</button></td>
    </tr>`;
  }
  return `<tr>
    <td>${symbolCell}</td>
    <td class="num"><strong>${fmtPct(summary.overall_score_pct ?? 0)}</strong><br><small>${escapeHtml(summary.overall_grade || "-")}</small></td>
    <td class="num quantity">${fmtNumber(row.qty)}</td>
    <td class="num price">${fmtMarketMoney(row.avg_price, market)}</td>
    <td class="num price" aria-live="polite">${fmtMarketMoney(row.market_price, market)}<br><small>${escapeHtml(summary.price_label || "LTP")}</small></td>
    <td class="num"><span class="day-badge ${pnlClass(dayPct)}">${fmtPct(dayPct)}</span></td>
    <td class="num price">${fmtMarketMoney(marketValue, market)}</td>
    <td class="num pnl ${pnlClass(pnl)}"><strong>${fmtMarketMoney(pnl, market)}</strong></td>
    <td class="num percentage ${pnlClass(pnlPct)}">${fmtPct(pnlPct)}</td>
    <td>${gates}</td>
    <td><button type="button" class="danger-outline manual-exit-btn" data-symbol="${escapeHtml(row.symbol || "")}" data-market="${escapeHtml(market)}">Exit</button> <button type="button" class="row-link">Details →</button></td>
  </tr>`;
}

function bindPositionExitButtons(body, rows) {
  [...body.querySelectorAll(".manual-exit-btn")].forEach((button) => {
    const symbol = button.dataset.symbol;
    const row = rows.find((item) => String(item.symbol || "").toUpperCase() === String(symbol || "").toUpperCase());
    if (!row) return;
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      manualExitPosition(row, button);
    });
  });
}

async function manualExitPosition(row, button) {
  const symbol = String(row.symbol || "").toUpperCase();
  if (!symbol) return;
  const market = rowMarket(row);
  const qty = Number(row.qty || 0);
  const label = `${symbol}${qty ? ` (${fmtNumber(qty)} qty)` : ""}`;
  if (!window.confirm(`Exit ${label} from OpenStocks now?`)) return;
  const originalText = button.textContent;
  button.disabled = true;
  button.textContent = "Exiting...";
  try {
    const response = await fetch(`/api/positions/${encodeURIComponent(symbol)}/exit`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ market_region: market }),
    });
    const payload = await response.json().catch(() => ({ detail: `HTTP ${response.status}` }));
    if (response.status === 401) {
      handleUnauthorized(payload.detail || "Session expired. Sign in again.");
      return;
    }
    if (!response.ok) {
      showDetails("Exit Error", payload);
      return;
    }
    render(payload);
    fetchCredits();
    showDetails("Manual Exit", { symbol, market_region: market, status: "submitted", message: `${symbol} exit was processed.` });
  } catch (error) {
    showBackendError(networkErrorMessage(error, "manual exit"), { symbol, market_region: market });
  } finally {
    button.disabled = false;
    button.textContent = originalText;
  }
}

function renderTrackedIdeas(rows) {
  const body = byId("tracked-ideas-body");
  if (!body) return;
  if (!rows.length) {
    body.innerHTML = emptyBlock(
      `No ${activeMarketLabel()} ideas being tracked`,
      "Track or paper-trade a signal to monitor target hits, stop status, and live return from the original recommendation.",
      "Review Signals",
      "suggestions",
    );
    return;
  }
  body.innerHTML = rows
    .slice(0, 20)
    .map((row, index) => {
      const mode = String(row.mode || row.user_follow?.mode || "TRACK").toUpperCase();
      const qty = Number(row.qty || row.user_follow?.qty || 0);
      const entry = Number(row.follow_entry_price || row.user_follow?.entry_price || row.entry_price || 0);
      const latest = Number(row.follow_latest_price || row.user_follow?.latest_price || row.latest_price || 0);
      const invested = Number(row.invested_amount || row.user_follow?.invested_amount || 0);
      const pnl = Number(row.unrealized_pnl || row.user_follow?.unrealized_pnl || 0);
      const returnPct = Number(row.return_pct || row.user_follow?.return_pct || 0);
      const market = rowMarket(row);
      const lifecycle = ideaLifecycle(row);
      return `<article class="tracked-idea-card" role="button" tabindex="0" data-index="${index}" aria-label="Open ${escapeHtml(displayValue(row.symbol, "symbol"))} tracked idea">
        <div class="tracked-idea-main">
          <div>
            <span class="signal-rank">${escapeHtml(mode)}</span>
            <div class="tracked-title-row"><strong>${escapeHtml(displayValue(row.symbol, "Symbol"))}</strong><span class="lifecycle-pill ${escapeHtml(lifecycle.className)}">${escapeHtml(lifecycle.label)}</span></div>
            <small>${escapeHtml(row.strategy || "-")} · ${escapeHtml(ideaTimelineText(row))} · followed ${fmtTime(row.followed_at || row.user_follow?.created_at)}</small>
          </div>
          <div class="tracked-return ${pnlClass(returnPct)}">
            <strong>${fmtPct(returnPct)}</strong>
            <small>${fmtMarketMoney(pnl, market)} unrealized</small>
          </div>
        </div>
        <div class="tracked-metrics">
          <span><small>Qty</small><strong>${fmtNumber(qty)}</strong></span>
          <span><small>Entry</small><strong>${fmtMarketMoney(entry, market)}</strong></span>
          <span><small>LTP</small><strong>${fmtMarketMoney(latest, market)}</strong></span>
          <span><small>Invested</small><strong>${fmtMarketMoney(invested, market)}</strong></span>
        </div>
        ${targetLadderHtml(row, market, true)}
      </article>`;
    })
    .join("");
  [...body.querySelectorAll(".tracked-idea-card")].forEach((card) => {
    const row = rows[Number(card.dataset.index)];
    card.addEventListener("click", () => showDetails("Tracked Idea", row));
  });
}

function renderSuggestions(rows) {
  const body = byId("suggestions-body");
  if (!rows.length) {
    body.innerHTML = emptyBlock(
      `No ${activeMarketLabel()} signal history yet`,
      "The shared engine will publish only ideas that survive the data, entry, risk, sentiment, and LLM decision gates.",
      "View Engine Checks",
      "decisions",
    );
    return;
  }
  body.innerHTML = rows
    .slice(0, 20)
    .map((row, index) => {
      const displaySignal = row.display_signal || row.suggestion || "WATCH";
      const action = String(row.suggestion || row.signal_type || "WATCH").toLowerCase();
      const targets = row.targets || [];
      const t1 = targets[0] || {};
      const t3 = targets[2] || {};
      const riskFlags = Array.isArray(row.risk_flags) ? row.risk_flags.slice(0, 3) : [];
      const institutionalFlags = row.institutional_flags && typeof row.institutional_flags === "object"
        ? Object.entries(row.institutional_flags)
            .filter(([, value]) => Boolean(value))
            .slice(0, 3)
            .map(([key]) => humanLabel(key))
        : [];
      const readiness = row.fresh_action_label || humanLabel(row.decision_readiness || "monitor_only");
      const latestSystemAction = row.latest_system_action ? String(row.latest_system_action).toUpperCase() : "";
      const followed = row.user_follow || null;
      const followedActive = followed && ["ACTIVE", "LIVE_REQUESTED", "LIVE_EXIT_REQUESTED"].includes(String(followed.status || "").toUpperCase()) && Number(followed.qty || 0) > 0;
      const executionLabel = row.execution_state_label || (followed ? `${followed.mode} active` : "Signal Only");
      const setupBucket = row.setup_bucket_label || "-";
      const confidence = Number(row.confidence || 0) * 100;
      const currentReturn = Number(row.current_return_pct || 0);
      const peakReturn = Number(row.peak_return_pct || 0);
      const worstReturn = Number(row.worst_return_pct || 0);
      const market = rowMarket(row);
      const lifecycle = ideaLifecycle(row);
      return `<article class="signal-history-card signal-${escapeHtml(cssToken(action))} ${index === 0 ? "featured" : ""}" role="button" tabindex="0" data-index="${index}" aria-label="Open ${escapeHtml(displayValue(row.symbol, "symbol"))} idea audit">
        <div class="signal-card-main">
          <div class="signal-card-title">
            <span class="signal-rank">Idea #${row.id || index + 1}</span>
            <div>
              <div class="signal-symbol-row">
                <strong>${escapeHtml(displayValue(row.symbol, "Symbol"))}</strong>
                <span class="tag ${escapeHtml(cssToken(action))}">${escapeHtml(displaySignal)}</span>
                <span class="lifecycle-pill ${escapeHtml(lifecycle.className)}">${escapeHtml(lifecycle.label)}</span>
                ${followed ? `<span class="signal-followed">${escapeHtml(followed.mode)} ${fmtPct(followed.return_pct || 0)}</span>` : ""}
              </div>
              <small>${fmtMarketMoney(row.price || row.latest_price, market)} · ${escapeHtml(MARKET_LABELS[market] || market)} · ${escapeHtml(row.strategy || "-")} · ${escapeHtml(ideaTimelineText(row))}</small>
            </div>
          </div>
          ${followedActive
            ? `<div class="signal-card-actions">
                <button type="button" data-idea-action="details" data-idea-id="${escapeHtml(row.id)}">Manage</button>
                <button type="button" class="danger-outline" data-idea-action="exit" data-idea-id="${escapeHtml(row.id)}" data-symbol="${escapeHtml(row.symbol || "")}">Exit</button>
              </div>`
            : `<div class="signal-card-actions">
                <button type="button" data-idea-action="track" data-idea-id="${escapeHtml(row.id)}">Track</button>
                <button type="button" data-idea-action="paper" data-idea-id="${escapeHtml(row.id)}">Paper</button>
                <button type="button" data-idea-action="live" data-idea-id="${escapeHtml(row.id)}">Live</button>
              </div>`}
        </div>
        <div class="signal-metric-strip">
          <div><span>Fresh Action</span><strong>${escapeHtml(readiness)}</strong><small>${latestSystemAction ? `engine ${escapeHtml(latestSystemAction)}` : `${fmtNumber(confidence)}% confidence`}</small></div>
          <div><span>Setup</span><strong>${escapeHtml(setupBucket)}</strong><small>${escapeHtml(row.setup_bucket || "-")}</small></div>
          <div><span>Confluence</span><strong>${escapeHtml(row.confluence ?? "-")}/26</strong><small>${escapeHtml(row.tier || "-")}</small></div>
          <div><span>Execution</span><strong>${escapeHtml(executionLabel)}</strong><small>${escapeHtml(row.execution_state || "SIGNAL_ONLY")}</small></div>
          <div><span>Since Signal</span><strong class="${pnlClass(currentReturn)}">${fmtPct(currentReturn)}</strong><small>best ${fmtPct(peakReturn)} · worst ${fmtPct(worstReturn)}</small></div>
        </div>
        ${targetLadderHtml(row, market)}
        <div class="signal-trade-strip">
          <span><small>Entry</small><strong>${formatZone(row.entry_zone, market)}</strong></span>
          <span><small>Stop</small><strong class="negative">${fmtMarketMoney(row.stop_loss, market)}</strong></span>
          <span><small>Target 1</small><strong class="positive">${fmtMarketMoney(t1.price, market)}</strong></span>
          <span><small>Final Target</small><strong class="positive">${fmtMarketMoney(t3.price, market)}</strong></span>
        </div>
        <div class="signal-reason-row">
          <span>Reason</span>
          <p>${escapeHtml(shortValue(row.display_reason || readableDecisionReason(row), 220))}</p>
        </div>
        <div class="signal-audit-row">
          <span>Full audit</span>
          <span>${escapeHtml(row.latest_decision_id ? `Decision #${row.latest_decision_id}` : "Decision audit")}</span>
          ${latestSystemAction ? `<span>Latest engine: ${escapeHtml(latestSystemAction)}</span>` : ""}
          <span>Setup: ${escapeHtml(setupBucket)}</span>
          <span>Execution: ${escapeHtml(executionLabel)}</span>
          ${riskFlags.map((flag) => `<span class="warning">${escapeHtml(humanLabel(flag))}</span>`).join("")}
          ${institutionalFlags.map((flag) => `<span>${escapeHtml(flag)}</span>`).join("")}
        </div>
      </article>`;
    })
    .join("");
  [...body.querySelectorAll(".signal-history-card")].forEach((button) => {
    const row = rows[Number(button.dataset.index)];
    button.addEventListener("click", (event) => {
      if (event.target.closest("[data-idea-action]")) return;
      showDetails("Suggestion", row);
    });
  });
  [...body.querySelectorAll("[data-idea-action]")].forEach((button) => {
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      const action = button.dataset.ideaAction;
      const row = rows.find((item) => Number(item.id) === Number(button.dataset.ideaId));
      if (action === "details") {
        showDetails("Suggestion", row || {});
        return;
      }
      if (action === "exit") {
        manualExitPosition(row || { symbol: button.dataset.symbol, market_region: state.activeMarket }, button);
        return;
      }
      followIdea(row || Number(button.dataset.ideaId), action, button);
    });
  });
}

function defaultPaperAmountForIdea(row = {}) {
  const market = rowMarket(row);
  const price = Number(row.latest_price || row.price || row.entry_price || 0);
  const scoped = marketPortfolioFromPayload(state.latest || {}, market);
  const accountPaper = state.account?.paper || {};
  const accountPortfolio = accountPaper.portfolio_by_market?.[market] || accountPaper.portfolio?.portfolio_by_market?.[market] || {};
  const cash = Number(scoped.cash ?? accountPortfolio.cash ?? 0);
  if (!Number.isFinite(price) || price <= 0) return 0;
  if (!Number.isFinite(cash) || cash <= 0) return price;
  const target = cash * 0.2;
  if (target >= price) return Math.min(target, cash);
  return price <= cash ? price : 0;
}

async function followIdea(rowOrId, action, button = null) {
  const row = typeof rowOrId === "object" && rowOrId ? rowOrId : {};
  const ideaId = Number(row.id || rowOrId || 0);
  if (!ideaId) return;
  if (state.auth?.admin) {
    showDetails("Paper Follow", {
      status: "user_account_required",
      message: "Paper and live follows are user-account actions. Sign in as a trading user, not admin, to follow ideas.",
    });
    return;
  }
  const mode = action === "paper" ? "PAPER" : action === "live" ? "LIVE" : "TRACK";
  const amount = mode === "PAPER" ? defaultPaperAmountForIdea(row) : 0;
  const originalText = button?.textContent || "";
  if (button) {
    button.disabled = true;
    button.textContent = mode === "PAPER" ? "Papering..." : mode === "LIVE" ? "Requesting..." : "Tracking...";
  }
  try {
    const response = await fetch(`/api/ideas/${ideaId}/follow`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mode, amount }),
    });
    const payload = await response.json().catch(() => ({ detail: `HTTP ${response.status}` }));
    if (response.status === 401) {
      handleUnauthorized(payload.detail || "Session expired. Sign in again.");
      return;
    }
    if (!response.ok) {
      showDetails("Follow Error", {
        idea_id: ideaId,
        mode,
        amount,
        message: payload.detail || "Could not update idea tracking",
        response: payload,
      });
      return;
    }
    if (Array.isArray(payload.ideas)) {
      state.latest = {
        ...(state.latest || {}),
        suggestions: payload.ideas,
        signal_ideas: payload.ideas,
        tracked_ideas: payload.tracked_ideas || state.latest?.tracked_ideas || [],
        tracked_ideas_by_market: payload.tracked_ideas_by_market || state.latest?.tracked_ideas_by_market || {},
        positions: payload.positions || state.latest?.positions || [],
      };
      const marketIdeas = filterRowsByMarket(payload.ideas || [], state.activeMarket);
      const marketTracked = payloadRowsForMarket(state.latest, "tracked_ideas", state.activeMarket);
      const marketPositions = filterRowsByMarket(state.latest.positions || [], state.activeMarket);
      renderSuggestions(marketIdeas);
      renderTrackedIdeas(marketTracked);
      renderPositions(marketPositions);
      byId("kpi-positions").textContent = String(marketPositions.length);
      byId("nav-positions-badge").textContent = String(marketPositions.length);
      byId("position-count").textContent = `${marketPositions.length} open`;
      const trackedCount = byId("tracked-count");
      if (trackedCount) trackedCount.textContent = `${marketTracked.length} active`;
      await refreshStatusOnly();
      showDetails("Paper Follow", {
        symbol: row.symbol,
        mode,
        amount,
        follow: payload.follow,
        exit_manager: payload.paper_exit_manager,
      });
    }
  } catch (error) {
    showBackendError(networkErrorMessage(error, "idea tracking"), { idea_id: ideaId, mode, amount });
  } finally {
    if (button) {
      button.disabled = false;
      button.textContent = originalText;
    }
  }
}

function renderQuotes(rows) {
  const accountBody = byId("quotes-body");
  const overviewBody = byId("overview-quotes-body");
  const filter = state.quoteFilter.trim().toUpperCase();
  const railRows = filter ? rows.filter((row) => String(row.symbol || "").toUpperCase().includes(filter)) : rows;
  byId("quote-count").textContent = filter ? `${railRows.length}/${rows.length} quotes` : `${rows.length} quotes`;
  const markup = rows
    .slice(0, 160)
    .map((row) => quoteRow(row))
    .join("");
  accountBody.innerHTML = markup || emptyTableRow(
    6,
    `No ${activeMarketLabel()} quotes yet`,
    "Connect the market feed or wait for the next backend cycle to populate the quote table.",
    "Open Account",
    "account",
  );
  overviewBody.innerHTML =
    railRows
      .slice(0, 80)
      .map((row) => quoteRow(row))
      .join("") || emptyTableRow(
        6,
        filter ? "No matching symbol" : `No ${activeMarketLabel()} quotes yet`,
        filter ? "Clear the filter or search the symbol in Analyze." : "Quotes appear here once the selected market feed returns data.",
        filter ? "Analyze Symbol" : "Open Account",
        filter ? "analyze" : "account",
      );
  bindRowDetails(accountBody, rows.slice(0, 160), "Quote");
  bindRowDetails(overviewBody, railRows.slice(0, 80), "Quote");
}

function renderMarketTape(rows, market = state.activeMarket) {
  const track = byId("market-tape-track");
  const label = byId("market-tape-market");
  if (!track) return;
  const region = normalizeUiMarket(market);
  if (label) label.textContent = MARKET_LABELS[region] || region;
  const ranked = [...(rows || [])]
    .filter((row) => Number.isFinite(Number(row.price)))
    .sort((a, b) => Math.abs(Number(quoteDayPct(b)) || 0) - Math.abs(Number(quoteDayPct(a)) || 0))
    .slice(0, 28);
  if (!ranked.length) {
    track.innerHTML = `<span class="market-tape-empty">${escapeHtml(MARKET_LABELS[region] || region)} quotes awaiting feed</span>`;
    return;
  }
  const items = ranked.map((row) => marketTapeItem(row)).join("");
  track.innerHTML = `${items}${items}`;
  track.querySelectorAll(".market-tape-item").forEach((button) => {
    button.addEventListener("click", () => {
      setAnalyzeMarket(region);
      const input = byId("analyze-symbol");
      if (input) input.value = button.dataset.quoteSymbol || "";
      setView("analyze");
    });
  });
}

function marketTapeItem(row) {
  const market = rowMarket(row);
  const dayPct = quoteDayPct(row);
  const cls = pnlClass(dayPct);
  return `<button class="market-tape-item ${cls}" type="button" data-quote-symbol="${escapeHtml(row.symbol)}">
    <strong>${escapeHtml(displayValue(row.symbol, "Symbol"))}</strong>
    <span>${fmtMarketMoney(row.price, market)}</span>
    <em>${fmtPct(dayPct)}</em>
  </button>`;
}

function quoteRow(row) {
  const dayPct = quoteDayPct(row);
  const market = rowMarket(row);
  const source = displayValue(row.source, "Feed");
  return `<tr>
        <td><strong>${escapeHtml(displayValue(row.symbol, "Symbol"))}</strong></td>
        <td class="num">${fmtMarketMoney(row.price, market)}</td>
        <td class="num ${pnlClass(dayPct)}">${fmtPct(dayPct)}</td>
        <td class="num">${fmtCompact(row.volume)}</td>
        <td><span class="source ${sourceClass(row.source)}">${escapeHtml(source)}</span></td>
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
  return "";
}

function decisionFeedEmptyHtml(controlRunning, marketLabel = activeMarketLabel()) {
  return emptyBlock(
    `No ${marketLabel} decisions yet`,
    controlRunning
      ? "The agent is running. This market feed will fill after the next completed strategy scan."
      : "Use Run Now in Dashboard to scan the selected market.",
  );
}

function renderDecisions(rows, options = {}) {
  const body = byId("decisions-body");
  const detail = byId("decision-detail-panel");
  if (!body) return;
  const visibleRows = sortDecisionRows(rows).slice(0, 120);
  if (!visibleRows.length) {
    body.innerHTML = decisionFeedEmptyHtml(Boolean(options.controlRunning));
    if (detail) {
      detail.innerHTML = emptyBlock(
        "No decision selected",
        "When decisions arrive, select one to inspect the score radar, LLM reasoning, gates, and timeline.",
        "Analyze Symbol",
        "analyze",
      );
    }
    return;
  }
  body.innerHTML = visibleRows.map((row, index) => decisionFeedCardHtml(row, index, false)).join("");
  [...body.querySelectorAll(".decision-feed-card")].forEach((card) => {
    const row = visibleRows[Number(card.dataset.index)];
    if (!row) return;
    card.addEventListener("click", () => {
      for (const item of body.querySelectorAll(".decision-feed-card")) item.classList.remove("active");
      card.classList.add("active");
      renderDecisionDetailPanel(row);
    });
  });
  body.querySelector(".decision-feed-card")?.classList.add("active");
  renderDecisionDetailPanel(visibleRows[0]);
}

function renderOverviewDecisions(rows, options = {}) {
  const body = byId("overview-decisions-body");
  const previewLimit = window.matchMedia?.("(max-width: 767px)")?.matches ? 3 : 5;
  const rankedRows = sortDecisionRows(rows);
  body.innerHTML = rankedRows.length
    ? rankedRows
        .slice(0, previewLimit)
        .map((row, index) => decisionFeedCardHtml(row, index, true))
        .join("")
    : decisionFeedEmptyHtml(Boolean(options.controlRunning));
  [...body.querySelectorAll(".decision-feed-card")].forEach((card) => {
    const row = rankedRows[Number(card.dataset.index)];
    if (row) card.addEventListener("click", () => showDetails("Decision", row));
  });
}

function decisionFeedCardHtml(row, index, compact = false) {
  const action = String(row.action || "HOLD").toLowerCase();
  const score = decisionScorePercent(row);
  const tech = Number(row.technical_score || 0);
  const sentiment = Number(row.sentiment_score || 0);
  const reason = shortValue(readableDecisionReason(row), compact ? 150 : 240);
  const initials = symbolInitials(row.symbol);
  return `<article class="decision-feed-card action-${escapeHtml(action)}" role="button" tabindex="0" data-index="${index}">
    <div class="decision-logo">${escapeHtml(initials)}</div>
    <div class="decision-main">
      <div class="decision-title-row">
        <strong>${escapeHtml(displayValue(row.symbol, "Symbol"))}</strong>
        ${row.company_name ? `<small>${escapeHtml(row.company_name)}</small>` : ""}
        <span class="tag ${escapeHtml(action)}">${escapeHtml(row.action || "HOLD")}</span>
        <span class="decision-time">${escapeHtml(fmtTime(row.ts))}</span>
      </div>
      <p>${escapeHtml(reason)}</p>
      <div class="decision-bars">
        <span><em style="width:${Math.max(0, Math.min(100, (tech + 1) * 50))}%"></em><b>Setup</b></span>
        <span><em style="width:${Math.max(0, Math.min(100, (sentiment + 1) * 50))}%"></em><b>News</b></span>
        <span><em style="width:${Math.max(0, Math.min(100, score))}%"></em><b>Confidence</b></span>
      </div>
    </div>
    <div class="score-ring" style="--score:${Math.max(0, Math.min(100, score))}">
      <strong>${fmtNumber(score)}</strong>
      <small>%</small>
    </div>
  </article>`;
}

function renderDecisionDetailPanel(row = {}) {
  const panel = byId("decision-detail-panel");
  if (!panel) return;
  const audit = decisionAudit(row);
  const context = audit.context || {};
  const full = decisionFullSpectrum(audit);
  const market = rowMarket(row);
  const action = String(row.action || audit.final_action || "HOLD").toLowerCase();
  const confidence = Math.max(0, Math.min(100, Number(row.confidence || audit.confidence || 0) * 100));
  const score = audit.score_breakdown || {};
  const metrics = [
    { label: "Tech", value: normalizedScore(row.technical_score ?? context.technical_math?.score) },
    { label: "Sentiment", value: normalizedScore(row.sentiment_score ?? context.sentiment?.score) },
    { label: "Risk", value: normalizedScore(score.risk_score ?? full.risk_score ?? (full.risk_overrides?.no_new_longs ? -0.7 : 0.35)) },
    { label: "Macro", value: normalizedScore(context.global_market_context?.risk_score ?? context.market_breadth?.breadth_score) },
  ];
  const llm = audit.llm_output || {};
  const timeline = [
    ["Quote", `${fmtMarketMoney(row.price || context.quote?.price, market)} from ${context.quote?.source || row.source || "market feed"}`],
    ["Score", `${fmtNumber(score.combined ?? row.combined_score)} combined · ${full.confluence_score?.total ?? row.confluence ?? "-"} confluence`],
    ["Gates", failedGatesFromAudit(audit, context).length ? `${failedGatesFromAudit(audit, context).length} blockers` : "hard gates clear"],
    ["Brain", audit.llm_error ? "OpenStocks Brain failed safely" : (audit.decision_path || "deterministic audit")],
    ["Decision", `${row.action || "HOLD"} · ${fmtNumber(confidence)}% confidence`],
  ];
  panel.innerHTML = `
    <section class="decision-detail-hero">
      <div class="decision-logo large">${escapeHtml(symbolInitials(row.symbol))}</div>
      <div>
        <span>${escapeHtml(MARKET_LABELS[market] || market)} decision</span>
        <h3>${escapeHtml(displayValue(row.symbol, "Symbol"))}</h3>
        <p>${fmtMarketMoney(row.price, market)} · ${escapeHtml(displayValue(row.strategy || context.best_strategy?.name, "Strategy pending"))} · ${escapeHtml(fmtTime(row.ts))}</p>
      </div>
      <span class="tag ${escapeHtml(action)}">${escapeHtml(row.action || "HOLD")}</span>
      <div class="score-ring large" style="--score:${confidence}"><strong>${fmtNumber(confidence)}</strong><small>%</small></div>
    </section>
    <section class="decision-radar-section">
      ${scoreRadarSvg(metrics)}
      <div class="decision-reason-block">
        <h4>Plain-English Decision</h4>
        <p>${escapeHtml(readableDecisionReason(row))}</p>
        ${auditList("Key Evidence", decisionReasonHighlights(row).slice(0, 5))}
      </div>
    </section>
    <section class="decision-review-grid">
      <div>
        <h4>OpenStocks Brain Review</h4>
        ${formattedLlmReasonHtml(llm, audit)}
      </div>
      <div>
        <h4>Timeline</h4>
        <ol class="decision-timeline">
          ${timeline.map(([label, text]) => `<li><strong>${escapeHtml(label)}</strong><span>${escapeHtml(text)}</span></li>`).join("")}
        </ol>
      </div>
    </section>
    ${preFilterHtml(audit, context, market)}
    ${riskGateHtml(audit, market)}
    ${fullSpectrumHtml(full, market)}
  `;
}

function normalizedScore(value) {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return 50;
  if (numeric >= 0 && numeric <= 100) return numeric;
  return Math.max(0, Math.min(100, (numeric + 1) * 50));
}

function scoreRadarSvg(metrics = []) {
  const points = metrics.map((metric, index) => {
    const angle = (-90 + index * (360 / metrics.length)) * (Math.PI / 180);
    const radius = 22 + (Math.max(0, Math.min(100, metric.value)) / 100) * 54;
    return {
      ...metric,
      x: 90 + Math.cos(angle) * radius,
      y: 90 + Math.sin(angle) * radius,
      lx: 90 + Math.cos(angle) * 83,
      ly: 90 + Math.sin(angle) * 83,
    };
  });
  const polygon = points.map((point) => `${point.x},${point.y}`).join(" ");
  return `<div class="score-radar" role="img" aria-label="Decision score breakdown radar">
    <svg viewBox="0 0 180 180" aria-hidden="true">
      <circle cx="90" cy="90" r="70"></circle>
      <circle cx="90" cy="90" r="44"></circle>
      <line x1="90" y1="20" x2="90" y2="160"></line>
      <line x1="20" y1="90" x2="160" y2="90"></line>
      <polygon points="${polygon}"></polygon>
      ${points.map((point) => `<text x="${point.lx}" y="${point.ly}">${escapeHtml(point.label)}</text>`).join("")}
    </svg>
    <div class="radar-legend">
      ${metrics.map((metric) => `<span><b>${escapeHtml(metric.label)}</b>${fmtNumber(metric.value)}%</span>`).join("")}
    </div>
  </div>`;
}

function formattedLlmReasonHtml(llm = {}, audit = {}) {
  const sections = [];
  const reason = llm.reason || llm.summary || audit.action_reason || "";
  if (reason) sections.push(["Conclusion", reason]);
  if (Array.isArray(llm.evidence) && llm.evidence.length) sections.push(["Technical analysis", llm.evidence.slice(0, 5).join(" ")]);
  if (Array.isArray(llm.risk_checks) && llm.risk_checks.length) sections.push(["Risk assessment", llm.risk_checks.slice(0, 5).join(" ")]);
  if (Array.isArray(llm.data_gaps) && llm.data_gaps.length) sections.push(["Data gaps", llm.data_gaps.slice(0, 5).join(" ")]);
  if (!sections.length) {
    return `<div class="empty-state product-empty"><strong>No LLM narrative captured</strong><span>This decision still used deterministic gates and safe policy checks.</span></div>`;
  }
  return `<div class="llm-formatted-review">
    ${sections.map(([title, text]) => `<article><h5>${escapeHtml(title)}</h5><p>${escapeHtml(shortValue(text, 520))}</p></article>`).join("")}
  </div>`;
}

function ideaLifecycle(row = {}) {
  const status = String(row.lifecycle_status || row.status || "active").toLowerCase();
  const highest = String(row.highest_target_hit || "NONE").toUpperCase();
  const state = row.signal_state || {};
  if (status === "stopped" || String(row.status || "").toUpperCase() === "STOP_HIT") {
    return { label: "Stop hit", className: "negative", note: "Idea invalidated by stop" };
  }
  if (status === "expired" || String(row.status || "").toUpperCase() === "EXPIRED") {
    return { label: "Expired", className: "warning", note: "Timeline is over" };
  }
  if (highest === "T3" || status === "target_3_hit") return { label: "T3 hit", className: "positive", note: "Final target reached" };
  if (highest === "T2" || status === "target_2_hit") return { label: "T2 hit", className: "positive", note: "Second target reached" };
  if (highest === "T1" || status === "target_1_hit") return { label: "T1 hit", className: "positive", note: "First target reached" };
  if (String(row.signal_type || "").toUpperCase() === "BUY" && ["active", "monitoring"].includes(status)) {
    return {
      label: state.trade_state_label || row.display_signal || "Active Buy",
      className: state.class_name || "open",
      note: state.fresh_action_label || "Tracking toward targets",
    };
  }
  if (status === "watch" || String(row.signal_type || "").toUpperCase() === "WATCH") return { label: "Watch", className: "warning", note: "Not actionable yet" };
  return { label: "Active", className: "open", note: "Tracking toward targets" };
}

function ideaTimelineText(row = {}) {
  const lifecycle = ideaLifecycle(row);
  if (lifecycle.label === "Expired" || lifecycle.label === "Stop hit" || lifecycle.label.includes("T3")) return lifecycle.note;
  const days = Number(row.days_to_expiry ?? row.timeline?.days_left);
  if (Number.isFinite(days)) return days <= 0 ? "expires today" : `${days} day${days === 1 ? "" : "s"} left`;
  if (row.expires_at) return `expires ${fmtDate(row.expires_at)}`;
  return row.timeline?.max_days ? `${row.timeline.max_days} day plan` : "timeline pending";
}

function targetLadderHtml(row = {}, market = "IN", compact = false) {
  const targets = Array.isArray(row.target_status) && row.target_status.length
    ? row.target_status
    : (Array.isArray(row.targets) ? row.targets : []).slice(0, 3).map((target, index) => ({
        label: target?.label || `T${index + 1}`,
        price: target?.price ?? target,
        hit: false,
      }));
  if (!targets.length) return `<div class="target-ladder empty">No targets published yet</div>`;
  return `<div class="target-ladder ${compact ? "compact" : ""}">
    ${targets.slice(0, 3).map((target) => {
      const hit = Boolean(target.hit);
      return `<span class="${hit ? "hit" : "pending"}">
        <small>${escapeHtml(target.label || "-")}</small>
        <strong>${fmtMarketMoney(target.price, market)}</strong>
        <em>${hit ? "hit" : escapeHtml(target.probability_label || "pending")}</em>
      </span>`;
    }).join("")}
  </div>`;
}

function renderOrders(rows) {
  const body = byId("orders-body");
  if (!rows.length) {
    body.innerHTML = emptyTableRow(
      9,
      `No ${activeMarketLabel()} orders yet`,
      "Orders appear after a paper/live idea is executed or when a risk exit, target, or stop is triggered.",
      "Open Positions",
      "positions",
    );
    return;
  }
  body.innerHTML = rows
    .slice(0, 120)
    .map((row) => {
      const side = String(row.side || "").toLowerCase();
      const exit = exitPlanFromOrder(row);
      const market = rowMarket(row);
      return `<tr>
        <td>${fmtTime(row.ts)}</td>
        <td><span class="tag ${side}">${escapeHtml(row.side)}</span></td>
        <td><strong>${escapeHtml(displayValue(row.symbol, "Symbol"))}</strong></td>
        <td>${escapeHtml(displayValue(row.strategy, "Strategy pending"))}</td>
        <td class="num">${row.qty}</td>
        <td class="num">${fmtMarketMoney(row.price, market)}</td>
        <td class="num">${fmtMarketMoney(row.notional, market)}</td>
        <td>${escapeHtml(row.status)}</td>
        <td>${exitPlanMini(exit, market)}</td>
      </tr>`;
    })
    .join("");
  bindRowDetails(body, rows.slice(0, 120), "Order");
}

function bindRowDetails(body, rows, title) {
  [...body.querySelectorAll("tr")].forEach((tr, index) => {
    const row = rows[index];
    if (!row) return;
    tr.addEventListener("click", (event) => {
      if (event.target.closest("button, a, input, select, textarea")) return;
      showDetails(title, row);
    });
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
  if (Array.isArray(value)) {
    return `<section class="audit-section">
      <h4>Summary</h4>
      <p>${escapeHtml(value.length ? `${value.length} records are available. Open the matching tab to work with the list.` : "No records available.")}</p>
    </section>
    <details class="raw-audit"><summary>Technical raw data</summary><pre>${escapeHtml(JSON.stringify(value, null, 2))}</pre></details>`;
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
    .map(([key, item]) => `<details class="raw-audit"><summary>${escapeHtml(humanLabel(key))}</summary><pre>${escapeHtml(prettyJson(item))}</pre></details>`)
    .join("");
  return `<section class="audit-section">
    <h4>Readable Summary</h4>
    <div class="detail-list">${rows || `<div><span>Status</span><strong>No summary fields available</strong></div>`}</div>
  </section>
  ${jsonBlocks}
  <details class="raw-audit"><summary>Full technical data</summary><pre>${escapeHtml(JSON.stringify(value, null, 2))}</pre></details>`;
}

function suggestionDetailHtml(row) {
  const audit = parseJsonObject(row.details_json);
  const context = audit.context || {};
  const market = rowMarket(row);
  const displaySignal = row.display_signal || row.suggestion;
  const latestSystemAction = row.latest_system_action ? String(row.latest_system_action).toUpperCase() : "";
  const whyChanged = row.why_changed || row.signal_state?.why_changed || {};
  const whyRows = [
    whyChanged.summary ? `Current State: ${whyChanged.summary}` : null,
    whyChanged.original_buy_reason ? `Original BUY: ${whyChanged.original_buy_reason}` : null,
    whyChanged.latest_monitor_reason ? `Latest Monitor: ${whyChanged.latest_monitor_reason}` : null,
  ].filter(Boolean);
  return `
    ${auditHero({
      label: "Suggestion",
      symbol: row.symbol,
      action: displaySignal,
      status: `${row.confluence}/26 ${row.tier || ""}`,
      meta: `${fmtMarketMoney(row.price, market)} · ${MARKET_LABELS[market] || market} · combined ${fmtNumber(row.combined_score)}`,
    })}
    <section class="audit-section">
      <h4>Why Suggested</h4>
      <p>${escapeHtml(row.display_reason || readableDecisionReason(row))}</p>
      ${auditList("Main Reasons", decisionReasonHighlights(row))}
      <div class="audit-chips">
        <span>Fresh action: ${escapeHtml(row.fresh_action_label || "-")}</span>
        <span>Setup: ${escapeHtml(row.setup_bucket_label || "-")}</span>
        <span>Execution: ${escapeHtml(row.execution_state_label || "Signal Only")}</span>
        ${latestSystemAction ? `<span>Latest engine: ${escapeHtml(latestSystemAction)}</span>` : ""}
        <span>Readiness: ${escapeHtml(row.decision_readiness || "-")}</span>
        <span>Strategy: ${escapeHtml(row.strategy || "-")}</span>
        <span>Institutional: ${escapeHtml(flowBiasText(row.institutional_bias))}</span>
      </div>
    </section>
    ${whyRows.length ? auditList("Why Changed", whyRows) : ""}
    ${exitPlanHtml(row.exit_plan, market)}
    ${scoreBreakdownHtml(audit.score_breakdown)}
    ${marketContextHtml(context, market)}
    ${fullSpectrumHtml(context.full_spectrum_analysis, market)}
  `;
}

function positionDetailHtml(row) {
  const pnl = (Number(row.market_price) - Number(row.avg_price)) * Number(row.qty);
  const summary = row.position_summary || {};
  const market = rowMarket(row);
  return `
    ${auditHero({
      label: "Position",
      symbol: row.symbol,
      action: summary.recommended_action || (pnl >= 0 ? "OPEN" : "WATCH"),
      status: summary.classification || row.strategy || "-",
      meta: `${row.qty} qty · ${fmtMarketMoney(pnl, market)} unrealized`,
    })}
    ${positionSummaryHtml(summary)}
    ${objectCardsHtml("Position", {
      qty: row.qty,
      avg_price: fmtMarketMoney(row.avg_price, market),
      market_price: fmtMarketMoney(row.market_price, market),
      market_value: fmtMarketMoney(Number(row.market_price) * Number(row.qty), market),
      unrealized_pnl: fmtMarketMoney(pnl, market),
    })}
    ${exitPlanHtml(row.exit_plan, market)}
    <section class="audit-section">
      <h4>Full Position JSON</h4>
      <pre>${escapeHtml(JSON.stringify(row, null, 2))}</pre>
    </section>
  `;
}

function positionSummaryHtml(summary = {}) {
  const flags = summary.active_flags || [];
  return `
    <section class="audit-section">
      <h4>Rules Summary</h4>
      <div class="audit-cards">
        <div class="audit-card"><span>Classification</span><strong>${escapeHtml(summary.classification || "-")}</strong><small>${escapeHtml(summary.symbol || "")}</small></div>
        <div class="audit-card"><span>Overall Score</span><strong>${fmtPct(summary.overall_score_pct ?? 0)}</strong><small>${escapeHtml(summary.overall_grade || "-")} production readiness</small></div>
        <div class="audit-card"><span>Entry / MTF / Delivery</span><strong>${escapeHtml(`${summary.entry_grade || "-"} / ${summary.mtf_grade || "-"} / ${summary.delivery_bias || "-"}`)}</strong><small>effective ${escapeHtml(summary.effective_entry_grade || "-")}</small></div>
        <div class="audit-card"><span>Sentiment</span><strong>${escapeHtml(summary.sentiment_status === "DATA_MISSING" ? "Awaiting news" : fmtNumber(summary.sentiment_score))}</strong><small>0.0 is not neutral</small></div>
        <div class="audit-card"><span>Price</span><strong>${escapeHtml(summary.price_label || "-")}</strong><small>${escapeHtml(summary.price_source || "-")} · ${escapeHtml(summary.price_timestamp || "-")}</small></div>
        <div class="audit-card"><span>Flags</span><strong>${escapeHtml(flags.length ? flags.join(", ") : "CLEAR")}</strong><small>hard/soft rule state</small></div>
        <div class="audit-card"><span>Action</span><strong>${escapeHtml(summary.recommended_action || "-")}</strong><small>${escapeHtml(summary.reason || "-")}</small></div>
      </div>
    </section>
  `;
}

function decisionDetailHtml(row) {
  const audit = parseJsonObject(row.details_json);
  const context = audit.context || {};
  const llm = audit.llm_output || null;
  const exit = exitPlanFromAudit(audit);
  const market = rowMarket(row);
  return `
    ${auditHero({
      label: "Decision audit",
      symbol: row.symbol,
      action: row.action,
      status: audit.decision_path || row.strategy || "-",
      meta: `${fmtNumber(Number(row.confidence) * 100)}% confidence · ${fmtMarketMoney(row.price, market)}`,
    })}
    <section class="audit-section">
      <h4>Why ${escapeHtml(row.action)}</h4>
      <p>${escapeHtml(readableDecisionReason(row))}</p>
      ${auditList("Main Reasons", decisionReasonHighlights(row))}
      <div class="audit-chips">
        <span>Strategy: ${escapeHtml(row.strategy || context.best_strategy?.name || "-")}</span>
        <span>Path: ${escapeHtml(humanLabel(audit.decision_path || "-"))}</span>
        <span>At: ${escapeHtml(fmtTime(row.ts))}</span>
      </div>
    </section>
    ${preFilterHtml(audit, context, market)}
    ${exitPlanHtml(exit, market)}
    ${scoreBreakdownHtml(audit.score_breakdown)}
    ${llm ? llmOutputHtml(llm, audit) : ""}
    ${llmPayloadHtml(audit, context)}
    ${riskGateHtml(audit, market)}
    ${marketContextHtml(context, market)}
    ${fullSpectrumHtml(context.full_spectrum_analysis, market)}
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
  const market = rowMarket(row);
  return `
    ${auditHero({
      label: "Order audit",
      symbol: row.symbol,
      action: row.side,
      status: row.status,
      meta: `${row.qty} qty · ${fmtMarketMoney(row.notional, market)}`,
    })}
    <section class="audit-section">
      <h4>Why Order ${escapeHtml(row.status)}</h4>
      <p>${escapeHtml(readableOrderReason(row))}</p>
      <div class="audit-chips">
        <span>Strategy: ${escapeHtml(row.strategy || "-")}</span>
        <span>Price: ${fmtMarketMoney(row.price, market)}</span>
        <span>Time: ${escapeHtml(fmtTime(row.ts))}</span>
      </div>
    </section>
    ${objectCardsHtml("Execution Sizing", execution.sizing)}
    ${objectCardsHtml("Execution Risk Checks", execution.risk_checks || execution.daily_loss)}
    ${exitPlanHtml(exit, market)}
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

function exitPlanMini(exit, market = "IN") {
  if (!exit || !Object.keys(exit).length) return `<span class="muted">pending</span>`;
  const t1 = exit.target_1 || {};
  return `<span class="exit-mini">SL ${fmtMarketMoney(exit.stop_loss, market)} · T1 ${fmtMarketMoney(t1.price, market)}</span>`;
}

function exitPlanHtml(exit, market = "IN") {
  if (!exit || !Object.keys(exit).length) return "";
  return `<section class="audit-section exit-plan">
    <h4>Exit Plan</h4>
    <div class="audit-cards">
      <div class="audit-card"><span>When</span><strong>${escapeHtml(exit.horizon || "swing_3_to_7_days")}</strong><small>review every cycle</small></div>
      <div class="audit-card"><span>Entry Zone</span><strong>${escapeHtml(formatZone(exit.entry_zone, market))}</strong><small>avoid chasing outside plan</small></div>
      <div class="audit-card"><span>Hard Stop</span><strong class="negative">${fmtMarketMoney(exit.stop_loss, market)}</strong><small>exit if invalidated</small></div>
      <div class="audit-card"><span>Target 1</span><strong class="positive">${fmtMarketMoney(exit.target_1?.price, market)}</strong><small>R:R ${escapeHtml(exit.target_1?.rr ?? "-")}</small></div>
      <div class="audit-card"><span>Target 2</span><strong class="positive">${fmtMarketMoney(exit.target_2?.price, market)}</strong><small>R:R ${escapeHtml(exit.target_2?.rr ?? "-")}</small></div>
      <div class="audit-card"><span>Target 3</span><strong class="positive">${fmtMarketMoney(exit.target_3?.price, market)}</strong><small>${escapeHtml(exit.target_3?.rr ?? "-")}</small></div>
    </div>
    <p>${escapeHtml(exit.plan || "-")}</p>
    ${objectCardsHtml("Invalidation", exit.invalidation)}
    ${auditList("Exit Monitoring", exit.monitoring_checklist)}
  </section>`;
}

function formatZone(zone, market = "IN") {
  if (!Array.isArray(zone) || !zone.length) return "-";
  return zone.length === 1
    ? fmtMarketMoney(zone[0], market)
    : `${fmtMarketMoney(zone[0], market)} - ${fmtMarketMoney(zone[zone.length - 1], market)}`;
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
      <div class="audit-card"><span>Deterministic Score</span><strong>${fmtPct(score.score_percent ?? ((Number(score.combined || 0) + 1) * 50))}</strong><small>${escapeHtml(score.score_percent_note || "50% is neutral before hard gates")}</small></div>
    </div>
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

function preFilterHtml(audit, context, market = "IN") {
  const pre = audit.pre_filter || context.pre_filter || audit.risk_gates?.pre_filter || {};
  const gateContext = audit.risk_gates?.decision_gate_context || {};
  const gates = gateContext.evaluated_gates || pre.gates || gateContext.failed_gates || [];
  if (!gates.length) return "";
  return `<section class="audit-section">
    <h4>Pre-Filter Gates</h4>
    <div class="audit-cards">
      ${gates.map((gate) => `<div class="audit-card">
        <span>${escapeHtml(humanLabel(gate.gate || "gate"))}</span>
        <strong class="${gate.passed === false ? "negative" : "positive"}">${gate.passed === false ? "needs attention" : "clear"}</strong>
        <small>${escapeHtml(gate.passed === false ? humanizeGateFailure(gate, market) : gateValueText(gate.gate, gate.value, market) || "passed")}</small>
      </div>`).join("")}
    </div>
    ${pre.elimination_reason ? `<p class="negative">${escapeHtml(reasonFromSnakeCase(pre.elimination_reason))}</p>` : ""}
  </section>`;
}

function llmOutputHtml(llm, audit) {
  const admin = Boolean(state.auth?.admin);
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
        <strong>${admin ? escapeHtml(audit.model || llm.model || "-") : "OpenStocks Brain"}</strong>
        <small>${admin ? `${escapeHtml(audit.provider || llm.provider || "-")} · ` : ""}${escapeHtml(audit.analysis_mode || llm.analysis_mode || "single_context")}</small>
      </div>
      <div class="audit-card">
        <span>Confidence Gate</span>
        <strong>${audit.confidence_gate?.passed ? "passed" : "not passed"}</strong>
        <small>minimum ${fmtNumber(Number(audit.confidence_gate?.minimum_required || 0) * 100)}%</small>
      </div>
    </div>
    ${admin ? objectCardsHtml("LLM Routing", {
      configured_provider: audit.configured_provider,
      configured_model: audit.configured_model,
      selected_provider: audit.provider,
      selected_model: audit.model,
      analysis_mode: audit.analysis_mode,
      rolling_context: audit.rolling_context,
    }) : ""}
    ${admin ? auditList("Model Attempts", (audit.model_attempts || []).map((item) => `${item.status}: ${item.provider}/${item.model} ${item.latency_ms || 0}ms ${item.error || ""}`)) : ""}
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

function llmPayloadHtml(audit, context = {}) {
  if (!state.auth?.admin) return "";
  const payload = audit.llm_prompt_audit || audit.llm_payload_audit || null;
  const selection = context.llm_primary_selection || {};
  if (!payload) {
    return `<section class="audit-section">
      <h4>LLM Payload</h4>
      <div class="audit-cards">
        <div class="audit-card"><span>Status</span><strong>${selection.selected ? "not captured" : "not sent"}</strong><small>${selection.selected ? "This older decision predates payload capture." : "This symbol was not selected for the LLM lane in this cycle."}</small></div>
        <div class="audit-card"><span>Candidate Limit</span><strong>${escapeHtml(selection.candidate_limit ?? "-")}</strong><small>${selection.required ? "primary LLM required" : "LLM not required"}</small></div>
      </div>
    </section>`;
  }
  const sections = Array.isArray(payload.included_sections) ? payload.included_sections.join(", ") : "-";
  return `<section class="audit-section">
    <h4>LLM Payload</h4>
    <div class="audit-cards">
      <div class="audit-card"><span>Market</span><strong>${escapeHtml(payload.market_region || "-")}</strong><small>${escapeHtml(payload.currency || "-")} context</small></div>
      <div class="audit-card"><span>Input Estimate</span><strong>${fmtNumber(payload.estimated_input_tokens)}</strong><small>approx tokens from prompt chars</small></div>
      <div class="audit-card"><span>Context Size</span><strong>${fmtNumber(payload.context_chars)}</strong><small>${fmtNumber(payload.system_prompt_chars)} system chars</small></div>
      <div class="audit-card"><span>Hash</span><strong>${escapeHtml(shortValue(payload.context_sha256, 14))}</strong><small>exact user context checksum</small></div>
    </div>
    <p class="audit-formula">Sections sent: ${escapeHtml(sections)}</p>
    <details class="raw-audit">
      <summary>Exact LLM system prompt</summary>
      <pre>${escapeHtml(payload.system_prompt || "")}</pre>
    </details>
    <details class="raw-audit">
      <summary>Exact LLM user context JSON</summary>
      <pre>${escapeHtml(JSON.stringify(payload.user_context || {}, null, 2))}</pre>
    </details>
  </section>`;
}

function riskGateHtml(audit, market = "IN") {
  const gates = audit.risk_gates || {};
  const context = audit.context || {};
  const full = decisionFullSpectrum(audit);
  const gateContext = gates.decision_gate_context || context.decision_gate_context || {};
  const riskLimits = context.risk_limits || {};
  const failed = failedGatesFromAudit(audit, context);
  const scorecard = gates.institutional_scorecard || full.institutional_scorecard || {};
  const scorecardStatus =
    scorecard.buy_ready === true ? "clear" : scorecard.buy_ready === false ? "not clear" : "not evaluated";
  const scorecardClass = scorecard.buy_ready === true ? "positive" : scorecard.buy_ready === false ? "negative" : "";
  const buyThreshold = gateContext.buy_threshold ?? gates.buy_combined_threshold;
  const currentOpenPositions = gates.current_open_positions ?? riskLimits.current_open_positions;
  const maxPositions = gates.max_positions ?? riskLimits.max_positions;
  const failedScorecardItems = scorecard.failed || scorecard.must_pass_failed || [];
  const llmSelected = gates.llm_deep_review_selected ?? context.llm_primary_selection?.selected ?? String(audit.decision_path || "").startsWith("llm");
  const llmLimit = gates.llm_candidate_limit ?? riskLimits.llm_candidate_limit ?? context.universe_scan?.llm_candidate_limit;
  return `<section class="audit-section">
    <h4>Risk Gates</h4>
    <div class="audit-cards">
      <div class="audit-card"><span>BUY Threshold</span><strong>${fmtNumber(buyThreshold)}</strong><small>combined score required before a fresh long</small></div>
      <div class="audit-card"><span>Open Position</span><strong>${(gates.has_existing_position ?? Number(context.position?.qty || 0) > 0) ? "yes" : "no"}</strong><small>${fmtNumber(currentOpenPositions)} / ${fmtNumber(maxPositions)} positions used</small></div>
      <div class="audit-card"><span>Institutional Gate</span><strong class="${scorecardClass}">${scorecardStatus}</strong><small>${escapeHtml(failedScorecardItems.map(reasonFromSnakeCase).join(" ") || "must-pass checks clear")}</small></div>
      <div class="audit-card"><span>LLM Review</span><strong>${llmSelected ? "selected" : "not selected"}</strong><small>candidate limit ${escapeHtml(llmLimit ?? "-")}</small></div>
    </div>
	    ${auditList("Failed Gates", failed.length ? failed.map((gate) => humanizeGateFailure(gate, market)) : ["No hard gate failed."])}
    <details class="raw-audit"><summary>Technical risk-gate data</summary><pre>${escapeHtml(JSON.stringify(gates, null, 2))}</pre></details>
  </section>`;
}

function marketContextHtml(context, market = "IN") {
  if (!context || !Object.keys(context).length) return "";
  return `<section class="audit-section">
    <h4>Market Context Used</h4>
    <div class="audit-cards">
      <div class="audit-card"><span>Quote</span><strong>${fmtMarketMoney(context.quote?.price, market)}</strong><small>${escapeHtml(context.quote?.source || "-")}</small></div>
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

function fullSpectrumHtml(analysis, market = "IN") {
  if (!analysis || typeof analysis !== "object") return "";
  const confluence = analysis.confluence_score || {};
  const trend = analysis.trend_context || {};
  const tradePlan = analysis.trade_plan || {};
  const risk = analysis.risk_overrides || {};
  const liquidity = analysis.liquidity_profile || {};
  const conflicts = analysis.signal_conflicts || {};
  const scorecard = analysis.institutional_scorecard || {};
  const timeframeData = analysis.timeframe_data || {};
  const backtest = analysis.backtest_snapshot || {};
  const bestBacktest = backtest.best_strategy_backtest || {};
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
      <div class="audit-card"><span>Timeframes</span><strong>${fmtNumber(timeframeData.daily_candle_count)}D/${fmtNumber(timeframeData.weekly_candle_count)}W</strong><small>${escapeHtml(timeframeData.analysis_source || "-")}</small></div>
      <div class="audit-card"><span>Best Backtest</span><strong>${escapeHtml(bestBacktest.strategy || "-")}</strong><small>${fmtNumber(bestBacktest.expectancy_pct)}% exp · ${fmtNumber(bestBacktest.trades)} trades</small></div>
      <div class="audit-card"><span>Confluence</span><strong>${escapeHtml(confluence.total ?? "-")}/26</strong><small>${escapeHtml(confluence.tier || "-")}</small></div>
      <div class="audit-card"><span>Institutional Score</span><strong>${escapeHtml(scorecard.total_score ?? "-")}/100</strong><small>${escapeHtml(`${scorecard.grade || "-"} · ${scorecard.buy_ready ? "buy ready" : "not ready"}`)}</small></div>
      <div class="audit-card"><span>Daily Trend</span><strong>${escapeHtml(trend.daily || "-")}</strong><small>${escapeHtml(trend.structure || "-")}</small></div>
      <div class="audit-card"><span>Signal Direction</span><strong>${escapeHtml(tradePlan.direction || "-")}</strong><small>${escapeHtml(tradePlan.horizon || "-")}</small></div>
      <div class="audit-card"><span>Risk Overrides</span><strong>${escapeHtml(risk.no_new_longs ? "no new longs" : "clear")}</strong><small>${escapeHtml((risk.flags || []).join(", ") || "-")}</small></div>
      <div class="audit-card"><span>Liquidity</span><strong>${escapeHtml(liquidity.liquidity_tier || "-")}</strong><small>${fmtMarketMoney(liquidity.avg_traded_value_20, market)} avg value</small></div>
      <div class="audit-card"><span>Conflicts</span><strong>${escapeHtml(conflicts.severity || "-")}</strong><small>${escapeHtml((conflicts.conflicts || []).join(", ") || "-")}</small></div>
    </div>
    ${objectCardsHtml("Stage Analysis", stage)}
    ${objectCardsHtml("Entry Quality", entry)}
    ${objectCardsHtml("Breakout Quality", breakout)}
    ${objectCardsHtml("Price-Volume Divergence", divergence)}
    ${objectCardsHtml("Multi-Timeframe Alignment", alignment)}
    ${objectCardsHtml("Timeframe Data", timeframeData)}
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
    <p>${escapeHtml(readableDecisionReason(decision))}</p>
    ${auditList("Main Reasons", decisionReasonHighlights(decision))}
    <details class="raw-audit"><summary>Linked decision raw data</summary><pre>${escapeHtml(JSON.stringify(decision, null, 2))}</pre></details>
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

function emptyTableRow(colspan, title, message, actionLabel = "", actionView = "") {
  return `<tr class="empty-table-row"><td colspan="${colspan}">
    <div class="empty-table-state">
      <strong>${escapeHtml(title)}</strong>
      <span>${escapeHtml(message)}</span>
      ${actionLabel && actionView ? `<button type="button" data-view-jump="${escapeHtml(actionView)}">${escapeHtml(actionLabel)}</button>` : ""}
    </div>
  </td></tr>`;
}

function emptyBlock(title, message, actionLabel = "", actionView = "") {
  return `<div class="empty-state product-empty">
    <strong>${escapeHtml(title)}</strong>
    <span>${escapeHtml(message)}</span>
    ${actionLabel && actionView ? `<button type="button" data-view-jump="${escapeHtml(actionView)}">${escapeHtml(actionLabel)}</button>` : ""}
  </div>`;
}

function prettyAgentDetails(details = {}) {
  if (!details || typeof details !== "object" || !Object.keys(details).length) return "-";
  const preferred = [
    "phase",
    "duration_seconds",
    "symbols_checked",
    "action_counts",
    "decision_paths",
    "llm_error_count",
    "provider",
    "market",
    "market_region",
    "quote_count",
    "cached_symbols",
    "fetched_symbols",
    "errors",
  ];
  const parts = [];
  for (const key of preferred) {
    if (!(key in details)) continue;
    const value = details[key];
    if (value === null || value === undefined || value === "") continue;
    const rendered = typeof value === "object" ? JSON.stringify(value) : String(value);
    parts.push(`${humanLabel(key)}: ${rendered}`);
  }
  if (!parts.length) {
    for (const [key, value] of Object.entries(details).slice(0, 3)) {
      const rendered = typeof value === "object" ? JSON.stringify(value) : String(value);
      parts.push(`${humanLabel(key)}: ${rendered}`);
    }
  }
  return parts.join(" · ");
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

function drawEquity(rows, market = state.activeMarket) {
  const canvas = byId("equity-chart");
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  const dpr = window.devicePixelRatio || 1;
  const width = canvas.clientWidth || 720;
  const height = canvas.clientHeight || 320;
  canvas.width = width * dpr;
  canvas.height = height * dpr;
  ctx.scale(dpr, dpr);
  ctx.clearRect(0, 0, width, height);

  const pad = 28;
  ctx.strokeStyle = "rgba(126, 146, 170, 0.18)";
  ctx.lineWidth = 1;
  for (let i = 0; i < 5; i += 1) {
    const y = pad + ((height - pad * 2) * i) / 4;
    ctx.beginPath();
    ctx.moveTo(pad, y);
    ctx.lineTo(width - pad, y);
    ctx.stroke();
  }
  for (let i = 0; i < 7; i += 1) {
    const x = pad + ((width - pad * 2) * i) / 6;
    ctx.beginPath();
    ctx.moveTo(x, pad);
    ctx.lineTo(x, height - pad);
    ctx.stroke();
  }

  if (rows.length < 2) {
    const baseline = rows.length ? Number(rows[0].equity) : null;
    ctx.strokeStyle = "rgba(0, 201, 139, 0.3)";
    ctx.setLineDash([6, 8]);
    ctx.beginPath();
    ctx.moveTo(pad, height / 2);
    ctx.lineTo(width - pad, height / 2);
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = "#7f8da0";
    ctx.font = "13px system-ui";
    ctx.fillText(
      baseline
        ? `${MARKET_LABELS[normalizeUiMarket(market)] || market} baseline ${fmtMarketMoney(baseline, market)}. Performance curve starts after tracked positions move.`
        : "Performance curve starts after portfolio snapshots arrive.",
      pad,
      height / 2 - 12,
    );
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
  const up = values[values.length - 1] >= values[0];
  const gradient = ctx.createLinearGradient(0, 0, width, 0);
  gradient.addColorStop(0, up ? "#00c98b" : "#ff6470");
  gradient.addColorStop(1, up ? "#76f7bf" : "#ff9aa2");
  ctx.strokeStyle = gradient;
  ctx.lineWidth = 2;
  ctx.stroke();

  ctx.fillStyle = "#8b98aa";
  ctx.font = "12px system-ui";
  ctx.fillText(fmtMarketMoney(max, market), pad, pad - 6);
  ctx.fillText(fmtMarketMoney(min, market), pad, height - 8);
}

async function postControl(path) {
  try {
    const response = await fetch(path, { method: "POST" });
    const payload = await response.json().catch(() => ({ detail: `HTTP ${response.status}` }));
    if (response.status === 401) {
      handleUnauthorized(payload.detail || "Session expired. Sign in again.");
      return;
    }
    if (!response.ok) {
      showDetails("Control Error", payload);
      const error = byId("error-box");
      error.hidden = false;
      error.textContent = payload.detail || `Control request failed: ${response.status}`;
      return;
    }
    render(payload);
    fetchCredits();
    if (state.auth?.admin) fetchLogs();
  } catch (error) {
    showBackendError(networkErrorMessage(error, "control request"), { path });
  }
}

function setActiveMarket(market, options = {}) {
  const next = normalizeUiMarket(market);
  state.activeMarket = next;
  updateMarketWorkspaceLabels();
  setAnalyzeMarket(next);
  if (options.rerender !== false && state.latest) {
    render(state.latest);
  }
}

function setAnalyzeMarket(market) {
  const next = String(market || "IN").toUpperCase() === "US" ? "US" : "IN";
  const marketInput = byId("analyze-market");
  const analyzeInput = byId("analyze-symbol");
  if (marketInput) marketInput.value = next;
  if (analyzeInput) analyzeInput.placeholder = next === "US" ? "Search US symbol, e.g. AAPL" : "Search India symbol, e.g. SUZLON";
  for (const tab of document.querySelectorAll(".market-tab")) {
    const active = tab.dataset.marketTab === next;
    tab.classList.toggle("active", active);
    tab.setAttribute("aria-selected", active ? "true" : "false");
  }
}

async function analyzeSymbol(event) {
  event.preventDefault();
  const input = byId("analyze-symbol");
  const marketSelect = byId("analyze-market");
  const button = byId("analyze-btn");
  const market = (marketSelect?.value || "IN").toUpperCase();
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
    byId("analyze-status").textContent = `analyzing ${market}:${symbol} · ${elapsed}s`;
  }, 1000);
  button.disabled = true;
  byId("analyze-status").textContent = `analyzing ${market}:${symbol}...`;
  byId("analyze-result").innerHTML = `<div class="empty-state">Running ${market} quote, candles, strategy, sentiment, risk gates, and OpenStocks Brain review if enabled...</div>`;
  try {
    const response = await fetch("/api/analyze-symbol", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ symbol, market, force_llm: true }),
      signal: controller.signal,
    });
    const payload = await response.json().catch(() => ({ detail: `HTTP ${response.status}` }));
    if (!response.ok) {
      byId("analyze-status").textContent = "analysis failed";
      byId("analyze-result").innerHTML = `<div class="error-box">${escapeHtml(payload.detail || "Analysis failed")}</div>`;
      return;
    }
    byId("analyze-status").textContent = `${payload.market || market}:${payload.symbol} analyzed`;
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
  const market = payload.market || "IN";
  const action = String(decision.action || "HOLD").toLowerCase();
  const details = decision.details || parseJsonObject(decision.details_json);
  const context = details.context || {};
  const full = decisionFullSpectrum(details);
  const path = details.decision_path || decision.strategy || "-";
  const news = payload.news || {};
  const fundamentals = payload.fundamentals || {};
  const referenceData = payload.reference_data || {};
  const headlines = news.headlines || [];
  const creditUsage = payload.credit_usage || {};
  const llmActivity = creditUsage.llm_activity || {};
  const beforeBalance = Number(creditUsage.before?.credit_balance || 0);
  const afterBalance = Number(creditUsage.after?.credit_balance || 0);
  const creditCharge = Math.max(beforeBalance - afterBalance, 0);
  const resolvedNote =
    payload.requested_symbol && payload.symbol && String(payload.requested_symbol).toUpperCase() !== String(payload.symbol).toUpperCase()
      ? `resolved from ${payload.requested_symbol}`
      : `${payload.market || "IN"} · ${payload.provider || "-"} · ${payload.candle_count || 0} candles`;
  const quote = payload.quote || context.quote || {};
  const dayPct = quoteDayPct({ price: decision.price || quote.price, close: quote.close || quote.prev_close });
  const high52 = firstFinite(quote.week_52_high, quote["52week_high"], quote.fifty_two_week_high, fundamentals.week_52_high, full.fundamental_quality?.week_52_high);
  const low52 = firstFinite(quote.week_52_low, quote["52week_low"], quote.fifty_two_week_low, fundamentals.week_52_low, full.fundamental_quality?.week_52_low);
  const ltp = Number(decision.price || quote.price || 0);
  const rangePct = Number.isFinite(high52) && Number.isFinite(low52) && high52 > low52
    ? Math.max(0, Math.min(100, ((ltp - low52) / (high52 - low52)) * 100))
    : 0;
  const metrics = [
    { label: "PE", value: firstPositiveFinite(full.fundamental_quality?.pe, fundamentals.pe), kind: "number", empty: "not reported by feed" },
    { label: "PB", value: firstPositiveFinite(full.fundamental_quality?.pb, fundamentals.pb), kind: "number", empty: "not reported by feed" },
    { label: "Market cap", value: firstPositiveFinite(full.fundamental_quality?.market_cap, fundamentals.market_cap), kind: "compact", empty: "not reported by feed" },
    { label: "Volume", value: firstFinite(quote.volume, fundamentals.volume, payload.volume), kind: "compact", empty: "not reported by feed" },
    { label: "52W high", value: high52, kind: "money", empty: "derived after candles load" },
    { label: "52W low", value: low52, kind: "money", empty: "derived after candles load" },
  ];
  const referenceNote = (referenceData.derived_from_candles || []).length
    ? `52-week levels derived from ${payload.candle_count || 0} candles.`
    : referenceData.source
      ? `Reference data: ${humanLabel(referenceData.source)}.`
      : "";
  byId("analyze-result").innerHTML = `
    <section class="analysis-result-shell">
      <header class="analysis-hero">
        <div class="decision-logo large">${escapeHtml(symbolInitials(payload.symbol || decision.symbol))}</div>
        <div>
          <span>${escapeHtml(resolvedNote)}</span>
          <h3>${escapeHtml(displayValue(payload.symbol || decision.symbol, "Symbol"))}</h3>
          <p>${escapeHtml(payload.company_name || decision.company_name || quote.company_name || fundamentals.company_name || "Symbol audit")}</p>
        </div>
        <div class="analysis-price">
          <strong>${fmtMarketMoney(decision.price || quote.price, market)}</strong>
          <span class="day-badge ${pnlClass(dayPct)}">${fmtPct(dayPct)}</span>
          <small>${escapeHtml(fmtTime(quote.asof || quote.ts))}</small>
        </div>
      </header>
      <div class="range-52w">
        <span>52W low ${fmtMarketMoney(low52, market)}</span>
        <div class="progress-track"><div style="width:${rangePct}%"></div></div>
        <span>52W high ${fmtMarketMoney(high52, market)}</span>
      </div>
      <div class="analysis-metric-strip">
        ${metrics.map((metric) => metricCardHtml(metric, market)).join("")}
      </div>
      ${referenceNote || referenceData.data_gaps?.length ? `<p class="analysis-data-note">${escapeHtml(referenceNote || "Some reference fields are not available from the connected feed.")}${referenceData.data_gaps?.length ? ` Missing: ${escapeHtml(referenceData.data_gaps.join(", "))}.` : ""}</p>` : ""}
      <div class="analysis-tabs" role="tablist" aria-label="Analysis result sections">
        <button class="active" type="button" data-analysis-tab="overview">Overview</button>
        <button type="button" data-analysis-tab="chart">Chart</button>
        <button type="button" data-analysis-tab="strategy">Strategy</button>
        <button type="button" data-analysis-tab="sentiment">Sentiment</button>
        <button type="button" data-analysis-tab="risk">Risk</button>
        <button type="button" data-analysis-tab="llm">LLM Review</button>
      </div>
      <div class="analysis-tab-panels">
        <section class="analysis-tab-panel active" data-analysis-panel="overview">
          <div class="manual-analysis-card">
            <div><span>Decision</span><strong><span class="tag ${action}">${escapeHtml(decision.action || "-")}</span></strong><small>${escapeHtml(path)}</small></div>
            <div><span>Confidence</span><strong>${fmtNumber(Number(decision.confidence || 0) * 100)}%</strong><small>policy gates still apply</small></div>
            <div><span>News Sentiment</span><strong class="${headlines.length ? pnlClass(news.score) : "muted"}">${headlines.length ? fmtNumber(news.score) : "Awaiting news"}</strong><small>${headlines.length ? `${headlines.length} items` : escapeHtml(news.note || "No verified news")}</small></div>
            <div><span>Credits Used</span><strong>${fmtCredits(creditCharge)}</strong><small>${fmtCredits(creditUsage.after?.daily_credits_remaining || 0)} left today</small></div>
          </div>
          <p>${escapeHtml(readableDecisionReason(decision))}</p>
        </section>
        <section class="analysis-tab-panel" data-analysis-panel="chart">
          ${miniPriceChartHtml(context.recent_candles_tail || [], market)}
        </section>
        <section class="analysis-tab-panel" data-analysis-panel="strategy">
          ${strategySignalsHtml(context.strategy_signals || []) || auditList("Strategy signals", decisionReasonHighlights(decision))}
        </section>
        <section class="analysis-tab-panel" data-analysis-panel="sentiment">
          ${headlines.length ? auditList("Latest News", headlines.slice(0, 6)) : `<p class="muted">${escapeHtml(news.note || "No recent verified news found from connected public feeds.")}</p>`}
        </section>
        <section class="analysis-tab-panel" data-analysis-panel="risk">
          ${preFilterHtml(details, context, market)}
          ${riskGateHtml(details, market)}
        </section>
        <section class="analysis-tab-panel" data-analysis-panel="llm">
          <button id="manual-detail-btn" type="button">Open Full Analysis</button>
          ${llmActivity.message ? `<p class="muted">${escapeHtml(llmActivity.message)}${llmActivity.latest_failure ? ` ${escapeHtml(llmActivity.latest_failure)}` : ""}</p>` : ""}
          ${details.llm_output ? formattedLlmReasonHtml(details.llm_output, details) : `<p class="muted">OpenStocks Brain evidence was not captured for this result. Re-run Analyze after checking credits and the user LLM provider/API key.</p>`}
          ${payload.provider_error ? `<p class="negative">${escapeHtml(payload.provider_error)}</p>` : ""}
        </section>
      </div>
    </section>
  `;
  byId("manual-detail-btn").addEventListener("click", () => showDetails("Manual Analysis", decision));
  bindAnalysisTabs();
  if (creditUsage.after) renderCreditSummary(creditUsage.after);
}

function bindAnalysisTabs() {
  const root = byId("analyze-result");
  if (!root) return;
  const buttons = root.querySelectorAll("[data-analysis-tab]");
  const panels = root.querySelectorAll("[data-analysis-panel]");
  buttons.forEach((button) => {
    button.addEventListener("click", () => {
      buttons.forEach((item) => item.classList.toggle("active", item === button));
      panels.forEach((panel) => panel.classList.toggle("active", panel.dataset.analysisPanel === button.dataset.analysisTab));
    });
  });
}

function metricCardHtml(metric, market = "IN") {
  const value = metric.value;
  let rendered = "-";
  if (Number.isFinite(Number(value))) {
    if (metric.kind === "money") rendered = fmtMarketMoney(value, market);
    else if (metric.kind === "compact") rendered = fmtCompact(value);
    else rendered = fmtNumber(value);
  } else if (value) {
    rendered = String(value);
  }
  return `<div>
    <span>${escapeHtml(metric.label)}</span>
    <strong class="${rendered === "-" ? "muted" : ""}">${escapeHtml(rendered)}</strong>
    ${rendered === "-" ? `<small>${escapeHtml(metric.empty || "data unavailable")}</small>` : ""}
  </div>`;
}

function miniPriceChartHtml(candles = [], market = "IN") {
  const closes = (candles || []).map((candle) => Number(candle.close ?? candle.price)).filter(Number.isFinite);
  if (closes.length < 2) {
    return emptyBlock("Chart history is building", "Price and volume chart appears after candles are available for this symbol.", "View Decisions", "decisions");
  }
  const min = Math.min(...closes);
  const max = Math.max(...closes);
  const width = 560;
  const height = 220;
  const range = max - min || 1;
  const points = closes.map((close, index) => {
    const x = (index / Math.max(closes.length - 1, 1)) * width;
    const y = height - ((close - min) / range) * (height - 22) - 11;
    return `${x},${y}`;
  }).join(" ");
  return `<div class="analysis-chart" role="img" aria-label="Recent price chart">
    <svg viewBox="0 0 ${width} ${height}" preserveAspectRatio="none">
      <polyline points="${points}"></polyline>
    </svg>
    <div class="analysis-chart-meta">
      <span>${closes.length} candles</span>
      <strong>${fmtMarketMoney(closes[closes.length - 1], market)}</strong>
      <span>range ${fmtMarketMoney(min, market)} - ${fmtMarketMoney(max, market)}</span>
    </div>
  </div>`;
}

function bindControls() {
  byId("start-btn").addEventListener("click", () => postControl("/api/control/start"));
  byId("stop-btn").addEventListener("click", () => postControl("/api/control/stop"));
  byId("run-btn").addEventListener("click", () => postControl("/api/control/run-once"));
  const dashboardRun = byId("dashboard-run-btn");
  if (dashboardRun) dashboardRun.addEventListener("click", () => postControl(state.auth?.admin ? "/api/control/run-once" : "/api/control/start"));
  const dashboardStop = byId("dashboard-stop-btn");
  if (dashboardStop) dashboardStop.addEventListener("click", () => postControl("/api/control/stop"));
  const paperDismiss = byId("paper-banner-dismiss");
  if (paperDismiss) {
    paperDismiss.addEventListener("click", () => {
      try {
        window.sessionStorage.setItem("openstocks-paper-banner-dismissed", "1");
      } catch {
        /* ignore storage failures */
      }
      const banner = byId("paper-mode-banner");
      if (banner) banner.hidden = true;
    });
  }
  const sidebarToggle = byId("sidebar-toggle-btn");
  initializeSidebarState();
  if (sidebarToggle) {
    sidebarToggle.addEventListener("click", (event) => {
      event.stopPropagation();
      toggleSidebar();
    });
  }
  byId("sidebar-backdrop")?.addEventListener("click", () => setSidebarOpen(false));
  byId("analyze-form").addEventListener("submit", analyzeSymbol);
  const globalSearch = byId("global-search-form");
  if (globalSearch) {
    globalSearch.addEventListener("submit", (event) => {
      event.preventDefault();
      const symbol = byId("global-symbol-search")?.value?.trim().toUpperCase();
      if (!symbol) return;
      setAnalyzeMarket(state.activeMarket);
      const input = byId("analyze-symbol");
      if (input) input.value = symbol;
      setView("analyze");
      input?.focus();
    });
  }
  const themeButton = byId("theme-toggle-btn");
  if (themeButton) {
    themeButton.addEventListener("click", () => applyTheme(document.body.dataset.theme === "dark" ? "light" : "dark"));
  }
  const creditPill = byId("credit-pill");
  const creditPopover = byId("credit-popover");
  if (creditPill && creditPopover) {
    creditPill.addEventListener("click", (event) => {
      event.stopPropagation();
      const nextHidden = !creditPopover.hidden;
      creditPopover.hidden = nextHidden;
      creditPill.setAttribute("aria-expanded", nextHidden ? "false" : "true");
    });
  }
  const creditClose = byId("credit-popover-close");
  if (creditClose && creditPopover) {
    creditClose.addEventListener("click", () => {
      creditPopover.hidden = true;
      creditPill?.setAttribute("aria-expanded", "false");
    });
  }
  const userMenuButton = byId("user-menu-btn");
  const userMenu = byId("user-menu-dropdown");
  if (userMenuButton && userMenu) {
    userMenuButton.addEventListener("click", (event) => {
      event.stopPropagation();
      const nextHidden = !userMenu.hidden;
      userMenu.hidden = nextHidden;
      userMenuButton.setAttribute("aria-expanded", nextHidden ? "false" : "true");
    });
  }
  document.querySelectorAll(".market-tab").forEach((button) => {
    button.addEventListener("click", () => setAnalyzeMarket(button.dataset.marketTab));
  });
  document.querySelectorAll(".market-workspace-tab").forEach((button) => {
    button.addEventListener("click", () => setActiveMarket(button.dataset.marketWorkspace));
  });
  byId("login-form").addEventListener("submit", login);
  byId("user-create-form").addEventListener("submit", createUser);
  byId("save-settings-btn").addEventListener("click", saveSettings);
  byId("save-provider-keys-btn").addEventListener("click", saveSettings);
  byId("reset-demo-btn").addEventListener("click", resetDemo);
  byId("test-llm-btn").addEventListener("click", testLlm);
  byId("upstox-connect-btn").addEventListener("click", connectUpstox);
  byId("my-upstox-token-save-btn").addEventListener("click", saveMyUpstoxToken);
  byId("my-kite-connect-btn").addEventListener("click", connectMyKite);
  byId("refresh-logs-btn").addEventListener("click", fetchLogs);
  const quoteFilter = byId("quote-filter");
  if (quoteFilter) {
    quoteFilter.value = "";
    state.quoteFilter = "";
    quoteFilter.addEventListener("input", () => {
      state.quoteFilter = quoteFilter.value || "";
      renderQuotes(filterRowsByMarket(state.latest?.quotes || [], state.activeMarket));
    });
  }
  byId("drawer-close").addEventListener("click", () => byId("detail-drawer").classList.remove("open"));
  for (const button of document.querySelectorAll(".nav-item")) {
    const navLabel = button.querySelector("span:not(.nav-icon)")?.textContent?.trim();
    if (navLabel) button.setAttribute("aria-label", navLabel);
    button.addEventListener("click", () => {
      if (isMobileSidebar()) setSidebarOpen(false);
      setView(button.dataset.view);
    });
  }
  for (const button of document.querySelectorAll("[data-view-jump]")) {
    button.addEventListener("click", () => setView(button.dataset.viewJump));
  }
  document.addEventListener("click", (event) => {
    const button = event.target.closest("[data-view-jump]");
    if (!button) return;
    byId("user-menu-dropdown")?.setAttribute("hidden", "");
    const popover = byId("credit-popover");
    if (popover) popover.hidden = true;
    setView(button.dataset.viewJump);
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") setSidebarOpen(false);
  });
  window.addEventListener("resize", () => {
    if (window.innerWidth > 767) setSidebarOpen(false);
    syncSidebarControls();
    syncResponsiveShellControls();
  });
  document.addEventListener("click", (event) => {
    if (!event.target.closest(".user-menu")) {
      const menu = byId("user-menu-dropdown");
      if (menu) menu.hidden = true;
      byId("user-menu-btn")?.setAttribute("aria-expanded", "false");
    }
    if (!event.target.closest("#credit-popover") && !event.target.closest("#credit-pill")) {
      const popover = byId("credit-popover");
      if (popover) popover.hidden = true;
      byId("credit-pill")?.setAttribute("aria-expanded", "false");
    }
  });
  for (const button of document.querySelectorAll(".settings-tab")) {
    button.addEventListener("click", () => setSettingsTab(button.dataset.settingsTab));
  }
  for (const button of document.querySelectorAll("[data-filter-group] [data-filter-value]")) {
    button.addEventListener("click", () => {
      const group = button.closest("[data-filter-group]")?.dataset.filterGroup;
      setPageFilter(group, button.dataset.filterValue);
    });
  }
  for (const tile of document.querySelectorAll(".kpi")) {
    tile.addEventListener("click", () => {
      const portfolio = state.latest?.portfolio || {};
      const target = tile.dataset.detailType;
      if (target === "positions-summary") {
        setView("positions");
        return;
      }
      if (target === "decision-summary") {
        setView("decisions");
        return;
      }
      if (target === "orders-summary") {
        setView("orders");
        return;
      }
      if (["portfolio", "cash", "invested", "pnl"].includes(target)) {
        setView("account");
        return;
      }
      showDetails("Portfolio", portfolio);
    });
  }
  for (const tile of document.querySelectorAll(".ops-card")) {
    tile.addEventListener("click", () => {
      const target = tile.dataset.detailType;
      if (!state.auth?.admin) {
        if (target === "feed-health" || target === "llm-health") {
          setView("account");
          return;
        }
        if (target === "risk-health") {
          setView("positions");
          return;
        }
        if (target === "opportunity-health") {
          setView("decisions");
          return;
        }
        if (target === "cycle-health") {
          setView("decisions");
          return;
        }
        if (target === "macro-health") {
          setView("decisions");
          return;
        }
      }
      if (target === "feed-health") {
        openSettingsTab("broker");
        return;
      }
      if (target === "llm-health") {
        openSettingsTab("ai");
        return;
      }
      if (target === "risk-health") {
        openSettingsTab("risk");
        return;
      }
      if (target === "opportunity-health") {
        showDetails("Opportunity Scan", state.latest?.opportunity_scan || {});
        return;
      }
      if (target === "cycle-health") {
        setView("logs");
        return;
      }
      const map = {
        "macro-health": {
          global: state.latest?.macro_context || {},
          institutional: state.latest?.institutional_context || {},
          market_breadth: state.latest?.market_breadth || {},
          sector_rotation: state.latest?.sector_rotation_context || {},
          upcoming_macro_events: state.latest?.upcoming_macro_events || [],
        },
      };
      showDetails("Global Risk", map[target]);
    });
  }
  byId("logout-btn").addEventListener("click", logout);
}

const SIDEBAR_COLLAPSED_STORAGE_KEY = "openstocks-sidebar-collapsed";

function isMobileSidebar() {
  return window.matchMedia("(max-width: 767px)").matches;
}

function readSidebarCollapsedPreference() {
  try {
    return window.localStorage.getItem(SIDEBAR_COLLAPSED_STORAGE_KEY) === "1";
  } catch {
    return false;
  }
}

function writeSidebarCollapsedPreference(collapsed) {
  try {
    window.localStorage.setItem(SIDEBAR_COLLAPSED_STORAGE_KEY, collapsed ? "1" : "0");
  } catch {
    /* ignore storage failures */
  }
}

function initializeSidebarState() {
  document.body.classList.remove("sidebar-open");
  document.body.classList.toggle("sidebar-collapsed", readSidebarCollapsedPreference());
  syncSidebarControls();
  syncResponsiveShellControls();
}

function toggleSidebar() {
  if (isMobileSidebar()) {
    setSidebarOpen(!document.body.classList.contains("sidebar-open"));
    return;
  }
  setSidebarCollapsed(!document.body.classList.contains("sidebar-collapsed"));
}

function setSidebarCollapsed(collapsed) {
  document.body.classList.toggle("sidebar-collapsed", Boolean(collapsed));
  writeSidebarCollapsedPreference(Boolean(collapsed));
  setSidebarOpen(false);
  syncSidebarControls();
}

function setSidebarOpen(open) {
  const shouldOpen = Boolean(open) && isMobileSidebar();
  document.body.classList.toggle("sidebar-open", shouldOpen);
  syncSidebarControls();
}

function syncSidebarControls() {
  const button = byId("sidebar-toggle-btn");
  const mobile = isMobileSidebar();
  const open = document.body.classList.contains("sidebar-open");
  const collapsed = document.body.classList.contains("sidebar-collapsed");
  if (button) {
    button.setAttribute("aria-expanded", mobile ? (open ? "true" : "false") : (collapsed ? "false" : "true"));
    button.setAttribute("aria-label", mobile ? (open ? "Close navigation" : "Open navigation") : (collapsed ? "Expand navigation" : "Collapse navigation"));
    button.textContent = mobile && open ? "×" : "☰";
  }
  const backdrop = byId("sidebar-backdrop");
  if (backdrop) backdrop.hidden = !(mobile && open);
}

function syncResponsiveShellControls() {
  const topbarUserMenu = document.querySelector(".terminal-topbar .user-menu");
  if (topbarUserMenu) topbarUserMenu.hidden = isMobileSidebar();
}

function setView(view) {
  const drawer = byId("detail-drawer");
  if (drawer) drawer.classList.remove("open");
  for (const item of document.querySelectorAll(".nav-item")) {
    item.classList.toggle("active", item.dataset.view === view);
  }
  for (const section of document.querySelectorAll(".view")) {
    section.classList.toggle("active", section.id === `${view}-view`);
  }
  const label = document.querySelector(`.nav-item[data-view="${view}"] span:not(.nav-icon)`)?.textContent || "Overview";
  const marketScoped = ["overview", "suggestions", "analyze", "positions", "orders", "decisions", "sentiment"].includes(view);
  byId("view-title").textContent = marketScoped ? `${activeMarketLabel()} ${label}` : label;
  updateMarketWorkspaceLabels();
}

function currentViewName() {
  const active = document.querySelector(".view.active");
  return active?.id?.replace(/-view$/, "") || "overview";
}

function openSettingsTab(tabName) {
  setView("settings");
  setSettingsTab(tabName || "broker");
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
    byId("login-status").textContent = "Backend unavailable. Start OpenStocks and refresh.";
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
    if ([statusResponse, configResponse, accountResponse].some((response) => response.status === 401)) {
      handleUnauthorized("Session expired. Sign in again.");
      return;
    }
    if (!statusResponse.ok || !configResponse.ok || !accountResponse.ok) {
      showBackendError("Initial load failed. Refresh after the backend is healthy.", {
        status: statusResponse.status,
        config: configResponse.status,
        account: accountResponse.status,
      });
      return;
    }
    render(await statusResponse.json());
    renderSettings(await configResponse.json());
    renderAccount(await accountResponse.json());
    fetchCredits();
    if (state.auth?.admin) {
      fetchUsers();
      fetchAdminCredits();
    }
  } catch (error) {
    showBackendError(networkErrorMessage(error, "initial load"), { action: "initial load" });
  }
}

async function refreshStatusOnly() {
  if (state.statusRefreshInFlight) return;
  state.statusRefreshInFlight = true;
  try {
    const response = await fetch("/api/status");
    const payload = await response.json().catch(() => ({ detail: `HTTP ${response.status}` }));
    if (response.status === 401) {
      handleUnauthorized(payload.detail || "Session expired. Sign in again.");
      return;
    }
    if (response.ok) render(payload);
  } catch (error) {
    showBackendError(networkErrorMessage(error, "status refresh"), { action: "status refresh" });
  } finally {
    state.statusRefreshInFlight = false;
  }
}

function openSocket() {
  if (!state.auth?.authenticated || state.socket) return;
  if (state.socketReconnectTimer) {
    clearTimeout(state.socketReconnectTimer);
    state.socketReconnectTimer = null;
  }
  const protocol = window.location.protocol === "https:" ? "wss" : "ws";
  const socket = new WebSocket(`${protocol}://${window.location.host}/ws`);
  socket.addEventListener("message", (event) => {
    if (state.auth?.admin) {
      render(JSON.parse(event.data));
      return;
    }
    refreshStatusOnly();
  });
  socket.addEventListener("close", (event) => {
    state.socket = null;
    if (event.code === 1008) return;
    if (state.auth?.authenticated) {
      state.socketReconnectTimer = setTimeout(() => {
        state.socketReconnectTimer = null;
        openSocket();
      }, 2000);
    }
  });
  state.socket = socket;
}

bindControls();
initTheme();
updateSessionPill();
setInterval(updateSessionPill, 60_000);
loadInitial();
window.addEventListener("resize", () => {
  if (state.latest && byId("equity-chart")) {
    const market = normalizeUiMarket(state.activeMarket);
    const scoped = marketPortfolioFromPayload(state.latest, market);
    const rows = state.latest.equity_curve_by_market?.[market] || state.latest.equity_curve || [{ equity: scoped.equity, ts: state.latest.last_cycle_at || new Date().toISOString() }];
    drawEquity(rows, market);
  }
});
