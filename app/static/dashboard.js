const state = {
  latest: null,
  config: null,
  auth: { authenticated: false, admin: false, admin_configured: false, user: null },
  account: null,
  logs: [],
  users: [],
  socket: null,
  quoteFilter: "",
  activeSettingsTab: "broker",
};

const SETTINGS_TAB_CATEGORIES = {
  broker: new Set(["Market Data", "Live Protection"]),
  runtime: new Set(["Runtime", "Agent Cycle"]),
  ai: new Set(["LLM Brain", "Sentiment", "Global Intelligence", "Institutional Feeds"]),
  risk: new Set(["Risk"]),
  access: new Set(["Access Control"]),
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

function pnlClass(value) {
  const numeric = Number(value);
  if (numeric > 0) return "positive";
  if (numeric < 0) return "negative";
  return "";
}

function humanLabel(value) {
  return String(value || "-")
    .replaceAll("_", " ")
    .replaceAll("-", " ")
    .replace(/\b\w/g, (letter) => letter.toUpperCase());
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
    pre_filter_stage2_distribution: "Delivery data shows distribution, so OpenTrade is avoiding a fresh BUY.",
    market_breadth_bear_confirmed_no_new_longs: "The broader market is in a confirmed bearish breadth regime, so fresh BUY signals are blocked.",
    expiry_day_no_new_longs: "It is expiry day, so OpenTrade is waiting instead of opening a fresh long.",
    monthly_expiry_no_new_longs: "Monthly expiry risk is active, so OpenTrade is waiting for cleaner confirmation.",
    monthly_expiry_eve_no_new_longs: "Monthly expiry is close, so OpenTrade is reducing event risk and avoiding fresh longs.",
    earnings_lockout: "Earnings are too close, so OpenTrade is waiting for clarity.",
    extended_entry_no_new_longs: "The entry is extended from the ideal breakout zone, so OpenTrade is waiting for a better price.",
    false_breakout_two_day_rule_failed: "The breakout failed confirmation and closed back below resistance.",
    stage_analysis_not_stage2_markup: "The stock is not in a clean Stage 2 markup trend, so fresh BUY is blocked.",
    climax_top_detected_no_new_longs: "Price-volume action looks like a possible climax top, so OpenTrade is not buying.",
    timeframe_alignment_conflict: "Weekly, daily, and short-term trends are not aligned enough for a BUY.",
    options_max_pain_8pct_below_no_new_longs: "Options Max Pain is far below the current price, so upside risk/reward is weak for a new BUY.",
    risk_override_no_new_longs: "Risk overrides are active, so OpenTrade is not opening a new long.",
    portfolio_concentration_correlation_too_high: "The portfolio already has too much correlated exposure, so this BUY is blocked.",
    bottom_quartile_distribution: "The sector is weak and in distribution, so the stock needs exceptional confirmation before buying.",
    llm_failed_or_timed_out_deterministic_trade_preserved: "The LLM did not return a clean answer in time, so OpenTrade preserved the deterministic risk decision.",
    llm_not_selected_due_candidate_limit_deterministic_action_allowed: "The symbol was outside the current LLM review limit, so deterministic rules handled it.",
    time_stop_no_progress_15_sessions: "The position has not moved enough after 15 sessions, so OpenTrade is exiting dead capital.",
  };
  if (mapped[text]) return mapped[text];
  return compactSentence(humanLabel(text).toLowerCase());
}

