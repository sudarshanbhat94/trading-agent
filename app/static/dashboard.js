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
  positionMarksInFlight: false,
  positionMarksPending: false,
  positionMarksTimer: null,
  positionMarksLastFetchAt: 0,
  positionMarksLastAppliedAt: null,
  marketIndexTimer: null,
  marketIndices: {},
  marketIndicesInFlight: false,
  quoteFilter: "",
  ideaWatchlistSearch: "",
  activeIdeaGroup: "buys",
  currentIdeaWatchlistRows: [],
  selectedIdeaWatchlistKey: "",
  currentDrawerValue: null,
  signalSearchQuery: "",
  signalSearchRows: [],
  signalSearchInFlight: false,
  signalSearchTimer: null,
  signalSearchSeq: 0,
  portfolioSearch: "",
  activePortfolioTab: "holdings",
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
  ai: new Set(["Signals", "Sentiment"]),
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

const inrTradeMoney = new Intl.NumberFormat("en-IN", {
  style: "currency",
  currency: "INR",
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

const usdTradeMoney = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

const creditsFmt = new Intl.NumberFormat("en-IN", {
  maximumFractionDigits: 4,
});

function byId(id) {
  return document.getElementById(id);
}

function numericValue(value) {
  if (value === null || value === undefined || value === "") return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function fmtMoney(value) {
  const parsed = numericValue(value);
  return parsed !== null ? money.format(parsed) : "-";
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

function dayClosedPositionRowsFromPayload(payload = {}, market = state.activeMarket) {
  const region = normalizeUiMarket(market);
  const paper = payload.paper || {};
  const candidates = [
    ...payloadRowsForMarket(payload, "follow_history", region),
    ...filterRowsByMarket(paper.follow_history || [], region),
    ...filterRowsByMarket(paper.closed_positions || [], region),
  ];
  const byKey = new Map();
  candidates.forEach((row) => {
    if (!row || !positionClosedToday(row)) return;
    const key = `${row.follow_id || row.idea_id || row.symbol || ""}:${row.closed_at || row.updated_at || ""}`;
    byKey.set(key, {
      ...row,
      __closed_day_position: true,
      qty: 0,
      avg_price: firstFinite(row.avg_price, row.entry_price),
      market_price: firstFinite(row.market_price, row.latest_price, row.follow_latest_price, row.exit_price),
      mode_label: row.mode_label || (String(row.mode || "").toUpperCase() === "LIVE" ? "Live request" : "Paper"),
      execution_state_label: row.execution_state_label || "Closed today",
      lifecycle_status: row.lifecycle_status || "closed_today",
    });
  });
  return [...byKey.values()].sort((a, b) => (rowTimestamp(b)?.getTime() || 0) - (rowTimestamp(a)?.getTime() || 0));
}

function positionRowsForMarket(payload = {}, market = state.activeMarket) {
  const region = normalizeUiMarket(market);
  const openRows = filterRowsByMarket(payload.positions || [], region);
  const openKeys = new Set(openRows.map((row) => String(row.follow_id || row.user_follow?.id || row.symbol || "").toUpperCase()));
  const closedRows = dayClosedPositionRowsFromPayload(payload, region).filter((row) => {
    const key = String(row.follow_id || row.user_follow?.id || row.symbol || "").toUpperCase();
    return !key || !openKeys.has(key);
  });
  return [...openRows, ...closedRows].sort((a, b) => {
    const aOpen = positionQuantity(a) > 0 ? 1 : 0;
    const bOpen = positionQuantity(b) > 0 ? 1 : 0;
    if (aOpen !== bOpen) return bOpen - aOpen;
    return (rowTimestamp(b)?.getTime() || 0) - (rowTimestamp(a)?.getTime() || 0);
  });
}

function scopedMarketContext(context = {}, market = state.activeMarket) {
  const region = normalizeUiMarket(market);
  const byMarket = context.by_market || context.byMarket;
  return byMarket?.[region] || context;
}

function scopedTomorrowPlan(plan = {}, market = state.activeMarket) {
  const region = normalizeUiMarket(market);
  const byMarket = plan.by_market || plan.byMarket;
  if (byMarket?.[region]) return byMarket[region];
  if (normalizeUiMarket(plan.market_region || plan.market) === region) return plan;
  return { enabled: false, market_region: region, items: [], sections: {}, summary: {} };
}

function scopedOpportunityScan(scan = {}, market = state.activeMarket) {
  const region = normalizeUiMarket(market);
  const byMarket = scan.by_market || scan.byMarket;
  const scoped = byMarket?.[region];
  return scoped && typeof scoped === "object"
    ? { ...scan, ...scoped, by_market: byMarket }
    : scan;
}

function scopedPreCatalystDiscovery(discovery = {}, market = state.activeMarket) {
  const region = normalizeUiMarket(market);
  const candidates = (discovery.candidates || []).filter((item) => rowMarket(item) === region);
  const liveConfirmations = (discovery.live_confirmations || []).filter((item) => rowMarket(item) === region);
  return {
    ...discovery,
    candidates,
    live_confirmations: liveConfirmations,
    candidate_count: candidates.length,
    live_confirmation_count: liveConfirmations.length,
    original_candidate_count: (discovery.candidates || []).length,
  };
}

function scopedMarketActionRadar(payload = {}, market = state.activeMarket) {
  const region = normalizeUiMarket(market);
  const source = payload.market_action_radar || payload.opportunity_scan?.market_action_radar || {};
  const events = (source.events || []).filter((item) => rowMarket(item) === region);
  const eventsBySymbol = {};
  for (const event of events) {
    if (event?.symbol) eventsBySymbol[String(event.symbol).toUpperCase()] = event;
  }
  return {
    ...source,
    events,
    events_by_symbol: eventsBySymbol,
    event_count: events.length,
    original_event_count: (source.events || []).length,
  };
}

function opportunityHealthPayload(payload = state.latest || {}, market = state.activeMarket) {
  const opportunity = scopedOpportunityScan(payload.opportunity_scan || {}, market);
  const premarket = scopedPreCatalystDiscovery(payload.pre_catalyst_discovery || {}, market);
  const marketAction = scopedMarketActionRadar(payload, market);
  return {
    __detail_type: "opportunity_health",
    market_region: normalizeUiMarket(market),
    opportunity_scan: opportunity,
    market_action_radar: marketAction,
    premarket_watchlist: premarket,
  };
}

function fmtMarketMoney(value, market = "IN") {
  const parsed = numericValue(value);
  if (parsed === null) return "-";
  return normalizeUiMarket(market) === "US" ? usdPrice.format(parsed) : money.format(parsed);
}

function fmtTradeMoney(value, market = "IN") {
  const parsed = numericValue(value);
  if (parsed === null) return "-";
  return normalizeUiMarket(market) === "US" ? usdTradeMoney.format(parsed) : inrTradeMoney.format(parsed);
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
    realized_pnl: firstFinite(
      portfolio.realized_pnl_by_market?.[region],
      portfolio.realizedPnlByMarket?.[region],
      portfolio.realized_by_market?.[region],
      portfolio.realized_pnl,
      0,
    ),
    unrealized_pnl: unrealized,
    today_pnl: firstFinite(
      portfolio.today_pnl_by_market?.[region],
      portfolio.todayPnlByMarket?.[region],
      portfolio.day_pnl_by_market?.[region],
      portfolio.today_pnl,
      portfolio.day_pnl,
    ),
  };
}

function quoteForSymbol(symbol, market = state.activeMarket) {
  const normalizedSymbol = String(symbol || "").toUpperCase();
  const region = normalizeUiMarket(market);
  if (!normalizedSymbol) return {};
  return (state.latest?.quotes || []).find((quote) => (
    String(quote.symbol || "").toUpperCase() === normalizedSymbol
    && normalizeUiMarket(rowMarket(quote)) === region
  )) || {};
}

function positionDayPnl(row = {}, market = state.activeMarket) {
  if (positionClosedToday(row)) {
    return positionRealizedPnl(row);
  }
  if (positionOpenedToday(row)) {
    return positionUnrealizedPnl(row);
  }
  const qty = positionQuantity(row);
  const latest = positionLatestPrice(row);
  const explicitToday = firstFinite(
    row.today_pnl,
    row.day_pnl,
    row.daily_pnl,
    row.position_summary?.today_pnl,
    row.position_summary?.day_pnl,
  );
  if (explicitToday !== null) return explicitToday;
  const quote = quoteForSymbol(row.symbol, market);
  const previousClose = firstPositiveFinite(
    row.previous_close,
    row.prev_close,
    row.position_summary?.previous_close,
    row.position_summary?.prev_close,
    quote.previous_close,
    quote.prev_close,
  );
  if (!qty || latest === null || previousClose === null) return null;
  return (latest - previousClose) * qty;
}

function positionStockDayPct(row = {}, market = state.activeMarket) {
  const explicit = firstFinite(row.day_change_pct, row.change_pct, row.daily_change_pct);
  if (explicit !== null) return explicit;
  const latest = positionLatestPrice(row);
  const quote = quoteForSymbol(row.symbol, market);
  const previousClose = firstPositiveFinite(
    row.previous_close,
    row.prev_close,
    row.position_summary?.previous_close,
    row.position_summary?.prev_close,
    quote.previous_close,
    quote.prev_close,
  );
  if (latest === null || previousClose === null) return null;
  return ((latest - previousClose) / previousClose) * 100;
}

function positionDayPct(row = {}, market = state.activeMarket) {
  if (positionClosedToday(row)) {
    const pct = firstFinite(row.return_pct, row.current_return_pct, row.manual_exit?.return_pct);
    if (pct !== null) return pct;
  }
  if (positionOpenedToday(row)) {
    const explicitTodayPct = firstFinite(row.today_pnl_pct, row.position_day_change_pct);
    if (explicitTodayPct !== null) return explicitTodayPct;
    const pnl = positionUnrealizedPnl(row);
    const entry = positionEntryPrice(row);
    const qty = positionQuantity(row);
    const invested = entry !== null ? entry * qty : null;
    if (pnl !== null && invested && invested > 0) return (pnl / invested) * 100;
  }
  return positionStockDayPct(row, market);
}

function positionDayPnlSource(row = {}, market = state.activeMarket) {
  const dayPnl = positionDayPnl(row, market);
  if (dayPnl === null || !Number.isFinite(Number(dayPnl))) return "unavailable";
  if (positionClosedToday(row)) return "closed_today";
  return positionOpenedToday(row) ? "entry_today" : "previous_close";
}

function portfolioPnlMetrics(portfolio = {}, positions = [], market = state.activeMarket) {
  const unrealized = Number(portfolio.unrealized_pnl || 0);
  const realized = Number(portfolio.realized_pnl || 0);
  const openPositions = (positions || []).filter((row) => positionQuantity(row) > 0);
  const dayClosedPositions = (positions || []).filter(positionClosedToday);
  const calculatedMarketValue = openPositions.reduce(
    (sum, row) => sum + Number(firstFinite(row.market_price, row.latest_price, row.follow_latest_price) || 0) * Number(row.qty || row.user_follow?.qty || 0),
    0,
  );
  const calculatedInvested = openPositions.reduce(
    (sum, row) => sum + Number(firstFinite(row.avg_price, row.entry_price, row.follow_entry_price) || 0) * Number(row.qty || row.user_follow?.qty || 0),
    0,
  );
  const dayValues = [...openPositions, ...dayClosedPositions]
    .map((row) => positionDayPnl(row, market))
    .filter((value) => value !== null && Number.isFinite(Number(value)));
  const daySources = [...openPositions, ...dayClosedPositions]
    .map((row) => positionDayPnlSource(row, market))
    .filter((source) => source !== "unavailable");
  const explicitToday = firstFinite(
    portfolio.today_pnl,
    portfolio.day_pnl,
    portfolio.todayPnl,
    portfolio.daily_pnl,
    portfolio.todays_pnl,
  );
  const calculatedToday = dayValues.length
    ? dayValues.reduce((sum, value) => sum + Number(value || 0), 0)
    : null;
  const today = calculatedToday !== null ? calculatedToday : explicitToday;
  let todaySource = "unavailable";
  if (calculatedToday === null && explicitToday !== null) {
    todaySource = "reported";
  } else if (daySources.includes("closed_today") && daySources.length === 1) {
    todaySource = "closed_today";
  } else if (daySources.includes("entry_today") && daySources.includes("previous_close")) {
    todaySource = "mixed";
  } else if (daySources.includes("closed_today") && daySources.length > 1) {
    todaySource = "mixed";
  } else if (daySources.includes("entry_today")) {
    todaySource = "entry_today";
  } else if (daySources.includes("previous_close")) {
    todaySource = "previous_close";
  }
  return {
    today,
    today_source: todaySource,
    today_has_close: explicitToday !== null || dayValues.length > 0,
    today_count: dayValues.length,
    today_missing_count: Math.max(openPositions.length - dayValues.length, 0),
    open_count: openPositions.length,
    market_value: firstFinite(portfolio.market_value, portfolio.marketValue, calculatedMarketValue) || 0,
    invested: firstFinite(portfolio.invested, calculatedInvested) || 0,
    unrealized,
    realized,
    total: realized + unrealized,
  };
}

function portfolioTodayLabel(metrics = {}) {
  if (metrics.today_source === "reported") return "reported today";
  if (metrics.today_source === "entry_today") return "from entry today";
  if (metrics.today_source === "closed_today") return "realized today";
  if (metrics.today_source === "mixed") return "holdings + today entries";
  if (metrics.today_source === "previous_close") {
    return Number(metrics.today_missing_count || 0) > 0 ? "partial previous close" : "from previous close";
  }
  return Number(metrics.open_count || 0) > 0 ? "needs previous close" : "no open holdings";
}

function positionQuantity(row = {}) {
  return Number(firstFinite(row.qty, row.user_follow?.qty, row.entry_qty, row.open_qty) || 0);
}

function positionLatestPrice(row = {}) {
  return firstFinite(
    row.market_price,
    row.follow_latest_price,
    row.user_follow?.latest_price,
    row.latest_price,
    row.ltp,
    row.price,
    row.last_price,
    row.entry_price,
    row.avg_price,
  );
}

function positionEntryPrice(row = {}) {
  return firstFinite(
    row.avg_price,
    row.follow_entry_price,
    row.user_follow?.entry_price,
    row.entry_price,
    row.price_at_follow,
  );
}

function positionUnrealizedPnl(row = {}) {
  const explicit = firstFinite(row.unrealized_pnl, row.user_follow?.unrealized_pnl, row.open_pnl, row.pnl);
  if (explicit !== null) return explicit;
  const qty = positionQuantity(row);
  const latest = positionLatestPrice(row);
  const entry = positionEntryPrice(row);
  if (!qty || latest === null || entry === null) return null;
  return (latest - entry) * qty;
}

function positionRealizedPnl(row = {}) {
  const details = parseJsonObject(row.details_json);
  const manual = details.manual_exit && typeof details.manual_exit === "object" ? details.manual_exit : {};
  return firstFinite(
    row.realized_pnl,
    row.cash_effect,
    row.manual_exit?.realized_pnl,
    manual.realized_pnl,
    row.exit_pnl,
  );
}

function positionIsClosed(row = {}) {
  const status = String(row.status || row.follow_status || row.lifecycle_status || "").toUpperCase();
  const state = String(row.state || "").toUpperCase();
  return Boolean(
    row.__closed_day_position
    || state === "CLOSED"
    || ["EXITED", "CLOSED"].includes(status)
    || (positionQuantity(row) <= 0 && positionRealizedPnl(row) !== null)
  );
}

function positionClosedAt(row = {}) {
  const details = parseJsonObject(row.details_json);
  const manual = details.manual_exit && typeof details.manual_exit === "object" ? details.manual_exit : {};
  const raw = row.closed_at
    || row.exited_at
    || row.exit_at
    || manual.exited_at
    || row.updated_at;
  const date = raw ? new Date(raw) : null;
  return date && Number.isFinite(date.getTime()) ? date : null;
}

function positionClosedToday(row = {}) {
  if (!positionIsClosed(row)) return false;
  const date = positionClosedAt(row);
  if (!date) return false;
  const now = new Date();
  return date.getFullYear() === now.getFullYear() && date.getMonth() === now.getMonth() && date.getDate() === now.getDate();
}

function portfolioRowsPnlMetrics(rows = [], market = state.activeMarket) {
  const openPositions = (rows || []).filter((row) => positionQuantity(row) > 0);
  const dayClosedPositions = (rows || []).filter(positionClosedToday);
  const marketValue = openPositions.reduce((sum, row) => {
    const latest = positionLatestPrice(row);
    return sum + (latest === null ? 0 : latest * positionQuantity(row));
  }, 0);
  const invested = openPositions.reduce((sum, row) => {
    const entry = positionEntryPrice(row);
    return sum + (entry === null ? 0 : entry * positionQuantity(row));
  }, 0);
  const unrealizedValues = openPositions
    .map(positionUnrealizedPnl)
    .filter((value) => value !== null && Number.isFinite(Number(value)));
  const dayValues = [...openPositions, ...dayClosedPositions]
    .map((row) => positionDayPnl(row, market))
    .filter((value) => value !== null && Number.isFinite(Number(value)));
  const daySources = [...openPositions, ...dayClosedPositions]
    .map((row) => positionDayPnlSource(row, market))
    .filter((source) => source !== "unavailable");
  const unrealized = unrealizedValues.reduce((sum, value) => sum + Number(value || 0), 0);
  const today = dayValues.length
    ? dayValues.reduce((sum, value) => sum + Number(value || 0), 0)
    : null;
  let todaySource = "unavailable";
  if (daySources.includes("closed_today") && daySources.length === 1) {
    todaySource = "closed_today";
  } else if (daySources.includes("entry_today") && daySources.includes("previous_close")) {
    todaySource = "mixed";
  } else if (daySources.includes("closed_today") && daySources.length > 1) {
    todaySource = "mixed";
  } else if (daySources.includes("entry_today")) {
    todaySource = "entry_today";
  } else if (daySources.includes("previous_close")) {
    todaySource = "previous_close";
  }
  return {
    today,
    today_source: todaySource,
    today_has_close: dayValues.length > 0,
    today_count: dayValues.length,
    today_missing_count: Math.max(openPositions.length - dayValues.length, 0),
    open_count: openPositions.length,
    market_value: marketValue,
    invested,
    unrealized,
    realized: 0,
    total: unrealized,
  };
}

function mobilePortfolioSummaryHtml(tab = "positions", metrics = {}, market = state.activeMarket) {
  const isHoldings = tab === "holdings";
  const pnlPct = Number(metrics.invested || 0) > 0 ? (Number(metrics.unrealized || 0) / Number(metrics.invested || 0)) * 100 : null;
  return `<section class="mobile-portfolio-summary broker-card" aria-label="${isHoldings ? "Holdings summary" : "Positions summary"}">
    <div class="mobile-portfolio-summary-grid">
      <span><small>${isHoldings ? "Invested" : "Deployed"}</small><strong>${fmtTradeMoney(metrics.invested, market)}</strong></span>
      <span><small>Current</small><strong>${fmtTradeMoney(metrics.market_value, market)}</strong></span>
    </div>
    <div class="mobile-portfolio-summary-pnl">
      <small>P&amp;L</small>
      <strong class="${pnlClass(metrics.unrealized)}">${fmtSignedTradeMoney(metrics.unrealized, market)}</strong>
      <em class="${pnlClass(pnlPct)}">${fmtSignedPct(pnlPct)}</em>
    </div>
  </section>`;
}

function mobilePortfolioDayPnlHtml(metrics = {}, market = state.activeMarket) {
  const dayPct = Number(metrics.market_value || 0) > 0 && metrics.today !== null
    ? (Number(metrics.today) / Number(metrics.market_value)) * 100
    : null;
  const label = metrics.today_source === "entry_today" ? "Today from entry" : "Day's P&amp;L";
  return `<div class="mobile-portfolio-day-pnl" aria-label="Day's P and L">
    <span>${label}</span>
    <strong class="${pnlClass(metrics.today)}">${fmtSignedTradeMoney(metrics.today, market)}</strong>
    <em class="${pnlClass(dayPct)}">${fmtSignedPct(dayPct)}</em>
  </div>`;
}

function fmtNumber(value) {
  const parsed = numericValue(value);
  return parsed !== null ? number.format(parsed) : "-";
}

function firstFinite(...values) {
  for (const value of values) {
    const numberValue = numericValue(value);
    if (numberValue !== null) return numberValue;
  }
  return null;
}

function firstPositiveFinite(...values) {
  for (const value of values) {
    const numberValue = numericValue(value);
    if (numberValue !== null && numberValue > 0) return numberValue;
  }
  return null;
}

function fmtCompact(value) {
  const parsed = numericValue(value);
  return parsed !== null ? compactNumber.format(parsed) : "-";
}

function fmtUsd(value) {
  const parsed = numericValue(value);
  return parsed !== null ? usd.format(parsed) : "-";
}

function fmtCredits(value) {
  const parsed = numericValue(value);
  return parsed !== null ? creditsFmt.format(parsed) : "-";
}

function fmtPct(value) {
  const parsed = numericValue(value);
  return parsed !== null ? `${number.format(parsed)}%` : "-";
}

function fmtSignedPct(value) {
  const parsed = numericValue(value);
  if (parsed === null) return "-";
  return `${parsed > 0 ? "+" : ""}${number.format(parsed)}%`;
}

function fmtSignedTradeMoney(value, market = "IN") {
  const parsed = numericValue(value);
  if (parsed === null) return "-";
  return `${parsed > 0 ? "+" : ""}${fmtTradeMoney(parsed, market)}`;
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

function fmtDateTime(value) {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString("en-IN", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" });
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
  document.documentElement.dataset.theme = next;
  document.documentElement.style.colorScheme = next;
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
  const mobileButton = byId("mobile-theme-toggle-btn");
  if (mobileButton) {
    const icon = mobileButton.querySelector("use");
    if (icon) icon.setAttribute("href", next === "dark" ? "#icon-sun" : "#icon-moon");
    mobileButton.setAttribute("aria-label", `Switch to ${next === "dark" ? "light" : "dark"} theme`);
    mobileButton.setAttribute("title", next === "dark" ? "Light theme" : "Dark theme");
  }
  const themeMeta = byId("theme-color-meta") || document.querySelector('meta[name="theme-color"]');
  if (themeMeta) themeMeta.setAttribute("content", next === "dark" ? "#0b0e13" : "#f0f1f4");
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

function secondsSince(value) {
  if (!value) return null;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return null;
  return Math.max(0, (Date.now() - date.getTime()) / 1000);
}

function fmtFreshness(value, fallback = "waiting") {
  const age = secondsSince(value);
  if (age === null) return fallback;
  if (age < 2) return "now";
  return fmtAge(age);
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

function autoFollowReasonText(item = {}) {
  const reason = String(item.reason || "").trim();
  const mapped = {
    already_followed_symbol: "already paper-followed",
    already_followed: "already followed",
    recent_risk_exit_cooldown: "cooling down after a risk exit",
    active_buy_not_fresh_enough_for_auto_follow: "not fresh enough or outside the entry zone",
    insufficient_paper_cash_for_position_size: "paper cash/position cap is too small for one share",
    live_unavailable_shared_engine_needs_user_broker_session: "live needs the user's own broker session",
    market_closed: "market is closed",
  };
  if (mapped[reason]) return mapped[reason];
  if (/amount is too small/i.test(reason)) return "paper amount is too small for one share";
  return reason ? reasonFromSnakeCase(reason) : "waiting for the next eligible BUY";
}

function autoTradeSummary(payload = state.latest || {}, market = state.activeMarket) {
  const session = payload.user_signal_session || {};
  const autoTrade = session.auto_trade || payload.shared_auto_trade || {};
  const mode = String(session.signal_execution_mode || autoTrade.mode || state.auth?.user?.signal_execution_mode || "SIGNAL_ONLY").toUpperCase();
  const enabled = ["AUTO_PAPER", "AUTO_LIVE"].includes(mode) && autoTrade.enabled !== false;
  const followed = Number(autoTrade.followed || 0);
  const skipped = Array.isArray(autoTrade.skipped) ? autoTrade.skipped : [];
  const meaningfulSkip = skipped.find((item) => !["already_followed", "already_followed_symbol"].includes(String(item.reason || "")))
    || skipped[0];
  if (!enabled) {
    return {
      label: signalModeLabel(mode),
      value: "Off",
      note: "BUY ideas are saved until Auto paper is enabled in Account.",
      tone: "watch",
    };
  }
  if (followed > 0) {
    return {
      label: signalModeLabel(mode),
      value: `${fmtNumber(followed)} followed`,
      note: skipped.length ? `${fmtNumber(skipped.length)} skipped by cash, cooldown, or entry rules.` : "New eligible BUY ideas were paper-followed.",
      tone: "open",
    };
  }
  if (meaningfulSkip) {
    const symbol = meaningfulSkip.symbol ? `${meaningfulSkip.symbol}: ` : "";
    return {
      label: signalModeLabel(mode),
      value: "No new add",
      note: `${symbol}${autoFollowReasonText(meaningfulSkip)}.`,
      tone: "warning",
    };
  }
  return {
    label: signalModeLabel(mode),
    value: "Ready",
    note: "No fresh eligible BUY needed a new paper position in the last cycle.",
    tone: "open",
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

function scoreCandidatePercent(value) {
  const parsed = Number(value);
  if (!Number.isFinite(parsed) || parsed < 0) return null;
  return parsed > 0 && parsed <= 1 ? parsed * 100 : parsed;
}

function confidencePercent(row = {}) {
  const values = [
    row.rank_score,
    row.confidence_pct,
    row.confidence,
    row.overall_score_pct,
    row.score_percent,
    row.score,
  ]
    .map(scoreCandidatePercent)
    .filter((value) => value !== null);
  return values.length ? Math.max(...values) : 0;
}

function decisionScorePercent(row = {}) {
  const backendRank = scoreCandidatePercent(row.rank_score);
  if (backendRank !== null) return Math.max(0, Math.min(100, backendRank));
  const audit = decisionAudit(row);
  const score = audit.score_breakdown || {};
  const values = [
    row.overall_score_pct,
    row.score_percent,
    score.score_percent,
    row.combined_score_pct,
    row.confidence_pct,
    row.confidence,
    row.score,
  ]
    .map(scoreCandidatePercent)
    .filter((value) => value !== null);
  const combined = Number(score.combined ?? row.combined_score);
  if (Number.isFinite(combined)) values.push(combined >= -1 && combined <= 1 ? (combined + 1) * 50 : combined);
  return values.length ? Math.max(...values) : 0;
}

function sortDecisionRows(rows = []) {
  return uniqueLatestDecisionRows(rows).sort((a, b) => {
    const scoreDelta = decisionScorePercent(b) - decisionScorePercent(a);
    if (scoreDelta !== 0) return scoreDelta;
    const timeDelta = (rowTimestamp(b)?.getTime() || 0) - (rowTimestamp(a)?.getTime() || 0);
    if (timeDelta !== 0) return timeDelta;
    return String(a.symbol || "").localeCompare(String(b.symbol || ""));
  });
}

function uniqueLatestDecisionRows(rows = []) {
  const bySymbol = new Map();
  for (const row of rows || []) {
    const symbol = String(row?.symbol || "").toUpperCase();
    if (!symbol) continue;
    const current = bySymbol.get(symbol);
    if (!current) {
      bySymbol.set(symbol, row);
      continue;
    }
    const rowTime = rowTimestamp(row)?.getTime() || 0;
    const currentTime = rowTimestamp(current)?.getTime() || 0;
    if (rowTime > currentTime || (rowTime === currentTime && Number(row.id || 0) > Number(current.id || 0))) {
      bySymbol.set(symbol, row);
    }
  }
  return [...bySymbol.values()];
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
    row.closed_at ||
    row.opened_at ||
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
  const scoped = filter === "all" ? rows : (rows || []).filter((row) => ideaMatchesFilter(row, filter));
  return sortSuggestionRows(scoped);
}

function sortSuggestionRows(rows = []) {
  return (rows || []).slice().sort((a, b) => {
    const confidenceDelta = confidencePercent(b) - confidencePercent(a);
    if (confidenceDelta !== 0) return confidenceDelta;
    const confluenceDelta = Number(b.confluence || 0) - Number(a.confluence || 0);
    if (confluenceDelta !== 0) return confluenceDelta;
    const returnDelta = Number(b.current_return_pct || 0) - Number(a.current_return_pct || 0);
    if (returnDelta !== 0) return returnDelta;
    const timeDelta = (rowTimestamp(b)?.getTime() || 0) - (rowTimestamp(a)?.getTime() || 0);
    if (timeDelta !== 0) return timeDelta;
    return String(a.symbol || "").localeCompare(String(b.symbol || ""));
  });
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

function signalSearchActive() {
  return String(state.signalSearchQuery || "").trim().length > 0;
}

function signalRowsForRender(payload = state.latest || {}, market = state.activeMarket) {
  if (signalSearchActive()) {
    return sortDecisionRows(filterRowsByMarket(state.signalSearchRows || [], market));
  }
  return sortDecisionRows(payloadRowsForMarket(payload, "decisions", market));
}

function updateSignalSearchStatus(rows = []) {
  const status = byId("signal-search-status");
  if (!status) return;
  const query = String(state.signalSearchQuery || "").trim();
  if (state.signalSearchInFlight) {
    status.textContent = "Searching database...";
  } else if (query) {
    status.textContent = `${fmtNumber(rows.length)} DB result${rows.length === 1 ? "" : "s"} for "${query}"`;
  } else {
    status.textContent = "Latest signals";
  }
}

function rerenderSignalsFromState() {
  if (!state.latest) return;
  const market = normalizeUiMarket(state.activeMarket);
  const rows = signalRowsForRender(state.latest, market);
  const visibleRows = applyDecisionFilter(rows);
  const count = byId("decision-count");
  if (count) {
    count.textContent = signalSearchActive()
      ? `${activeMarketLabel()} · ${filteredCountLabel(visibleRows.length, rows.length, "DB signal")}`
      : `${activeMarketLabel()} · ${filteredCountLabel(visibleRows.length, rows.length, "signal")}`;
  }
  updateSignalSearchStatus(rows);
  renderDecisions(visibleRows, { controlRunning: Boolean(state.latest?.running) });
}

async function searchSignalsFromDatabase(query) {
  const trimmed = String(query || "").trim();
  state.signalSearchQuery = trimmed;
  if (!trimmed) {
    state.signalSearchRows = [];
    state.signalSearchInFlight = false;
    rerenderSignalsFromState();
    return;
  }
  const seq = ++state.signalSearchSeq;
  state.signalSearchInFlight = true;
  updateSignalSearchStatus(state.signalSearchRows || []);
  try {
    const params = new URLSearchParams({
      q: trimmed,
      market: normalizeUiMarket(state.activeMarket),
      limit: "120",
    });
    const response = await fetch(`/api/signals/search?${params.toString()}`, { cache: "no-store" });
    const payload = await response.json().catch(() => ({ detail: `HTTP ${response.status}` }));
    if (!response.ok) throw new Error(payload.detail || `HTTP ${response.status}`);
    if (seq !== state.signalSearchSeq) return;
    state.signalSearchRows = Array.isArray(payload.results) ? payload.results : [];
  } catch (error) {
    if (seq === state.signalSearchSeq) {
      showBackendError(networkErrorMessage(error, "signal search"), { action: "signal search" });
      state.signalSearchRows = [];
    }
  } finally {
    if (seq === state.signalSearchSeq) {
      state.signalSearchInFlight = false;
      rerenderSignalsFromState();
    }
  }
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
  return (rows || []).filter((row) => orderStatusBucket(row) === filter);
}

function orderStatusBucket(row = {}) {
  const status = String(row.status || "").trim().toUpperCase();
  if (["OPEN", "PENDING", "SUBMITTED", "WORKING", "REQUESTED", "ACCEPTED", "PARTIALLY_FILLED", "LIVE_REQUESTED", "LIVE_EXIT_REQUESTED"].includes(status)) return "open";
  if (["FILLED", "EXECUTED", "COMPLETE", "COMPLETED", "EXITED", "CLOSED", "PARTIAL", "REDUCED"].includes(status)) return "filled";
  if (["REJECTED", "VETOED", "CANCELLED", "CANCELED", "FAILED", "EXPIRED"].includes(status)) return "rejected";
  return status ? "open" : "rejected";
}

function orderStatusLabel(row = {}) {
  const bucket = orderStatusBucket(row);
  if (bucket === "filled") return "EXECUTED";
  if (bucket === "rejected") return String(row.status || "REJECTED").toUpperCase();
  return "OPEN";
}

function orderFilledText(row = {}) {
  const qty = Number(row.qty || 0);
  const filled = firstFinite(row.filled_qty, row.executed_qty, row.average_filled_qty);
  if (filled !== null) return `${fmtNumber(filled)}/${fmtNumber(qty)}`;
  return orderStatusBucket(row) === "filled" ? `${fmtNumber(qty)}/${fmtNumber(qty)}` : `0/${fmtNumber(qty)}`;
}

function orderMetaText(row = {}) {
  const market = rowMarket(row);
  const exchange = row.exchange || (market === "IN" ? "NSE" : "US");
  const audit = parseJsonObject(row.details_json);
  const route = audit.route || {};
  const product = row.product || route.product || (market === "IN" ? "CNC" : "EQ");
  const orderType = row.order_type || route.order_type || route.orderType || "LIMIT";
  return `${exchange}  ${product}  ${orderType}`;
}

function updateOrderFilterCounts(rows = []) {
  const counts = {
    open: (rows || []).filter((row) => orderStatusBucket(row) === "open").length,
    filled: (rows || []).filter((row) => orderStatusBucket(row) === "filled").length,
    rejected: (rows || []).filter((row) => orderStatusBucket(row) === "rejected").length,
    all: (rows || []).length,
  };
  const labels = { open: "Open", filled: "Executed", rejected: "Rejected", all: "All" };
  document.querySelectorAll('[data-filter-group="orders"] [data-filter-value]').forEach((button) => {
    const key = button.dataset.filterValue || "all";
    if (!(key in counts)) return;
    button.innerHTML = `${escapeHtml(labels[key] || humanLabel(key))} <span>${fmtNumber(counts[key])}</span>`;
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

const OPPORTUNITY_STATE_COPY = {
  BUY_NOW: {
    label: "Ready to buy",
    summary: "Fresh-entry checks are clear: score, grade, confirmation, data, and risk all passed.",
    next_step: "Use the entry zone, stop, targets, and position sizing shown in the trade plan.",
  },
  BUY_CANDIDATE: {
    label: "Buy candidate",
    summary: "Close to actionable, but one remaining confirmation or score/risk check is still missing.",
    next_step: "Wait for the final check to clear; no paper/live entry until marked Ready to buy.",
  },
  PULLBACK_BUY_ZONE: {
    label: "Wait for pullback",
    summary: "Good setup, wrong price. The current price is stretched from the ideal entry area.",
    next_step: "Wait for price to come back near the entry zone, then re-check volume and risk.",
  },
  BREAKOUT_CONFIRMATION_NEEDED: {
    label: "Needs breakout confirmation",
    summary: "Potential breakout setup, but it needs stronger volume and follow-through before a BUY.",
    next_step: "Wait for a strong close above the breakout level with better volume participation.",
  },
  ACTIONABLE_WATCH: {
    label: "Strong watchlist setup",
    summary: "Worth watching closely, but one or more entry or risk checks still needs to clear.",
    next_step: "Keep it on watch until the remaining confirmation appears.",
  },
  DATA_NEEDED: {
    label: "Missing market evidence",
    summary: "The engine found a possible setup, but required market evidence is missing for a trade-grade decision.",
    next_step: "Refresh or connect the missing quote, candle, volume, delivery, options, or event data before trading.",
  },
  EXIT: {
    label: "Exit check",
    summary: "The latest decision is about reducing or closing risk, not opening a new trade.",
    next_step: "Check stop, target, and exit rules for any existing position.",
  },
  MONITOR: {
    label: "Monitor only",
    summary: "There is not enough evidence for a fresh trade yet.",
    next_step: "Do not enter now; wait for a cleaner scan.",
  },
  BLOCKED: {
    label: "Avoid for now",
    summary: "One or more risk or quality checks blocks a fresh BUY.",
    next_step: "Do not enter now; wait for the blocking risk to clear.",
  },
};

function opportunityStatePayload(row = {}) {
  const details = row.details && typeof row.details === "object" ? row.details : {};
  const nested = details.opportunity_state && typeof details.opportunity_state === "object" ? details.opportunity_state : {};
  const state = String(row.opportunity_state || nested.state || "").toUpperCase();
  const fallback = OPPORTUNITY_STATE_COPY[state] || {};
  return {
    state,
    label: row.opportunity_label || nested.label || fallback.label || "",
    summary: row.opportunity_summary || nested.summary || fallback.summary || "",
    next_step: row.opportunity_next_step || nested.next_step || fallback.next_step || "",
    reasons: Array.isArray(row.opportunity_reasons)
      ? row.opportunity_reasons
      : Array.isArray(nested.reasons)
        ? nested.reasons
        : [],
    terms: Array.isArray(row.opportunity_terms)
      ? row.opportunity_terms
      : Array.isArray(nested.term_explanations)
        ? nested.term_explanations
        : [],
  };
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
    monthly_expiry_eve_no_new_longs: "Monthly expiry is close, so OpenStocks only allows confirmed probe-size entries.",
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
    llm_primary_failed_safe_hold: "Signal check did not return a clean answer in time, so OpenStocks forced HOLD and did not trade.",
    llm_buy_blocked_by_system_rules: "Signal check wanted to buy, but hard system rules blocked the trade.",
    llm_primary_unavailable_no_trade: "Signal check approval is required, but it is unavailable, so OpenStocks forced HOLD.",
    llm_primary_required_no_unreviewed_trade: "The symbol did not receive a signal check in this cycle, so OpenStocks forced HOLD.",
    llm_failed_or_timed_out_deterministic_trade_preserved: "Signal check did not return a clean answer in time, so OpenStocks forced HOLD and did not trade.",
    llm_not_selected_due_candidate_limit_deterministic_action_allowed: "The symbol was outside the current signal-check limit; new trades now require that check, so this should be HOLD.",
    time_stop_no_progress_15_sessions: "The position has not moved enough after 15 sessions, so OpenStocks is exiting dead capital.",
    overall_score_below_55_no_new_longs: "The production-readiness score is below 70%, so OpenStocks is not opening a fresh long.",
    overall_score_below_70_no_new_longs: "The production-readiness score is below 70%, so OpenStocks is not opening a fresh long.",
    fundamentals_unknown_needs_news_or_delivery_confirmation: "Fundamentals are still unknown, news/sentiment is missing, and delivery accumulation is not confirmed.",
    watch_entry_needs_exceptional_confirmation: "The setup is only WATCH grade, so it needs exceptional confirmation before a BUY.",
    delivery_distribution_no_new_longs: "Delivery data shows distribution, so OpenStocks is avoiding a fresh BUY.",
    price_extended_from_pivot: "Price is stretched from the ideal entry area, so OpenStocks is waiting for a pullback.",
    suspect_breakout_without_volume: "The breakout does not have enough volume support yet.",
    repeated_failed_breakouts: "Recent breakout attempts have failed, so the next entry needs stronger confirmation.",
    low_volume_ratio: "Volume participation is weak.",
    weak_volume_ratio: "Volume participation is below the preferred level.",
  };
  if (mapped[text]) return mapped[text];
  return compactSentence(humanLabel(text).toLowerCase());
}

function plainDecisionPath(value) {
  const text = String(value || "").trim();
  const mapped = {
    llm_review: "Signal completed",
    llm_review_failed: "Safe signal check",
    llm_primary: "Signal completed",
    manual_exit: "Manual exit",
    deterministic_audit: "Rules check",
  };
  if (!text || text === "-") return "Rules check";
  return mapped[text] || humanLabel(text);
}

function referenceFieldLabel(value) {
  const mapped = {
    pe: "PE",
    forward_pe: "forward PE",
    pb: "PB",
    market_cap: "market cap",
    week_52_high: "52-week high",
    week_52_low: "52-week low",
    eps_ttm: "EPS TTM",
    beta: "beta",
  };
  return mapped[String(value || "").toLowerCase()] || humanLabel(value).toLowerCase();
}

function referenceSourceLabel(value) {
  const mapped = {
    nse_quote_reference: "NSE quote reference",
    yahoo_quote_reference: "Yahoo quote reference",
    yahoo_quote_summary_reference: "Yahoo fundamentals reference",
    yahoo_chart_reference: "Yahoo chart reference",
    candles: "price candles",
    market_data: "market data",
    reference_feed: "reference feed",
    reference_unavailable: "reference feed unavailable",
  };
  return mapped[String(value || "").toLowerCase()] || humanLabel(value);
}

function analysisReferenceNoteHtml(referenceData = {}, payload = {}) {
  const sources = Array.isArray(referenceData.sources) && referenceData.sources.length
    ? referenceData.sources.map(referenceSourceLabel).join(", ")
    : referenceData.source ? referenceSourceLabel(referenceData.source) : "";
  const hardGaps = (referenceData.data_gaps || []).filter(Boolean).map(referenceFieldLabel);
  const unavailable = (referenceData.unavailable_fields || []).filter(Boolean).map(referenceFieldLabel);
  const derived = (referenceData.derived_from_candles || []).filter(Boolean).map(referenceFieldLabel);
  const parts = [];
  if (sources) parts.push(`Reference sources checked: ${sources}.`);
  if (derived.length) parts.push(`${derived.join(", ")} derived from ${payload.candle_count || 0} candles.`);
  if (unavailable.length) parts.push(`${unavailable.join(", ")} not reported by the connected public feeds, so OpenStocks treats them as unavailable, not as bad fundamentals.`);
  if (hardGaps.length) parts.push(`Still missing for this analysis: ${hardGaps.join(", ")}.`);
  if (!parts.length) return "";
  return `<p class="analysis-data-note">${escapeHtml(parts.join(" "))}</p>`;
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
    .replace(/llm_primary_required_no_unreviewed_trade/g, "Signal check was required before trading, so OpenStocks held")
    .replace(/llm_primary_failed_safe_hold/g, "Signal check failed, so OpenStocks held safely")
    .replace(/llm_primary_unavailable_no_trade/g, "Signal check was unavailable, so OpenStocks held safely")
    .replace(/llm_failed_deterministic_action_preserved/g, "Signal check failed, so OpenStocks held safely")
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
    return `The signal check could not complete cleanly, so OpenStocks kept the safer ${action} decision.`;
  }
  if (action === "BUY") {
    return `BUY setup is actionable: price action, confirmation, and risk checks are aligned for a fresh entry.`;
  }
  if (action === "SELL") {
    return humanizeReasonText(audit.action_reason || row.reason || "Exit rule triggered.", action);
  }
  if (combinedValue !== undefined || confluenceValue !== undefined || scorecard.buy_ready !== undefined) {
    return "HOLD for now. The setup does not have enough confirmation for a fresh entry yet.";
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
  if (combinedValue !== undefined) highlights.push(`Signal quality: ${fmtPct(Number(combinedValue) * 100)}`);
  if (confluenceValue !== undefined) highlights.push(`Setup confirmation: ${confluenceValue}/26 (${humanLabel(confluence.tier || row.tier || "pending")})`);
  if (scorecard.total_score !== undefined) {
    highlights.push(`Accumulation quality: ${scorecard.total_score}/100, ${scorecard.buy_ready ? "ready" : "not ready"}`);
  }
  if (stage.stage) highlights.push(`Market stage: ${humanLabel(stage.stage)} (${stage.buy_permitted ? "entry allowed" : "entry blocked"})`);
  if (entry.entry_grade) highlights.push(`Entry quality: ${entry.entry_grade}, ${fmtPct(entry.distance_from_pivot_pct)} from pivot`);
  if (alignment.alignment_grade) highlights.push(`Timeframe alignment: ${alignment.alignment_grade}`);
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
  const openPositions = filterRowsByMarket(allPositions, activeMarket);
  const positions = positionRowsForMarket(payload, activeMarket);
  const quotes = filterRowsByMarket(allQuotes, activeMarket);
  const latestDecisions = sortDecisionRows(payloadRowsForMarket(payload, "decisions", activeMarket));
  const decisions = signalRowsForRender(payload, activeMarket);
  const suggestions = payloadRowsForMarket(payload, "suggestions", activeMarket);
  const trackedIdeas = payloadRowsForMarket(payload, "tracked_ideas", activeMarket);
  const orders = filterRowsByMarket(allOrders, activeMarket);
  const dayOrders = orders.filter(isTodayRow);
  const strategies = payload.strategy_metrics || [];
  const strategyPlans = payload.strategy_plans || [];
  const sentiment = filterRowsByMarket(allSentiment, activeMarket);
  const visibleDecisions = applyDecisionFilter(decisions);
  const visibleSuggestions = applySuggestionFilter(suggestions);
  const visibleTrackedIdeas = applySuggestionFilter(trackedIdeas);
  const visibleOrders = applyOrderFilter(dayOrders);
  updateOrderFilterCounts(dayOrders);
  const visibleSentiment = applySentimentFilter(sentiment);
  const visibleStrategyPlans = applyStrategyPlanFilter(strategyPlans);

  const scopedPortfolio = marketPortfolioFromPayload(payload, activeMarket);
  const pnlMetrics = portfolioPnlMetrics(scopedPortfolio, positions, activeMarket);
  const unrealizedPct = Number(scopedPortfolio.invested) > 0
    ? (Number(pnlMetrics.unrealized || 0) / Number(scopedPortfolio.invested)) * 100
    : 0;
  byId("kpi-equity").textContent = fmtMarketMoney(scopedPortfolio.equity, activeMarket);
  byId("kpi-cash").textContent = fmtMarketMoney(scopedPortfolio.cash, activeMarket);
  byId("kpi-unrealized").textContent = fmtMarketMoney(pnlMetrics.unrealized, activeMarket);
  byId("kpi-unrealized").className = pnlClass(pnlMetrics.unrealized);
  const totalPnlEl = byId("kpi-total-pnl");
  if (totalPnlEl) {
    totalPnlEl.textContent = fmtMarketMoney(pnlMetrics.total, activeMarket);
    totalPnlEl.className = pnlClass(pnlMetrics.total);
  }
  const totalPnlNote = byId("kpi-total-pnl-note");
  if (totalPnlNote) {
    totalPnlNote.textContent = `realized ${fmtMarketMoney(pnlMetrics.realized, activeMarket)} + holdings`;
    totalPnlNote.className = pnlClass(pnlMetrics.realized);
  }
  const equityDelta = byId("kpi-equity-delta");
  if (equityDelta) {
    equityDelta.textContent = `Today ${fmtMarketMoney(pnlMetrics.today, activeMarket)} · ${portfolioTodayLabel(pnlMetrics)}`;
    equityDelta.className = pnlClass(pnlMetrics.today);
  }
  const unrealizedPctEl = byId("kpi-unrealized-pct");
  if (unrealizedPctEl) {
    unrealizedPctEl.textContent = fmtPct(unrealizedPct);
    unrealizedPctEl.className = pnlClass(unrealizedPct);
  }
  byId("kpi-positions").textContent = String(openPositions.length);
  const kpiOrders = byId("kpi-orders");
  if (kpiOrders) kpiOrders.textContent = String(dayOrders.length);
  const currency = byId("kpi-currency");
  if (currency) currency.textContent = marketCurrencyLabel(activeMarket);
  byId("last-cycle").textContent = state.auth?.admin
    ? (payload.last_cycle_at ? `Last cycle ${fmtTime(payload.last_cycle_at)}` : "waiting")
    : userSession.shared_backend
      ? (payload.last_cycle_at ? `Shared backend ${fmtTime(payload.last_cycle_at)}` : "shared backend waiting")
      : (userSession.last_cycle_at ? `Your signal cycle ${fmtTime(userSession.last_cycle_at)}` : "signals waiting");

  const pill = byId("status-pill");
  pill.textContent = controlRunning ? "Scanner running" : "Scanner idle";
  pill.className = `pill ${controlRunning ? "running" : "stopped"}`;

  const error = byId("error-box");
  const displayError = state.auth?.admin ? payload.last_error : userSession.last_error;
  if (displayError) {
    error.hidden = false;
    const feedPending = isFeedPending(payload);
    error.className = `error-box ${feedPending ? "warning" : ""}`;
    error.textContent = feedPending
      ? "Market data connection pending. Connect or refresh the selected feed when ready; the terminal remains available for account, settings, and analysis."
      : displayError;
  } else {
    error.hidden = true;
    error.textContent = "";
    error.className = "error-box";
  }

  byId("position-count").textContent = `${openPositions.length} open · ${Math.max(positions.length - openPositions.length, 0)} closed today`;
  byId("quote-count").textContent = `${quotes.length}/${allQuotes.length} quotes`;
  byId("account-quote-count").textContent = `${quotes.length} ${activeMarketLabel()} quotes`;
  byId("decision-count").textContent = signalSearchActive()
    ? `${activeMarketLabel()} · ${filteredCountLabel(visibleDecisions.length, decisions.length, "DB signal")}`
    : `${activeMarketLabel()} · ${filteredCountLabel(visibleDecisions.length, decisions.length, "signal")}`;
  updateSignalSearchStatus(decisions);
  byId("overview-decision-count").textContent = `${activeMarketLabel()} · ${latestDecisions.length} signals`;
  byId("suggestion-count").textContent = visibleSuggestions.length
    ? `${filteredCountLabel(visibleSuggestions.length, suggestions.length, "idea", "ideas")}`
    : suggestions.length
      ? `0/${suggestions.length} ideas`
      : "0 ideas";
  byId("order-count").textContent = `${filteredCountLabel(visibleOrders.length, dayOrders.length, "order")} today`;
  byId("strategy-count").textContent = `${strategies.length} strategies`;
  const planCount = byId("strategy-plan-count");
  if (planCount) planCount.textContent = `${filteredCountLabel(visibleStrategyPlans.length, strategyPlans.length, "plan")}`;
  const trackedCount = byId("tracked-count");
  if (trackedCount) trackedCount.textContent = visibleTrackedIdeas.length ? `${filteredCountLabel(visibleTrackedIdeas.length, trackedIdeas.length, "active idea", "active ideas")}` : "0 active";
  byId("sentiment-count").textContent = `${filteredCountLabel(visibleSentiment.length, sentiment.length, "event")}`;
  byId("nav-positions-badge").textContent = String(openPositions.length);
  byId("nav-suggestions-badge").textContent = String(suggestions.length);
  byId("nav-decisions-badge").textContent = String(latestDecisions.length);
  byId("nav-orders-badge").textContent = String(dayOrders.length);
  byId("nav-sentiment-badge").textContent = String(sentiment.length);
  byId("nav-logs-badge").textContent = state.auth?.admin ? String(state.logs.length) : "admin";
  byId("nav-overview-badge").textContent = controlRunning ? "on" : "off";
  updateMarketWorkspaceLabels(payload);

  renderPositions(positions);
  renderStrategies(strategies);
  updatePageFilterButtons();
  renderStrategyPlans(visibleStrategyPlans);
  renderIdeasWatchlist(suggestions, trackedIdeas, strategyPlans, payloadRowsForMarket(payload, "monitor_watchlist", activeMarket));
  renderTomorrowPlan(payload.tomorrow_plan || {});
  renderMobileNativeHeader(payload, quotes, activeMarket);
  renderTrackedIdeas(visibleTrackedIdeas);
  renderSentiment(visibleSentiment);
  renderQuotes(quotes);
  renderMarketTape(quotes, activeMarket);
  renderProductActionPanel(payload, suggestions, trackedIdeas, positions, decisions, scopedPortfolio);
  renderProductTrackingPanel(trackedIdeas, positions, suggestions);
  renderSuggestions(visibleSuggestions);
  renderDecisions(visibleDecisions, { controlRunning });
  renderOverviewDecisions(latestDecisions, { controlRunning });
  renderOverviewPositions(positions);
  renderMobilePortfolio(positions, trackedIdeas, scopedPortfolio);
  renderOrders(visibleOrders);
  renderMarketBreadth(scopedMarketContext(payload.market_breadth || {}, activeMarket));
  renderSectorRotation(scopedMarketContext(payload.sector_rotation_context || {}, activeMarket));
  renderPerformance(payload.performance || {});
  renderMacroEvents(payload.upcoming_macro_events || []);
  renderAgentConsole(payload);
  renderSelfAudit(payload.self_audit || {});
  renderShell(payload);
}

function updatePositionMarkKpis(scopedPortfolio, positions, allPositions, activeMarket, trackedIdeas, visibleTrackedIdeas) {
  const pnlMetrics = portfolioPnlMetrics(scopedPortfolio, positions, activeMarket);
  const unrealizedPct = Number(scopedPortfolio.invested) > 0
    ? (Number(pnlMetrics.unrealized || 0) / Number(scopedPortfolio.invested)) * 100
    : 0;
  byId("kpi-equity").textContent = fmtMarketMoney(scopedPortfolio.equity, activeMarket);
  byId("kpi-cash").textContent = fmtMarketMoney(scopedPortfolio.cash, activeMarket);
  byId("kpi-unrealized").textContent = fmtMarketMoney(pnlMetrics.unrealized, activeMarket);
  byId("kpi-unrealized").className = pnlClass(pnlMetrics.unrealized);
  const totalPnlEl = byId("kpi-total-pnl");
  if (totalPnlEl) {
    totalPnlEl.textContent = fmtMarketMoney(pnlMetrics.total, activeMarket);
    totalPnlEl.className = pnlClass(pnlMetrics.total);
  }
  const totalPnlNote = byId("kpi-total-pnl-note");
  if (totalPnlNote) {
    totalPnlNote.textContent = `realized ${fmtMarketMoney(pnlMetrics.realized, activeMarket)} + holdings`;
    totalPnlNote.className = pnlClass(pnlMetrics.realized);
  }
  const equityDelta = byId("kpi-equity-delta");
  if (equityDelta) {
    equityDelta.textContent = `Today ${fmtMarketMoney(pnlMetrics.today, activeMarket)} · ${portfolioTodayLabel(pnlMetrics)}`;
    equityDelta.className = pnlClass(pnlMetrics.today);
  }
  const unrealizedPctEl = byId("kpi-unrealized-pct");
  if (unrealizedPctEl) {
    unrealizedPctEl.textContent = fmtPct(unrealizedPct);
    unrealizedPctEl.className = pnlClass(unrealizedPct);
  }
  byId("kpi-positions").textContent = String(positions.length);
  byId("position-count").textContent = `${positions.length}/${allPositions.length} open`;
  byId("nav-positions-badge").textContent = String(positions.length);
  const trackedCount = byId("tracked-count");
  if (trackedCount) {
    trackedCount.textContent = visibleTrackedIdeas.length
      ? `${filteredCountLabel(visibleTrackedIdeas.length, trackedIdeas.length, "active idea", "active ideas")}`
      : "0 active";
  }
}

function renderPositionMarkPanels(payload) {
  if (!payload) return;
  const activeMarket = normalizeUiMarket(state.activeMarket);
  const allPositions = payload.positions || [];
  const positions = filterRowsByMarket(allPositions, activeMarket);
  const suggestions = payloadRowsForMarket(payload, "suggestions", activeMarket);
  const trackedIdeas = payloadRowsForMarket(payload, "tracked_ideas", activeMarket);
  const visibleTrackedIdeas = applySuggestionFilter(trackedIdeas);
  const decisions = sortDecisionRows(payloadRowsForMarket(payload, "decisions", activeMarket));
  const scopedPortfolio = marketPortfolioFromPayload(payload, activeMarket);
  updatePositionMarkKpis(scopedPortfolio, positions, allPositions, activeMarket, trackedIdeas, visibleTrackedIdeas);
  renderPositions(positions);
  renderOverviewPositions(positions);
  renderIdeasWatchlist(suggestions, trackedIdeas, state.latest?.strategy_plans || [], payloadRowsForMarket(payload, "monitor_watchlist", activeMarket));
  renderMobileNativeHeader(payload, filterRowsByMarket(payload.quotes || [], activeMarket), activeMarket);
  renderMobilePortfolio(positions, trackedIdeas, scopedPortfolio);
  renderTrackedIdeas(visibleTrackedIdeas);
  renderProductActionPanel(payload, suggestions, trackedIdeas, positions, decisions, scopedPortfolio);
  renderProductTrackingPanel(trackedIdeas, positions, suggestions);
}

function accountEditorIsFocused() {
  const active = document.activeElement;
  return Boolean(active?.closest?.("#account-body form"));
}

function applyAccountPositionMarks(payload = {}) {
  if (state.auth?.admin || !state.account) return;
  const paperPayload = payload.paper || {};
  const paper = {
    ...(state.account.paper || {}),
    ...paperPayload,
    positions: payload.positions || paperPayload.positions || state.account.paper?.positions || [],
    follow_history: payload.follow_history || paperPayload.follow_history || state.account.paper?.follow_history || [],
    closed_positions: paperPayload.closed_positions || state.account.paper?.closed_positions || [],
    portfolio: payload.portfolio || paperPayload.portfolio || state.account.paper?.portfolio || {},
    portfolio_by_market: payload.portfolio_by_market || paperPayload.portfolio_by_market || state.account.paper?.portfolio_by_market || {},
    cash_pool_by_market: paperPayload.cash_pool_by_market || state.account.paper?.cash_pool_by_market || {},
    realized_pnl_by_market: paperPayload.realized_pnl_by_market || state.account.paper?.realized_pnl_by_market || {},
    cash_by_market: paperPayload.cash_by_market || state.account.paper?.cash_by_market || {},
  };
  state.account = {
    ...state.account,
    tracked_ideas: payload.tracked_ideas || state.account.tracked_ideas || [],
    follow_history: payload.follow_history || state.account.follow_history || [],
    follow_history_by_market: payload.follow_history_by_market || state.account.follow_history_by_market || {},
    paper,
  };
  if (currentViewName() === "account" && !accountEditorIsFocused()) {
    renderAccount(state.account);
  }
}

function applyPositionMarks(payload = {}) {
  if (!payload || !state.latest) return;
  state.positionMarksLastAppliedAt = payload.updated_at || new Date().toISOString();
  state.latest = {
    ...state.latest,
    tracked_ideas: payload.tracked_ideas || state.latest.tracked_ideas || [],
    tracked_ideas_by_market: payload.tracked_ideas_by_market || state.latest.tracked_ideas_by_market || {},
    follow_history: payload.follow_history || state.latest.follow_history || [],
    follow_history_by_market: payload.follow_history_by_market || state.latest.follow_history_by_market || {},
    positions: payload.positions || state.latest.positions || [],
    portfolio: payload.portfolio || state.latest.portfolio || {},
    portfolio_by_market: payload.portfolio_by_market || state.latest.portfolio_by_market || {},
    paper_cash_pool_by_market: payload.paper_cash_pool_by_market || state.latest.paper_cash_pool_by_market || {},
    paper_realized_pnl_by_market: payload.paper_realized_pnl_by_market || state.latest.paper_realized_pnl_by_market || {},
    paper_exit_manager: payload.paper_exit_manager || state.latest.paper_exit_manager || {},
    equity_curve_by_market: payload.equity_curve_by_market || state.latest.equity_curve_by_market || {},
    equity_curve: payload.equity_curve || state.latest.equity_curve || [],
  };
  renderPositionMarkPanels(state.latest);
  applyAccountPositionMarks(payload);
}

async function refreshPositionMarks() {
  if (!state.auth?.authenticated || state.auth?.admin) return;
  const now = Date.now();
  if (now - Number(state.positionMarksLastFetchAt || 0) < 850) return;
  if (state.positionMarksInFlight) {
    state.positionMarksPending = true;
    return;
  }
  state.positionMarksLastFetchAt = now;
  state.positionMarksInFlight = true;
  try {
    const response = await fetch("/api/position-marks", { cache: "no-store" });
    const payload = await response.json().catch(() => ({ detail: `HTTP ${response.status}` }));
    if (response.status === 401) {
      handleUnauthorized(payload.detail || "Session expired. Sign in again.");
      return;
    }
    if (response.ok) {
      applyPositionMarks(payload);
    }
  } catch {
    /* Websocket/status refreshes will keep retrying; avoid noisy UI errors every second. */
  } finally {
    state.positionMarksInFlight = false;
    if (state.positionMarksPending && state.auth?.authenticated && !state.auth?.admin) {
      state.positionMarksPending = false;
      window.setTimeout(refreshPositionMarks, 0);
    }
  }
}

function startPositionMarkPolling() {
  if (!state.auth?.authenticated || state.auth?.admin || state.positionMarksTimer) return;
  state.positionMarksTimer = window.setInterval(refreshPositionMarks, 1000);
  refreshPositionMarks();
}

function stopPositionMarkPolling() {
  if (state.positionMarksTimer) {
    window.clearInterval(state.positionMarksTimer);
    state.positionMarksTimer = null;
  }
  state.positionMarksPending = false;
  state.positionMarksInFlight = false;
}

function renderProductActionPanel(payload, suggestions, trackedIdeas, positions, decisions, portfolio) {
  const panel = byId("product-action-panel");
  if (!panel) return;
  const market = normalizeUiMarket(state.activeMarket);
  const breadth = scopedMarketContext(payload.market_breadth || {}, market);
  const feed = marketDataLabel(payload, market);
  const readyIdeas = actionableIdeaRows(suggestions);
  const reviewPositions = (positions || []).filter(positionNeedsAction);
  const pnl = Number(portfolio.unrealized_pnl || 0);
  const pnlPct = Number(portfolio.invested) > 0 ? (pnl / Number(portfolio.invested)) * 100 : 0;
  const autoSummary = autoTradeSummary(payload, market);
  const lastDecision = decisions?.[0];
  const lastReason = lastDecision ? readableDecisionReason(lastDecision) : "";
  let headline = "Waiting for the first scan";
  let note = "OpenStocks will publish a tracked idea only after price, trend, risk, news, and decision gates are clear.";
  let cta = { label: "Analyze Symbol", view: "analyze" };
  let tone = "neutral";
  if (reviewPositions.length) {
    headline = `${reviewPositions.length} position${reviewPositions.length === 1 ? " needs" : "s need"} action`;
    note = "Risk, stop, or target rules are asking for attention before adding fresh exposure.";
    cta = { label: "Open Positions", view: "positions" };
    tone = "warning";
  } else if (readyIdeas.length) {
    headline = `${readyIdeas.length} trade idea${readyIdeas.length === 1 ? "" : "s"} ready`;
    note = "These ideas cleared the main gates. Check entry, stop, targets, and expiry before following.";
    cta = { label: "Open Watchlist", view: "suggestions" };
    tone = "positive";
  } else if (trackedIdeas.length) {
    headline = `${trackedIdeas.length} idea${trackedIdeas.length === 1 ? "" : "s"} being tracked`;
    note = "No fresh buy has cleared all gates yet. Continue tracking active ideas against targets and stops.";
    cta = { label: "Open Watchlist", view: "suggestions" };
    tone = "open";
  } else if (decisions.length) {
    headline = "No fresh buys yet";
    note = lastReason || "The latest scan did not find a setup strong enough for a new trade.";
    cta = { label: "See Scan Log", view: "decisions" };
    tone = "neutral";
  }
  const stance = marketStanceText(breadth);
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
          <span>${escapeHtml(autoSummary.label)}</span>
          <strong class="${escapeHtml(autoSummary.tone)}">${escapeHtml(autoSummary.value)}</strong>
          <small>${escapeHtml(shortValue(autoSummary.note, 92))}</small>
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
      "Open Watchlist",
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
          <small>${escapeHtml(positionActionState(row).label)} · ${escapeHtml(shortValue(summary.reason || row.strategy || "Position is being monitored.", 90))}</small>
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
    { label: "Trade Safety", value: `${fmtPct(audit.overall_score_pct ?? 0)}`, note: ok ? "rules clear" : "needs action" },
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
  const runtime = payload.runtime || {};
  const userSession = payload.user_signal_session || {};
  const activeMarket = normalizeUiMarket(state.activeMarket);
  const opportunity = scopedOpportunityScan(payload.opportunity_scan || {}, activeMarket);
  const premarket = scopedPreCatalystDiscovery(payload.pre_catalyst_discovery || {}, activeMarket);
  const marketAction = scopedMarketActionRadar(payload, activeMarket);
  const activeQuotes = filterRowsByMarket(payload.quotes || [], activeMarket);
  const rankedDecisions = sortDecisionRows(payloadRowsForMarket(payload, "decisions", activeMarket));
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
        ? "Signal Check"
      : "offline";
  const llmDisplay = llmProvider === "assigned" ? "Signal Check" : llmProvider;
  const llmUsage = payload.llm_usage?.today_utc || {};
  const llmActivity = userSession.last_llm_activity || {};
  const llmUsageText = llmUsage.calls
    ? `${fmtCompact(llmUsage.total_tokens)} tok · ${fmtUsd(llmUsage.cost_usd)} today`
    : `${llmModel || "model unset"}`;
  const executionMode = String(plainSetting("execution_mode", runtime.execution_mode || "paper")).toLowerCase();
  const liveMode = executionMode === "live" || executionMode === "live_trading";

  byId("top-provider").textContent = state.auth?.admin ? provider : feed.title;
  byId("top-llm").textContent = state.auth?.admin ? (llmProvider === "offline" ? "off" : llmModel) : "Signal Check";
  byId("top-execution").textContent = state.auth?.admin ? plainSetting("execution_mode", runtime.execution_mode || "-") : marketCurrencyLabel(activeMarket);

  const feedPending = isFeedPending(payload);
  const feedConnected = !feedPending && feed.hasQuotes;
  const paperBanner = byId("paper-mode-banner");
  if (paperBanner) {
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
    modePill.textContent = liveMode ? "LIVE" : "PAPER";
    modePill.className = `mode-pill ${liveMode ? "live" : "paper"}`;
  }
  byId("ops-feed").textContent = feedConnected ? feed.title : "Connect feed";
  byId("ops-feed-meta").textContent = feedPending
    ? "quotes paused until token/feed is ready"
    : feed.meta;
  byId("ops-llm").textContent = state.auth?.admin ? (llmProvider === "offline" ? "Offline" : llmDisplay) : "Signal Check";
  const userCreditMeta = state.credits
    ? `${fmtCredits(state.credits.credits_used_today || 0)} credits today · ${fmtCredits(state.credits.daily_credits_remaining || 0)} available`
    : `${fmtCredits(llmActivity.credits_charged || 0)} credits last cycle`;
  byId("ops-llm-meta").textContent = !state.auth?.admin
    ? userCreditMeta
    : `${llmMode} · ${llmUsageText}`;
  byId("ops-risk").textContent = `${plainSetting("max_positions", "-")} slots`;
  byId("ops-risk-meta").textContent = `${fmtPct(Number(plainSetting("max_order_value_pct", 0)) * 100)} max order`;
  const rawSymbols = Number(opportunity.scanned_symbols_this_cycle || opportunity.raw_symbols || 0);
  const selectedSymbols = Number(opportunity.selected_symbols || 0);
  const tradeableSymbols = Number(opportunity.tradeable_screening_symbols || 0);
  const scanPaused = Boolean(opportunity.scan_paused || opportunity.mode === "market_closed_tomorrow_prep");
  const lastOpenScan = opportunity.last_open_scan || {};
  const lastOpenScanned = Number(lastOpenScan.scanned_symbols_this_cycle || lastOpenScan.raw_symbols || 0);
  const openUniverseSymbols = Number(opportunity.open_universe_symbols || 0);
  const enabledUniverseSymbols = Number(opportunity.enabled_universe_symbols || 0);
  const universeSymbols = openUniverseSymbols || enabledUniverseSymbols || rawSymbols;
  const newsCoveredCandidates = Number(opportunity.news_covered_candidates || 0);
  const verifiedCatalysts = Number(opportunity.verified_catalyst_candidates || opportunity.positive_news_candidates || 0);
  const newsScreenedSymbols = Number(opportunity.news_screened_symbols || opportunity.news_probe?.symbols_requested || 0);
  const premarketCount = Number(premarket.candidate_count || 0);
  const premarketTopSymbols = (premarket.candidates || []).slice(0, 3).map((item) => item.symbol).filter(Boolean).join(", ");
  const marketActionCount = Number(marketAction.event_count || 0);
  const marketActionTopSymbols = (marketAction.events || []).slice(0, 3).map((item) => item.symbol).filter(Boolean).join(", ");
  const gainersPlaybook = opportunity.top_gainers_playbook || {};
  const playbookLabel = gainersPlaybook.label || (activeMarket === "US" ? "US Top Movers Playbook" : "NSE Top-Gainers Playbook");
  const gainersSummary = gainersPlaybook.signal_summary || {};
  const gainersBuyCount = Number(gainersSummary.strong_buy || 0) + Number(gainersSummary.moderate_buy || 0);
  const gainersEvaluated = Number(gainersPlaybook.total_gainers_evaluated || 0);
  const gainersTopSymbols = (gainersPlaybook.buy_signals || gainersPlaybook.records || []).slice(0, 3).map((item) => item.symbol).filter(Boolean).join(", ");
  const scanScopeText = universeSymbols > rawSymbols
    ? `${fmtNumber(rawSymbols)}/cycle from ${fmtNumber(universeSymbols)} open`
    : `${fmtNumber(rawSymbols)} scanned`;
  const newsScopeText = newsCoveredCandidates > 0
    ? `${fmtNumber(newsCoveredCandidates)} with news · ${fmtNumber(verifiedCatalysts)} verified catalysts`
    : `${fmtNumber(newsScreenedSymbols)} news checked · ${fmtNumber(verifiedCatalysts)} verified catalysts`;
  const pausedScanText = `${fmtNumber(enabledUniverseSymbols || universeSymbols)} enabled · ${fmtNumber(newsScreenedSymbols)} news prep · resumes at open`;
  const lastOpenText = lastOpenScanned ? `last open scan ${fmtNumber(lastOpenScanned)} symbols` : "open scan pending";
  byId("ops-opportunity").textContent = premarketCount
    ? `${fmtNumber(premarketCount)} premarket`
    : gainersBuyCount
      ? `${fmtNumber(gainersBuyCount)} gainer buys`
    : gainersEvaluated
      ? `${fmtNumber(gainersEvaluated)} gainers`
    : marketActionCount
      ? `${fmtNumber(marketActionCount)} movers`
    : scanPaused
      ? "Market closed"
      : (opportunity.enabled ? `${fmtNumber(selectedSymbols)} picked` : "Static");
  byId("ops-opportunity-meta").textContent = opportunity.enabled
    ? (premarketCount
      ? `${activeMarketLabel()} watchlist · ${premarketTopSymbols || "building"} · ${fmtNumber(premarket.live_confirmation_count || 0)} confirmed`
      : gainersBuyCount
        ? `${playbookLabel} · ${gainersTopSymbols || "building"} · ${fmtNumber(gainersEvaluated)} evaluated`
      : gainersEvaluated
        ? `${playbookLabel} · 0 buy · ${fmtNumber((gainersPlaybook.signal_summary || {}).watch || 0)} watch`
      : marketActionCount
        ? `Live radar · ${marketActionTopSymbols || "building"} · ${fmtNumber(tradeableSymbols)} tradeable`
      : scanPaused
        ? pausedScanText
        : `${scanScopeText} · ${fmtNumber(tradeableSymbols)} tradeable · ${newsScopeText} · ${(opportunity.top_candidates || []).slice(0, 3).map((item) => item.symbol).filter(Boolean).join(", ") || "building"}`)
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
  const cockpitReview = byId("ai-cockpit-review");
  const cockpitReviewNote = byId("ai-cockpit-review-note");
  const cockpitFeed = byId("ai-cockpit-feed");
  const cockpitFeedNote = byId("ai-cockpit-feed-note");
  const cockpitScan = byId("ai-cockpit-scan");
  const cockpitScanNote = byId("ai-cockpit-scan-note");
  const cockpitControl = byId("ai-cockpit-control");
  const cockpitControlNote = byId("ai-cockpit-control-note");
  const cockpitActionNote = byId("ai-cockpit-action-note");
  const topDecision = rankedDecisions[0] || {};
  const topDecisionScore = topDecision.symbol ? Math.round(decisionScorePercent(topDecision)) : 0;
  if (cockpitReview) cockpitReview.textContent = state.auth?.admin ? (llmProvider === "offline" ? "Offline" : llmDisplay) : "Signal Check";
  if (cockpitReviewNote) cockpitReviewNote.textContent = !state.auth?.admin ? userCreditMeta : `${llmMode} · ${llmUsageText}`;
  if (cockpitFeed) cockpitFeed.textContent = feedConnected ? feed.title : "Feed pending";
  if (cockpitFeedNote) cockpitFeedNote.textContent = feedPending ? "quotes paused until broker/data token is ready" : feed.meta;
  if (cockpitScan) cockpitScan.textContent = premarketCount
    ? `${fmtNumber(premarketCount)} premarket`
    : marketActionCount
      ? `${fmtNumber(marketActionCount)} movers`
    : scanPaused
      ? "Market closed"
      : (opportunity.enabled ? `${fmtNumber(selectedSymbols)}/${fmtNumber(tradeableSymbols || rawSymbols)} candidates` : "Static scan");
  if (cockpitScanNote) cockpitScanNote.textContent = opportunity.enabled
    ? (premarketCount
      ? `${premarketTopSymbols || "watchlist building"} · ${fmtNumber(premarket.live_confirmation_count || 0)} live confirmations`
      : marketActionCount
        ? `${marketActionTopSymbols || "market action"} · ${fmtNumber(tradeableSymbols || selectedSymbols)} ranked`
      : scanPaused
        ? `${pausedScanText} · ${lastOpenText}`
        : `${scanScopeText} · ${fmtNumber(verifiedCatalysts)} verified catalysts · ${(opportunity.top_candidates || []).slice(0, 2).map((item) => item.symbol).filter(Boolean).join(", ") || "ranking"}`)
    : `${fmtNumber(rankedDecisions.length)} ranked decisions in ${activeMarketLabel()}`;
  if (cockpitControl) cockpitControl.textContent = liveMode ? "Live guarded" : "Paper guarded";
  if (cockpitControlNote) cockpitControlNote.textContent = `${plainSetting("max_positions", "-")} slots · ${fmtPct(Number(plainSetting("max_order_value_pct", 0)) * 100)} max order`;
  if (cockpitActionNote) cockpitActionNote.textContent = topDecision.symbol
    ? `${rankedDecisions.length} ranked · ${topDecision.symbol} ${topDecisionScore}%`
    : `${rankedDecisions.length} ranked`;
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
    const adminSubtitles = {
      account: "Profile, broker connections, cash ledger, and personal signal controls",
      logs: "Scanner events, market feed messages, and runtime trace",
      users: "Admin users, roles, credits, and account access",
      settings: "Risk limits, broker setup, scan scope, and execution mode",
    };
    subtitle.textContent = ["overview", "suggestions", "analyze", "positions", "orders", "decisions", "sentiment"].includes(currentView)
      ? `${label} market only · ${quotes.length} quotes · ${positions.length} positions · ${decisions.length} decisions`
      : adminSubtitles[currentView] || "Admin controls · split-market routing · runtime settings";
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
  if (!authenticated || auth.admin) stopPositionMarkPolling();
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
  stopPositionMarkPolling();
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
      <span>Signal checks debit credits from the daily budget and account balance.</span>
      <span>Pure quote refresh, candle refresh, market screening, and news prep do not debit credits.</span>
      <span>Full market screening ranks eligible symbols first; credits apply when the shortlisted ideas receive signal checks.</span>
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
        <small>${escapeHtml(row.description || "Signal check")} · ${fmtTime(row.ts)}</small>
      </button>`).join("") || `<div class="empty-state product-empty"><strong>No signal checks yet</strong><span>Last five credit events will appear here.</span></div>`}
    </div>
    <small class="credit-policy-note">${fmtCredits(policy.estimated_signal_credit || 0)} estimated credits per signal check. Shared signal checks are split across active users.</small>
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
    body.innerHTML = emptyTableRow(6, "Admin logs are protected", "Sign in as admin to inspect backend cycle, feed, signal, and execution logs.");
    return;
  }
  byId("logs-count").textContent = `${state.logs.length} logs`;
  byId("nav-logs-badge").textContent = String(state.logs.length);
  if (!state.logs.length) {
    body.innerHTML = emptyTableRow(6, "No logs yet", "Cycle, feed, signal, and order events will appear here once the backend starts.");
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
  bindRowDetails(body, state.logs, "System Log");
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
    `Assign signal engine for ${user.username}\nUse provider:model`,
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

function followModeLabel(row = {}) {
  const mode = String(row.mode || row.classification || "").toUpperCase();
  if (mode === "PAPER") return "Paper";
  if (mode === "LIVE" || mode === "LIVE_REQUEST") return "Live request";
  return humanLabel(mode || "Track");
}

function followStateLabel(row = {}) {
  const status = String(row.status || row.follow_status || "").toUpperCase();
  const state = String(row.state || "").toUpperCase();
  if (status === "LIVE_EXIT_REQUESTED" || state === "EXIT_PENDING") return "Exit requested";
  if (state === "OPEN" || ["ACTIVE", "LIVE_REQUESTED"].includes(status)) return "Open";
  if (status === "EXITED" || state === "CLOSED") return "Closed";
  return humanLabel(status || state || "Open");
}

function followReasonText(row = {}) {
  const state = String(row.state || "").toUpperCase();
  const status = String(row.status || "").toUpperCase();
  if (state === "OPEN" && status === "ACTIVE") return "Open position.";
  if (status === "LIVE_REQUESTED") return "Live order request pending.";
  const reason = row.exit_reason || row.reason || row.status || row.state;
  return reasonFromSnakeCase(reason, humanLabel(reason || "Position update"));
}

function renderAccountFollowTable(rows = [], options = {}) {
  const emptyTitle = options.emptyTitle || "No positions";
  const emptyText = options.emptyText || "Positions will appear here after you follow an idea.";
  const visibleRows = (rows || []).slice(0, options.limit || 30);
  if (!visibleRows.length) {
    return `<div class="account-history-empty"><strong>${escapeHtml(emptyTitle)}</strong><span>${escapeHtml(emptyText)}</span></div>`;
  }
  const body = visibleRows.map((row) => {
    const market = rowMarket(row);
    const modeClass = cssToken(row.mode || row.classification || "track");
    const stateClass = cssToken(row.state || row.status || "open");
    const entryQty = Number(row.entry_qty ?? row.qty ?? 0);
    const openQty = Number(row.qty ?? 0);
    const entryPrice = firstFinite(row.entry_price, row.avg_price);
    const latestPrice = firstFinite(row.latest_price, row.market_price, row.exit_price, entryPrice);
    const exitPrice = firstFinite(row.exit_price, latestPrice);
    const realized = Number(row.realized_pnl || 0);
    const unrealized = Number(row.unrealized_pnl || 0);
    const statusLabel = followStateLabel(row);
    const isOpen = statusLabel === "Open";
    const secondaryPrice = isOpen ? latestPrice : exitPrice;
    const pnl = isOpen ? realized + unrealized : realized;
    return `
      <article class="account-history-card">
        <div class="account-history-card-main">
          <div class="account-history-symbol">
            <strong>${escapeHtml(row.symbol || "-")}</strong>
            <small>${escapeHtml(row.company_name || row.strategy || MARKET_LABELS[market] || market)}</small>
          </div>
          <div class="account-history-mode">
            <span class="trade-mode-pill mode-${modeClass}">${escapeHtml(followModeLabel(row))}</span>
            <small class="trade-state state-${stateClass}">${escapeHtml(statusLabel)}</small>
          </div>
        </div>
        <div class="account-history-metrics">
          <div>
            <span>Qty</span>
            <strong>${fmtNumber(entryQty)}</strong>
            <small>${isOpen ? `${fmtNumber(openQty)} open` : `${fmtNumber(row.closed_qty || entryQty)} closed`}</small>
          </div>
          <div>
            <span>Entry</span>
            <strong>${fmtTradeMoney(entryPrice, market)}</strong>
            <small>${isOpen ? "LTP" : "Exit"} ${fmtTradeMoney(secondaryPrice, market)}</small>
          </div>
          <div class="account-history-pnl ${pnlClass(pnl)}">
            <span>P&L</span>
            <strong>${fmtTradeMoney(pnl, market)}</strong>
            <small>${isOpen ? `${fmtTradeMoney(realized, market)} realized / ${fmtTradeMoney(unrealized, market)} open` : `${fmtPct(row.return_pct)} return`}</small>
          </div>
        </div>
        <div class="account-history-reason">
          <span>Reason</span>
          <strong>${escapeHtml(followReasonText(row))}</strong>
          <small>${fmtDateTime(row.opened_at)}${row.closed_at ? ` - ${fmtDateTime(row.closed_at)}` : ""}</small>
        </div>
      </article>
    `;
  }).join("");
  return `
    <div class="account-history-card-list">
      ${body}
    </div>
  `;
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
  const followHistory = paper.follow_history || account.follow_history || [];
  const openFollowHistory = followHistory.filter((row) => {
    const state = String(row.state || "").toUpperCase();
    const status = String(row.status || "").toUpperCase();
    return state === "OPEN" || ["ACTIVE", "LIVE_REQUESTED"].includes(status);
  });
  const closedFollowHistory = (paper.closed_positions || followHistory.filter((row) => String(row.state || "").toUpperCase() !== "OPEN")).slice(0, 40);
  const paperOpenCount = openFollowHistory.filter((row) => String(row.mode || "").toUpperCase() === "PAPER").length;
  const liveRequestCount = followHistory.filter((row) => String(row.mode || "").toUpperCase() === "LIVE").length;
  const closedPaperCount = followHistory.filter((row) => (
    String(row.mode || "").toUpperCase() === "PAPER" && String(row.state || "").toUpperCase() !== "OPEN"
  )).length;
  const userUpstox = state.auth?.user?.broker_accounts?.upstox || {};
  const userKite = state.auth?.user?.broker_accounts?.kite || {};
  const paperPositions = (paper.positions || []).length
    ? paper.positions
    : (account.positions || []).length
      ? account.positions
      : openFollowHistory;
  const indiaPositions = filterRowsByMarket(paperPositions, "IN");
  const usPositions = filterRowsByMarket(paperPositions, "US");
  const indiaPaper = portfolioByMarket.IN || portfolioMetricsForMarket(portfolio, indiaPositions, "IN");
  const usPaper = portfolioByMarket.US || portfolioMetricsForMarket(portfolio, usPositions, "US");
  const indiaPnl = portfolioPnlMetrics(indiaPaper, indiaPositions, "IN");
  const usPnl = portfolioPnlMetrics(usPaper, usPositions, "US");
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
      <small id="paper-cash-status">Starting paper cash per market; realized paper profit/loss is applied automatically.</small>
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
  const user = state.auth?.user || {};
  const initials = symbolInitials(user.display_name || user.username || "OS");
  const accountMenu = `
    <section class="account-profile-card">
      <div>
        <strong>${escapeHtml(user.display_name || user.username || "OpenStocks User")}</strong>
        <span>${escapeHtml(user.email || user.username || "")}</span>
      </div>
      <em>${escapeHtml(initials)}</em>
    </section>
    <section class="account-menu-list">
      <button type="button" data-view-jump="account"><span>Funds</span><strong>${fmtMarketMoney(indiaPaper.cash, "IN")}</strong></button>
      <button type="button" data-view-jump="settings"><span>Profile</span><strong>${escapeHtml(user.username || "-")}</strong></button>
      <button type="button" data-view-jump="settings"><span>Settings</span><strong>Risk, markets, tokens</strong></button>
      <button type="button" data-view-jump="account"><span>Connected apps</span><strong>${escapeHtml(userFeedLabel)}</strong></button>
      <button type="button" data-account-logout><span>Logout</span><strong>Sign out</strong></button>
    </section>
  `;
  const accountHistoryMarkup = `
    <div class="account-history-grid">
      <section class="account-history-section">
        <div class="account-history-head">
          <strong>Open Paper / Live</strong>
          <span>${fmtNumber(openFollowHistory.length)} active</span>
        </div>
        ${renderAccountFollowTable(openFollowHistory, { emptyTitle: "No open paper or live positions", emptyText: "Follow a BUY idea as Paper or Live request to see it here.", limit: 20 })}
      </section>
      <section class="account-history-section">
        <div class="account-history-head">
          <strong>Position History</strong>
          <span>${fmtNumber(closedFollowHistory.length)} recent</span>
        </div>
        ${renderAccountFollowTable(closedFollowHistory, { emptyTitle: "No closed positions yet", emptyText: "Exited paper and live-request positions will stay visible here.", limit: 30 })}
      </section>
    </div>
  `;
  byId("account-status").textContent = userFeedLabel;
  byId("account-body").innerHTML = `
    ${accountMenu}
    <div class="account-metrics">
      <div><span>Mode</span><strong>${paper.mode || "-"}</strong></div>
      <div><span>India Cash</span><strong>${fmtMarketMoney(indiaPaper.cash, "IN")}</strong></div>
      <div><span>India Equity</span><strong>${fmtMarketMoney(indiaPaper.equity, "IN")}</strong></div>
      <div class="account-pnl-card ${pnlClass(indiaPnl.today)}"><span>India Today P&L</span><strong class="${pnlClass(indiaPnl.today)}">${fmtMarketMoney(indiaPnl.today, "IN")}</strong><small>${escapeHtml(portfolioTodayLabel(indiaPnl))}</small></div>
      <div class="account-pnl-card ${pnlClass(indiaPnl.unrealized)}"><span>India Holdings P&L</span><strong class="${pnlClass(indiaPnl.unrealized)}">${fmtMarketMoney(indiaPnl.unrealized, "IN")}</strong><small>current holdings</small></div>
      <div class="account-pnl-card ${pnlClass(indiaPnl.total)}"><span>India Net P&L</span><strong class="${pnlClass(indiaPnl.total)}">${fmtMarketMoney(indiaPnl.total, "IN")}</strong><small>realized ${fmtMarketMoney(indiaPnl.realized, "IN")} + holdings</small></div>
      ${Number(indiaPaper.cash_deficit || 0) > 0 ? `<div><span>India Cash Gap</span><strong class="negative">${fmtMarketMoney(indiaPaper.cash_deficit, "IN")}</strong></div>` : ""}
      <div><span>US Cash</span><strong>${fmtMarketMoney(usPaper.cash, "US")}</strong></div>
      <div><span>US Equity</span><strong>${fmtMarketMoney(usPaper.equity, "US")}</strong></div>
      <div class="account-pnl-card ${pnlClass(usPnl.today)}"><span>US Today P&L</span><strong class="${pnlClass(usPnl.today)}">${fmtMarketMoney(usPnl.today, "US")}</strong><small>${escapeHtml(portfolioTodayLabel(usPnl))}</small></div>
      <div class="account-pnl-card ${pnlClass(usPnl.unrealized)}"><span>US Holdings P&L</span><strong class="${pnlClass(usPnl.unrealized)}">${fmtMarketMoney(usPnl.unrealized, "US")}</strong><small>current holdings</small></div>
      <div class="account-pnl-card ${pnlClass(usPnl.total)}"><span>US Net P&L</span><strong class="${pnlClass(usPnl.total)}">${fmtMarketMoney(usPnl.total, "US")}</strong><small>realized ${fmtMarketMoney(usPnl.realized, "US")} + holdings</small></div>
      ${Number(usPaper.cash_deficit || 0) > 0 ? `<div><span>US Cash Gap</span><strong class="negative">${fmtMarketMoney(usPaper.cash_deficit, "US")}</strong></div>` : ""}
      <div><span>User Feed</span><strong>${userFeedLabel}</strong></div>
      <div><span>Signal Action</span><strong>${escapeHtml(signalModeLabel(signalExecutionMode))}</strong></div>
      <div><span>Monitor Scope</span><strong>${monitorSymbols.length ? `${fmtNumber(monitorSymbols.length)} custom` : "Dynamic"}</strong></div>
      <div><span>Broker Sync</span><strong>${escapeHtml(brokerSync.status_label || brokerSync.status || "Not Connected")}</strong></div>
      <div><span>Tracked Ideas</span><strong>${fmtNumber(trackedIdeas.length)}</strong></div>
      <div><span>Open Paper</span><strong>${fmtNumber(paperOpenCount)}</strong></div>
      <div><span>Live Requests</span><strong>${fmtNumber(liveRequestCount)}</strong></div>
      <div><span>Closed Paper</span><strong>${fmtNumber(closedPaperCount)}</strong></div>
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
  const historyBody = byId("account-history-body");
  if (historyBody) historyBody.innerHTML = accountHistoryMarkup;
  const historyStatus = byId("account-history-status");
  if (historyStatus) historyStatus.textContent = `${fmtNumber(openFollowHistory.length)} open · ${fmtNumber(closedFollowHistory.length)} closed`;
  const cashForm = byId("paper-cash-form");
  if (cashForm) cashForm.addEventListener("submit", savePaperCash);
  const signalModeForm = byId("signal-mode-form");
  if (signalModeForm) signalModeForm.addEventListener("submit", saveSignalExecutionMode);
  const monitorForm = byId("monitor-symbols-form");
  if (monitorForm) monitorForm.addEventListener("submit", saveMonitorSymbols);
  const clearMonitorButton = byId("clear-monitor-symbols-btn");
  if (clearMonitorButton) clearMonitorButton.addEventListener("click", clearMonitorSymbols);
  byId("account-body")?.querySelector("[data-account-logout]")?.addEventListener("click", logout);
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
        <h3>${escapeHtml(settingsCategoryLabel(category))}</h3>
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

function settingsCategoryLabel(category) {
  const mapped = {
    "Agent Cycle": "Scan Cycle",
    "Full Audit": "Analysis Archive",
  };
  return mapped[category] || category;
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
  if (byId("admin-runtime-provider")) byId("admin-runtime-provider").value = plainSetting("llm_provider", "offline");
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
  return `Backend ${action} failed: ${reason}. Check that OpenStocks backend is running and port 8000 is reachable.`;
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
    status.textContent = `testing signal engine, ${elapsed}s / ${healthTimeout}s`;
  }, 1000);
  status.textContent = `testing signal engine, 0s / ${healthTimeout}s`;
  status.className = "settings-inline-status";
  button.disabled = true;
  try {
    const response = await fetch("/api/llm/test", { method: "POST", signal: controller.signal });
    const payload = await response.json();
    if (!response.ok || !payload.ok) {
      const reason = payload.reason || payload.detail || `HTTP ${payload.status_code || response.status}`;
      status.textContent = `Signal test failed: ${reason}`;
      status.className = "settings-inline-status negative";
      showDetails("Signal Test", payload);
      return;
    }
    status.textContent = `Signal engine ready: ${payload.model} · ${payload.latency_ms} ms`;
    status.className = "settings-inline-status positive";
    showDetails("Signal Test", payload);
  } catch (error) {
    const reason = error.name === "AbortError" ? `browser timed out after ${healthTimeout + 5}s` : error.message;
    status.textContent = `Signal test failed: ${reason}`;
    status.className = "settings-inline-status negative";
    showDetails("Signal Test", { ok: false, reason });
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
    startPositionMarkPolling();
    startMarketIndexPolling();
    startMarketIndexPolling();
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
    stopPositionMarkPolling();
    if (state.marketIndexTimer) {
      window.clearInterval(state.marketIndexTimer);
      state.marketIndexTimer = null;
    }
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
      "Open Watchlist",
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
      const marketStats = strategyPlanMarketStats(row, market);
      const marketConstituents = ((row.constituents_by_market || {})[market] || (row.constituents || []).filter((idea) => rowMarket(idea) === market));
      const ideas = marketConstituents.slice(0, 4);
      const symbolList = ideas.length
        ? ideas.map((idea) => {
            const life = ideaLifecycle(idea);
            return `<span class="plan-symbol ${escapeHtml(life.className)}"><strong>${escapeHtml(displayValue(idea.symbol, "Symbol"))}</strong><small>${escapeHtml(life.label)} · ${fmtPct(idea.current_return_pct || 0)}</small></span>`;
          }).join("")
        : marketStats.idea_count > 0
          ? `<span class="plan-symbol empty">${fmtNumber(marketStats.idea_count)} ${escapeHtml(activeMarketLabel())} stocks in this plan</span>`
        : `<span class="plan-symbol empty">No ${escapeHtml(activeMarketLabel())} stocks in this plan yet</span>`;
      const sinceSignals = marketStats.idea_count > 0
        ? `<strong class="${pnlClass(marketStats.avg_return_pct)}">${fmtPct(marketStats.avg_return_pct || 0)}</strong>`
        : `<strong class="muted">No signals</strong>`;
      const followRow = state.auth?.admin
        ? `<div class="plan-admin-note compact">Admin managed. Users follow with their own budget.</div>`
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
        <span><small>${escapeHtml(activeMarketLabel())} Stocks</small><strong>${fmtNumber(marketStats.idea_count)}</strong></span>
        <span><small>Since Signals</small>${sinceSignals}</span>
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

function strategyPlanMarketStats(row = {}, market = state.activeMarket) {
  const region = normalizeUiMarket(market);
  const stats = ((row.market_stats || row.by_market || {})[region]) || {};
  const fallbackIdeas = (row.constituents || []).filter((idea) => rowMarket(idea) === region);
  const ideaCount = numericValue(stats.idea_count);
  const avgReturn = numericValue(stats.avg_return_pct);
  const bestReturn = numericValue(stats.best_return_pct);
  const worstReturn = numericValue(stats.worst_return_pct);
  return {
    idea_count: ideaCount !== null ? ideaCount : fallbackIdeas.length,
    avg_return_pct: avgReturn !== null ? avgReturn : (
      fallbackIdeas.length
        ? fallbackIdeas.reduce((sum, idea) => sum + Number(idea.current_return_pct || 0), 0) / fallbackIdeas.length
        : 0
    ),
    best_return_pct: bestReturn !== null ? bestReturn : 0,
    worst_return_pct: worstReturn !== null ? worstReturn : 0,
  };
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

function findIndexQuote(rows = [], aliases = []) {
  const normalizedAliases = aliases.map((item) => String(item).toUpperCase().replace(/[^A-Z0-9]/g, ""));
  return (rows || []).find((row) => {
    const symbol = String(row.symbol || row.trading_symbol || row.name || "").toUpperCase().replace(/[^A-Z0-9]/g, "");
    const label = String(row.company_name || row.display_name || "").toUpperCase().replace(/[^A-Z0-9]/g, "");
    return normalizedAliases.some((alias) => symbol === alias || label === alias);
  });
}

function institutionalIndexQuote(payload = {}, label = "") {
  const liveItems = state.marketIndices?.IN?.items || {};
  const items = Object.keys(liveItems).length
    ? liveItems
    : payload?.institutional_context?.feeds?.indices?.items
    || payload?.institutional_context?.indices?.items
    || payload?.indices?.items
    || {};
  const raw = items[label] || items[String(label).toUpperCase()] || null;
  if (!raw || typeof raw !== "object") return null;
  const price = firstFinite(raw.last, raw.price, raw.value);
  if (price === null) return null;
  return {
    symbol: label,
    price,
    day_change_pct: firstFinite(raw.change_pct, raw.percentChange, raw.percent_change),
    day_change: firstFinite(raw.change, raw.variation, raw.change_value),
    source: "nse-index-feed",
  };
}

function mobileIndexPayload(payload = state.latest || {}, rows = [], market = "IN") {
  const region = normalizeUiMarket(market);
  if (region === "US") {
    return [
      { label: "S&P 500", quote: findIndexQuote(rows, ["SPY", "SPX", "GSPC", "S&P 500"]) },
      { label: "NASDAQ", quote: findIndexQuote(rows, ["QQQ", "IXIC", "NASDAQ"]) },
    ];
  }
  return [
    { label: "NIFTY 50", quote: institutionalIndexQuote(payload, "NIFTY 50") || findIndexQuote(rows, ["NIFTY", "NIFTY50", "NIFTY 50"]) },
    { label: "NIFTY BANK", quote: institutionalIndexQuote(payload, "NIFTY BANK") || findIndexQuote(rows, ["BANKNIFTY", "NIFTYBANK", "NIFTY BANK"]) },
  ];
}

function renderMobileNativeHeader(payload = state.latest || {}, rows = [], market = state.activeMarket) {
  const primaryLabel = byId("mobile-index-primary-label");
  const primaryValue = byId("mobile-index-primary-value");
  const primaryChange = byId("mobile-index-primary-change");
  const secondaryLabel = byId("mobile-index-secondary-label");
  const secondaryValue = byId("mobile-index-secondary-value");
  const secondaryChange = byId("mobile-index-secondary-change");
  if (!primaryLabel || !primaryValue || !primaryChange || !secondaryLabel || !secondaryValue || !secondaryChange) return;
  const [primary, secondary] = mobileIndexPayload(payload, rows, market);
  const paint = (slot, labelEl, valueEl, changeEl) => {
    const quote = slot.quote || {};
    const price = firstFinite(quote.price, quote.last_price, quote.close);
    const pct = quote.symbol ? quoteDayPct(quote) : null;
    const change = firstFinite(quote.day_change, quote.change, quote.change_value);
    const hasPct = Number.isFinite(pct);
    const sign = change !== null && change > 0 ? "+" : "";
    labelEl.textContent = slot.label;
    valueEl.textContent = price !== null ? fmtNumber(price) : "-";
    changeEl.textContent = hasPct
      ? `${change !== null ? `${sign}${fmtNumber(change)} ` : ""}${fmtPct(pct)}`
      : (quote.symbol ? "0.00%" : "feed pending");
    changeEl.className = hasPct ? pnlClass(pct) : "";
    valueEl.className = hasPct ? pnlClass(pct) : "";
  };
  paint(primary, primaryLabel, primaryValue, primaryChange);
  paint(secondary, secondaryLabel, secondaryValue, secondaryChange);
}

async function refreshMarketIndices() {
  if (!state.auth?.authenticated || state.marketIndicesInFlight) return;
  const market = normalizeUiMarket(state.activeMarket);
  if (market !== "IN") return;
  state.marketIndicesInFlight = true;
  try {
    const response = await fetch(`/api/market-indices?market=${encodeURIComponent(market)}`, { cache: "no-store" });
    const payload = await response.json().catch(() => ({ detail: `HTTP ${response.status}` }));
    if (!response.ok) throw new Error(payload.detail || `HTTP ${response.status}`);
    state.marketIndices[market] = payload;
    renderMobileNativeHeader(state.latest || {}, filterRowsByMarket(state.latest?.quotes || [], market), market);
  } catch {
    /* Keep last known indices; regular status refresh will retry. */
  } finally {
    state.marketIndicesInFlight = false;
  }
}

function startMarketIndexPolling() {
  if (state.marketIndexTimer) return;
  refreshMarketIndices();
  state.marketIndexTimer = window.setInterval(refreshMarketIndices, 30000);
}

function bindSwipeTabList(element) {
  if (!element || element.dataset.swipeBound === "1") return;
  element.dataset.swipeBound = "1";
  let startX = 0;
  let startY = 0;
  element.addEventListener("touchstart", (event) => {
    const touch = event.touches?.[0];
    if (!touch) return;
    startX = touch.clientX;
    startY = touch.clientY;
  }, { passive: true });
  element.addEventListener("touchend", (event) => {
    const touch = event.changedTouches?.[0];
    if (!touch) return;
    const deltaX = touch.clientX - startX;
    const deltaY = touch.clientY - startY;
    if (Math.abs(deltaX) < 48 || Math.abs(deltaX) < Math.abs(deltaY) * 1.4) return;
    const buttons = [...element.querySelectorAll("button")].filter((button) => !button.disabled && button.offsetParent !== null);
    if (buttons.length < 2) return;
    const activeIndex = Math.max(0, buttons.findIndex((button) => button.classList.contains("active") || button.getAttribute("aria-selected") === "true"));
    const nextIndex = Math.max(0, Math.min(buttons.length - 1, activeIndex + (deltaX < 0 ? 1 : -1)));
    if (nextIndex !== activeIndex) buttons[nextIndex].click();
  }, { passive: true });
}

function bindSwipeTabLists(root = document) {
  root.querySelectorAll("[role='tablist'], .page-filter-bar, .order-filter-tabs, .mobile-portfolio-tabs, .mobile-watchlist-tabs")
    .forEach(bindSwipeTabList);
}

function ideaIsFollowed(row = {}) {
  const followed = row.user_follow && typeof row.user_follow === "object" ? row.user_follow : {};
  const status = String(followed.status || row.follow_status || row.status || "").toUpperCase();
  if (["EXITED", "CLOSED", "CANCELLED", "REJECTED"].includes(status)) return false;
  return Boolean(row.follow_id || row.user_follow || row.mode || followed.id);
}

function ideaIsTradeReady(row = {}) {
  const action = rowActionText(row);
  const fresh = String(row.fresh_action || row.latest_system_action || "").toUpperCase();
  const readiness = String(row.decision_readiness || row.setup_bucket_label || "").toLowerCase();
  return action === "BUY" || fresh === "BUY_NOW" || readiness.includes("buy") || readiness.includes("trade");
}

function ideaIsHighQuality(row = {}) {
  return confidencePercent(row) >= 65 || Number(row.overall_score_pct || 0) >= 70 || Number(row.confluence || 0) >= 18;
}

function ideaDetails(row = {}) {
  const details = row.details && typeof row.details === "object" ? row.details : parseJsonObject(row.details_json);
  return details && typeof details === "object" ? details : {};
}

function ideaHasEvent(row = {}) {
  const details = ideaDetails(row);
  const full = details.full_spectrum || {};
  const eventRisk = full.corporate_event_risk || details.corporate_event_risk || {};
  const sentiment = full.news_sentiment || details.news_sentiment || {};
  return Boolean(
    row.catalyst_type ||
      row.catalyst_date ||
      row.earnings_date ||
      row.headline_count ||
      row.news_quality ||
      eventRisk.event_type ||
      eventRisk.earnings_date ||
      eventRisk.has_event ||
      sentiment.headline_count ||
      sentiment.score,
  );
}

function ideaSearchMatches(row = {}, search = "") {
  if (!search) return true;
  const text = [
    row.symbol,
    row.company_name,
    row.strategy,
    row.sector,
    row.industry,
    row.exchange,
    row.signal_type,
    row.suggestion,
  ].join(" ").toUpperCase();
  return text.includes(search);
}

function enrichPlanIdeaRows(rows = [], ideaIndex = new Map()) {
  return (rows || []).map((row) => {
    const idKey = row.id || row.idea_id ? `id:${row.id || row.idea_id}` : "";
    const symbolKey = row.symbol ? `symbol:${String(row.symbol).toUpperCase()}` : "";
    const base = ideaIndex.get(idKey) || ideaIndex.get(symbolKey) || {};
    return { ...base, ...row, user_follow: row.user_follow || base.user_follow };
  });
}

function ideaIndexByIdAndSymbol(rows = []) {
  const output = new Map();
  for (const row of rows || []) {
    if (row.id || row.idea_id) output.set(`id:${row.id || row.idea_id}`, row);
    if (row.symbol) output.set(`symbol:${String(row.symbol).toUpperCase()}`, row);
  }
  return output;
}

function planRowsForMarket(plan = {}, market = state.activeMarket, ideas = []) {
  const region = normalizeUiMarket(market);
  const code = String(plan.code || "").toUpperCase();
  const byMarket = plan.constituents_by_market || {};
  const matchingIdeas = code
    ? (ideas || []).filter((row) => String(row.plan_code || "").toUpperCase() === code && rowMarket(row) === region)
    : [];
  const marketConstituents = Array.isArray(byMarket[region]) ? byMarket[region] : [];
  const constituents = Array.isArray(plan.constituents) ? plan.constituents.filter((row) => rowMarket(row) === region) : [];
  const seen = new Set();
  return [...matchingIdeas, ...marketConstituents, ...constituents].filter((row) => {
    const key = row.id || row.idea_id
      ? `id:${row.id || row.idea_id}`
      : `symbol:${String(row.symbol || "").toUpperCase()}:${String(row.plan_code || code)}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

function watchlistQualityGroups(ideas = []) {
  return [
    { key: "buys", label: "Buys", rows: sortSuggestionRows((ideas || []).filter(ideaIsTradeReady)) },
    { key: "high", label: "High Confidence", rows: sortSuggestionRows((ideas || []).filter(ideaIsHighQuality)) },
  ];
}

function ideaWatchlistGroups(suggestions = [], trackedIdeas = [], strategyPlans = [], monitorWatchlist = []) {
  const ideas = sortSuggestionRows(suggestions || []);
  const market = normalizeUiMarket(state.activeMarket);
  const ideaIndex = ideaIndexByIdAndSymbol([...(suggestions || []), ...(trackedIdeas || [])]);
  const qualityGroups = watchlistQualityGroups(ideas);
  const monitorRows = (monitorWatchlist || []).slice();
  const monitorGroups = monitorRows.length
    ? [{ key: "monitor", label: `Monitor ${monitorRows.length}`, rows: monitorRows }]
    : [];
  const planSources = (strategyPlans || []).filter((plan) => plan.enabled !== false && plan.enabled !== 0);
  const derivedPlanSources = planSources.length
    ? []
    : Array.from(new Set(ideas.map((row) => String(row.plan_code || "").trim()).filter(Boolean)))
        .map((code) => ({ code, name: humanLabel(code) }));
  const planGroups = [...planSources, ...derivedPlanSources]
    .map((plan) => {
      const planRows = planRowsForMarket(plan, market, ideas);
      const rows = sortSuggestionRows(enrichPlanIdeaRows(planRows, ideaIndex));
      return {
        key: `plan:${plan.code || plan.name}`,
        label: plan.name || plan.code || "Plan",
        rows,
        plan,
      };
    });
  const tracked = sortSuggestionRows((trackedIdeas || []).slice());
  return [...qualityGroups, ...monitorGroups, ...planGroups, { key: "tracked", label: `Watchlist ${tracked.length}`, rows: tracked }];
}

function ideaWatchlistReturn(row = {}) {
  return Number(row.return_pct ?? row.current_return_pct ?? row.user_follow?.return_pct ?? 0);
}

function ideaWatchlistPrice(row = {}) {
  return firstFinite(row.follow_latest_price, row.user_follow?.latest_price, row.latest_price, row.price, row.entry_price, row.follow_entry_price);
}

function quoteForIdea(row = {}) {
  const symbol = String(row.symbol || "").toUpperCase();
  if (!symbol) return null;
  const market = rowMarket(row);
  return (state.latest?.quotes || []).find((quote) => String(quote.symbol || "").toUpperCase() === symbol && rowMarket(quote) === market) || null;
}

function ideaWatchlistMove(row = {}) {
  const quote = quoteForIdea(row);
  if (!quote) return { pct: ideaWatchlistReturn(row), text: `${fmtPct(ideaWatchlistReturn(row))} since signal` };
  const price = firstFinite(quote.price, quote.last_price, quote.close);
  const close = firstFinite(quote.close, quote.prev_close, quote.previous_close);
  const change = price !== null && close !== null ? price - close : null;
  const pct = quoteDayPct(quote);
  const sign = change !== null && change > 0 ? "+" : "";
  return {
    pct,
    price,
    text: change !== null ? `${sign}${fmtNumber(change)} (${fmtPct(pct)})` : fmtPct(pct),
  };
}

function ideaWatchlistMeta(row = {}) {
  const market = rowMarket(row);
  const exchange = row.exchange || (market === "IN" ? "NSE" : "US");
  const parts = [`<span>${escapeHtml(exchange)}</span>`];
  if (ideaHasEvent(row)) parts.push(`<strong>EVENT</strong>`);
  const mode = String(row.mode || row.user_follow?.mode || "").toUpperCase();
  if (mode && mode !== "TRACK") parts.push(`<strong>${escapeHtml(mode)}</strong>`);
  return parts.join("");
}

function ideaWatchlistKey(row = {}, index = 0) {
  const id = row.id || row.idea_id || row.follow_id || row.user_follow?.id;
  if (id) return `id:${id}`;
  return `row:${rowMarket(row)}:${String(row.symbol || "").toUpperCase()}:${index}`;
}

function ideaWatchlistTargets(row = {}) {
  const rawTargets = Array.isArray(row.target_status) && row.target_status.length
    ? row.target_status
    : Array.isArray(row.targets)
      ? row.targets
      : [];
  return normalizedTargets(rawTargets);
}

function ideaWatchlistLevelsHtml(row = {}, market = rowMarket(row), compact = false) {
  const targets = ideaWatchlistTargets(row);
  const t1 = targets[0] || {};
  const entry = formatZone(row.entry_zone, market);
  const stop = firstPositiveFinite(row.stop_loss, row.exit_plan?.stop_loss);
  if (compact) {
    return `<div class="watchlist-levels compact">
      <span><small>Entry</small><strong>${escapeHtml(entry)}</strong></span>
      <span><small>SL</small><strong class="negative">${fmtTradeMoney(stop, market)}</strong></span>
      <span><small>T1</small><strong class="positive">${fmtTradeMoney(t1.price, market)}</strong></span>
    </div>`;
  }
  return `<div class="watchlist-levels">
    <span><small>Entry</small><strong>${escapeHtml(entry)}</strong></span>
    <span><small>Stop</small><strong class="negative">${fmtTradeMoney(stop, market)}</strong></span>
    <span><small>T1</small><strong class="positive">${fmtTradeMoney(t1.price, market)}</strong></span>
  </div>`;
}

function ideaWatchlistSignalHtml(row = {}) {
  const opportunity = opportunityStatePayload(row);
  const signal = row.display_signal || row.suggestion || row.signal_type || opportunity.label || "Watch";
  const confidence = confidencePercent(row);
  const action = rowActionText(row) || String(signal || "WATCH").toUpperCase();
  return `<div class="watchlist-signal-cell">
    <span class="watchlist-signal-pill ${escapeHtml(cssToken(action))}">${escapeHtml(signal)}</span>
    <small>${confidence ? `${fmtNumber(confidence)}% confidence` : escapeHtml(opportunity.label || "scanner ranked")}</small>
  </div>`;
}

function ideaWatchlistSetupHtml(row = {}) {
  const opportunity = opportunityStatePayload(row);
  const setup = row.strategy || row.plan_name || row.plan_code || row.setup_bucket_label || row.signal_strategy || "Watchlist";
  const note = opportunity.summary || row.display_reason || row.setup_bucket_reason || readableDecisionReason(row);
  return `<div class="watchlist-setup-cell">
    <strong>${escapeHtml(shortValue(setup, 36))}</strong>
    <small>${escapeHtml(shortValue(note, 58))}</small>
  </div>`;
}

function ideaWatchlistActionHtml(row = {}) {
  const followed = ideaIsFollowed(row);
  const mode = String(row.mode || row.user_follow?.mode || "TRACK").toUpperCase();
  const rowId = row.id || row.idea_id || "";
  if (followed) {
    return `<span class="mobile-watchlist-status">${escapeHtml(mode === "TRACK" ? "TRACKING" : `${mode} ACTIVE`)}</span>`;
  }
  if (!rowId) return `<button type="button" data-watchlist-open-detail>View</button>`;
  return `<span class="mobile-watchlist-actions">
    <button type="button" data-watchlist-buy-row data-idea-id="${escapeHtml(rowId)}">${preferredManualTradeMode() === "LIVE" ? "Buy Live" : "Buy Paper"}</button>
    <button type="button" data-watchlist-track data-idea-id="${escapeHtml(rowId)}">Track</button>
  </span>`;
}

function ideaWatchlistRowHtml(row = {}, index = 0, selectedKey = "") {
  const move = ideaWatchlistMove(row);
  const price = firstFinite(move.price, ideaWatchlistPrice(row));
  const key = ideaWatchlistKey(row, index);
  const market = rowMarket(row);
  return `<article class="mobile-watchlist-row ${key === selectedKey ? "active" : ""}" role="button" tabindex="0" data-index="${index}" data-row-key="${escapeHtml(key)}">
    <div class="mobile-watchlist-symbol">
      <strong>${escapeHtml(displayValue(row.symbol, "Symbol"))}</strong>
      <small>${ideaWatchlistMeta(row)}</small>
    </div>
    ${ideaWatchlistSignalHtml(row)}
    ${ideaWatchlistSetupHtml(row)}
    <div class="mobile-watchlist-price ${pnlClass(move.pct)}">
      <strong>${fmtNumber(price)}</strong>
      <small>${escapeHtml(move.text)}</small>
    </div>
    ${ideaWatchlistLevelsHtml(row, market, true)}
    <div class="watchlist-row-actions">${ideaWatchlistActionHtml(row)}</div>
  </article>`;
}

function watchlistDetailPanelHtml(row = {}) {
  if (!row || !Object.keys(row).length) {
    return `<div class="watchlist-detail-empty">
      <strong>Select a stock</strong>
      <span>Pick a row to see price, setup, trade levels, and paper/live actions.</span>
    </div>`;
  }
  const market = rowMarket(row);
  const quote = quoteForIdea(row) || {};
  const move = ideaWatchlistMove(row);
  const price = firstFinite(move.price, ideaWatchlistPrice(row));
  const exchange = row.exchange || quote.exchange || (market === "IN" ? "NSE" : "US");
  const opportunity = opportunityStatePayload(row);
  const lifecycle = ideaLifecycle(row);
  const dayPct = Number.isFinite(Number(move.pct)) ? Number(move.pct) : null;
  const dayChange = (() => {
    const direct = firstFinite(quote.day_change, quote.change);
    if (direct !== null) return direct;
    const close = firstFinite(quote.prev_close, quote.previous_close, quote.close);
    return price !== null && close !== null ? price - close : null;
  })();
  const signal = row.display_signal || row.suggestion || row.signal_type || lifecycle.label;
  const setup = row.strategy || row.plan_name || row.plan_code || row.setup_bucket_label || "Watchlist setup";
  const reason = opportunity.summary || row.display_reason || row.setup_bucket_reason || readableDecisionReason(row);
  const nextStep = opportunity.next_step || lifecycle.note || ideaTimelineText(row);
  const targets = ideaWatchlistTargets(row);
  return `<section class="watchlist-detail-card">
    <header class="watchlist-detail-head">
      <div>
        <span>${escapeHtml(exchange)} · ${escapeHtml(activeMarketLabel())}</span>
        <h3>${escapeHtml(displayValue(row.symbol, "Symbol"))}</h3>
        <p>${escapeHtml(shortValue(setup, 70))}</p>
      </div>
      <span class="watchlist-signal-pill ${escapeHtml(cssToken(rowActionText(row) || signal))}">${escapeHtml(signal)}</span>
    </header>

    <div class="watchlist-detail-price">
      <strong class="${pnlClass(dayPct)}">${fmtTradeMoney(price, market)}</strong>
      <span class="${pnlClass(dayPct)}">${dayChange !== null ? fmtSignedTradeMoney(dayChange, market) : "-"} (${fmtSignedPct(dayPct)})</span>
      <small>${confidencePercent(row) ? `${fmtNumber(confidencePercent(row))}% confidence` : escapeHtml(lifecycle.label)}</small>
    </div>

    <div class="watchlist-detail-metrics">
      <div><span>Volume</span><strong>${fmtCompact(quote.volume)}</strong></div>
      <div><span>Open</span><strong>${fmtTradeMoney(quote.open, market)}</strong></div>
      <div><span>High</span><strong>${fmtTradeMoney(quote.high || quote.day_high, market)}</strong></div>
      <div><span>Low</span><strong>${fmtTradeMoney(quote.low || quote.day_low, market)}</strong></div>
    </div>

    <section class="watchlist-detail-section">
      <h4>Trade Levels</h4>
      ${ideaWatchlistLevelsHtml(row, market)}
      ${targets.length > 1 ? targetLadderHtml({ targets }, market, true) : ""}
    </section>

    <section class="watchlist-detail-section">
      <h4>Why It Is Here</h4>
      <p>${escapeHtml(shortValue(reason, 190))}</p>
    </section>

    <section class="watchlist-detail-section">
      <h4>Next Action</h4>
      <p>${escapeHtml(shortValue(nextStep, 170))}</p>
    </section>

    <div class="watchlist-detail-actions">
      <button class="buy-action" type="button" data-watchlist-buy>${escapeHtml(preferredManualTradeLabel())}</button>
      <button type="button" data-watchlist-track-panel>Track</button>
      <button type="button" data-watchlist-open-detail>Full audit</button>
    </div>
  </section>`;
}

function bindWatchlistDetailPanel(row = {}) {
  const panel = byId("ideas-watchlist-detail");
  if (!panel) return;
  panel.querySelectorAll("[data-watchlist-buy]").forEach((button) => {
    button.addEventListener("click", () => buyWatchlistIdea(row, button));
  });
  panel.querySelectorAll("[data-watchlist-track-panel]").forEach((button) => {
    button.addEventListener("click", () => followIdea(row, "track", button));
  });
  panel.querySelectorAll("[data-watchlist-open-detail]").forEach((button) => {
    button.addEventListener("click", () => showDetails("Watchlist Idea", { __detail_type: "watchlist_stock", ...row }));
  });
}

function renderWatchlistDetail(row = {}) {
  const panel = byId("ideas-watchlist-detail");
  if (!panel) return;
  panel.innerHTML = watchlistDetailPanelHtml(row);
  bindWatchlistDetailPanel(row);
}

function renderIdeasWatchlist(suggestions = [], trackedIdeas = [], strategyPlans = state.latest?.strategy_plans || [], monitorWatchlist = []) {
  const tabs = byId("ideas-watchlist-tabs");
  const body = byId("ideas-watchlist-body");
  if (!tabs || !body) return;
  const groups = ideaWatchlistGroups(suggestions, trackedIdeas, strategyPlans, monitorWatchlist);
  if (state.activeIdeaGroup === "big" || !groups.some((group) => group.key === state.activeIdeaGroup)) {
    state.activeIdeaGroup = groups[0]?.key || "buys";
  }
  let activeGroup = groups.find((group) => group.key === state.activeIdeaGroup) || groups[0] || { rows: [] };
  const monitorGroup = groups.find((group) => group.key === "monitor" && (group.rows || []).length);
  if (monitorGroup && state.activeIdeaGroup === "buys" && !(activeGroup.rows || []).length && !(suggestions || []).length) {
    state.activeIdeaGroup = monitorGroup.key;
    activeGroup = monitorGroup;
  }
  const search = String(state.ideaWatchlistSearch || "").trim().toUpperCase();
  const rows = (activeGroup.rows || []).filter((row) => ideaSearchMatches(row, search));
  const visibleRows = rows.slice(0, 60);
  if (!visibleRows.some((row, index) => ideaWatchlistKey(row, index) === state.selectedIdeaWatchlistKey)) {
    state.selectedIdeaWatchlistKey = visibleRows.length ? ideaWatchlistKey(visibleRows[0], 0) : "";
  }
  const selectedRow = visibleRows.find((row, index) => ideaWatchlistKey(row, index) === state.selectedIdeaWatchlistKey) || visibleRows[0] || {};
  state.currentIdeaWatchlistRows = rows;
  tabs.innerHTML = groups
    .map((group) => `<button type="button" class="${group.key === activeGroup.key ? "active" : ""}" data-idea-group="${escapeHtml(group.key)}">${escapeHtml(group.label)}</button>`)
    .join("");
  bindSwipeTabList(tabs);
  const count = byId("ideas-watchlist-count");
  if (count) count.textContent = `${rows.length}/${(activeGroup.rows || []).length}`;
  body.innerHTML = rows.length
    ? visibleRows.map((row, index) => ideaWatchlistRowHtml(row, index, state.selectedIdeaWatchlistKey)).join("")
    : emptyBlock(
        `No ${activeMarketLabel()} rows in ${activeGroup.label || "this group"}`,
        "Run the scanner or switch tabs. Trackable symbols appear here as clean watchlist rows.",
        "Run Stock Check",
        "analyze",
      );
  renderWatchlistDetail(selectedRow);
  tabs.querySelectorAll("[data-idea-group]").forEach((button) => {
    button.addEventListener("click", () => {
      state.activeIdeaGroup = button.dataset.ideaGroup || "buys";
      state.selectedIdeaWatchlistKey = "";
      renderIdeasWatchlist(suggestions, trackedIdeas, strategyPlans, monitorWatchlist);
    });
  });
  body.querySelectorAll(".mobile-watchlist-row").forEach((card) => {
    const row = visibleRows[Number(card.dataset.index)];
    card.addEventListener("click", (event) => {
      if (event.target.closest("[data-watchlist-track], [data-watchlist-buy-row], [data-watchlist-open-detail]")) return;
      if (!row) return;
      if (window.matchMedia && window.matchMedia("(max-width: 767px)").matches) {
        showDetails("Watchlist Idea", { __detail_type: "watchlist_stock", ...row });
        return;
      }
      state.selectedIdeaWatchlistKey = card.dataset.rowKey || ideaWatchlistKey(row, Number(card.dataset.index));
      body.querySelectorAll(".mobile-watchlist-row").forEach((item) => item.classList.toggle("active", item === card));
      renderWatchlistDetail(row);
    });
  });
  body.querySelectorAll("[data-watchlist-buy-row]").forEach((button) => {
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      const row = visibleRows.find((item) => Number(item.id || item.idea_id || 0) === Number(button.dataset.ideaId));
      buyWatchlistIdea(row || {}, button);
    });
  });
  body.querySelectorAll("[data-watchlist-track]").forEach((button) => {
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      const row = visibleRows.find((item) => Number(item.id || item.idea_id || 0) === Number(button.dataset.ideaId));
      followIdea(row || Number(button.dataset.ideaId), "track", button);
    });
  });
  body.querySelectorAll("[data-watchlist-open-detail]").forEach((button) => {
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      const card = button.closest(".mobile-watchlist-row");
      const row = visibleRows[Number(card?.dataset.index || 0)];
      if (row) showDetails("Watchlist Idea", { __detail_type: "watchlist_stock", ...row });
    });
  });
}

const TOMORROW_PLAN_SECTIONS = [
  { key: "ready_at_open", label: "Ready At Open", empty: "No ready-at-open trades. That is fine; tomorrow starts with validation." },
  { key: "near_breakout", label: "Near Breakout", empty: "No near-breakout watch names prepared yet." },
  { key: "news_watch", label: "News Watch", empty: "No catalyst watch names prepared yet." },
  { key: "position_actions", label: "Position Actions", empty: "No open position action required." },
  { key: "avoid", label: "Avoid", empty: "No avoid names logged for tomorrow." },
];

function tomorrowPlanSectionRows(plan = {}, sectionKey = "") {
  const sections = plan.sections && typeof plan.sections === "object" ? plan.sections : {};
  if (Array.isArray(sections[sectionKey])) return sections[sectionKey];
  return (plan.items || []).filter((item) => String(item.section || "") === sectionKey);
}

function tomorrowPlanLevelsHtml(item = {}) {
  const market = rowMarket(item);
  const parts = [
    ["Trigger", item.trigger_price],
    ["Max", item.max_entry],
    ["Stop", item.stop_loss],
    ["T1", item.target1],
  ].filter(([, value]) => numericValue(value) !== null);
  if (!parts.length) return `<span><small>Validation</small><strong>${escapeHtml(item.validation ? "Required" : "Check")}</strong></span>`;
  return parts
    .map(([label, value]) => `<span><small>${escapeHtml(label)}</small><strong class="${label === "Stop" ? "negative" : label === "T1" ? "positive" : ""}">${fmtTradeMoney(value, market)}</strong></span>`)
    .join("");
}

function tomorrowPlanRowHtml(item = {}, index = 0) {
  const action = String(item.action || "WATCH").toUpperCase();
  const section = String(item.section || "").toLowerCase();
  const score = Number(item.score || 0);
  const confidence = Number(item.confidence || 0);
  return `<article class="tomorrow-plan-row plan-${escapeHtml(cssToken(section || action))}" role="button" tabindex="0" data-tomorrow-index="${index}">
    <div class="tomorrow-plan-symbol">
      <span class="tomorrow-action ${escapeHtml(cssToken(action))}">${escapeHtml(action)}</span>
      <div>
        <strong>${escapeHtml(displayValue(item.symbol, "Symbol"))}</strong>
        <small>${escapeHtml(item.name || item.strategy || "Prepared candidate")}</small>
      </div>
    </div>
    <div class="tomorrow-plan-levels">${tomorrowPlanLevelsHtml(item)}</div>
    <div class="tomorrow-plan-reason">
      <strong>${escapeHtml(item.strategy || "validation plan")}</strong>
      <p>${escapeHtml(shortValue(item.rationale || item.validation || "Validate pre-open and first live candle before action.", 150))}</p>
    </div>
    <div class="tomorrow-plan-score">
      <strong>${fmtNumber(score)}</strong>
      <small>${confidence ? `${fmtNumber(confidence)}% confidence` : "prepared"}</small>
    </div>
  </article>`;
}

function renderTomorrowPlan(rawPlan = {}) {
  const body = byId("tomorrow-plan-body");
  const count = byId("tomorrow-plan-count");
  const rules = byId("tomorrow-plan-rules");
  const title = byId("tomorrow-plan-title");
  if (!body) return;
  const market = normalizeUiMarket(state.activeMarket);
  const plan = scopedTomorrowPlan(rawPlan, market);
  if (title) title.textContent = `${activeMarketLabel()} Tomorrow Plan`;
  const items = plan.items || [];
  if (count) {
    const ready = Number(plan.summary?.ready_at_open || tomorrowPlanSectionRows(plan, "ready_at_open").length || 0);
    count.textContent = items.length ? `${items.length} items · ${ready} ready` : "not prepared";
  }
  const preopenRules = Array.isArray(plan.preopen_rules) ? plan.preopen_rules : [];
  if (rules) {
    rules.innerHTML = preopenRules.length
      ? preopenRules.slice(0, 5).map((rule) => `<span><strong>${escapeHtml(rule.time || "")}</strong>${escapeHtml(rule.action || "")}</span>`).join("")
      : `<span><strong>Post-market</strong>Run closed-market prep to build tomorrow's battle sheet.</span>`;
  }
  if (!items.length) {
    body.innerHTML = emptyBlock(
      `No ${activeMarketLabel()} tomorrow plan yet`,
      "After market close, OpenStocks will prepare ready names, near breakouts, news watch, position actions, and avoid names for the next session.",
      "Run Stock Check",
      "analyze",
    );
    state.currentTomorrowPlanRows = [];
    return;
  }
  const renderedRows = [];
  body.innerHTML = TOMORROW_PLAN_SECTIONS.map((section) => {
    const rows = tomorrowPlanSectionRows(plan, section.key);
    renderedRows.push(...rows);
    return `<section class="tomorrow-plan-section">
      <div class="tomorrow-plan-section-head">
        <strong>${escapeHtml(section.label)}</strong>
        <span>${fmtNumber(rows.length)}</span>
      </div>
      <div class="tomorrow-plan-list">
        ${rows.length ? rows.slice(0, 12).map((item) => tomorrowPlanRowHtml(item, renderedRows.indexOf(item))).join("") : `<div class="tomorrow-plan-empty">${escapeHtml(section.empty)}</div>`}
      </div>
    </section>`;
  }).join("");
  state.currentTomorrowPlanRows = renderedRows;
  body.querySelectorAll("[data-tomorrow-index]").forEach((rowEl) => {
    const item = renderedRows[Number(rowEl.dataset.tomorrowIndex)];
    if (item) rowEl.addEventListener("click", () => showDetails("Tomorrow Plan", item));
  });
}

function portfolioSearchMatches(row = {}, search = "") {
  if (!search) return true;
  const text = [
    row.symbol,
    row.company_name,
    row.strategy,
    row.sector,
    row.industry,
    row.exchange,
    row.mode,
  ].join(" ").toUpperCase();
  return text.includes(search);
}

function mobileHoldingRowHtml(row = {}, index = 0) {
  const market = rowMarket(row);
  const latest = positionLatestPrice(row);
  const entry = positionEntryPrice(row);
  const qty = positionQuantity(row);
  const invested = entry !== null ? entry * qty : null;
  const pnl = positionUnrealizedPnl(row);
  const explicitReturnPct = firstFinite(row.return_pct, row.user_follow?.return_pct, row.current_return_pct);
  const returnPct = explicitReturnPct !== null
    ? explicitReturnPct
    : latest !== null && entry !== null && entry > 0
      ? ((latest - entry) / entry) * 100
      : null;
  const dayPct = positionStockDayPct(row, market);
  const mode = String(row.mode || row.user_follow?.mode || "TRACK").toUpperCase();
  const exchange = row.exchange || (market === "IN" ? "NSE" : "US");
  return `<article class="mobile-portfolio-row broker-row" role="button" tabindex="0" data-holding-index="${index}">
    <div class="mobile-portfolio-main">
      <small>Qty. ${fmtNumber(qty)} <b>•</b> Avg. ${fmtTradeMoney(entry, market)}</small>
      <strong>${escapeHtml(displayValue(row.symbol, "Symbol"))}</strong>
      <em>Invested ${fmtTradeMoney(invested, market)} <span>${escapeHtml(exchange)} · ${escapeHtml(mode === "TRACK" ? "WATCH" : mode)}</span></em>
    </div>
    <div class="mobile-portfolio-price ${pnlClass(pnl ?? returnPct)}">
      <small class="${pnlClass(returnPct)}">${fmtSignedPct(returnPct)}</small>
      <strong>${pnl !== null ? fmtSignedTradeMoney(pnl, market) : "-"}</strong>
      <em>LTP ${fmtTradeMoney(latest, market)} <span class="${pnlClass(dayPct)}">(${fmtSignedPct(dayPct)})</span></em>
    </div>
  </article>`;
}

function mobilePositionRowHtml(row = {}, index = 0) {
  const market = rowMarket(row);
  const closed = positionIsClosed(row);
  const qty = positionQuantity(row);
  const entry = positionEntryPrice(row);
  const latest = positionLatestPrice(row);
  const invested = entry !== null ? entry * qty : null;
  const pnl = closed ? positionRealizedPnl(row) : positionUnrealizedPnl(row);
  const pnlPct = closed
    ? firstFinite(row.return_pct, row.current_return_pct)
    : latest !== null && entry !== null && entry > 0 ? ((latest - entry) / entry) * 100 : null;
  const dayPnl = positionDayPnl(row, market);
  const dayPct = closed ? positionDayPct(row, market) : positionStockDayPct(row, market);
  const mode = positionModeState(row);
  const exitPrice = firstFinite(row.exit_price, row.latest_price, row.market_price);
  const displayQty = closed ? Number(row.closed_qty || row.entry_qty || 0) : qty;
  return `<article class="mobile-portfolio-row broker-row position" role="button" tabindex="0" data-position-index="${index}">
    <div class="mobile-portfolio-main">
      <small>${closed ? "Sold" : "Qty."} ${fmtNumber(displayQty)} <b>•</b> Avg. ${fmtTradeMoney(entry, market)}</small>
      <strong>${escapeHtml(displayValue(row.symbol, "Symbol"))}</strong>
      <em>${closed ? `Exit ${fmtTradeMoney(exitPrice, market)}` : `Invested ${fmtTradeMoney(invested, market)}`} <span>${escapeHtml(mode.label)}</span></em>
    </div>
    <div class="mobile-portfolio-price ${pnlClass(pnl)}">
      <small class="${pnlClass(pnlPct)}">${fmtSignedPct(pnlPct)}</small>
      <strong>${fmtSignedTradeMoney(pnl, market)}</strong>
      <em>${closed ? "After sell" : "LTP"} ${fmtTradeMoney(latest, market)} <span class="${pnlClass(dayPct)}">(${fmtSignedPct(dayPct)})</span></em>
      <em class="${pnlClass(dayPnl)}">${closed ? "Realized" : "Today"} ${dayPnl === null ? "-" : fmtSignedTradeMoney(dayPnl, market)}</em>
    </div>
  </article>`;
}

function mobilePortfolioEmpty(tab = "positions") {
  const isPositions = tab === "positions";
  return `<div class="mobile-portfolio-empty">
    <div class="mobile-empty-illustration" aria-hidden="true">
      <span></span><span></span><span></span>
    </div>
    <strong>${isPositions ? "No positions" : "No holdings"}</strong>
    <small>${isPositions ? "Place an order from your watchlist" : "Track ideas from your watchlist to build holdings"}</small>
  </div>`;
}

function positionDetails(row = {}) {
  return row.details && typeof row.details === "object" ? row.details : parseJsonObject(row.details_json);
}

function positionOpenDate(row = {}) {
  const details = positionDetails(row);
  const raw = row.opened_at
    || row.created_at
    || row.followed_at
    || details.opened_at
    || details.created_at
    || details.entry_at
    || details.opened_from_decision?.asof
    || details.opened_from_decision?.created_at;
  const date = raw ? new Date(raw) : null;
  return date && Number.isFinite(date.getTime()) ? date : null;
}

function positionOpenedToday(row = {}) {
  const date = positionOpenDate(row);
  if (!date) return false;
  const now = new Date();
  return date.getFullYear() === now.getFullYear() && date.getMonth() === now.getMonth() && date.getDate() === now.getDate();
}

function renderMobilePortfolio(positions = [], trackedIdeas = [], scopedPortfolio = {}) {
  const body = byId("mobile-positions-body");
  if (!body) return;
  const openRows = (positions || []).filter((row) => positionQuantity(row) > 0);
  const closedDayRows = (positions || []).filter(positionClosedToday);
  const positionRows = openRows.filter(positionOpenedToday);
  positionRows.push(...closedDayRows);
  const holdingRows = openRows.filter((row) => !positionOpenedToday(row));
  const search = String(state.portfolioSearch || "").trim().toUpperCase();
  const activeTab = state.activePortfolioTab || "positions";
  byId("mobile-holdings-count").textContent = fmtNumber(holdingRows.length);
  byId("mobile-positions-tab-count").textContent = fmtNumber(positionRows.length);
  document.querySelectorAll("[data-mobile-portfolio-tab]").forEach((button) => {
    const active = button.dataset.mobilePortfolioTab === activeTab;
    button.classList.toggle("active", active);
    button.setAttribute("aria-selected", active ? "true" : "false");
  });
  const rows = (activeTab === "holdings" ? holdingRows : positionRows).filter((row) => portfolioSearchMatches(row, search));
  const market = normalizeUiMarket(state.activeMarket);
  const metrics = portfolioRowsPnlMetrics(rows, market);
  const summaryHtml = mobilePortfolioSummaryHtml(activeTab, metrics, market);
  const dayPnlHtml = mobilePortfolioDayPnlHtml(metrics, market);
  body.innerHTML = summaryHtml + (rows.length
    ? rows.slice(0, 80).map((row, index) => (
        activeTab === "holdings" ? mobileHoldingRowHtml(row, index) : mobilePositionRowHtml(row, index)
      )).join("")
    : mobilePortfolioEmpty(activeTab)) + dayPnlHtml;
  body.querySelectorAll("[data-position-index]").forEach((card) => {
    const row = rows[Number(card.dataset.positionIndex)];
    if (row) card.addEventListener("click", () => showDetails("Position", row));
  });
  body.querySelectorAll("[data-holding-index]").forEach((card) => {
    const row = rows[Number(card.dataset.holdingIndex)];
    if (row) card.addEventListener("click", () => showDetails("Tracked Holding", row));
  });
  const analytics = byId("mobile-portfolio-analytics");
  if (analytics && !analytics.dataset.bound) {
    analytics.dataset.bound = "1";
    analytics.addEventListener("click", () => {
      const market = normalizeUiMarket(state.activeMarket);
      const openRows = filterRowsByMarket(state.latest?.positions || [], market).filter((row) => Number(row.qty || 0) > 0);
      showDetails("Portfolio Analytics", {
        market,
        portfolio: marketPortfolioFromPayload(state.latest || {}, market),
        holdings: openRows.filter((row) => !positionOpenedToday(row)).length,
        positions: openRows.filter(positionOpenedToday).length,
      });
    });
  }
}

function rerenderIdeasWatchlistFromState() {
  const market = normalizeUiMarket(state.activeMarket);
  renderIdeasWatchlist(
    payloadRowsForMarket(state.latest || {}, "suggestions", market),
    payloadRowsForMarket(state.latest || {}, "tracked_ideas", market),
    state.latest?.strategy_plans || [],
    payloadRowsForMarket(state.latest || {}, "monitor_watchlist", market),
  );
}

function rerenderMobilePortfolioFromState() {
  const market = normalizeUiMarket(state.activeMarket);
  renderMobilePortfolio(
    filterRowsByMarket(state.latest?.positions || [], market),
    payloadRowsForMarket(state.latest || {}, "tracked_ideas", market),
    marketPortfolioFromPayload(state.latest || {}, market),
  );
}

function renderPositions(rows) {
  const body = byId("positions-body");
  const summary = byId("positions-summary-strip");
  if (summary) {
    const market = normalizeUiMarket(state.activeMarket);
    const openRows = rows.filter((row) => positionQuantity(row) > 0);
    const closedTodayRows = rows.filter(positionClosedToday);
    const scopedPortfolio = marketPortfolioFromPayload(state.latest || {}, market);
    const pnlMetrics = portfolioPnlMetrics(scopedPortfolio, rows, market);
    const winners = openRows.filter((row) => (Number(row.market_price) - Number(row.avg_price)) * Number(row.qty) > 0).length;
    const losers = openRows.filter((row) => (Number(row.market_price) - Number(row.avg_price)) * Number(row.qty) < 0).length;
    const needsReview = rows.filter(positionNeedsAction).length;
    const deployed = openRows.reduce((sum, row) => sum + Number(row.avg_price || 0) * Number(row.qty || 0), 0);
    const pnl = openRows.reduce((sum, row) => sum + (Number(row.market_price || 0) - Number(row.avg_price || 0)) * Number(row.qty || 0), 0);
    const pnlPct = deployed > 0 ? (pnl / deployed) * 100 : 0;
    summary.innerHTML = `
      <button type="button"><span>Deployed</span><strong>${fmtTradeMoney(deployed, market)}</strong></button>
      <button type="button"><span>Today P&L</span><strong class="${pnlClass(pnlMetrics.today)}">${fmtTradeMoney(pnlMetrics.today, market)}</strong><small class="${pnlClass(pnlMetrics.today)}">${escapeHtml(portfolioTodayLabel(pnlMetrics))}</small></button>
      <button type="button"><span>Holdings P&L</span><strong class="${pnlClass(pnl)}">${fmtTradeMoney(pnl, market)}</strong><small class="${pnlClass(pnlPct)}">${fmtPct(pnlPct)} on holdings</small></button>
      <button type="button"><span>Net P&L</span><strong class="${pnlClass(pnlMetrics.total)}">${fmtTradeMoney(pnlMetrics.total, market)}</strong><small class="${pnlClass(pnlMetrics.realized)}">realized ${fmtTradeMoney(pnlMetrics.realized, market)}</small></button>
      <button type="button"><span>Winners / Losers</span><strong><span class="positive">${fmtNumber(winners)}</span> / <span class="negative">${fmtNumber(losers)}</span></strong></button>
      <button type="button"><span>Closed Today</span><strong>${fmtNumber(closedTodayRows.length)}</strong><small>kept until day close</small></button>
      <button type="button"><span>Need Action</span><strong class="${needsReview ? "warning" : "positive"}">${fmtNumber(needsReview)}</strong></button>
    `;
  }
  if (!rows.length) {
    body.innerHTML = emptyTableRow(
      7,
      `No open ${activeMarketLabel()} positions`,
      "Open positions appear here when qualifying opportunities are found.",
      "Open Watchlist",
      "suggestions",
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
      7,
      `No open ${activeMarketLabel()} positions`,
      "Open positions appear here when qualifying opportunities are found.",
      "Open Watchlist",
      "suggestions",
    );
    return;
  }
  body.innerHTML = sorted.slice(0, 5).map((row) => positionRowHtml(row, true)).join("");
  bindRowDetails(body, sorted.slice(0, 5), "Position");
}

function positionModeState(row = {}) {
  const summary = row.position_summary || {};
  const details = parseJsonObject(row.details_json);
  if (positionIsClosed(row)) {
    return {
      label: row.mode_label || (String(row.mode || "").toUpperCase() === "LIVE" ? "Live request" : "Paper"),
      note: "Closed today",
      className: "closed",
    };
  }
  const raw = String(row.mode || details.mode || row.execution_state || summary.classification || "").toUpperCase();
  if (raw.includes("LIVE")) {
    return {
      label: row.mode_label || (raw.includes("REQUEST") ? "Live request" : "Live"),
      note: row.execution_state_label || summary.classification || "Broker sync required",
      className: "live",
    };
  }
  if (raw.includes("PAPER")) {
    return {
      label: row.mode_label || "Paper",
      note: row.execution_state_label || "Paper Active",
      className: "paper",
    };
  }
  return {
    label: row.mode_label || "Tracked",
    note: row.execution_state_label || summary.classification || "Signal only",
    className: "tracked",
  };
}

function positionExitPlan(row = {}) {
  const details = parseJsonObject(row.details_json);
  const exit = row.exit_plan && typeof row.exit_plan === "object"
    ? row.exit_plan
    : details.exit_plan && typeof details.exit_plan === "object"
      ? details.exit_plan
      : {};
  const rawTargets = Array.isArray(row.target_status) && row.target_status.length
    ? row.target_status
    : Array.isArray(row.targets) && row.targets.length
      ? row.targets
      : Array.isArray(details.target_status) && details.target_status.length
        ? details.target_status
        : Array.isArray(details.targets)
          ? details.targets
          : [];
  const targets = normalizedTargets(rawTargets);
  return {
    ...exit,
    entry_zone: exit.entry_zone || row.entry_zone || details.entry_zone,
    stop_loss: firstPositiveFinite(exit.stop_loss, row.stop_loss, details.stop_loss),
    target_1: exit.target_1 && Object.keys(exit.target_1).length ? exit.target_1 : (targets[0] || {}),
    target_2: exit.target_2 && Object.keys(exit.target_2).length ? exit.target_2 : (targets[1] || {}),
    target_3: exit.target_3 && Object.keys(exit.target_3).length ? exit.target_3 : (targets[2] || {}),
  };
}

function positionTargets(row = {}, exit = positionExitPlan(row)) {
  return [exit.target_1, exit.target_2, exit.target_3].filter((target) => target && Object.keys(target).length);
}

function importantPositionFlags(flags = []) {
  if (!Array.isArray(flags)) return [];
  return flags.filter((flag) => {
    const value = String(flag || "").toUpperCase();
    return [
      "STOP",
      "EXIT",
      "ADVERSE",
      "DRAWDOWN",
      "MAE",
      "TARGET",
      "EXPIRED",
      "INVALID",
      "BROKER",
      "REJECT",
    ].some((token) => value.includes(token));
  });
}

function positionActionState(row = {}) {
  if (positionIsClosed(row)) {
    return { label: "Closed today", tone: "closed", reason: followReasonText(row), needsAction: false };
  }
  const summary = row.position_summary || {};
  const flags = Array.isArray(summary.active_flags) ? summary.active_flags : [];
  const importantFlags = importantPositionFlags(flags);
  const details = parseJsonObject(row.details_json);
  const raw = String(summary.recommended_action || row.recommended_action || "TRACK").toUpperCase();
  const lifecycle = String(row.lifecycle_status || details.lifecycle_status || "").toLowerCase();
  const stopStatus = row.stop_status && typeof row.stop_status === "object" ? row.stop_status : details.stop_status;
  const stopHit = Boolean(stopStatus && stopStatus.hit);
  const reason = summary.reason || row.action_reason || "Position is being monitored against stop, targets, and latest price.";
  if (raw.includes("EXIT") || stopHit || ["stopped", "exit_signal", "expired"].includes(lifecycle)) {
    return { label: "Exit now", tone: "negative", reason, needsAction: true };
  }
  if (raw.includes("REDUCE") || raw.includes("BOOK")) {
    return { label: "Book partial", tone: "warning", reason, needsAction: true };
  }
  if (raw.includes("TRAIL")) {
    return { label: "Trail stop", tone: "warning", reason, needsAction: true };
  }
  if (raw.includes("REVIEW") || importantFlags.length) {
    return { label: "Risk check", tone: "warning", reason, needsAction: true };
  }
  return { label: "Monitor", tone: "open", reason, needsAction: false };
}

function positionNeedsAction(row = {}) {
  return Boolean(positionActionState(row).needsAction);
}

function positionFlagsHtml(flags = []) {
  if (!Array.isArray(flags) || !flags.length) {
    return `<span class="position-flags-summary positive">Rules clear</span>`;
  }
  const important = importantPositionFlags(flags);
  const visible = important.length ? important : flags;
  const first = humanLabel(visible[0]);
  const extra = visible.length > 1 ? ` +${visible.length - 1}` : "";
  const tone = important.length ? "warning" : "muted";
  return `<span class="position-flags-summary ${tone}">${escapeHtml(important.length ? first : `Data note: ${first}`)}${escapeHtml(extra)}</span>`;
}

function positionPriceHtml(row = {}, market = "IN") {
  if (positionIsClosed(row)) {
    const entry = positionEntryPrice(row);
    const exit = firstFinite(row.exit_price, row.closed_price, row.latest_price, row.market_price);
    const latest = positionLatestPrice(row);
    const afterExitPct = exit && latest !== null && exit > 0 ? ((latest - exit) / exit) * 100 : null;
    return `<div class="position-price-pair closed">
      <span><small>Entry</small><strong>${fmtTradeMoney(entry, market)}</strong></span>
      <span class="live-price-tile"><small>Exit</small><strong>${fmtTradeMoney(exit, market)}</strong><em>${escapeHtml(fmtDateTime(positionClosedAt(row)))}</em><em class="${pnlClass(afterExitPct)}">after exit ${fmtSignedPct(afterExitPct)}</em></span>
    </div>`;
  }
  const markedAt = row.marked_at || row.updated_at || row.position_summary?.mark_timestamp || row.position_summary?.price_timestamp;
  const quoteAt = row.quote_updated_at || row.position_summary?.quote_timestamp;
  const source = humanLabel(row.quote_source || row.position_summary?.price_source || "live quote");
  const markText = `Mark ${fmtFreshness(markedAt)}`;
  const quoteText = quoteAt ? `tick ${fmtFreshness(quoteAt)}` : "tick waiting";
  return `<div class="position-price-pair">
    <span><small>Entry</small><strong>${fmtTradeMoney(row.avg_price, market)}</strong></span>
    <span class="live-price-tile"><small>${escapeHtml(row.position_summary?.price_label || "LTP")}</small><strong>${fmtTradeMoney(row.market_price, market)}</strong><em>${escapeHtml(markText)} · ${escapeHtml(quoteText)}</em><em>${escapeHtml(source)}</em></span>
  </div>`;
}

function positionPnlHtml(row = {}, market = "IN") {
  if (positionIsClosed(row)) {
    const realized = positionRealizedPnl(row);
    const returnPct = firstFinite(row.return_pct, row.current_return_pct);
    const exit = firstFinite(row.exit_price, row.latest_price, row.market_price);
    const latest = positionLatestPrice(row);
    const afterExitPct = exit && latest !== null && exit > 0 ? ((latest - exit) / exit) * 100 : null;
    const tone = pnlClass(realized ?? returnPct);
    return `<div class="position-pnl-cell ${tone}">
      <strong class="${tone}">${fmtSignedTradeMoney(realized, market)}</strong>
      <small class="${tone}">${fmtSignedPct(returnPct)} realized</small>
      <small class="${pnlClass(afterExitPct)}">after sell ${fmtSignedPct(afterExitPct)}</small>
      <small>closed qty ${fmtNumber(row.closed_qty || row.entry_qty || 0)}</small>
    </div>`;
  }
  const pnl = (Number(row.market_price) - Number(row.avg_price)) * Number(row.qty);
  const pnlPct = Number(row.avg_price) > 0 ? ((Number(row.market_price) - Number(row.avg_price)) / Number(row.avg_price)) * 100 : 0;
  const dayPnl = positionDayPnl(row, market);
  const marketValue = Number(row.market_price) * Number(row.qty);
  const tone = pnlClass(pnl);
  return `<div class="position-pnl-cell ${tone}">
    <strong class="${tone}">${fmtTradeMoney(pnl, market)}</strong>
    <small class="${tone}">${fmtPct(pnlPct)} all-time</small>
    <small class="${pnlClass(dayPnl)}">today ${dayPnl === null ? "-" : fmtTradeMoney(dayPnl, market)}</small>
    <small>value ${fmtTradeMoney(marketValue, market)}</small>
  </div>`;
}

function positionTargetTile(label, target = {}, market = "IN") {
  const hit = Boolean(target.hit || String(target.status || "").toLowerCase() === "hit" || target.reached);
  const labelText = hit ? `${label} hit` : label;
  return `<span class="${hit ? "hit" : ""}">
    <small>${escapeHtml(labelText)}</small>
    <strong class="${hit ? "positive" : ""}">${fmtTradeMoney(target.price, market)}</strong>
  </span>`;
}

function positionRiskHtml(row = {}, market = "IN", compact = false) {
  if (positionIsClosed(row)) {
    const afterExitPct = (() => {
      const exit = firstFinite(row.exit_price, row.latest_price, row.market_price);
      const latest = positionLatestPrice(row);
      return exit && latest !== null && exit > 0 ? ((latest - exit) / exit) * 100 : null;
    })();
    return `<div class="position-risk-stack compact closed">
      <span><small>Sold</small><strong>${fmtTradeMoney(firstFinite(row.exit_price, row.latest_price), market)}</strong></span>
      <span><small>After Sell</small><strong class="${pnlClass(afterExitPct)}">${fmtSignedPct(afterExitPct)}</strong></span>
    </div>`;
  }
  const exit = positionExitPlan(row);
  const targets = positionTargets(row, exit);
  const t1 = targets[0] || {};
  const t2 = targets[1] || {};
  const t3 = targets[2] || {};
  if (compact) {
    return `<div class="position-risk-stack compact">
      <span><small>Stop</small><strong class="negative">${fmtTradeMoney(exit.stop_loss, market)}</strong></span>
      ${positionTargetTile("T1", t1, market)}
    </div>`;
  }
  return `<div class="position-risk-stack">
    <span><small>Stop</small><strong class="negative">${fmtTradeMoney(exit.stop_loss, market)}</strong></span>
    ${positionTargetTile("T1", t1, market)}
    ${positionTargetTile("T2", t2, market)}
    ${positionTargetTile("T3", t3, market)}
  </div>`;
}

function positionActionHtml(row = {}, compact = false) {
  const summary = row.position_summary || {};
  const flags = Array.isArray(summary.active_flags) ? summary.active_flags : [];
  const action = positionActionState(row);
  return `<div class="position-action-callout ${escapeHtml(action.tone)}">
    <strong>${escapeHtml(action.label)}</strong>
    <small>${escapeHtml(shortValue(action.reason, compact ? 82 : 120))}</small>
    ${positionFlagsHtml(flags)}
  </div>`;
}

function positionTradeStateLabel(row = {}, mode = positionModeState(row)) {
  if (positionIsClosed(row)) return "Closed today";
  const lifecycle = String(row.lifecycle_status || "").toLowerCase();
  if (["target_1_hit", "target_2_hit", "target_3_hit", "stopped", "exit_signal", "expired"].includes(lifecycle)) {
    return humanLabel(lifecycle);
  }
  return mode.note || `${mode.label || "Position"} active`;
}

function positionRowHtml(row, compact = false) {
  const summary = row.position_summary || {};
  const market = rowMarket(row);
  const mode = positionModeState(row);
  const closed = positionIsClosed(row);
  const symbolCell = `<div class="symbol-cell"><span class="symbol-logo">${escapeHtml(symbolInitials(row.symbol))}</span><div><strong>${escapeHtml(displayValue(row.symbol, "Symbol"))}</strong><small>${escapeHtml(displayValue(row.company_name || row.strategy, "Position"))}</small></div></div>`;
  const modeCell = `<span class="position-mode-pill ${escapeHtml(mode.className)}">${escapeHtml(mode.label)}</span><small class="position-mode-note">${escapeHtml(mode.note)}</small>`;
  const tradeState = positionTradeStateLabel(row, mode);
  if (compact) {
    return `<tr class="position-row compact">
      <td data-label="Symbol">${symbolCell}</td>
      <td data-label="Mode">${modeCell}</td>
      <td data-label="Entry / LTP">${positionPriceHtml(row, market)}</td>
      <td data-label="P&L">${positionPnlHtml(row, market)}</td>
      <td data-label="Stop / T1">${positionRiskHtml(row, market, true)}</td>
      <td data-label="Next action">${positionActionHtml(row, true)}</td>
      <td data-label="Details"><button type="button" class="row-link">Details →</button></td>
    </tr>`;
  }
  return `<tr class="position-row">
    <td data-label="Symbol">${symbolCell}</td>
    <td data-label="Mode">${modeCell}</td>
    <td data-label="Qty" class="num quantity">${fmtNumber(closed ? row.closed_qty || row.entry_qty || 0 : row.qty)}<br><small>${escapeHtml(tradeState)}</small></td>
    <td data-label="Entry / LTP" aria-live="polite">${positionPriceHtml(row, market)}</td>
    <td data-label="P&L">${positionPnlHtml(row, market)}</td>
    <td data-label="Stop / Targets">${positionRiskHtml(row, market)}</td>
    <td data-label="Next action">
      ${positionActionHtml(row)}
      <div class="position-row-actions">
        ${closed ? "" : `<button type="button" class="danger-outline manual-exit-btn" data-symbol="${escapeHtml(row.symbol || "")}" data-market="${escapeHtml(market)}">Exit</button>`}
        <button type="button" class="row-link">Details →</button>
      </div>
    </td>
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
      "Open Watchlist",
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
      const targets = row.targets || [];
      const t1 = targets[0] || {};
      const t3 = targets[2] || {};
      return `<article class="tracked-idea-card tracked-idea-row" role="button" tabindex="0" data-index="${index}" aria-label="Open ${escapeHtml(displayValue(row.symbol, "symbol"))} tracked idea">
        <div class="tracked-list-symbol">
          <span class="signal-rank">${escapeHtml(mode)}</span>
          <div>
            <div class="tracked-title-row"><strong>${escapeHtml(displayValue(row.symbol, "Symbol"))}</strong><span class="lifecycle-pill ${escapeHtml(lifecycle.className)}">${escapeHtml(lifecycle.label)}</span></div>
            <small>${escapeHtml(row.strategy || "-")} · ${escapeHtml(ideaTimelineText(row))} · followed ${fmtTime(row.followed_at || row.user_follow?.created_at)}</small>
          </div>
        </div>
        <div class="tracked-list-prices">
          <span><small>Qty</small><strong>${fmtNumber(qty)}</strong></span>
          <span><small>Entry</small><strong>${fmtTradeMoney(entry, market)}</strong></span>
          <span><small>LTP</small><strong>${fmtTradeMoney(latest, market)}</strong></span>
        </div>
        <div class="tracked-list-risk">
          <span><small>Stop</small><strong class="negative">${fmtTradeMoney(row.stop_loss, market)}</strong></span>
          <span><small>T1</small><strong class="positive">${fmtTradeMoney(t1.price, market)}</strong></span>
          <span><small>Final</small><strong class="positive">${fmtTradeMoney(t3.price, market)}</strong></span>
        </div>
        <div class="tracked-return ${pnlClass(returnPct)}">
          <strong>${fmtPct(returnPct)}</strong>
          <small>${fmtTradeMoney(pnl, market)} unrealized · ${fmtTradeMoney(invested, market)} invested</small>
        </div>
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
  rows = sortSuggestionRows(rows || []);
  if (!rows.length) {
    body.innerHTML = emptyBlock(
      `No ${activeMarketLabel()} signal history yet`,
      "The shared engine will publish only ideas that survive the data, entry, risk, sentiment, and tradeability checks.",
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
      const opportunity = opportunityStatePayload(row);
      const readiness = String(row.fresh_action || "").toUpperCase() === "BUY_NOW"
        ? "Ready to buy"
        : opportunity.label || row.fresh_action_label || humanLabel(row.decision_readiness || "monitor_only");
      const latestSystemAction = row.latest_system_action ? String(row.latest_system_action).toUpperCase() : "";
      const followed = row.user_follow || null;
      const followedActive = followed && ["ACTIVE", "LIVE_REQUESTED", "LIVE_EXIT_REQUESTED"].includes(String(followed.status || "").toUpperCase()) && Number(followed.qty || 0) > 0;
      const executionLabel = row.execution_state_label || (followed ? `${followed.mode} active` : "Signal Only");
      const setupBucket = opportunity.label || row.setup_bucket_label || "-";
      const canEnterTrade = String(row.fresh_action || "").toUpperCase() === "BUY_NOW";
      const entryDisabled = canEnterTrade ? "" : "disabled";
      const entryBlockReason = escapeHtml(opportunity.next_step || row.setup_bucket_reason || row.display_reason || "Only actionable fresh BUY ideas can be paper/live entered.");
      const tradeBlockText = canEnterTrade ? "" : shortValue(opportunity.next_step || row.setup_bucket_reason || row.display_reason || "Not trade-ready", 70);
      const opportunityReason = opportunity.summary || row.display_reason || readableDecisionReason(row);
      const confidence = Number(row.confidence || 0) * 100;
      const currentReturn = Number(row.current_return_pct || 0);
      const peakReturn = Number(row.peak_return_pct || 0);
      const worstReturn = Number(row.worst_return_pct || 0);
      const market = rowMarket(row);
      const lifecycle = ideaLifecycle(row);
      return `<article class="signal-history-card signal-history-row signal-${escapeHtml(cssToken(action))} ${index === 0 ? "featured" : ""}" role="button" tabindex="0" data-index="${index}" aria-label="Open ${escapeHtml(displayValue(row.symbol, "symbol"))} idea audit">
        <div class="signal-list-symbol">
          <span class="signal-rank">Idea #${row.id || index + 1}</span>
          <div>
            <div class="signal-symbol-row">
              <strong>${escapeHtml(displayValue(row.symbol, "Symbol"))}</strong>
              <span class="tag ${escapeHtml(cssToken(action))}">${escapeHtml(displaySignal)}</span>
              <span class="lifecycle-pill ${escapeHtml(lifecycle.className)}">${escapeHtml(lifecycle.label)}</span>
              ${followed ? `<span class="signal-followed">${escapeHtml(followed.mode)} ${fmtPct(followed.return_pct || 0)}</span>` : ""}
            </div>
            <small>${fmtTradeMoney(row.price || row.latest_price, market)} · ${escapeHtml(MARKET_LABELS[market] || market)} · ${escapeHtml(row.strategy || "-")} · ${escapeHtml(ideaTimelineText(row))}</small>
          </div>
        </div>
        <div class="signal-list-state">
          <span><small>Fresh Action</small><strong>${escapeHtml(readiness)}</strong></span>
          <span><small>Setup</small><strong>${escapeHtml(setupBucket)}</strong></span>
          <span><small>Execution</small><strong>${escapeHtml(executionLabel)}</strong></span>
        </div>
        <div class="signal-list-trade">
          <span><small>Entry</small><strong>${formatZone(row.entry_zone, market)}</strong></span>
          <span><small>Stop</small><strong class="negative">${fmtTradeMoney(row.stop_loss, market)}</strong></span>
          <span><small>Target 1</small><strong class="positive">${fmtTradeMoney(t1.price, market)}</strong></span>
          <span><small>Final Target</small><strong class="positive">${fmtTradeMoney(t3.price, market)}</strong></span>
        </div>
        <div class="signal-list-return">
          <span><small>Since Signal</small><strong class="${pnlClass(currentReturn)}">${fmtPct(currentReturn)}</strong></span>
          <small>best ${fmtPct(peakReturn)} · worst ${fmtPct(worstReturn)} · ${latestSystemAction ? `engine ${escapeHtml(latestSystemAction)}` : `${fmtNumber(confidence)}% confidence`}</small>
        </div>
        <div class="signal-list-reason">
          <span>${escapeHtml(opportunity.label || "Reason")}</span>
          <p>${escapeHtml(shortValue(opportunityReason, 170))}</p>
        </div>
        <div class="signal-list-meta">
          <span>${escapeHtml(row.latest_decision_id ? `Decision #${row.latest_decision_id}` : "Decision audit")}</span>
          <span>Confluence ${escapeHtml(row.confluence ?? "-")}/26</span>
          <span>${escapeHtml(row.tier || "-")}</span>
          ${tradeBlockText ? `<span class="warning">${escapeHtml(tradeBlockText)}</span>` : ""}
          ${riskFlags.slice(0, 1).map((flag) => `<span class="warning">${escapeHtml(humanLabel(flag))}</span>`).join("")}
          ${institutionalFlags.slice(0, 1).map((flag) => `<span>${escapeHtml(flag)}</span>`).join("")}
        </div>
        ${followedActive
          ? `<div class="signal-card-actions">
              <button type="button" data-idea-action="details" data-idea-id="${escapeHtml(row.id)}">Manage</button>
              <button type="button" class="danger-outline" data-idea-action="exit" data-idea-id="${escapeHtml(row.id)}" data-symbol="${escapeHtml(row.symbol || "")}">Exit</button>
            </div>`
          : `<div class="signal-card-actions">
              <button type="button" data-idea-action="track" data-idea-id="${escapeHtml(row.id)}">Track</button>
              <button type="button" data-idea-action="paper" data-idea-id="${escapeHtml(row.id)}" ${entryDisabled} title="${entryBlockReason}">Paper</button>
              <button type="button" data-idea-action="live" data-idea-id="${escapeHtml(row.id)}" ${entryDisabled} title="${entryBlockReason}">Live</button>
            </div>`}
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
  const minimumEconomicNotional = market === "US" ? 250 : 7500;
  const target = Math.max(cash * 0.2, minimumEconomicNotional);
  if (target >= price) return Math.min(target, cash);
  return price <= cash ? price : 0;
}

function applyIdeaFollowPayload(payload = {}) {
  if (!Array.isArray(payload.ideas)) return false;
  state.latest = {
    ...(state.latest || {}),
    suggestions: payload.ideas,
    signal_ideas: payload.ideas,
    suggestions_by_market: payload.ideas_by_market || state.latest?.suggestions_by_market || {},
    tracked_ideas: payload.tracked_ideas || state.latest?.tracked_ideas || [],
    tracked_ideas_by_market: payload.tracked_ideas_by_market || state.latest?.tracked_ideas_by_market || {},
    follow_history: payload.follow_history || state.latest?.follow_history || [],
    follow_history_by_market: payload.follow_history_by_market || state.latest?.follow_history_by_market || {},
    orders: payload.orders || state.latest?.orders || [],
    positions: payload.positions || state.latest?.positions || [],
    portfolio: payload.portfolio || state.latest?.portfolio || {},
    portfolio_by_market: payload.portfolio_by_market || state.latest?.portfolio_by_market || {},
    paper: payload.paper || state.latest?.paper || {},
  };
  render(state.latest);
  return true;
}

async function followIdea(rowOrId, action, button = null, options = {}) {
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
      body: JSON.stringify({ mode, amount, manual_override: Boolean(options.manualOverride) }),
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
        note: mode === "LIVE"
          ? "Live requests need your personal broker token, broker guard, cash, and risk checks."
          : "Paper buys can be manually confirmed from the watchlist, but cash/quantity checks still apply.",
        response: payload,
      });
      return;
    }
    if (applyIdeaFollowPayload(payload)) {
      await refreshStatusOnly();
      showDetails(mode === "TRACK" ? "Idea Tracking" : mode === "LIVE" ? "Live Request" : "Paper Buy", {
        symbol: row.symbol,
        mode,
        amount,
        note: mode === "LIVE" ? "Guarded live request created. Broker fill still needs reconciliation." : "Paper position created with simulated cash and P&L.",
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
    .filter((row) => firstFinite(row.price, row.last_price, row.latest_price) !== null)
    .sort((a, b) => Math.abs(Number(quoteDayPct(b)) || 0) - Math.abs(Number(quoteDayPct(a)) || 0))
    .slice(0, 28);
  track.closest(".market-tape")?.classList.toggle("is-empty", !ranked.length);
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
  const price = firstFinite(row.price, row.last_price, row.latest_price);
  return `<button class="market-tape-item ${cls}" type="button" data-quote-symbol="${escapeHtml(row.symbol)}">
    <strong>${escapeHtml(displayValue(row.symbol, "Symbol"))}</strong>
    <span>${fmtMarketMoney(price, market)}</span>
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
  const direct = firstFinite(
    row.day_change_pct,
    row.change_pct,
    row.change_percent,
    row.percent_change,
    row.pct_change,
    row.day_pct,
  );
  if (direct !== null) return direct;
  const price = firstFinite(row.price, row.last_price, row.latest_price);
  const close = firstFinite(row.prev_close, row.previous_close, row.close);
  const open = firstFinite(row.open);
  const reference = Number.isFinite(close) && close > 0 && Math.abs(close - price) > 0.000001
    ? close
    : (Number.isFinite(open) && open > 0 ? open : close);
  if (!Number.isFinite(price) || !Number.isFinite(reference) || reference === 0) return NaN;
  return ((price - reference) / reference) * 100;
}

function sourceClass(source) {
  const value = String(source || "");
  if (value.includes("live")) return "live";
  if (value.includes("delayed")) return "delayed";
  return "";
}

function decisionFeedEmptyHtml(controlRunning, marketLabel = activeMarketLabel()) {
  return emptyBlock(
    `No ${marketLabel} signals yet`,
    controlRunning
      ? "The scanner is running. This market feed will fill after the next completed strategy scan."
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
        "No signal selected",
        "When signals arrive, select one to inspect quality, risk checks, and timeline.",
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
      renderDecisionDetailFromRow(row);
    });
  });
  body.querySelector(".decision-feed-card")?.classList.add("active");
  renderDecisionDetailFromRow(visibleRows[0]);
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
  const opportunity = opportunityStatePayload(row);
  const reason = shortValue(opportunity.summary || readableDecisionReason(row), compact ? 150 : 240);
  const initials = symbolInitials(row.symbol);
  const scoreLabel = row.rank_reason || row.rank_score_source || "Score";
  return `<article class="decision-feed-card action-${escapeHtml(action)}" role="button" tabindex="0" data-index="${index}">
    <div class="decision-logo">${escapeHtml(initials)}</div>
    <div class="decision-main">
      <div class="decision-title-row">
        <strong>${escapeHtml(displayValue(row.symbol, "Symbol"))}</strong>
        ${row.company_name ? `<small>${escapeHtml(row.company_name)}</small>` : ""}
        <span class="tag ${escapeHtml(action)}">${escapeHtml(row.action || "HOLD")}</span>
        ${opportunity.label ? `<span class="tag neutral">${escapeHtml(opportunity.label)}</span>` : ""}
        <span class="decision-time">${escapeHtml(fmtTime(row.ts))}</span>
      </div>
      <p>${escapeHtml(reason)}</p>
      <div class="decision-bars">
        <span><em style="width:${Math.max(0, Math.min(100, (tech + 1) * 50))}%"></em><b>Setup</b></span>
        <span><em style="width:${Math.max(0, Math.min(100, (sentiment + 1) * 50))}%"></em><b>News</b></span>
        <span><em style="width:${Math.max(0, Math.min(100, score))}%"></em><b>${escapeHtml(scoreLabel)}</b></span>
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
  const opportunity = opportunityStatePayload(row);
  const confidence = Math.max(0, Math.min(100, Number(row.confidence || audit.confidence || 0) * 100));
  const score = audit.score_breakdown || {};
  const strategyLabel = humanLabel(row.strategy || context.best_strategy?.name || "Strategy pending");
  const metrics = [
    { label: "Setup", value: normalizedScore(row.technical_score ?? context.technical_math?.score) },
    { label: "News", value: normalizedScore(row.sentiment_score ?? context.sentiment?.score) },
    { label: "Risk", value: normalizedScore(score.risk_score ?? full.risk_score ?? (full.risk_overrides?.no_new_longs ? -0.7 : 0.35)) },
    { label: "Market", value: normalizedScore(context.global_market_context?.risk_score ?? context.market_breadth?.breadth_score) },
  ];
  const llm = audit.llm_output || {};
  const timeline = [
    ["Quote", `${fmtMarketMoney(row.price || context.quote?.price, market)} from ${context.quote?.source || row.source || "market feed"}`],
    ["Quality", `${fmtPct(Number(score.combined ?? row.combined_score ?? 0) * 100)} signal quality · ${full.confluence_score?.total ?? row.confluence ?? "-"} confirmations`],
    ["Checks", failedGatesFromAudit(audit, context).length ? `${failedGatesFromAudit(audit, context).length} item(s) need improvement` : "trade checks clear"],
    ["Signal", audit.llm_error ? "completed safely with rules" : humanLabel(plainDecisionPath(audit.decision_path || "deterministic_audit"))],
    ["Decision", `${row.action || "HOLD"} · ${fmtNumber(confidence)}% conviction`],
  ];
  panel.innerHTML = `
    <section class="decision-detail-hero">
      <div class="decision-logo large">${escapeHtml(symbolInitials(row.symbol))}</div>
      <div>
        <span>${escapeHtml(MARKET_LABELS[market] || market)} decision</span>
        <h3>${escapeHtml(displayValue(row.symbol, "Symbol"))}</h3>
        <p>${fmtMarketMoney(row.price, market)} · ${escapeHtml(strategyLabel)} · ${escapeHtml(fmtTime(row.ts))}</p>
      </div>
      <span class="tag ${escapeHtml(action)}">${escapeHtml(row.action || "HOLD")}</span>
      <div class="score-ring large decision-conviction-ring" style="--score:${confidence}"><strong>${fmtNumber(confidence)}</strong><small>%</small></div>
    </section>
    <section class="decision-radar-section">
      ${scoreRadarSvg(metrics)}
      <div class="decision-reason-block">
        <h4>${escapeHtml(opportunity.label || "Decision Summary")}</h4>
        <p>${escapeHtml(opportunity.summary || readableDecisionReason(row))}</p>
        ${opportunity.next_step ? `<p>${escapeHtml(opportunity.next_step)}</p>` : ""}
        ${auditList("Primary Checks", decisionReasonHighlights(row).slice(0, 5))}
      </div>
    </section>
    <section class="decision-review-grid">
      <div>
        <h4>Signal Notes</h4>
        ${formattedLlmReasonHtml(llm, audit, row)}
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
  const center = 90;
  const metricBaseRadius = 20;
  const metricRangeRadius = 50;
  const labelRadius = 74;
  const points = metrics.map((metric, index) => {
    const angle = (-90 + index * (360 / metrics.length)) * (Math.PI / 180);
    const cos = Math.cos(angle);
    const sin = Math.sin(angle);
    const radius = metricBaseRadius + (Math.max(0, Math.min(100, metric.value)) / 100) * metricRangeRadius;
    return {
      ...metric,
      x: center + cos * radius,
      y: center + sin * radius,
      lx: center + cos * labelRadius,
      ly: center + sin * labelRadius,
      anchor: cos < -0.4 ? "start" : cos > 0.4 ? "end" : "middle",
      dy: sin < -0.4 ? "0.85em" : sin > 0.4 ? "-0.35em" : "0.35em",
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
      ${points.map((point) => `<text x="${point.lx}" y="${point.ly}" text-anchor="${point.anchor}" dy="${point.dy}">${escapeHtml(point.label)}</text>`).join("")}
    </svg>
    <div class="radar-legend">
      ${metrics.map((metric) => `<span><b>${escapeHtml(metric.label)}</b>${fmtNumber(metric.value)}%</span>`).join("")}
    </div>
  </div>`;
}

function plainInsightText(value, action = "HOLD") {
  if (value === null || value === undefined || value === "") return "";
  if (typeof value === "object") return shortValue(value, 260);
  const text = String(value).trim();
  if (!text) return "";
  if (/^[a-z0-9_]+$/i.test(text)) return reasonFromSnakeCase(text);
  return humanizeReasonText(text, action);
}

function listValue(items) {
  return Array.isArray(items) ? items : items ? [items] : [];
}

function plainInsightList(items, action = "HOLD", limit = 5) {
  return listValue(items)
    .map((item) => plainInsightText(item, action))
    .filter(Boolean)
    .slice(0, limit);
}

function sentenceFromList(items, fallback = "") {
  const clean = (items || []).filter(Boolean);
  return clean.length ? clean.map((item) => compactSentence(item)).join(" ") : fallback;
}

function formattedLlmReasonHtml(llm = {}, audit = {}, decision = {}) {
  const action = String(audit.final_action || decision.action || llm.action || "HOLD").toUpperCase();
  const reason = plainInsightText(llm.reason || llm.summary || audit.action_reason || "", action);
  const evidence = plainInsightList(llm.evidence, action, 5);
  const checklist = plainInsightList(llm.checklist, action, 4);
  const risks = plainInsightList([...listValue(llm.risk_checks), ...listValue(llm.invalidators)], action, 6);
  const monitoring = plainInsightList(llm.monitoring_checklist, action, 5);
  const dataLimits = plainInsightList(llm.data_gaps, action, 5);
  const fallbackReason = decision.symbol
    ? readableDecisionReason({ ...decision, details: audit })
    : "";
  const sections = [
    ["Bottom line", reason || fallbackReason],
    ["What is working", sentenceFromList(evidence, checklist.length ? sentenceFromList(checklist) : "")],
    ["What is missing", sentenceFromList(dataLimits, audit.llm_error ? "The signal check could not complete, so the safer decision was used." : "")],
    ["Risk to watch", sentenceFromList(risks)],
    ["What would change the view", sentenceFromList(monitoring)],
  ].filter(([, text]) => text);
  if (!sections.length) {
    return `<div class="empty-state product-empty"><strong>No signal notes captured</strong><span>This decision still used price, volume, risk checks, and strategy rules.</span></div>`;
  }
  return `<div class="openstocks-formatted-view">
    ${sections.map(([title, text]) => `<article><h5>${escapeHtml(title)}</h5><p>${escapeHtml(shortValue(text, 620))}</p></article>`).join("")}
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
  const rawTargets = Array.isArray(row.target_status) && row.target_status.length
    ? row.target_status
    : (Array.isArray(row.targets) ? row.targets : []).slice(0, 3).map((target, index) => ({
        label: target?.label || `T${index + 1}`,
        price: target?.price ?? target,
        hit: false,
      }));
  const targets = normalizedTargets(rawTargets).map((target, index) => ({
    ...(rawTargets[index] && typeof rawTargets[index] === "object" ? rawTargets[index] : {}),
    ...target,
  }));
  if (!targets.length) return `<div class="target-ladder empty">No targets published yet</div>`;
  return `<div class="target-ladder ${compact ? "compact" : ""}">
    ${targets.slice(0, 3).map((target) => {
      const hit = Boolean(target.hit);
      return `<span class="${hit ? "hit" : "pending"}">
        <small>${escapeHtml(target.label || "-")}</small>
        <strong>${fmtTradeMoney(target.price, market)}</strong>
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
      `No ${activeMarketLabel()} orders today`,
      "Today's paper/live buys, exits, target actions, and rejected requests will appear here.",
      "Open Positions",
      "positions",
    );
    return;
  }
  body.innerHTML = rows
    .map((row) => {
      const side = String(row.side || "").toLowerCase();
      const market = rowMarket(row);
      const quote = quoteForSymbol(row.symbol, market);
      const ltp = firstFinite(quote.price, quote.last_price, quote.latest_price, row.latest_price);
      const bucket = orderStatusBucket(row);
      return `<tr class="broker-order-row order-${escapeHtml(bucket)}">
        <td><span class="tag ${side}">${escapeHtml(String(row.side || "-").toUpperCase())}</span></td>
        <td>${escapeHtml(orderFilledText(row))}</td>
        <td><strong>${escapeHtml(displayValue(row.symbol, "Symbol"))}</strong></td>
        <td>${escapeHtml(orderMetaText(row))}</td>
        <td><span class="order-time">${escapeHtml(fmtTime(row.ts))}</span></td>
        <td><span class="order-status-pill ${escapeHtml(bucket)}">${escapeHtml(orderStatusLabel(row))}</span></td>
        <td class="num">${fmtMarketMoney(row.price, market)}</td>
        <td class="num">LTP ${fmtMarketMoney(ltp, market)}</td>
        <td>${escapeHtml(shortValue(readableOrderReason(row), 80))}</td>
      </tr>`;
    })
    .join("");
  bindRowDetails(body, rows, "Order");
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

async function fetchDetailValue(value) {
  if (value?.detail_url && !value.details_json) {
    try {
      const response = await fetch(value.detail_url);
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      return { ...value, ...(await response.json()) };
    } catch (error) {
      return {
        ...value,
        detail_error: error.message || String(error),
      };
    }
  }
  return value;
}

let decisionDetailRequestSeq = 0;

async function renderDecisionDetailFromRow(row) {
  const panel = byId("decision-detail-panel");
  if (!panel || !row) return;
  const requestSeq = ++decisionDetailRequestSeq;
  if (row.detail_url && !row.details_json) {
    panel.innerHTML = `<div class="empty-state">Loading decision audit...</div>`;
  }
  const detailValue = await fetchDetailValue(row);
  if (requestSeq !== decisionDetailRequestSeq) return;
  renderDecisionDetailPanel(detailValue);
}

async function showDetails(title, value) {
  byId("drawer-title").textContent = title;
  byId("detail-drawer").classList.add("open");
  byId("drawer-body").innerHTML = `<div class="empty-state">Loading details...</div>`;
  const detailValue = await fetchDetailValue(value);
  state.currentDrawerValue = detailValue;
  byId("drawer-body").innerHTML = detailHtml(detailValue);
  bindDetailActions();
}

function bindDetailActions() {
  byId("drawer-body")?.querySelectorAll("[data-order-cancel]").forEach((button) => {
    button.addEventListener("click", () => cancelOrder(Number(button.dataset.orderCancel || 0), button));
  });
  byId("drawer-body")?.querySelectorAll("[data-order-modify]").forEach((button) => {
    button.addEventListener("click", () => {
      showDetails("Modify Order", {
        status: "Modify is available for open broker orders. For now, cancel and place a fresh guarded order from the signal or watchlist.",
      });
    });
  });
  byId("drawer-body")?.querySelectorAll("[data-watchlist-buy]").forEach((button) => {
    button.addEventListener("click", () => buyWatchlistIdea(state.currentDrawerValue || {}, button));
  });
  byId("drawer-body")?.querySelectorAll("[data-watchlist-sell]").forEach((button) => {
    button.addEventListener("click", () => sellWatchlistPosition(state.currentDrawerValue || {}, button));
  });
  byId("drawer-body")?.querySelectorAll("[data-watchlist-chart]").forEach((button) => {
    button.addEventListener("click", () => openWatchlistChart(state.currentDrawerValue || {}));
  });
  byId("drawer-body")?.querySelectorAll("[data-watchlist-note]").forEach((button) => {
    button.addEventListener("click", () => showDetails(button.dataset.watchlistNoteTitle || "Watchlist Action", {
      status: "not_enabled",
      message: button.dataset.watchlistNote || "This action needs a broker/order feature connection before OpenStocks can execute it.",
    }));
  });
}

async function cancelOrder(orderId, button) {
  if (!orderId) return;
  const previous = button?.textContent || "CANCEL";
  if (button) {
    button.disabled = true;
    button.textContent = "CANCELLING";
  }
  try {
    const response = await fetch(`/api/orders/${encodeURIComponent(orderId)}/cancel`, { method: "POST" });
    const payload = await response.json().catch(() => ({ detail: `HTTP ${response.status}` }));
    if (!response.ok) throw new Error(payload.detail || `HTTP ${response.status}`);
    await refreshStatusOnly();
    byId("detail-drawer")?.classList.remove("open");
  } catch (error) {
    showBackendError(networkErrorMessage(error, "cancel order"), { action: "cancel order", order_id: orderId });
    if (button) {
      button.disabled = false;
      button.textContent = previous;
    }
  }
}

function detailHtml(value) {
  if (!value || typeof value !== "object") {
    return `<section class="audit-section"><p>${escapeHtml(value)}</p></section>`;
  }
  if (Array.isArray(value)) {
    return `<section class="audit-section">
      <h4>Summary</h4>
      <p>${escapeHtml(value.length ? `${value.length} records are available. Open the matching tab to work with the list.` : "No records available.")}</p>
    </section>`;
  }
  if (value.__detail_type === "opportunity_health") {
    return opportunityHealthDetailHtml(value);
  }
  if (value.suggestion || value.__detail_type === "watchlist_stock") {
    return watchlistStockDetailHtml(value);
  }
  if (value.exit_plan) {
    return positionDetailHtml(value);
  }
  if (value.details_json && value.action) {
    return decisionDetailHtml(value);
  }
  if (value.side) {
    return orderDetailHtml(value);
  }
  const rows = Object.entries(value)
    .filter(([key]) => !key.endsWith("_json"))
    .map(([key, item]) => `<div><span>${escapeHtml(humanLabel(key))}</span><strong>${escapeHtml(formatDetailValue(item))}</strong></div>`)
    .join("");
  return `<section class="audit-section">
    <h4>Summary</h4>
    <div class="detail-list">${rows || `<div><span>Status</span><strong>No summary fields available</strong></div>`}</div>
  </section>`;
}

function opportunityHealthDetailHtml(value = {}) {
  const scan = value.opportunity_scan || {};
  const marketAction = value.market_action_radar || scan.market_action_radar || {};
  const gainersPlaybook = scan.top_gainers_playbook || {};
  const playbookLabel = gainersPlaybook.label || (normalizeUiMarket(value.market_region || state.activeMarket) === "US" ? "US Top Movers Playbook" : "NSE Top-Gainers Playbook");
  const playbookMethod = normalizeUiMarket(gainersPlaybook.market_region || value.market_region || state.activeMarket) === "US"
    ? "US movers are tiered first, then scored with deterministic Weinstein, VCP, RS, relative volume, 52-week positioning, dollar liquidity, Darvas, ATR-aware stop, and anti-pattern rules. Catalyst text is tagged separately."
    : "Top gainers are tiered first, then scored with deterministic Weinstein, VCP, RS, volume, 52-week positioning, delivery, Darvas, and anti-pattern rules. Catalyst text is tagged separately.";
  const playbookRecords = Array.isArray(gainersPlaybook.records) ? gainersPlaybook.records : [];
  const marketEvents = marketAction.events || [];
  const premarket = value.premarket_watchlist || {};
  const candidates = premarket.candidates || [];
  const playbookRows = playbookRecords.slice(0, 30).map((item, index) => {
    const signal = String(item.final_signal || "WATCH").toLowerCase().replace(/\s+/g, "-");
    const levels = item.levels || {};
    const catalyst = item.catalyst_review || {};
    const anti = (item.anti_patterns || []).slice(0, 2).map((flag) => flag.label || flag.code).filter(Boolean).join(" · ");
    return `<tr>
      <td>${fmtNumber(index + 1)}</td>
      <td><strong>${escapeHtml(item.symbol || "-")}</strong><small>${escapeHtml(shortValue(item.name || item.sector || "-", 34))}</small></td>
      <td>${fmtNumber(item.gain_pct)}%</td>
      <td><span class="tag ${signal}">${escapeHtml(item.final_signal || "-")}</span><small>${escapeHtml(item.tier || "-")}</small></td>
      <td>${fmtNumber(item.quant_score || 0)}/100</td>
      <td>${escapeHtml((item.weinstein || {}).stage || "-")}</td>
      <td>${fmtNumber((item.vcp || {}).score || 0)}/10</td>
      <td>${fmtNumber((item.relative_strength || {}).rs_rank)}</td>
      <td>${fmtNumber(item.volume_ratio)}x</td>
      <td>${escapeHtml(catalyst.catalyst_type || "-")}<small>${escapeHtml(catalyst.catalyst_strength || "-")}</small></td>
      <td>${levels.pivot ? `${fmtNumber(levels.pivot)} / ${fmtNumber(levels.stop)}` : "-"}</td>
      <td>${escapeHtml(shortValue(anti || (item.tier_reasons || []).join(" · ") || "-", 120))}</td>
    </tr>`;
  }).join("");
  const watchRows = (gainersPlaybook.tomorrow_watchlist || []).slice(0, 12).map((item) => {
    const levels = item.levels || {};
    return `<tr>
      <td><strong>${escapeHtml(item.symbol || "-")}</strong></td>
      <td>${escapeHtml(item.final_signal || "WATCH")}</td>
      <td>${levels.pivot ? fmtNumber(levels.pivot) : "-"}</td>
      <td>${fmtNumber(item.volume_ratio)}x now</td>
      <td>${escapeHtml(shortValue(((item.audit_trail || {}).watch || (item.tier_reasons || []).join(" · ") || "-"), 130))}</td>
    </tr>`;
  }).join("");
  const avoidRows = (gainersPlaybook.do_not_chase || []).slice(0, 12).map((item) => `<tr>
    <td><strong>${escapeHtml(item.symbol || "-")}</strong></td>
    <td>${escapeHtml(shortValue(item.reason || "-", 180))}</td>
  </tr>`).join("");
  const marketRows = marketEvents.slice(0, 25).map((item, index) => {
    const eventTypes = (item.event_types || []).slice(0, 3).map(humanLabel).join(" · ");
    const move = numericValue(item.pct_change);
    const volume = numericValue(item.volume_multiplier);
    return `<tr>
      <td>${fmtNumber(index + 1)}</td>
      <td><strong>${escapeHtml(item.symbol || "-")}</strong><small>${escapeHtml(shortValue(item.name || item.source || "-", 32))}</small></td>
      <td>${move !== null ? `${fmtNumber(move)}%` : "-"}</td>
      <td>${fmtNumber(item.market_action_score || item.score || 0)}</td>
      <td>${escapeHtml(humanLabel(item.strategy || "market action"))}</td>
      <td>${volume !== null ? `${fmtNumber(volume)}x` : "-"}</td>
      <td>${escapeHtml(shortValue(item.reason || eventTypes || "-", 130))}</td>
    </tr>`;
  }).join("");
  const rows = candidates.slice(0, 20).map((item, index) => {
    const reasons = (item.key_reasons || []).slice(0, 3).join(" · ");
    const entry = item.entry_zone || {};
    const entryText = entry.low || entry.high ? `${fmtNumber(entry.low)}-${fmtNumber(entry.high)}` : "-";
    return `<tr>
      <td>${fmtNumber(index + 1)}</td>
      <td><strong>${escapeHtml(item.symbol || "-")}</strong><small>${escapeHtml(humanLabel(item.label || "watch"))}</small></td>
      <td>${fmtNumber(Number(item.score || 0) * 100)}</td>
      <td>${escapeHtml(humanLabel(item.catalyst_type || "unknown"))}</td>
      <td>${fmtNumber(item.pivot)}</td>
      <td>${escapeHtml(entryText)}</td>
      <td>${escapeHtml(shortValue(reasons || item.setup_summary || "-", 130))}</td>
    </tr>`;
  }).join("");
  const summaryRows = [
    ["Live market-action movers", fmtNumber(marketAction.event_count || marketEvents.length || 0)],
    ["Premarket candidates", fmtNumber(premarket.candidate_count || candidates.length || 0)],
    [`${playbookLabel} evaluated`, fmtNumber(gainersPlaybook.total_gainers_evaluated || playbookRecords.length || 0)],
    ["Playbook BUY signals", fmtNumber((gainersPlaybook.signal_summary || {}).strong_buy + (gainersPlaybook.signal_summary || {}).moderate_buy || 0)],
    ["Playbook excluded", fmtNumber((gainersPlaybook.tier_summary || {}).excluded || 0)],
    ["Live confirmations", fmtNumber(premarket.live_confirmation_count || 0)],
    ["Generated", fmtTime(marketAction.scanned_at || premarket.generated_at || scan.scanned_at)],
    ["Symbols with history", fmtNumber(premarket.symbols_with_history || 0)],
    ["Missing history", fmtNumber(premarket.missing_history_symbols || 0)],
    ["Open scan symbols", fmtNumber(scan.scanned_symbols_this_cycle || scan.raw_symbols || 0)],
  ].map(([label, detail]) => `<div><span>${escapeHtml(label)}</span><strong>${escapeHtml(detail)}</strong></div>`).join("");
  return `<section class="audit-section">
    <h4>Live Market Action</h4>
    <p>${escapeHtml(marketEvents.length ? "Moneycontrol/Yahoo market-action movers are shown immediately; the slower strategy engine still confirms quality before normal conviction." : "No live market-action movers are available for this market yet.")}</p>
    <h4>${escapeHtml(playbookLabel)}</h4>
    <p>${escapeHtml(playbookRecords.length ? playbookMethod : "No top-movers playbook records are available yet.")}</p>
    <h4>Premarket Watchlist</h4>
    <p>${escapeHtml(candidates.length ? "Candidates are watches, not automatic BUYs. They need live confirmation at open." : "No premarket candidates are available for this market yet.")}</p>
    <div class="detail-list">${summaryRows}</div>
  </section>
  <section class="audit-section">
    <h4>${escapeHtml(playbookLabel)} Rulebook</h4>
    <div class="table-wrap">
      <table>
        <thead><tr><th>#</th><th>Symbol</th><th>Move</th><th>Signal</th><th>Score</th><th>Stage</th><th>VCP</th><th>RS</th><th>Vol</th><th>Catalyst</th><th>Pivot / Stop</th><th>Risk</th></tr></thead>
        <tbody>${playbookRows || `<tr><td colspan="12">No top-movers playbook rows yet</td></tr>`}</tbody>
      </table>
    </div>
  </section>
  <section class="audit-section">
    <h4>Tomorrow Watchlist</h4>
    <div class="table-wrap">
      <table>
        <thead><tr><th>Symbol</th><th>State</th><th>Trigger</th><th>Volume</th><th>What to watch</th></tr></thead>
        <tbody>${watchRows || `<tr><td colspan="5">No watchlist rows from today’s top movers.</td></tr>`}</tbody>
      </table>
    </div>
  </section>
  <section class="audit-section">
    <h4>Do Not Chase</h4>
    <div class="table-wrap">
      <table>
        <thead><tr><th>Symbol</th><th>Reason</th></tr></thead>
        <tbody>${avoidRows || `<tr><td colspan="2">No explicit avoid rows from today’s top movers.</td></tr>`}</tbody>
      </table>
    </div>
  </section>
  <section class="audit-section">
    <h4>Top Live Movers</h4>
    <div class="table-wrap">
      <table>
        <thead><tr><th>#</th><th>Symbol</th><th>Move</th><th>Radar</th><th>Setup</th><th>Vol</th><th>Why</th></tr></thead>
        <tbody>${marketRows || `<tr><td colspan="7">No market-action movers for ${escapeHtml(value.market_region || activeMarketLabel())}</td></tr>`}</tbody>
      </table>
    </div>
  </section>
  <section class="audit-section">
    <h4>Top Candidates</h4>
    <div class="table-wrap">
      <table>
        <thead><tr><th>#</th><th>Symbol</th><th>Score</th><th>Catalyst</th><th>Pivot</th><th>Entry Zone</th><th>Why</th></tr></thead>
        <tbody>${rows || `<tr><td colspan="7">No candidates for ${escapeHtml(value.market_region || activeMarketLabel())}</td></tr>`}</tbody>
      </table>
    </div>
  </section>`;
}

function rangePositionPct(low, high, price) {
  const lo = Number(low);
  const hi = Number(high);
  const current = Number(price);
  if (!Number.isFinite(lo) || !Number.isFinite(hi) || !Number.isFinite(current) || hi <= lo) return null;
  return Math.max(0, Math.min(100, ((current - lo) / (hi - lo)) * 100));
}

function dayRangeHtml(quote = {}, market = state.activeMarket) {
  const low = firstFinite(quote.low, quote.day_low);
  const high = firstFinite(quote.high, quote.day_high);
  const price = firstFinite(quote.price, quote.last_price, quote.latest_price, quote.close);
  const pct = rangePositionPct(low, high, price);
  return `<section class="trade-sheet-section">
    <h4>Day's range</h4>
    <div class="day-range-labels"><span>Low<br><strong>${fmtTradeMoney(low, market)}</strong></span><span>High<br><strong>${fmtTradeMoney(high, market)}</strong></span></div>
    <div class="day-range-bar"><span style="width:${pct === null ? 0 : pct}%"></span><i style="left:${pct === null ? 50 : pct}%"></i></div>
    <div class="day-range-labels muted"><span>Open<br><strong>${fmtTradeMoney(quote.open, market)}</strong></span><span>Prev. close<br><strong>${fmtTradeMoney(quote.previous_close || quote.prev_close || quote.close, market)}</strong></span></div>
  </section>`;
}

function quoteDetailRowsHtml(quote = {}, market = state.activeMarket) {
  const rows = [
    ["Volume", fmtCompact(quote.volume)],
    ["Avg. trade price", fmtTradeMoney(quote.avg_trade_price || quote.average_price, market)],
    ["Last traded quantity", fmtNumber(quote.last_traded_quantity || quote.ltq)],
    ["Last traded at", quote.ts ? fmtDateTime(quote.ts) : "-"],
    ["Lower circuit", fmtTradeMoney(quote.lower_circuit || quote.lower_circuit_limit, market)],
    ["Upper circuit", fmtTradeMoney(quote.upper_circuit || quote.upper_circuit_limit, market)],
  ];
  return `<section class="trade-sheet-section compact-list">
    ${rows.map(([label, value]) => `<div><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></div>`).join("")}
  </section>`;
}

function preferredManualTradeMode() {
  const mode = String(state.account?.signal_execution_mode || state.auth?.user?.signal_execution_mode || "SIGNAL_ONLY").toUpperCase();
  if (mode === "AUTO_LIVE") return "LIVE";
  return "PAPER";
}

function preferredManualTradeAction() {
  return preferredManualTradeMode() === "LIVE" ? "live" : "paper";
}

function preferredManualTradeLabel() {
  return preferredManualTradeMode() === "LIVE" ? "Buy Live" : "Buy Paper";
}

function preferredManualTradeNote(row = {}) {
  const market = rowMarket(row);
  if (preferredManualTradeMode() === "LIVE") {
    return "This creates a guarded live request only if your personal broker token, cash, and risk checks pass.";
  }
  return `This uses ${market === "US" ? "US" : "India"} paper cash. No real broker order is placed, and P&L is simulated in Positions.`;
}

function openPositionForSymbol(symbol, market = state.activeMarket) {
  const normalized = String(symbol || "").toUpperCase();
  const region = normalizeUiMarket(market);
  return (state.latest?.positions || []).find((row) => (
    String(row.symbol || "").toUpperCase() === normalized
    && rowMarket(row) === region
    && positionQuantity(row) > 0
  ));
}

function openWatchlistChart(row = {}) {
  const symbol = String(row.symbol || "").toUpperCase();
  if (symbol) {
    const input = byId("analyze-symbol");
    if (input) input.value = symbol;
    setAnalyzeMarket(rowMarket(row));
  }
  byId("detail-drawer")?.classList.remove("open");
  setView("analyze");
}

async function buyWatchlistIdea(row = {}, button = null) {
  const ideaId = Number(row.id || row.idea_id || 0);
  if (!ideaId) {
    showDetails("Buy Unavailable", {
      status: "missing_signal",
      message: "This watchlist row does not have a signal id. Open a signal idea row to place a paper/live request.",
    });
    return;
  }
  const action = preferredManualTradeAction();
  const modeLabel = preferredManualTradeMode() === "LIVE" ? "LIVE broker request" : "PAPER money";
  const symbol = String(row.symbol || "this stock").toUpperCase();
  const note = preferredManualTradeNote(row);
  if (!window.confirm(`Buy ${symbol} using ${modeLabel}?\n\n${note}`)) return;
  await followIdea(row, action, button, { manualOverride: action === "paper" });
}

async function sellWatchlistPosition(row = {}, button = null) {
  const symbol = String(row.symbol || "").toUpperCase();
  const market = rowMarket(row);
  const position = openPositionForSymbol(symbol, market);
  if (!position) {
    showDetails("Sell Unavailable", {
      symbol,
      market_region: market,
      status: "no_open_position",
      message: "There is no open paper/live position for this stock. Sell is available after you buy or follow it as a position.",
    });
    return;
  }
  await manualExitPosition(position, button);
}

function watchlistStockDetailHtml(row = {}) {
  const market = rowMarket(row);
  const quote = quoteForIdea(row) || {};
  const price = firstFinite(quote.price, quote.last_price, quote.latest_price, ideaWatchlistPrice(row));
  const dayPct = quote.symbol ? quoteDayPct(quote) : ideaWatchlistReturn(row);
  const dayChange = firstFinite(quote.day_change, quote.change);
  const sign = dayChange !== null && dayChange > 0 ? "+" : "";
  const exchange = row.exchange || quote.exchange || (market === "IN" ? "NSE" : "US");
  const eventTag = ideaHasEvent(row) ? `<span class="event-pill">EVENT</span>` : "";
  const buyLabel = preferredManualTradeLabel();
  const tradeNote = preferredManualTradeNote(row);
  return `<section class="trade-sheet">
    <header class="trade-sheet-head">
      <div>
        <h3>${escapeHtml(displayValue(row.symbol, "Symbol"))}</h3>
        <p>${escapeHtml(exchange)} <span class="${pnlClass(dayPct)}">${fmtTradeMoney(price, market)} ${dayChange !== null ? `${sign}${fmtNumber(dayChange)}` : ""} (${fmtSignedPct(dayPct)})</span></p>
      </div>
      ${eventTag}
    </header>
    <section class="trade-sheet-caution">
      <strong>${escapeHtml(buyLabel)}</strong>
      <span>${escapeHtml(tradeNote)}</span>
    </section>
    <div class="trade-sheet-actions primary-actions">
      <button class="buy-action" type="button" data-watchlist-buy>${escapeHtml(buyLabel)}</button>
      <button class="sell-action" type="button" data-watchlist-sell>Sell Position</button>
    </div>
    <div class="trade-sheet-actions secondary-actions">
      <button type="button" data-watchlist-chart>View chart</button>
      <button type="button" data-watchlist-note-title="Option Chain" data-watchlist-note="Option chain is not wired for this symbol yet. OpenStocks will not pretend to route an unavailable option action.">Option chain</button>
      <button type="button" data-watchlist-note-title="Set Alert" data-watchlist-note="Price alerts are not enabled yet. Use Buy Paper/Live or View chart for live actions today.">Set alert</button>
      <button type="button" data-watchlist-note-title="Add Notes" data-watchlist-note="Notes are not saved yet. This button is now explicit so it does not behave like a hidden dummy action.">Add notes</button>
      <button type="button" data-watchlist-note-title="Create GTT" data-watchlist-note="GTT creation needs broker-side order support. Use Buy Live only when your broker account is connected and guarded.">Create GTT</button>
    </div>
    ${dayRangeHtml({ ...quote, price }, market)}
    ${quoteDetailRowsHtml(quote, market)}
    <section class="trade-sheet-section compact-list">
      <h4>Apps</h4>
      <div><span>Fundamentals</span><strong>${escapeHtml(row.fundamental_quality || row.setup_bucket_label || "Open analysis")}</strong></div>
      <div><span>Technicals</span><strong>${escapeHtml(row.strategy || row.signal_type || "Open chart")}</strong></div>
    </section>
  </section>`;
}

function suggestionDetailHtml(row) {
  const audit = parseJsonObject(row.details_json);
  const context = audit.context || {};
  const market = rowMarket(row);
  const displaySignal = row.display_signal || row.suggestion;
  const opportunity = opportunityStatePayload(row);
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
      <h4>${escapeHtml(opportunity.label || "Why Suggested")}</h4>
      <p>${escapeHtml(opportunity.summary || row.display_reason || readableDecisionReason(row))}</p>
      ${opportunity.next_step ? `<p>${escapeHtml(opportunity.next_step)}</p>` : ""}
      ${auditList("Main Reasons", decisionReasonHighlights(row))}
      <div class="audit-chips">
        <span>Fresh action: ${escapeHtml(row.fresh_action_label || "-")}</span>
        <span>Setup: ${escapeHtml(opportunity.label || row.setup_bucket_label || "-")}</span>
        <span>Execution: ${escapeHtml(row.execution_state_label || "Signal Only")}</span>
        ${latestSystemAction ? `<span>Latest engine: ${escapeHtml(latestSystemAction)}</span>` : ""}
        <span>Next check: ${escapeHtml(opportunity.next_step || row.decision_readiness || "-")}</span>
        <span>Strategy: ${escapeHtml(row.strategy || "-")}</span>
        <span>Institutional: ${escapeHtml(flowBiasText(row.institutional_bias))}</span>
      </div>
    </section>
    ${opportunity.terms.length ? auditList("Term Guide", opportunity.terms.map((item) => `${item.term}: ${item.meaning}`)) : ""}
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
      status: plainDecisionPath(audit.decision_path || row.strategy || "-"),
      meta: `${fmtNumber(Number(row.confidence) * 100)}% confidence · ${fmtMarketMoney(row.price, market)}`,
    })}
    <section class="audit-section">
      <h4>Why ${escapeHtml(row.action)}</h4>
      <p>${escapeHtml(readableDecisionReason(row))}</p>
      ${auditList("Main Reasons", decisionReasonHighlights(row))}
      <div class="audit-chips">
        <span>Strategy: ${escapeHtml(row.strategy || context.best_strategy?.name || "-")}</span>
        <span>Path: ${escapeHtml(plainDecisionPath(audit.decision_path || "-"))}</span>
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
  `;
}

function orderDetailHtml(row) {
  const audit = parseJsonObject(row.details_json);
  const market = rowMarket(row);
  const quote = quoteForSymbol(row.symbol, market);
  const ltp = firstFinite(quote.price, quote.last_price, quote.latest_price);
  const dayPct = quote.symbol ? quoteDayPct(quote) : null;
  const bucket = orderStatusBucket(row);
  const cancellable = bucket === "open" && Number.isFinite(Number(row.id));
  const avgPrice = firstFinite(audit.execution?.avg_price, row.avg_price, row.average_price, bucket === "filled" ? row.price : null);
  return `<section class="trade-sheet order-sheet">
    <header class="trade-sheet-head">
      <div>
        <h3>${escapeHtml(displayValue(row.symbol, "Symbol"))}</h3>
        <p>${escapeHtml(orderMetaText(row))} <span class="${pnlClass(dayPct)}">${fmtTradeMoney(ltp, market)} ${fmtSignedPct(dayPct)}</span></p>
      </div>
      <span class="order-status-pill ${escapeHtml(bucket)}">${escapeHtml(orderStatusLabel(row))}</span>
    </header>
    <div class="trade-sheet-actions primary-actions">
      <button class="modify-action" type="button" ${cancellable ? "" : "disabled"} data-order-modify="${escapeHtml(row.id || "")}">MODIFY</button>
      <button class="cancel-action" type="button" ${cancellable ? "" : "disabled"} data-order-cancel="${escapeHtml(row.id || "")}">CANCEL</button>
    </div>
    <div class="trade-sheet-actions secondary-actions">
      <button type="button" data-view-jump="analyze">View chart</button>
      <button type="button" data-view-jump="analyze">Option chain</button>
      <button type="button" data-view-jump="suggestions">Set alert</button>
      <button type="button" data-view-jump="orders">Create GTT</button>
    </div>
    <section class="trade-sheet-section order-summary-grid">
      <div><span>Type</span><strong>${escapeHtml(orderMetaText(row).split("  ").pop() || "LIMIT")}</strong></div>
      <div><span>Price</span><strong>${fmtTradeMoney(row.price, market)}</strong></div>
      <div><span>Avg. price</span><strong>${fmtTradeMoney(avgPrice, market)}</strong></div>
      <div><span>Filled qty.</span><strong>${escapeHtml(orderFilledText(row))}</strong></div>
      <div><span>Value</span><strong>${fmtTradeMoney(row.notional, market)}</strong></div>
      <div><span>Placed at</span><strong>${escapeHtml(fmtDateTime(row.ts))}</strong></div>
    </section>
    <section class="trade-sheet-section">
      <h4>Order note</h4>
      <p>${escapeHtml(readableOrderReason(row))}</p>
      ${bucket === "open" && !cancellable ? `<p class="muted">This request is tracked from the portfolio workflow. Manage or exit it from Portfolio.</p>` : ""}
    </section>
    ${dayRangeHtml({ ...quote, price: ltp }, market)}
  </section>`;
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
  const normalized = Array.isArray(targets)
    ? targets.slice(0, 3).map((target, index) => {
        const payload = target && typeof target === "object" ? { ...target } : { price: target };
        const price = numericValue(payload.price);
        return price === null ? null : { ...payload, label: String(payload.label || `T${index + 1}`).toUpperCase(), price };
      }).filter(Boolean)
    : [];
  let previous = null;
  normalized.forEach((target) => {
    if (previous !== null && Number(target.price) <= previous) {
      const original = Number(target.price);
      target.price = previous + Math.max(previous * 0.01, 0.01);
      target.structure_reference = target.structure_reference ?? original;
      target.basis = target.basis || "spacing_adjusted";
      target.note = target.note || "Normalized so target ladder remains sequential.";
      if (target.rr === "structure") target.rr = "3.5_or_structure";
    }
    previous = Number(target.price);
  });
  return normalized;
}

function exitPlanMini(exit, market = "IN") {
  if (!exit || !Object.keys(exit).length) return `<span class="muted">pending</span>`;
  const t1 = exit.target_1 || {};
  return `<span class="exit-mini">SL ${fmtTradeMoney(exit.stop_loss, market)} · T1 ${fmtTradeMoney(t1.price, market)}</span>`;
}

function exitPlanHtml(exit, market = "IN") {
  if (!exit || !Object.keys(exit).length) return "";
  return `<section class="audit-section exit-plan">
    <h4>Exit Plan</h4>
    <div class="audit-cards">
      <div class="audit-card"><span>When</span><strong>${escapeHtml(exit.horizon || "swing_3_to_7_days")}</strong><small>check every cycle</small></div>
      <div class="audit-card"><span>Entry Zone</span><strong>${escapeHtml(formatZone(exit.entry_zone, market))}</strong><small>avoid chasing outside plan</small></div>
      <div class="audit-card"><span>Hard Stop</span><strong class="negative">${fmtTradeMoney(exit.stop_loss, market)}</strong><small>exit if invalidated</small></div>
      <div class="audit-card"><span>Target 1</span><strong class="positive">${fmtTradeMoney(exit.target_1?.price, market)}</strong><small>R:R ${escapeHtml(exit.target_1?.rr ?? "-")}</small></div>
      <div class="audit-card"><span>Target 2</span><strong class="positive">${fmtTradeMoney(exit.target_2?.price, market)}</strong><small>R:R ${escapeHtml(exit.target_2?.rr ?? "-")}</small></div>
      <div class="audit-card"><span>Target 3</span><strong class="positive">${fmtTradeMoney(exit.target_3?.price, market)}</strong><small>${escapeHtml(exit.target_3?.rr ?? "-")}</small></div>
    </div>
    <p>${escapeHtml(exit.plan || "-")}</p>
    ${objectCardsHtml("Invalidation", exit.invalidation)}
    ${auditList("Exit Monitoring", exit.monitoring_checklist)}
  </section>`;
}

function formatZone(zone, market = "IN") {
  if (!Array.isArray(zone) || !zone.length) return "-";
  return zone.length === 1
    ? fmtTradeMoney(zone[0], market)
    : `${fmtTradeMoney(zone[0], market)} - ${fmtTradeMoney(zone[zone.length - 1], market)}`;
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
    <h4>Signal Evidence</h4>
    <div class="audit-cards two">
      <div class="audit-card">
        <span>Requested Action</span>
        <strong>${escapeHtml(audit.requested_action || audit.final_action || "-")}</strong>
        <small>model risk: ${escapeHtml(llm.risk || "-")}</small>
      </div>
      <div class="audit-card">
        <span>Analysed By</span>
        <strong>${admin ? escapeHtml(audit.model || llm.model || "-") : "Signal Check"}</strong>
        <small>${admin ? `${escapeHtml(audit.provider || llm.provider || "-")} · ` : ""}${escapeHtml(audit.analysis_mode || llm.analysis_mode || "single_context")}</small>
      </div>
      <div class="audit-card">
        <span>Confidence Gate</span>
        <strong>${audit.confidence_gate?.passed ? "passed" : "not passed"}</strong>
        <small>minimum ${fmtNumber(Number(audit.confidence_gate?.minimum_required || 0) * 100)}%</small>
      </div>
    </div>
    ${admin ? objectCardsHtml("Signal Routing", {
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
    ${objectCardsHtml("Signal Plan", llm.signal_plan)}
    ${objectCardsHtml("Trade Plan", llm.trade_plan)}
    ${auditList("Monitoring Checklist", llm.monitoring_checklist)}
    ${auditList("Data Limits", llm.data_gaps)}
	  </section>`;
}

function llmPayloadHtml() {
  return "";
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
      <div class="audit-card"><span>Signal Check</span><strong>${llmSelected ? "selected" : "not selected"}</strong><small>candidate limit ${escapeHtml(llmLimit ?? "-")}</small></div>
    </div>
	    ${auditList("Failed Gates", failed.length ? failed.map((gate) => humanizeGateFailure(gate, market)) : ["No hard gate failed."])}
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
      <div class="audit-card"><span>Accumulation Proxy</span><strong>${escapeHtml(scorecard.total_score ?? "-")}/100</strong><small>${escapeHtml(`${scorecard.grade || "-"} · ${scorecard.buy_ready ? "trade ready" : "not ready"}`)}</small></div>
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
    ${objectCardsHtml("Flow Evidence", analysis.institutional_flow)}
    ${objectCardsHtml("Key Levels", analysis.key_levels)}
    ${objectCardsHtml("Accumulation Structure", analysis.institutional_structure)}
    ${auditList("Monitoring Checklist", analysis.monitoring_checklist)}
    ${auditList("Data Gaps", analysis.data_gaps)}
  </section>`;
}

function scorecardHtml(scorecard) {
  if (!scorecard || typeof scorecard !== "object" || !Object.keys(scorecard).length) return "";
  const sections = scorecard.sections || {};
  return `<section class="audit-section">
    <h4>Accumulation Proxy Scorecard</h4>
    <div class="audit-cards">
      <div class="audit-card"><span>Score</span><strong>${escapeHtml(scorecard.total_score ?? "-")}/100</strong><small>${escapeHtml(scorecard.grade || "-")}</small></div>
      <div class="audit-card"><span>Trade Ready</span><strong>${escapeHtml(scorecard.buy_ready ? "yes" : "no")}</strong><small>${escapeHtml((scorecard.must_pass_failed || []).join(", ") || "all must-pass clear")}</small></div>
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
  const text = typeof value === "object" ? objectSummary(value) : String(value);
  return text.length > max ? `${text.slice(0, max)}...` : text;
}

function objectSummary(value) {
  if (Array.isArray(value)) return `${fmtNumber(value.length)} items`;
  const keys = Object.keys(value || {}).filter((key) => !key.endsWith("_json"));
  if (!keys.length) return "-";
  return keys.slice(0, 3).map((key) => `${humanLabel(key)}: ${formatDetailValue(value[key])}`).join(" · ");
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
    const rendered = typeof value === "object" ? objectSummary(value) : String(value);
    parts.push(`${humanLabel(key)}: ${rendered}`);
  }
  if (!parts.length) {
    for (const [key, value] of Object.entries(details).slice(0, 3)) {
      const rendered = typeof value === "object" ? objectSummary(value) : String(value);
      parts.push(`${humanLabel(key)}: ${rendered}`);
    }
  }
  return parts.join(" · ");
}

function formatDetailValue(value) {
  if (value === null || value === undefined) return "-";
  if (typeof value === "object") return objectSummary(value);
  return value;
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
  if (signalSearchActive()) {
    searchSignalsFromDatabase(state.signalSearchQuery);
  }
  refreshMarketIndices();
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
  byId("analyze-result").innerHTML = `<div class="empty-state">Running ${market} quote, candles, strategy, sentiment, risk checks, and signal notes if enabled...</div>`;
  try {
    const response = await fetch("/api/analyze-symbol", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ symbol, market, force_llm: false }),
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
  const pathLabel = plainDecisionPath(path);
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
  const referenceNoteHtml = analysisReferenceNoteHtml(referenceData, payload);
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
      ${referenceNoteHtml}
      <div class="analysis-tabs" role="tablist" aria-label="Analysis result sections">
        <button class="active" type="button" data-analysis-tab="overview">Overview</button>
        <button type="button" data-analysis-tab="chart">Chart</button>
        <button type="button" data-analysis-tab="strategy">Strategy</button>
        <button type="button" data-analysis-tab="sentiment">Sentiment</button>
        <button type="button" data-analysis-tab="risk">Risk</button>
      </div>
      <div class="analysis-tab-panels">
        <section class="analysis-tab-panel active" data-analysis-panel="overview">
          <div class="manual-analysis-card">
            <div><span>Signal</span><strong><span class="tag ${action}">${escapeHtml(decision.action || "-")}</span></strong><small>${escapeHtml(pathLabel)}</small></div>
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
      </div>
    </section>
  `;
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
  const parsed = numericValue(value);
  if (parsed !== null) {
    if (metric.kind === "money") rendered = fmtMarketMoney(parsed, market);
    else if (metric.kind === "compact") rendered = fmtCompact(parsed);
    else rendered = fmtNumber(parsed);
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
  const mobileThemeButton = byId("mobile-theme-toggle-btn");
  if (mobileThemeButton) {
    mobileThemeButton.addEventListener("click", () => applyTheme(document.body.dataset.theme === "dark" ? "light" : "dark"));
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
  const ideasSearch = byId("ideas-watchlist-search");
  if (ideasSearch) {
    ideasSearch.addEventListener("input", () => {
      state.ideaWatchlistSearch = ideasSearch.value || "";
      rerenderIdeasWatchlistFromState();
    });
  }
  const signalSearchForm = byId("signal-search-form");
  const signalSearchInput = byId("signal-search-input");
  const signalSearchClear = byId("signal-search-clear");
  if (signalSearchForm && signalSearchInput) {
    signalSearchForm.addEventListener("submit", (event) => {
      event.preventDefault();
      if (state.signalSearchTimer) window.clearTimeout(state.signalSearchTimer);
      searchSignalsFromDatabase(signalSearchInput.value || "");
    });
    signalSearchInput.addEventListener("input", () => {
      const value = signalSearchInput.value || "";
      state.signalSearchQuery = value.trim();
      if (state.signalSearchTimer) window.clearTimeout(state.signalSearchTimer);
      state.signalSearchTimer = window.setTimeout(() => searchSignalsFromDatabase(value), 300);
      if (!value.trim()) {
        state.signalSearchRows = [];
        state.signalSearchInFlight = false;
        rerenderSignalsFromState();
      } else {
        updateSignalSearchStatus(state.signalSearchRows || []);
      }
    });
  }
  if (signalSearchClear && signalSearchInput) {
    signalSearchClear.addEventListener("click", () => {
      signalSearchInput.value = "";
      if (state.signalSearchTimer) window.clearTimeout(state.signalSearchTimer);
      searchSignalsFromDatabase("");
      signalSearchInput.focus();
    });
  }
  const trackGroupButton = byId("ideas-watchlist-track-group");
  if (trackGroupButton) {
    trackGroupButton.addEventListener("click", () => showDetails("Watchlist Filters", {
      active_group: state.activeIdeaGroup,
      search: state.ideaWatchlistSearch || "",
      visible_symbols: (state.currentIdeaWatchlistRows || []).map((row) => row.symbol).filter(Boolean),
      tracking: "Use the Track button on a row to add it to your watchlist without opening a paper position.",
    }));
  }
  const newGroupButton = byId("ideas-watchlist-new-group");
  if (newGroupButton) {
    newGroupButton.addEventListener("click", () => showDetails("Watchlist Groups", {
      groups: ideaWatchlistGroups(
        payloadRowsForMarket(state.latest || {}, "suggestions", state.activeMarket),
        payloadRowsForMarket(state.latest || {}, "tracked_ideas", state.activeMarket),
        state.latest?.strategy_plans || [],
        payloadRowsForMarket(state.latest || {}, "monitor_watchlist", state.activeMarket),
      ).map((group) => ({ key: group.key, label: group.label, count: group.rows.length })),
      note: "Groups come from Buys, confidence, custom monitor symbols, Strategy Plans, and tracked watchlist.",
    }));
  }
  const portfolioSearch = byId("mobile-portfolio-search");
  if (portfolioSearch) {
    portfolioSearch.addEventListener("input", () => {
      state.portfolioSearch = portfolioSearch.value || "";
      rerenderMobilePortfolioFromState();
    });
  }
  document.querySelectorAll("[data-mobile-portfolio-tab]").forEach((button) => {
    button.addEventListener("click", () => {
      state.activePortfolioTab = button.dataset.mobilePortfolioTab || "positions";
      rerenderMobilePortfolioFromState();
    });
  });
  document.querySelectorAll("[data-mobile-portfolio-tool]").forEach((button) => {
    button.addEventListener("click", () => showDetails("Portfolio Tools", {
      active_tab: state.activePortfolioTab,
      search: state.portfolioSearch || "",
      filters: "Use Holdings for tracked ideas and Positions for paper/live open quantities.",
    }));
  });
  byId("drawer-close").addEventListener("click", () => byId("detail-drawer").classList.remove("open"));
  for (const button of document.querySelectorAll(".nav-item")) {
    const navLabel = button.querySelector("span:not(.nav-icon)")?.textContent?.trim();
    if (navLabel) button.setAttribute("aria-label", navLabel);
    button.addEventListener("click", () => {
      if (isMobileSidebar()) setSidebarOpen(false);
      setView(button.dataset.view);
    });
  }
  for (const button of document.querySelectorAll(".mobile-bottom-nav-item")) {
    button.addEventListener("click", () => {
      setSidebarOpen(false);
      setView(button.dataset.mobileView);
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
  bindSwipeTabLists();
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
      if (["portfolio", "cash", "invested", "pnl", "total-pnl"].includes(target)) {
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
          showDetails(`${activeMarketLabel()} Opportunity Scan`, opportunityHealthPayload(state.latest || {}, state.activeMarket));
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
        showDetails(`${activeMarketLabel()} Opportunity Scan`, opportunityHealthPayload(state.latest || {}, state.activeMarket));
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
  document.body.dataset.view = view;
  for (const item of document.querySelectorAll(".nav-item")) {
    item.classList.toggle("active", item.dataset.view === view);
  }
  for (const item of document.querySelectorAll(".mobile-bottom-nav-item")) {
    const active = item.dataset.mobileView === view;
    item.classList.toggle("active", active);
    item.setAttribute("aria-current", active ? "page" : "false");
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
    startPositionMarkPolling();
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
    const payload = JSON.parse(event.data);
    if (payload?.event === "position_marks_refreshed") {
      if (!state.positionMarksTimer) refreshPositionMarks();
      return;
    }
    if (state.auth?.admin) {
      render(payload);
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