function gateValueText(gateName, value) {
  if (value === null || value === undefined || value === "") return "";
  if (typeof value !== "object") return String(value);
  if (gateName === "stage_gate") {
    return `price ${fmtMoney(value.price)}, 30-period SMA ${fmtMoney(value.sma30)}, slope ${fmtNumber(value.slope)}`;
  }
  if (gateName === "sector_gate" || gateName === "sector_rotation_gate") {
    return `${value.sector_tier || "sector tier unknown"} · ${value.sector_stage || "stage unknown"}`;
  }
  if (gateName === "options_max_pain_gate") {
    return `Max Pain ${fmtMoney(value.max_pain)}, distance ${fmtPct(value.max_pain_distance_pct)}, source ${value.source || "-"}`;
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
  if (gateName === "system_rule_gates" || gateName.startsWith("system_rule_")) {
    const blocks = Array.isArray(value) ? value : value ? [value] : [];
    return blocks
      .map((block) => `${block.flag || "hard block"}: ${block.reason || "-"}`)
      .join("; ");
  }
  return shortValue(value, 120);
}

function humanizeGateFailure(gate) {
  const gateName = String(gate?.gate || gate?.name || "gate");
  const reason = String(gate?.reason || "");
  const value = gateValueText(gateName, gate?.value);
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
      ? "OpenTrade found a BUY setup that passed the main score and risk checks"
      : actionText === "SELL"
        ? "OpenTrade found exit pressure on an existing position"
        : "OpenTrade held because the setup did not pass every BUY requirement";
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
  if (!value) return "-";
  if (/tools\s+technical=/i.test(value)) return deterministicReasonFromText(value, action);
  if (/^[a-z0-9_]+$/i.test(value)) return reasonFromSnakeCase(value);
  return value
    .replace(/llm_primary_required_no_unreviewed_trade/g, "LLM approval was required before trading")
    .replace(/llm_failed_deterministic_action_preserved/g, "LLM failed, deterministic decision preserved")
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
    return `No fresh BUY: ${failed.slice(0, 2).map(humanizeGateFailure).join(" ")}`;
  }
  if (pre.elimination_reason) {
    return reasonFromSnakeCase(pre.elimination_reason);
  }
  if (audit.llm_error) {
    return `LLM did not return a usable decision, so OpenTrade used the safe ${action} result.`;
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
  if (stop) return `Sold for risk control: price ${fmtMoney(stop[1])} reached the stop level ${fmtMoney(stop[2])}.`;
  const tier2 = raw.match(/profit tier2: price ([0-9.]+) >= target2 ([0-9.]+)/i);
  if (tier2) return `Booked profit at Target 2: price ${fmtMoney(tier2[1])} reached ${fmtMoney(tier2[2])}.`;
  const tier1 = raw.match(/profit tier1: price ([0-9.]+) >= target1 ([0-9.]+)/i);
  if (tier1) return `Booked partial profit at Target 1: price ${fmtMoney(tier1[1])} reached ${fmtMoney(tier1[2])}, and the stop should tighten toward break-even.`;
  return humanizeReasonText(raw, row.side);
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
    const feedPending = isFeedPending(payload);
    error.className = `error-box ${feedPending ? "warning" : ""}`;
    error.textContent = feedPending
      ? "Market data connection pending. Connect or refresh Upstox when ready; the terminal remains available for account, settings, and audit review."
      : payload.last_error;
  } else {
    error.hidden = true;
    error.textContent = "";
    error.className = "error-box";
  }

  byId("position-count").textContent = `${positions.length} open`;
  byId("quote-count").textContent = `${quotes.length} quotes`;
  byId("account-quote-count").textContent = `${quotes.length} quotes`;
  byId("decision-count").textContent = `${decisions.length} decisions`;
  byId("overview-decision-count").textContent = `${decisions.length} decisions`;
  byId("suggestion-count").textContent = suggestions.length ? `${suggestions.length} full-audit ideas` : "0 ideas";
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
  renderPerformance(payload.performance || {});
  renderMacroEvents(payload.upcoming_macro_events || []);
  renderAgentConsole(payload);
  renderSelfAudit(payload.self_audit || {});
  renderShell(payload);
  drawEquity(payload.equity_curve || []);
}

function renderSelfAudit(audit = {}) {
  const panel = byId("self-audit-panel");
  if (!panel) return;
  const ok = audit.capital_pool_within_position_count_rule !== false && !Number(audit.price_mismatch_count || 0);
  byId("self-audit-status").textContent = audit.updated_at ? (ok ? "clear" : "flags") : "pending";
  panel.innerHTML = [
    { label: "Grade Violations", value: audit.grade_violation_count ?? 0, note: "WATCH/undefined entries" },
    { label: "Delivery Conflicts", value: audit.delivery_conflict_count ?? 0, note: "distribution vs long" },
    { label: "Price Mismatch", value: audit.price_mismatch_count ?? 0, note: ">1% source gap" },
    { label: "Earnings Calendar", value: audit.earnings_calendar_last_updated ? fmtDate(audit.earnings_calendar_last_updated) : "missing", note: "last updated" },
    { label: "Speculative", value: `${fmtNumber(audit.speculative_pct_of_open_positions || 0)}%`, note: `${audit.speculative_positions || 0}/${audit.open_positions || 0} positions` },
    { label: "Capital Rule", value: audit.capital_pool_within_position_count_rule === false ? "over limit" : "within limit", note: `${audit.open_positions || 0}/${audit.position_limit || "-"} positions` },
  ]
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
  byId("performance-status").textContent = `${orders.filled || 0} fills`;
  panel.innerHTML = `
    <button type="button" data-performance-detail="fills"><span>Filled</span><strong>${fmtNumber(orders.filled)}</strong><small>${fmtNumber(orders.vetoed)} vetoed</small></button>
    <button type="button" data-performance-detail="win"><span>Win Rate</span><strong>${fmtPct(Number(pnl.win_rate || 0) * 100)}</strong><small>${fmtNumber(positions.closed)} closed</small></button>
    <button type="button" data-performance-detail="realized"><span>Realized</span><strong class="${pnlClass(pnl.realized)}">${fmtMoney(pnl.realized)}</strong><small>${fmtMoney(pnl.expectancy_per_closed_trade)} expectancy</small></button>
    <button type="button" data-performance-detail="dd"><span>Max DD</span><strong class="${pnlClass(pnl.max_drawdown_pct)}">${fmtPct(pnl.max_drawdown_pct)}</strong><small>equity curve</small></button>
  `;
  panel.querySelectorAll("button").forEach((button) => button.addEventListener("click", () => showDetails("Trade Scoreboard", performance)));
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
  const feedLabel = health.display_label || (mode === "last_traded" ? "Last traded" : mode === "stale" ? "Stale quote" : "Upstox live");
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

  const feedPending = isFeedPending(payload);
  const upstoxConnected = String(provider).includes("upstox") && !feedPending;
  const feedIsFresh = upstoxConnected && mode === "live";
  byId("feed-pill").textContent = upstoxConnected ? feedLabel : "Upstox pending";
  byId("feed-pill").className = `pill ${feedIsFresh ? "running" : upstoxConnected ? "warning" : "stopped"}`;
  byId("ops-feed").textContent = upstoxConnected ? feedLabel : "Connect Upstox";
  byId("ops-feed-meta").textContent = feedPending
    ? "quotes paused until token/feed is ready"
    : `${health.quote_count || 0} quotes · ${health.is_market_open ? "market open" : "market closed"} · ${fmtAge(health.latest_quote_age_seconds)}`;
  byId("ops-llm").textContent = llmProvider === "offline" ? "Offline" : llmProvider;
  byId("ops-llm-meta").textContent = `${llmMode} · ${llmUsageText}`;
  byId("ops-risk").textContent = `${plainSetting("max_positions", "-")} slots`;
  byId("ops-risk-meta").textContent = `${fmtPct(Number(plainSetting("max_order_value_pct", 0)) * 100)} max order`;
  byId("ops-macro").textContent = macro.regime || "unknown";
  byId("ops-macro-meta").textContent = `${fmtNumber(macro.risk_score)} risk · breadth ${escapeHtml(breadth.breadth_regime || "neutral")}`;
  byId("ops-cycle").textContent = payload.running ? "Running" : "Stopped";
  byId("ops-cycle-meta").textContent = payload.last_cycle_at ? `${fmtTime(payload.last_cycle_at)} · ${plainSetting("agent_interval_seconds", "-")}s` : "manual run pending";
}

function isFeedPending(payload = state.latest || {}) {
  const provider = String(payload.market_health?.provider || payload.provider || payload.runtime?.market_data_provider || "");
  const error = String(payload.last_error || "");
  return provider.includes("upstox-not-connected") || /upstox|marketdataerror|no quotes|access token/i.test(error);
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
    {
      label: "Feed mode",
      value: health.mode || "unknown",
      note: health.provider || payload.provider || "-",
      onClick: () => openSettingsTab("broker"),
    },
    {
      label: "Universe",
      value: `${universe.enabled ?? "-"} enabled`,
      note: `${universe.symbols_per_cycle || "all"} per cycle · ${universe.low_price_enabled ?? 0} <= ₹100 priced`,
      onClick: () => setView("account"),
    },
    {
      label: "Exposure",
      value: fmtMoney(portfolio.invested),
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
  for (const tabName of ["broker", "runtime", "ai", "risk", "access"]) {
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
  for (const tabName of ["broker", "runtime", "ai", "risk", "access"]) {
    const target = byId(`settings-fields-${tabName}`);
    if (target && !target.innerHTML.trim()) {
      target.innerHTML = `<div class="empty-state">No settings in this tab.</div>`;
    }
  }
  renderUpstoxConnect(config.settings || {});
  setSettingsTab(state.activeSettingsTab || "broker");
  applyAccessMode();
  renderShell();
}

function settingsTabForCategory(category) {
  for (const [tabName, categories] of Object.entries(SETTINGS_TAB_CATEGORIES)) {
    if (categories.has(category)) return tabName;
  }
  return "ai";
}

function setSettingsTab(tabName) {
  const next = tabName || "broker";
  state.activeSettingsTab = next;
  for (const button of document.querySelectorAll(".settings-tab")) {
    button.classList.toggle("active", button.dataset.settingsTab === next);
  }
  for (const panel of document.querySelectorAll(".settings-tab-panel")) {
    panel.classList.toggle("active", panel.dataset.settingsPanel === next);
  }
}

function renderUpstoxConnect(settings) {
  const apiKey = byId("upstox-api-key");
  const apiSecret = byId("upstox-api-secret");
  const redirectUri = byId("upstox-redirect-uri");
  const status = byId("upstox-connect-status");
  if (apiKey && !apiKey.value) apiKey.placeholder = settings.upstox_api_key?.saved ? "API key saved" : "API Key";
  if (apiSecret && !apiSecret.value) apiSecret.placeholder = settings.upstox_api_secret?.saved ? "API secret saved" : "API Secret";
  if (redirectUri) redirectUri.value = settings.upstox_redirect_uri || `${window.location.origin}/upstox/callback`;
  if (status) {
    status.textContent = settings.upstox_access_token?.saved ? "connected" : "not connected";
    status.className = `settings-inline-status ${settings.upstox_access_token?.saved ? "positive" : ""}`;
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
      const summary = row.position_summary || {};
      const flags = summary.active_flags || [];
      return `<tr>
        <td><strong>${escapeHtml(row.symbol)}</strong></td>
        <td><span class="tag ${String(summary.classification || "").toLowerCase()}">${escapeHtml(summary.classification || "-")}</span><br><small>${escapeHtml(row.strategy || "-")}</small></td>
        <td><small>Entry ${escapeHtml(summary.entry_grade || "-")} · MTF ${escapeHtml(summary.mtf_grade || "-")} · Delivery ${escapeHtml(summary.delivery_bias || "-")}</small><br>${flags.length ? flags.map((flag) => `<span class="tag watch">${escapeHtml(flag)}</span>`).join(" ") : `<span class="tag open">CLEAR</span>`}</td>
        <td class="num">${row.qty}</td>
        <td class="num">${fmtMoney(row.market_price)}<br><small>${escapeHtml(summary.price_label || "LTP")}</small></td>
        <td class="num">${fmtMoney(marketValue)}</td>
        <td class="num ${pnlClass(pnl)}">${fmtMoney(pnl)}</td>
        <td><strong>${escapeHtml(summary.recommended_action || "HOLD")}</strong><br><small>${escapeHtml(summary.reason || "-")}</small></td>
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
      const targets = row.targets || [];
      const t1 = targets[0] || {};
      const t2 = targets[1] || {};
      const riskFlags = Array.isArray(row.risk_flags) ? row.risk_flags.slice(0, 3) : [];
      const institutionalFlags = row.institutional_flags && typeof row.institutional_flags === "object"
        ? Object.entries(row.institutional_flags)
            .filter(([, value]) => Boolean(value))
            .slice(0, 3)
            .map(([key]) => humanLabel(key))
        : [];
      const readiness = humanLabel(row.decision_readiness || "monitor_only");
      const confidence = Number(row.confidence || 0) * 100;
      return `<button class="suggestion-card ${index === 0 ? "featured" : ""}" type="button" data-index="${index}" aria-label="Open ${escapeHtml(row.symbol)} idea audit">
        <div class="suggestion-signal">
          <span class="rank">Idea #${index + 1}</span>
          <div class="suggestion-symbol-line">
            <strong>${escapeHtml(row.symbol)}</strong>
            <span class="tag ${action}">${escapeHtml(row.suggestion)}</span>
          </div>
          <small>${fmtMoney(row.price)} · ${escapeHtml(row.strategy || "-")}</small>
        </div>
        <div class="suggestion-score">
          <div><span>Signal</span><strong>${escapeHtml(readiness)}</strong><small>${fmtNumber(confidence)}% confidence</small></div>
          <div><span>Confluence</span><strong>${escapeHtml(row.confluence ?? "-")}/26</strong><small>${escapeHtml(row.tier || "-")}</small></div>
          <div><span>Combined</span><strong class="${pnlClass(row.combined_score)}">${fmtNumber(row.combined_score)}</strong><small>score after gates</small></div>
          <div><span>Institutional</span><strong class="${pnlClass(row.institutional_bias)}">${fmtNumber(row.institutional_bias)}</strong><small>${escapeHtml(institutionalFlags.join(", ") || "neutral")}</small></div>
        </div>
        <div class="suggestion-plan">
          <span><small>Entry</small><strong>${formatZone(row.entry_zone)}</strong></span>
          <span><small>Stop</small><strong class="negative">${fmtMoney(row.stop_loss)}</strong></span>
          <span><small>Target 1</small><strong class="positive">${fmtMoney(t1.price)}</strong></span>
          <span><small>Target 2</small><strong class="positive">${fmtMoney(t2.price)}</strong></span>
        </div>
        <div class="suggestion-reason">
          <span>Why</span>
          <p>${escapeHtml(shortValue(readableDecisionReason(row), 240))}</p>
        </div>
        <div class="suggestion-flags">
          <span>Full audit</span>
          <span>${escapeHtml(row.id ? `Decision #${row.id}` : "Decision audit")}</span>
          ${riskFlags.map((flag) => `<span class="warning">${escapeHtml(humanLabel(flag))}</span>`).join("")}
        </div>
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
  const filter = state.quoteFilter.trim().toUpperCase();
  const railRows = filter ? rows.filter((row) => String(row.symbol || "").toUpperCase().includes(filter)) : rows;
  byId("quote-count").textContent = filter ? `${railRows.length}/${rows.length} quotes` : `${rows.length} quotes`;
  const markup = rows
    .slice(0, 160)
    .map((row) => quoteRow(row))
    .join("");
  accountBody.innerHTML = markup || `<tr><td colspan="6">No quotes yet</td></tr>`;
  overviewBody.innerHTML =
    railRows
      .slice(0, 80)
      .map((row) => quoteRow(row))
      .join("") || `<tr><td colspan="6">No quotes yet</td></tr>`;
  bindRowDetails(accountBody, rows.slice(0, 160), "Quote");
  bindRowDetails(overviewBody, railRows.slice(0, 80), "Quote");
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
        <td class="reason">${escapeHtml(readableDecisionReason(row))}</td>
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
      <p>${escapeHtml(readableDecisionReason(row))}</p>
      ${auditList("Main Reasons", decisionReasonHighlights(row))}
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
  const summary = row.position_summary || {};
  return `
    ${auditHero({
      label: "Position",
      symbol: row.symbol,
      action: summary.recommended_action || (pnl >= 0 ? "OPEN" : "WATCH"),
      status: summary.classification || row.strategy || "-",
      meta: `${row.qty} qty · ${fmtMoney(pnl)} unrealized`,
    })}
    ${positionSummaryHtml(summary)}
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

function positionSummaryHtml(summary = {}) {
  const flags = summary.active_flags || [];
  return `
    <section class="audit-section">
      <h4>Rules Summary</h4>
      <div class="audit-cards">
        <div class="audit-card"><span>Classification</span><strong>${escapeHtml(summary.classification || "-")}</strong><small>${escapeHtml(summary.symbol || "")}</small></div>
        <div class="audit-card"><span>Entry / MTF / Delivery</span><strong>${escapeHtml(`${summary.entry_grade || "-"} / ${summary.mtf_grade || "-"} / ${summary.delivery_bias || "-"}`)}</strong><small>effective ${escapeHtml(summary.effective_entry_grade || "-")}</small></div>
        <div class="audit-card"><span>Sentiment</span><strong>${escapeHtml(summary.sentiment_status === "DATA_MISSING" ? "DATA_MISSING" : fmtNumber(summary.sentiment_score))}</strong><small>0.0 is not neutral</small></div>
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
      <p>${escapeHtml(readableDecisionReason(row))}</p>
      ${auditList("Main Reasons", decisionReasonHighlights(row))}
      <div class="audit-chips">
        <span>Strategy: ${escapeHtml(row.strategy || context.best_strategy?.name || "-")}</span>
        <span>Path: ${escapeHtml(humanLabel(audit.decision_path || "-"))}</span>
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
      <p>${escapeHtml(readableOrderReason(row))}</p>
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
        <span>${escapeHtml(humanLabel(gate.gate || "gate"))}</span>
        <strong class="${gate.passed === false ? "negative" : "positive"}">${gate.passed === false ? "needs attention" : "clear"}</strong>
        <small>${escapeHtml(gate.passed === false ? humanizeGateFailure(gate) : gateValueText(gate.gate, gate.value) || "passed")}</small>
      </div>`).join("")}
    </div>
    ${pre.elimination_reason ? `<p class="negative">${escapeHtml(reasonFromSnakeCase(pre.elimination_reason))}</p>` : ""}
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
  const gateContext = gates.decision_gate_context || {};
  const failed = failedGatesFromAudit(audit, audit.context || {});
  const scorecard = gates.institutional_scorecard || {};
  const scorecardStatus =
    scorecard.buy_ready === true ? "clear" : scorecard.buy_ready === false ? "not clear" : "not evaluated";
  const scorecardClass = scorecard.buy_ready === true ? "positive" : scorecard.buy_ready === false ? "negative" : "";
  return `<section class="audit-section">
    <h4>Risk Gates</h4>
    <div class="audit-cards">
      <div class="audit-card"><span>BUY Threshold</span><strong>${fmtNumber(gateContext.buy_threshold ?? gates.buy_combined_threshold)}</strong><small>combined score required before a fresh long</small></div>
      <div class="audit-card"><span>Open Position</span><strong>${gates.has_existing_position ? "yes" : "no"}</strong><small>${fmtNumber(gates.current_open_positions)} / ${fmtNumber(gates.max_positions)} positions used</small></div>
      <div class="audit-card"><span>Institutional Gate</span><strong class="${scorecardClass}">${scorecardStatus}</strong><small>${escapeHtml((scorecard.failed || []).map(reasonFromSnakeCase).join(" ") || "must-pass checks clear")}</small></div>
      <div class="audit-card"><span>LLM Review</span><strong>${gates.llm_deep_review_selected ? "selected" : "not selected"}</strong><small>candidate limit ${escapeHtml(gates.llm_candidate_limit ?? "-")}</small></div>
    </div>
    ${auditList("Failed Gates", failed.length ? failed.map(humanizeGateFailure) : ["No hard gate failed."])}
    <details class="raw-audit"><summary>Technical risk-gate data</summary><pre>${escapeHtml(JSON.stringify(gates, null, 2))}</pre></details>
  </section>`;
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
      <div class="audit-card"><span>Liquidity</span><strong>${escapeHtml(liquidity.liquidity_tier || "-")}</strong><small>${fmtMoney(liquidity.avg_traded_value_20)} avg value</small></div>
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
    ctx.strokeStyle = "rgba(0, 201, 139, 0.3)";
    ctx.setLineDash([6, 8]);
    ctx.beginPath();
    ctx.moveTo(pad, height / 2);
    ctx.lineTo(width - pad, height / 2);
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = "#7f8da0";
    ctx.font = "13px system-ui";
    ctx.fillText("Waiting for portfolio snapshots", pad, height / 2 - 12);
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
      <p>${escapeHtml(readableDecisionReason(decision))}</p>
      ${auditList("Main Reasons", decisionReasonHighlights(decision))}
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
  byId("refresh-logs-btn").addEventListener("click", fetchLogs);
  const quoteFilter = byId("quote-filter");
  if (quoteFilter) {
    quoteFilter.addEventListener("input", () => {
      state.quoteFilter = quoteFilter.value || "";
      renderQuotes(state.latest?.quotes || []);
    });
  }
  byId("drawer-close").addEventListener("click", () => byId("detail-drawer").classList.remove("open"));
  for (const button of document.querySelectorAll(".nav-item")) {
    button.addEventListener("click", () => setView(button.dataset.view));
  }
  for (const button of document.querySelectorAll("[data-view-jump]")) {
    button.addEventListener("click", () => setView(button.dataset.viewJump));
  }
  for (const button of document.querySelectorAll(".settings-tab")) {
    button.addEventListener("click", () => setSettingsTab(button.dataset.settingsTab));
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

function setView(view) {
  const drawer = byId("detail-drawer");
  if (drawer) drawer.classList.remove("open");
  for (const item of document.querySelectorAll(".nav-item")) {
    item.classList.toggle("active", item.dataset.view === view);
  }
  for (const section of document.querySelectorAll(".view")) {
    section.classList.toggle("active", section.id === `${view}-view`);
  }
  const label = document.querySelector(`.nav-item[data-view="${view}"] span`)?.textContent || "Overview";
  byId("view-title").textContent = label;
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
