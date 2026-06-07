const state = {
  configs: [],
  currentMode: "backtest",
  currentReport: null,
  currentTable: "orders",
  ctpMonitor: null,
  ctpRefreshTimer: null,
  ctpRefreshInFlight: false,
  watchlist: [],
  activeWatchSymbol: null,
  klineBars: [],
  klinePayload: null,
  klineRequestId: 0,
  klineHoverIndex: null,
  klineVisibleCount: 140,
  klineViewEnd: null,
};

const metricSpecs = [
  ["final_equity", "最终权益", "number"],
  ["total_return", "总收益", "percent"],
  ["max_drawdown", "最大回撤", "percent"],
  ["sharpe", "夏普", "fixed"],
  ["trade_count", "成交数", "integer"],
  ["win_rate", "胜率", "percent"],
  ["final_margin", "保证金", "number"],
  ["final_available", "可用资金", "number"],
  ["final_risk_ratio", "风险度", "percent"],
  ["final_settlement_pnl", "结算盈亏", "number"],
  ["working_order_count", "挂单数", "integer"],
];

const tableColumns = {
  orders: ["submitted_at", "symbol", "side", "offset", "order_type", "quantity", "status", "fill_price", "reject_reason"],
  trades: ["timestamp", "symbol", "side", "offset", "quantity", "price", "notional", "margin", "realized_pnl"],
  events: ["timestamp", "event_type", "severity", "symbol", "message"],
  optimization: ["fast_window", "slow_window", "sharpe", "total_return", "max_drawdown", "trade_count"],
};

const ctpOrderColumns = ["submitted_at", "symbol", "side", "offset", "order_type", "quantity", "status", "limit_price", "fill_price", "reject_reason"];
const ctpTradeColumns = ["timestamp", "symbol", "side", "offset", "quantity", "price", "notional", "realized_pnl"];
const ctpEventColumns = ["timestamp", "event_type", "severity", "symbol", "message"];
const defaultWatchlist = ["RB0", "IF0", "CU0"];
const watchlistStorageKey = "quant_platform_watchlist";
const maSpecs = [
  { id: "fast", label: "MA1", color: "#1463a5", defaultWindow: 5 },
  { id: "mid", label: "MA2", color: "#a15c07", defaultWindow: 20 },
  { id: "slow", label: "MA3", color: "#5b3f9b", defaultWindow: 60 },
];
const klineZoomSteps = [60, 90, 140, 240, 480];
const klineFetchLimit = 720;

const modeLabels = {
  backtest: "回测",
  replay: "回放",
  paper: "模拟",
  optimize: "优化",
};

const healthLabels = {
  OK: "正常",
  WARN: "警告",
  ERROR: "异常",
  UNKNOWN: "未知",
};

const valueLabels = {
  BUY: "买",
  SELL: "卖",
  OPEN: "开仓",
  CLOSE: "平仓",
  CLOSE_TODAY: "平今",
  CLOSE_YESTERDAY: "平昨",
  AUTO: "自动",
  MARKET: "市价",
  LIMIT: "限价",
  PENDING: "挂单",
  FILLED: "已成交",
  REJECTED: "已拒绝",
  CANCELED: "已撤单",
  INFO: "信息",
  WARN: "警告",
  WARNING: "警告",
  ERROR: "错误",
};

const columnLabels = {
  submitted_at: "提交时间",
  timestamp: "时间",
  symbol: "合约",
  side: "方向",
  offset: "开平",
  order_type: "类型",
  quantity: "数量",
  status: "状态",
  limit_price: "限价",
  fill_price: "成交价",
  price: "价格",
  notional: "名义金额",
  margin: "保证金",
  realized_pnl: "已实现盈亏",
  reject_reason: "拒绝原因",
  event_type: "事件",
  severity: "级别",
  message: "消息",
  fast_window: "快线",
  slow_window: "慢线",
  sharpe: "夏普",
  total_return: "总收益",
  max_drawdown: "最大回撤",
  trade_count: "成交数",
};

const alertText = {
  STATE_MISSING: ["状态文件缺失", "请先运行 ctp-realtime 并保存状态，或检查状态文件路径。"],
  STATE_STALE: ["状态文件已过期", "最近一次状态保存已经超过阈值。"],
  EVENT_LOG_MISSING: ["事件日志缺失", "请启用 event-log-path，或检查事件日志路径。"],
  EVENT_LOG_EMPTY: ["事件日志为空", "没有读到可展示的 JSONL 事件。"],
  TRADING_UNHEALTHY: ["交易连接异常", "交易柜台连接状态不健康。"],
  TRADING_DISCONNECTED: ["交易前置断开", "交易柜台当前未连接。"],
  TRADING_NOT_LOGGED_IN: ["交易未登录", "交易柜台已连接但未完成登录。"],
  MARKET_DATA_UNHEALTHY: ["行情连接异常", "行情柜台连接状态不健康。"],
  MARKET_DATA_DISCONNECTED: ["行情前置断开", "行情柜台当前未连接。"],
  MARKET_DATA_NOT_LOGGED_IN: ["行情未登录", "行情柜台已连接但未完成登录。"],
  MARKET_DATA_NO_SUBSCRIPTIONS: ["未订阅行情", "行情连接可用，但没有订阅合约。"],
  NO_TICK_RECEIVED: ["未收到 Tick", "已有订阅合约，但状态中没有最新 Tick。"],
  ORDER_REJECTED: ["存在拒单", "运行状态中发现被拒绝的订单。"],
};

function $(selector) {
  return document.querySelector(selector);
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const payload = await response.json();
  if (!response.ok || payload.error) {
    throw new Error(payload.error || `请求失败：${response.status}`);
  }
  return payload;
}

async function loadInitialState() {
  setStatus("加载中", "busy");
  const configsPayload = await api("/api/configs");
  state.configs = configsPayload.configs;
  renderConfigs();
  await refreshRuns();
  await refreshCtpMonitor();
  setStatus("空闲");
}

function renderConfigs() {
  const select = $("#config-select");
  select.innerHTML = "";
  for (const config of state.configs) {
    const option = document.createElement("option");
    option.value = config.path;
    option.textContent = `${config.name} - ${config.strategy}`;
    option.dataset.modeHint = config.modeHint;
    select.appendChild(option);
  }
  const selected = state.configs[0];
  if (selected) {
    select.value = selected.path;
    setMode(selected.modeHint || "backtest");
  }
}

async function refreshRuns() {
  const payload = await api("/api/runs");
  renderRuns(payload.runs);
}

function renderRuns(runs) {
  const list = $("#runs-list");
  list.innerHTML = "";
  if (!runs.length) {
    list.innerHTML = `<div class="run-item"><strong>暂无运行记录</strong><span>运行一次后这里会显示结果</span></div>`;
    return;
  }
  for (const run of runs.slice(0, 10)) {
    const item = document.createElement("button");
    item.className = "run-item";
    item.innerHTML = `<strong>${escapeHtml(run.name)}</strong><span>${escapeHtml(modeLabel(run.mode || "run"))} - ${formatMetric(run.metrics?.total_return, "percent")}</span>`;
    item.addEventListener("click", () => loadReport(run.outputDir));
    list.appendChild(item);
  }
}

async function startRun() {
  const configPath = $("#config-select").value;
  if (!configPath) return;

  setStatus("运行中", "busy");
  $("#run-button").disabled = true;
  $("#connection-state").textContent = "运行中";
  try {
    const maxStepsRaw = $("#max-steps-input").value;
    const payload = await api("/api/run", {
      method: "POST",
      body: JSON.stringify({
        mode: state.currentMode,
        configPath,
        maxSteps: maxStepsRaw ? Number(maxStepsRaw) : null,
      }),
    });
    state.currentReport = payload;
    renderReport(payload);
    await refreshRuns();
    setStatus("完成");
    $("#connection-state").textContent = "就绪";
  } catch (error) {
    setStatus("错误", "error");
    $("#connection-state").textContent = error.message;
  } finally {
    $("#run-button").disabled = false;
  }
}

async function startAkshareRun() {
  const symbol = $("#akshare-symbol-input").value.trim();
  if (!symbol) return;

  setStatus("拉取中", "busy");
  $("#run-akshare-button").disabled = true;
  $("#connection-state").textContent = "正在拉取 AkShare";
  try {
    const payload = await api("/api/akshare-run", {
      method: "POST",
      body: JSON.stringify({
        symbol,
        api: $("#akshare-api-select").value,
        outputSymbol: textInput("#akshare-output-symbol-input", symbol),
        market: textInput("#akshare-market-input", ""),
        variety: textInput("#akshare-variety-input", ""),
        period: $("#akshare-period-select").value,
        startDate: compactDate($("#akshare-start-input").value),
        endDate: compactDate($("#akshare-end-input").value),
        fastWindow: numberInput("#akshare-fast-input", 5),
        slowWindow: numberInput("#akshare-slow-input", 20),
        quantity: numberInput("#akshare-quantity-input", 1),
        cash: numberInput("#akshare-cash-input", 100000),
        multiplier: numberInput("#akshare-multiplier-input", 10),
        marginRate: numberInput("#akshare-margin-input", 0.12),
        commissionRate: numberInput("#akshare-commission-input", 0),
        slippage: numberInput("#akshare-slippage-input", 0),
        exchange: textInput("#akshare-exchange-input", "SHFE"),
        accountMode: "futures",
        dailySettlement: true,
      }),
    });
    state.currentReport = payload;
    state.currentMode = "backtest";
    renderReport(payload);
    await refreshRuns();
    ensureWatchSymbol(symbol);
    selectWatchSymbol(symbol);
    setStatus("完成");
    $("#connection-state").textContent = `AkShare ${payload.bars || 0} 根K线`;
  } catch (error) {
    setStatus("错误", "error");
    $("#connection-state").textContent = error.message;
  } finally {
    $("#run-akshare-button").disabled = false;
  }
}

function initializeWatchlist() {
  state.watchlist = loadWatchlist();
  state.activeWatchSymbol = state.watchlist[0] || null;
  renderWatchlist();
  if (state.activeWatchSymbol) {
    selectWatchSymbol(state.activeWatchSymbol);
  } else {
    drawEmptyKline($("#kline-chart").getContext("2d"), $("#kline-chart"), "添加自选合约后显示K线");
  }
}

function loadWatchlist() {
  try {
    const raw = JSON.parse(localStorage.getItem(watchlistStorageKey) || "[]");
    if (Array.isArray(raw)) {
      const symbols = raw.map(normalizeSymbol).filter(Boolean);
      const uniqueSymbols = [...new Set(symbols)].slice(0, 24);
      if (uniqueSymbols.length) {
        return uniqueSymbols;
      }
    }
  } catch (error) {
    console.warn("自选列表读取失败", error);
  }
  return [...defaultWatchlist];
}

function saveWatchlist() {
  localStorage.setItem(watchlistStorageKey, JSON.stringify(state.watchlist));
}

function addWatchSymbol() {
  const symbol = normalizeSymbol($("#watch-symbol-input").value || $("#akshare-symbol-input").value);
  if (!symbol) return;
  ensureWatchSymbol(symbol);
  selectWatchSymbol(symbol);
  $("#watch-symbol-input").value = "";
}

function ensureWatchSymbol(symbol) {
  const normalized = normalizeSymbol(symbol);
  if (!normalized) return;
  if (!state.watchlist.includes(normalized)) {
    state.watchlist.push(normalized);
    saveWatchlist();
  }
  renderWatchlist();
}

function removeWatchSymbol(symbol) {
  state.watchlist = state.watchlist.filter((item) => item !== symbol);
  if (!state.watchlist.length) {
    state.watchlist = [...defaultWatchlist];
  }
  if (!state.watchlist.includes(state.activeWatchSymbol)) {
    state.activeWatchSymbol = state.watchlist[0] || null;
  }
  saveWatchlist();
  renderWatchlist();
  if (state.activeWatchSymbol) {
    selectWatchSymbol(state.activeWatchSymbol);
  }
}

function renderWatchlist() {
  const list = $("#watchlist");
  list.innerHTML = "";
  for (const symbol of state.watchlist) {
    const item = document.createElement("div");
    item.className = `watch-item${symbol === state.activeWatchSymbol ? " active" : ""}`;

    const selectButton = document.createElement("button");
    selectButton.className = "watch-symbol";
    selectButton.textContent = symbol;
    selectButton.addEventListener("click", () => selectWatchSymbol(symbol));

    const removeButton = document.createElement("button");
    removeButton.className = "watch-remove";
    removeButton.title = `移除 ${symbol}`;
    removeButton.innerHTML = `<i data-lucide="x"></i>`;
    removeButton.addEventListener("click", () => removeWatchSymbol(symbol));

    item.appendChild(selectButton);
    item.appendChild(removeButton);
    list.appendChild(item);
  }
  renderLucideIcons();
}

function selectWatchSymbol(symbol) {
  const normalized = normalizeSymbol(symbol);
  if (!normalized) return;
  state.activeWatchSymbol = normalized;
  $("#akshare-symbol-input").value = normalized;
  $("#akshare-output-symbol-input").value = normalized;
  $("#watch-symbol-input").placeholder = normalized;
  $("#run-title").textContent = `${normalized} 行情与回测`;
  renderWatchlist();
  fetchKline(normalized);
}

async function fetchKline(symbol) {
  const requestId = state.klineRequestId + 1;
  state.klineRequestId = requestId;
  $("#kline-note").textContent = `${symbol} 正在拉取`;
  try {
    const params = new URLSearchParams({
      symbol,
      outputSymbol: symbol,
      api: $("#akshare-api-select").value,
      market: textInput("#akshare-market-input", ""),
      variety: textInput("#akshare-variety-input", ""),
      period: $("#akshare-period-select").value,
      startDate: compactDate($("#akshare-start-input").value),
      endDate: compactDate($("#akshare-end-input").value),
      limit: String(klineFetchLimit),
    });
    const payload = await api(`/api/akshare-bars?${params.toString()}`);
    if (requestId !== state.klineRequestId) return;
    state.klineBars = payload.bars || [];
    state.klinePayload = payload;
    state.klineHoverIndex = null;
    state.klineViewEnd = state.klineBars.length;
    renderKlineChart();
  } catch (error) {
    if (requestId !== state.klineRequestId) return;
    state.klineBars = [];
    state.klinePayload = null;
    state.klineHoverIndex = null;
    state.klineViewEnd = null;
    $("#kline-note").textContent = error.message;
    drawEmptyKline($("#kline-chart").getContext("2d"), $("#kline-chart"), "K线数据暂不可用");
  }
}

function renderKlineChart(payload = state.klinePayload) {
  const canvas = $("#kline-chart");
  const ctx = canvas.getContext("2d");
  const bars = (payload?.bars || []).filter((bar) => (
    Number.isFinite(Number(bar.open))
    && Number.isFinite(Number(bar.high))
    && Number.isFinite(Number(bar.low))
    && Number.isFinite(Number(bar.close))
  ));
  clearCanvas(ctx, canvas);

  const symbol = payload?.symbol || state.activeWatchSymbol || "--";
  const countText = payload?.totalCount && payload.totalCount !== bars.length
    ? `${bars.length}/${payload.totalCount}`
    : `${bars.length}`;
  $("#kline-note").textContent = bars.length ? `${symbol} ${countText} 根K线` : `${symbol} 暂无K线`;
  if (!bars.length) {
    renderKlineLegend([]);
    renderKlineInfo([]);
    drawEmptyKline(ctx, canvas, "当前参数没有返回K线");
    return;
  }

  const windowRange = klineVisibleRange(bars.length);
  const visible = bars.slice(windowRange.start, windowRange.end);
  const startIndex = windowRange.start;
  $("#kline-note").textContent = `${symbol} ${windowRange.start + 1}-${windowRange.end}/${bars.length} 根K线`;
  const maConfigs = getMaConfigs();
  const maSeries = Object.fromEntries(
    maConfigs.map((config) => [config.id, movingAverage(bars, config.window)])
  );
  const lows = visible.map((bar) => Number(bar.low));
  const highs = visible.map((bar) => Number(bar.high));
  const maValues = maConfigs
    .filter((config) => config.enabled)
    .flatMap((config) => maSeries[config.id].slice(startIndex))
    .filter(Number.isFinite);
  const volumes = visible.map((bar) => Number(bar.volume) || 0);
  const minPrice = Math.min(...lows, ...maValues);
  const maxPrice = Math.max(...highs, ...maValues);
  const priceSpan = maxPrice - minPrice || 1;
  const maxVolume = Math.max(...volumes, 1);
  const leftPad = 52;
  const rightPad = 18;
  const topPad = 18;
  const priceHeight = 222;
  const volumeTop = 258;
  const volumeHeight = 42;
  const plotWidth = canvas.width - leftPad - rightPad;
  const step = plotWidth / visible.length;
  const bodyWidth = clamp(step * 0.56, 3, 12);

  ctx.strokeStyle = "#d8e1e8";
  ctx.lineWidth = 1;
  for (let index = 0; index < 5; index += 1) {
    const y = topPad + (priceHeight / 4) * index;
    ctx.beginPath();
    ctx.moveTo(leftPad, y);
    ctx.lineTo(canvas.width - rightPad, y);
    ctx.stroke();
  }

  visible.forEach((bar, index) => {
    const open = Number(bar.open);
    const high = Number(bar.high);
    const low = Number(bar.low);
    const close = Number(bar.close);
    const volume = Number(bar.volume) || 0;
    const up = close >= open;
    const color = up ? "#b42318" : "#067647";
    const x = leftPad + step * index + step / 2;
    const highY = priceToY(high, minPrice, priceSpan, topPad, priceHeight);
    const lowY = priceToY(low, minPrice, priceSpan, topPad, priceHeight);
    const openY = priceToY(open, minPrice, priceSpan, topPad, priceHeight);
    const closeY = priceToY(close, minPrice, priceSpan, topPad, priceHeight);
    const bodyTop = Math.min(openY, closeY);
    const bodyHeight = Math.max(Math.abs(closeY - openY), 2);
    const volumeBarHeight = Math.max((volume / maxVolume) * volumeHeight, volume ? 1 : 0);

    ctx.strokeStyle = color;
    ctx.fillStyle = color;
    ctx.beginPath();
    ctx.moveTo(x, highY);
    ctx.lineTo(x, lowY);
    ctx.stroke();
    ctx.fillRect(x - bodyWidth / 2, bodyTop, bodyWidth, bodyHeight);
    ctx.globalAlpha = 0.24;
    ctx.fillRect(x - bodyWidth / 2, volumeTop + volumeHeight - volumeBarHeight, bodyWidth, volumeBarHeight);
    ctx.globalAlpha = 1;
  });

  for (const config of maConfigs) {
    if (config.enabled) {
      drawMovingAverageLine(
        ctx,
        maSeries[config.id].slice(startIndex),
        config.color,
        minPrice,
        priceSpan,
        leftPad,
        step,
        topPad,
        priceHeight,
      );
    }
  }

  const hoverIndex = Number.isInteger(state.klineHoverIndex)
    ? clamp(state.klineHoverIndex, 0, visible.length - 1)
    : null;
  if (hoverIndex !== null) {
    drawKlineCrosshair(
      ctx,
      visible,
      hoverIndex,
      leftPad,
      rightPad,
      step,
      minPrice,
      priceSpan,
      topPad,
      priceHeight,
      volumeTop,
      volumeHeight,
      canvas,
    );
  }

  ctx.fillStyle = "#667085";
  ctx.font = "12px Arial, sans-serif";
  ctx.fillText(maxPrice.toFixed(2), 10, topPad + 4);
  ctx.fillText(minPrice.toFixed(2), 10, topPad + priceHeight);
  ctx.fillText("成交量", 10, volumeTop + volumeHeight - 2);

  const firstDate = shortDateTime(visible[0]?.timestamp).slice(0, 10);
  const lastDate = shortDateTime(visible[visible.length - 1]?.timestamp).slice(0, 10);
  ctx.fillText(firstDate, leftPad, canvas.height - 8);
  ctx.textAlign = "right";
  ctx.fillText(lastDate, canvas.width - rightPad, canvas.height - 8);
  ctx.textAlign = "left";

  const infoIndex = hoverIndex ?? visible.length - 1;
  renderKlineLegend(maConfigs, maSeries, startIndex + infoIndex);
  renderKlineInfo(klineInfoRows(symbol, visible[infoIndex], bars[startIndex + infoIndex - 1], maConfigs, maSeries, startIndex + infoIndex));
  updateKlineNavState(bars.length, windowRange.start, windowRange.end);
  state.klineGeometry = {
    leftPad,
    rightPad,
    step,
    visibleCount: visible.length,
  };
}

function klineVisibleRange(total) {
  const visibleCount = Math.min(Math.max(Number(state.klineVisibleCount) || 140, 20), Math.max(total, 1));
  const minEnd = Math.min(visibleCount, total);
  const end = Math.round(clamp(state.klineViewEnd || total, minEnd, total));
  const start = Math.max(0, end - visibleCount);
  state.klineViewEnd = end;
  return { start, end };
}

function updateKlineNavState(total, start, end) {
  const hasBars = total > 0;
  $("#kline-pan-left-button").disabled = !hasBars || start <= 0;
  $("#kline-pan-right-button").disabled = !hasBars || end >= total;
  $("#kline-latest-button").disabled = !hasBars || end >= total;
  $("#kline-zoom-in-button").disabled = !hasBars || state.klineVisibleCount <= klineZoomSteps[0];
  $("#kline-zoom-out-button").disabled = !hasBars || state.klineVisibleCount >= Math.min(klineZoomSteps[klineZoomSteps.length - 1], total);
}

function drawEmptyKline(ctx, canvas, message) {
  clearCanvas(ctx, canvas);
  renderKlineLegend([]);
  renderKlineInfo([]);
  updateKlineNavState(0, 0, 0);
  state.klineGeometry = null;
  ctx.fillStyle = "#667085";
  ctx.font = "14px Arial, sans-serif";
  ctx.fillText(message, 28, 42);
}

function drawMovingAverageLine(ctx, series, color, minPrice, priceSpan, leftPad, step, topPad, priceHeight) {
  ctx.strokeStyle = color;
  ctx.lineWidth = 1.6;
  ctx.beginPath();
  let started = false;
  series.forEach((value, index) => {
    if (!Number.isFinite(value)) return;
    const x = leftPad + step * index + step / 2;
    const y = priceToY(value, minPrice, priceSpan, topPad, priceHeight);
    if (!started) {
      ctx.moveTo(x, y);
      started = true;
    } else {
      ctx.lineTo(x, y);
    }
  });
  if (started) {
    ctx.stroke();
  }
}

function drawKlineCrosshair(ctx, bars, index, leftPad, rightPad, step, minPrice, priceSpan, topPad, priceHeight, volumeTop, volumeHeight, canvas) {
  const bar = bars[index];
  if (!bar) return;
  const x = leftPad + step * index + step / 2;
  const y = priceToY(Number(bar.close), minPrice, priceSpan, topPad, priceHeight);
  ctx.save();
  ctx.strokeStyle = "#344455";
  ctx.lineWidth = 1;
  ctx.setLineDash([4, 4]);
  ctx.beginPath();
  ctx.moveTo(x, topPad);
  ctx.lineTo(x, volumeTop + volumeHeight);
  ctx.moveTo(leftPad, y);
  ctx.lineTo(canvas.width - rightPad, y);
  ctx.stroke();
  ctx.restore();
}

function renderKlineLegend(configs, series = {}, index = 0) {
  const legend = $("#kline-legend");
  legend.innerHTML = configs.filter((config) => config.enabled).map((config) => {
    const value = series[config.id]?.[index];
    return `<span class="legend-item" style="color:${config.color}"><span class="legend-swatch"></span>${config.label}${config.window} ${formatNumber(value)}</span>`;
  }).join("");
}

function renderKlineInfo(rows) {
  const strip = $("#kline-info-strip");
  if (!rows.length) {
    strip.innerHTML = `<div class="kline-info-cell"><span>状态</span><strong>等待K线</strong></div>`;
    return;
  }
  strip.innerHTML = rows.map(([label, value]) => (
    `<div class="kline-info-cell"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></div>`
  )).join("");
}

function klineInfoRows(symbol, bar, previousBar, maConfigs, maSeries, index) {
  const close = Number(bar.close);
  const previousClose = Number(previousBar?.close);
  const change = Number.isFinite(previousClose) && previousClose !== 0
    ? (close - previousClose) / previousClose
    : null;
  const enabledMas = maConfigs
    .filter((config) => config.enabled)
    .slice(0, 2)
    .map((config) => [`${config.label}${config.window}`, formatNumber(maSeries[config.id]?.[index])]);
  return [
    ["合约", symbol],
    ["时间", shortDateTime(bar.timestamp)],
    ["开", formatNumber(bar.open)],
    ["高", formatNumber(bar.high)],
    ["低", formatNumber(bar.low)],
    ["收", formatNumber(bar.close)],
    ["涨跌", change === null ? "--" : `${(change * 100).toFixed(2)}%`],
    ["量", formatNumber(bar.volume, 0)],
    ...enabledMas,
  ].slice(0, 10);
}

function getMaConfigs() {
  return maSpecs.map((spec) => ({
    ...spec,
    enabled: $(`#ma-${spec.id}-enabled`).checked,
    window: Math.round(clamp(numberInput(`#ma-${spec.id}-input`, spec.defaultWindow), 1, 240)),
  }));
}

function movingAverage(bars, windowSize) {
  const values = [];
  let sum = 0;
  bars.forEach((bar, index) => {
    const close = Number(bar.close);
    sum += close;
    if (index >= windowSize) {
      sum -= Number(bars[index - windowSize].close);
    }
    values.push(index + 1 >= windowSize ? sum / windowSize : null);
  });
  return values;
}

function clearCanvas(ctx, canvas) {
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  ctx.fillStyle = "#f8fafc";
  ctx.fillRect(0, 0, canvas.width, canvas.height);
}

function priceToY(price, minPrice, priceSpan, topPad, priceHeight) {
  return topPad + priceHeight - ((price - minPrice) / priceSpan) * priceHeight;
}

function clamp(value, min, max) {
  return Math.min(Math.max(value, min), max);
}

function normalizeSymbol(value) {
  return String(value || "").trim().replace(/\s+/g, "").toUpperCase();
}

function handleKlineMouseMove(event) {
  const geometry = state.klineGeometry;
  if (!geometry || !state.klinePayload?.bars?.length) return;
  const canvas = $("#kline-chart");
  const rect = canvas.getBoundingClientRect();
  const scaleX = canvas.width / Math.max(rect.width, 1);
  const x = (event.clientX - rect.left) * scaleX;
  const minX = geometry.leftPad;
  const maxX = canvas.width - geometry.rightPad;
  if (x < minX || x > maxX) {
    clearKlineHover();
    return;
  }
  const index = Math.round((x - geometry.leftPad - geometry.step / 2) / geometry.step);
  const nextIndex = Math.round(clamp(index, 0, geometry.visibleCount - 1));
  if (state.klineHoverIndex !== nextIndex) {
    state.klineHoverIndex = nextIndex;
    renderKlineChart();
  }
}

function clearKlineHover() {
  if (state.klineHoverIndex !== null) {
    state.klineHoverIndex = null;
    renderKlineChart();
  }
}

function refreshKlineIndicators() {
  if (state.klinePayload?.bars?.length) {
    renderKlineChart();
  }
}

function scrollToWorkspacePanel(targetId) {
  const target = document.getElementById(targetId);
  if (!target) return;
  target.scrollIntoView({ behavior: "smooth", block: "start" });
  for (const item of document.querySelectorAll("[data-scroll-target]")) {
    item.classList.toggle("active", item.dataset.scrollTarget === targetId);
  }
}

function panKline(direction) {
  const total = state.klinePayload?.bars?.length || 0;
  if (!total) return;
  const range = klineVisibleRange(total);
  const step = Math.max(Math.round(state.klineVisibleCount * 0.7), 1);
  state.klineViewEnd = direction < 0
    ? Math.max(Math.min(state.klineVisibleCount, total), range.end - step)
    : Math.min(total, range.end + step);
  state.klineHoverIndex = null;
  renderKlineChart();
}

function zoomKline(direction) {
  const total = state.klinePayload?.bars?.length || 0;
  if (!total) return;
  const current = state.klineVisibleCount;
  const sortedSteps = [...klineZoomSteps].sort((left, right) => left - right);
  const next = direction < 0
    ? [...sortedSteps].reverse().find((step) => step < current)
    : sortedSteps.find((step) => step > current);
  if (!next) return;
  state.klineVisibleCount = next;
  state.klineHoverIndex = null;
  renderKlineChart();
}

function jumpKlineLatest() {
  const total = state.klinePayload?.bars?.length || 0;
  if (!total) return;
  state.klineViewEnd = total;
  state.klineHoverIndex = null;
  renderKlineChart();
}

function handleKlineWheel(event) {
  if (!state.klinePayload?.bars?.length) return;
  event.preventDefault();
  if (event.shiftKey) {
    panKline(event.deltaY > 0 ? 1 : -1);
  } else {
    zoomKline(event.deltaY > 0 ? 1 : -1);
  }
}

async function loadReport(outputDir) {
  setStatus("加载中", "busy");
  const payload = await api(`/api/report?output_dir=${encodeURIComponent(outputDir)}`);
  state.currentReport = payload;
  renderReport(payload);
  setStatus("已加载");
}

async function refreshCtpMonitor(options = {}) {
  if (state.ctpRefreshInFlight) return;
  const button = $("#refresh-monitor-button");
  state.ctpRefreshInFlight = true;
  button.disabled = true;
  if (!options.silent) {
    $("#ctp-monitor-note").textContent = "正在刷新";
  }
  try {
    const params = new URLSearchParams({
      state_path: $("#ctp-state-input").value || "output/ctp_realtime_state.json",
      event_log_path: $("#ctp-events-input").value || "output/ctp_events.jsonl",
      limit: "80",
      stale_seconds: $("#ctp-stale-input").value || "120",
    });
    const payload = await api(`/api/ctp-monitor?${params.toString()}`);
    state.ctpMonitor = payload;
    renderCtpMonitor(payload);
  } catch (error) {
    $("#ctp-monitor-note").textContent = error.message;
    renderCtpMonitor(null);
  } finally {
    state.ctpRefreshInFlight = false;
    button.disabled = false;
  }
}

function renderReport(report) {
  $("#run-title").textContent = report.outputDir || "运行";
  $("#active-mode").textContent = modeLabel(report.mode || state.currentMode);
  $("#open-report-button").disabled = !report.reportUrl;
  renderMetrics(report.metrics || {});
  renderChart(report.equity || []);
  renderTable();
}

function renderCtpMonitor(monitor) {
  const summary = monitor?.summary || {};
  const symbols = summary.symbols || [];
  const stateLabel = monitor?.stateExists ? "已加载" : "缺失";
  const eventLabel = monitor?.eventLogExists ? `${summary.eventCount || 0}` : "缺失";
  const healthStatus = monitor ? (summary.healthStatus || "UNKNOWN") : "ERROR";
  const note = monitor
    ? `${monitor.statePath} / ${monitor.eventLogPath}`
    : "监控不可用";
  $("#ctp-monitor-note").textContent = note;
  renderHealthPill(healthStatus);
  renderAlerts(monitor ? (summary.alerts || []) : [{
    level: "ERROR",
    title: "监控不可用",
    message: "监控 API 请求失败。",
  }]);

  const metrics = [
    ["健康", healthLabel(healthStatus), healthClass(healthStatus)],
    ["状态", stateLabel, monitor?.stateExists ? "ok" : "error"],
    ["订单", summary.orderCount ?? 0, ""],
    ["挂单", summary.workingOrderCount ?? 0, (summary.workingOrderCount || 0) > 0 ? "warn" : "ok"],
    ["成交", summary.tradeCount ?? 0, ""],
    ["事件", eventLabel, monitor?.eventLogExists ? "" : "warn"],
    ["合约", symbols.length ? symbols.join(", ") : "--", ""],
    ["策略", summary.strategyName || "--", ""],
  ];
  const grid = $("#ctp-monitor-summary");
  grid.innerHTML = metrics.map(([label, value, kind]) => (
    `<div class="monitor-cell ${kind}"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></div>`
  )).join("");

  renderConnectionStrip(summary);
  renderMiniTable(
    "#ctp-orders-table",
    (monitor?.orders || []).filter((order) => order.status === "PENDING").slice(-8).reverse(),
    ctpOrderColumns,
    "暂无挂单",
  );
  renderMiniTable("#ctp-trades-table", (monitor?.trades || []).slice(-8).reverse(), ctpTradeColumns, "暂无成交");
  renderMiniTable("#ctp-events-table", (monitor?.events || []).slice(-12).reverse(), ctpEventColumns, "暂无事件日志");
}

function renderHealthPill(status) {
  const pill = $("#ctp-health-pill");
  pill.textContent = healthLabel(status);
  pill.className = `health-pill ${healthClass(status)}`;
}

function renderAlerts(alerts) {
  const list = $("#ctp-alert-list");
  if (!alerts.length) {
    list.innerHTML = `<div class="alert-item ok"><i data-lucide="check-circle-2"></i><div><strong>正常</strong><span>当前没有监控告警</span></div></div>`;
    renderLucideIcons();
    return;
  }
  list.innerHTML = alerts.slice(0, 6).map((alert) => {
    const level = String(alert.level || "WARN").toLowerCase();
    const icon = level === "error" ? "circle-alert" : "triangle-alert";
    const [title, message] = localizedAlert(alert);
    return `<div class="alert-item ${escapeHtml(level)}"><i data-lucide="${icon}"></i><div><strong>${escapeHtml(title)}</strong><span>${escapeHtml(message)}</span></div></div>`;
  }).join("");
  renderLucideIcons();
}

function renderConnectionStrip(summary) {
  const trading = summary.trading || {};
  const marketData = summary.marketData || {};
  const lastReconcile = summary.lastReconcile || {};
  const items = [
    ["交易", trading.healthy, connectionText(trading)],
    ["行情", marketData.healthy, connectionText(marketData)],
    ["订阅", (marketData.subscribed_symbols || []).length > 0, (marketData.subscribed_symbols || []).join(", ") || "--"],
    ["最新 Tick", Boolean(summary.lastTickAt), shortDateTime(summary.lastTickAt)],
    ["对账", Boolean(lastReconcile.event_type), lastReconcile.event_type || "--"],
  ];
  $("#ctp-connection-strip").innerHTML = items.map(([label, ok, text]) => (
    `<div class="connection-item"><span class="${statusDotClass(ok)}"></span><div><strong>${escapeHtml(label)}</strong><small>${escapeHtml(text)}</small></div></div>`
  )).join("");
}

function renderMiniTable(selector, rows, columns, emptyLabel) {
  const table = $(selector);
  if (!rows.length) {
    table.innerHTML = `<tbody><tr><td><div class="empty-state compact">${escapeHtml(emptyLabel)}</div></td></tr></tbody>`;
    return;
  }
  const visibleColumns = columns.filter((column) => column in rows[0]);
  const head = `<thead><tr>${visibleColumns.map((column) => `<th>${escapeHtml(columnLabel(column))}</th>`).join("")}</tr></thead>`;
  const body = rows.map((row) => {
    const cells = visibleColumns.map((column) => `<td>${escapeHtml(formatCell(row[column]))}</td>`).join("");
    return `<tr>${cells}</tr>`;
  }).join("");
  table.innerHTML = `${head}<tbody>${body}</tbody>`;
}

function renderMetrics(metrics) {
  const band = $("#metrics-band");
  band.innerHTML = "";
  for (const [key, label, type] of metricSpecs) {
    const cell = document.createElement("div");
    cell.className = "metric-cell";
    cell.innerHTML = `<span>${label}</span><strong>${formatMetric(metrics[key], type)}</strong>`;
    band.appendChild(cell);
  }
}

function renderChart(rows) {
  const canvas = $("#equity-chart");
  const ctx = canvas.getContext("2d");
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  ctx.fillStyle = "#f8fafc";
  ctx.fillRect(0, 0, canvas.width, canvas.height);

  const values = rows.map((row) => Number(row.equity)).filter(Number.isFinite);
  $("#chart-note").textContent = values.length ? `${values.length} 个点` : "暂无权益数据";
  if (!values.length) {
    drawEmptyChart(ctx, canvas);
    return;
  }

  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min || 1;
  const pad = 28;
  const width = canvas.width - pad * 2;
  const height = canvas.height - pad * 2;

  ctx.strokeStyle = "#cfd7df";
  ctx.lineWidth = 1;
  for (let i = 0; i < 4; i += 1) {
    const y = pad + (height / 3) * i;
    ctx.beginPath();
    ctx.moveTo(pad, y);
    ctx.lineTo(canvas.width - pad, y);
    ctx.stroke();
  }

  ctx.strokeStyle = "#1463a5";
  ctx.lineWidth = 3;
  ctx.beginPath();
  values.forEach((value, index) => {
    const x = pad + (width * index) / Math.max(values.length - 1, 1);
    const y = canvas.height - pad - ((value - min) / span) * height;
    if (index === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  });
  ctx.stroke();

  ctx.fillStyle = "#667085";
  ctx.font = "12px Arial";
  ctx.fillText(max.toFixed(2), pad, 18);
  ctx.fillText(min.toFixed(2), pad, canvas.height - 8);
}

function drawEmptyChart(ctx, canvas) {
  ctx.fillStyle = "#667085";
  ctx.font = "14px Arial, sans-serif";
  ctx.fillText("运行回测、回放、模拟交易或参数优化后显示曲线", 28, 42);
}

function renderTable() {
  const report = state.currentReport || {};
  const rows = report[state.currentTable] || [];
  const table = $("#result-table");
  if (!rows.length) {
    table.innerHTML = `<tbody><tr><td><div class="empty-state">暂无${tableLabel(state.currentTable)}数据</div></td></tr></tbody>`;
    return;
  }

  const columns = tableColumns[state.currentTable].filter((column) => column in rows[0]);
  const fallbackColumns = Object.keys(rows[0]).slice(0, 8);
  const visibleColumns = columns.length ? columns : fallbackColumns;
  const head = `<thead><tr>${visibleColumns.map((column) => `<th>${escapeHtml(columnLabel(column))}</th>`).join("")}</tr></thead>`;
  const body = rows.map((row) => {
    const cells = visibleColumns.map((column) => `<td>${escapeHtml(row[column] ?? "")}</td>`).join("");
    return `<tr>${cells}</tr>`;
  }).join("");
  table.innerHTML = `${head}<tbody>${body}</tbody>`;
}

function setMode(mode) {
  state.currentMode = mode;
  for (const button of document.querySelectorAll(".segment")) {
    button.classList.toggle("active", button.dataset.mode === mode);
  }
  $("#active-mode").textContent = modeLabel(mode);
  const canLimitSteps = mode === "replay" || mode === "paper";
  $("#max-steps-input").disabled = !canLimitSteps;
  if (!canLimitSteps) {
    $("#max-steps-input").value = "";
  }
}

function setStatus(text, kind = "") {
  const pill = $("#run-status");
  pill.textContent = text;
  pill.classList.toggle("busy", kind === "busy");
  pill.classList.toggle("error", kind === "error");
}

function connectionText(payload) {
  if (!payload || !Object.keys(payload).length) return "--";
  const flags = [];
  if (payload.state) flags.push(payload.state);
  flags.push(payload.connected ? "已连接" : "离线");
  if (payload.logged_in !== undefined) flags.push(payload.logged_in ? "已登录" : "未登录");
  if (payload.last_disconnect_reason) flags.push(`原因 ${payload.last_disconnect_reason}`);
  return flags.join(" / ");
}

function statusDotClass(value) {
  if (value === true) return "status-dot ok";
  if (value === false) return "status-dot error";
  return "status-dot warn";
}

function healthClass(status) {
  const normalized = String(status || "").toLowerCase();
  if (normalized === "ok") return "ok";
  if (normalized === "error") return "error";
  if (normalized === "warn") return "warn";
  return "unknown";
}

function scheduleCtpAutoRefresh() {
  if (state.ctpRefreshTimer) {
    clearTimeout(state.ctpRefreshTimer);
    state.ctpRefreshTimer = null;
  }
  if (!$("#ctp-auto-refresh-input").checked) return;
  const seconds = Math.max(Number($("#ctp-refresh-interval-input").value || 5), 2);
  state.ctpRefreshTimer = setTimeout(async () => {
    await refreshCtpMonitor({ silent: true });
    scheduleCtpAutoRefresh();
  }, seconds * 1000);
}

function restartCtpAutoRefresh() {
  scheduleCtpAutoRefresh();
}

function shortDateTime(value) {
  if (!value) return "--";
  const text = String(value);
  return text.includes("T") ? text.replace("T", " ").slice(0, 19) : text;
}

function modeLabel(mode) {
  return modeLabels[mode] || mode || "--";
}

function tableLabel(table) {
  return {
    orders: "订单",
    trades: "成交",
    events: "事件",
    optimization: "优化",
  }[table] || table;
}

function columnLabel(column) {
  return columnLabels[column] || column;
}

function healthLabel(status) {
  return healthLabels[String(status || "UNKNOWN").toUpperCase()] || status || "未知";
}

function localizedAlert(alert) {
  const known = alertText[alert.code];
  if (known) return known;
  return [
    alert.title || alert.code || "告警",
    alert.message || "请检查最新事件和运行状态。",
  ];
}

function compactDate(value) {
  return value ? String(value).replaceAll("-", "") : "";
}

function textInput(selector, fallback = "") {
  const value = $(selector).value.trim();
  return value || fallback;
}

function numberInput(selector, fallback) {
  const raw = $(selector).value;
  if (raw === "" || raw === null || raw === undefined) return fallback;
  const value = Number(raw);
  return Number.isFinite(value) ? value : fallback;
}

function formatCell(value) {
  if (value === null || value === undefined || value === "") return "--";
  if (typeof value === "number") return Number.isInteger(value) ? String(value) : value.toFixed(4).replace(/0+$/, "").replace(/\.$/, "");
  if (typeof value === "object") return JSON.stringify(value);
  return valueLabels[value] || shortDateTime(value);
}

function formatMetric(value, type) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "--";
  if (type === "percent") return `${(number * 100).toFixed(2)}%`;
  if (type === "integer") return String(Math.round(number));
  if (type === "fixed") return number.toFixed(2);
  return number.toLocaleString(undefined, { maximumFractionDigits: 2 });
}

function formatNumber(value, digits = 2) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "--";
  return number.toLocaleString(undefined, {
    maximumFractionDigits: digits,
    minimumFractionDigits: digits > 0 ? Math.min(digits, 2) : 0,
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

function wireEvents() {
  $("#run-button").addEventListener("click", startRun);
  $("#run-akshare-button").addEventListener("click", startAkshareRun);
  for (const item of document.querySelectorAll("[data-scroll-target]")) {
    item.addEventListener("click", () => scrollToWorkspacePanel(item.dataset.scrollTarget));
  }
  $("#add-watch-symbol-button").addEventListener("click", addWatchSymbol);
  $("#watch-symbol-input").addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      addWatchSymbol();
    }
  });
  $("#akshare-symbol-input").addEventListener("change", (event) => {
    const symbol = normalizeSymbol(event.target.value);
    if (symbol) {
      ensureWatchSymbol(symbol);
      selectWatchSymbol(symbol);
    }
  });
  for (const selector of [
    "#akshare-api-select",
    "#akshare-market-input",
    "#akshare-variety-input",
    "#akshare-period-select",
    "#akshare-start-input",
    "#akshare-end-input",
  ]) {
    $(selector).addEventListener("change", () => {
      if (state.activeWatchSymbol) {
        fetchKline(state.activeWatchSymbol);
      }
    });
  }
  for (const spec of maSpecs) {
    $(`#ma-${spec.id}-enabled`).addEventListener("change", refreshKlineIndicators);
    $(`#ma-${spec.id}-input`).addEventListener("input", refreshKlineIndicators);
  }
  $("#kline-pan-left-button").addEventListener("click", () => panKline(-1));
  $("#kline-pan-right-button").addEventListener("click", () => panKline(1));
  $("#kline-zoom-in-button").addEventListener("click", () => zoomKline(-1));
  $("#kline-zoom-out-button").addEventListener("click", () => zoomKline(1));
  $("#kline-latest-button").addEventListener("click", jumpKlineLatest);
  $("#kline-chart").addEventListener("mousemove", handleKlineMouseMove);
  $("#kline-chart").addEventListener("mouseleave", clearKlineHover);
  $("#kline-chart").addEventListener("wheel", handleKlineWheel, { passive: false });
  $("#refresh-monitor-button").addEventListener("click", () => refreshCtpMonitor());
  $("#ctp-auto-refresh-input").addEventListener("change", restartCtpAutoRefresh);
  $("#ctp-refresh-interval-input").addEventListener("change", restartCtpAutoRefresh);
  $("#ctp-state-input").addEventListener("change", () => refreshCtpMonitor());
  $("#ctp-events-input").addEventListener("change", () => refreshCtpMonitor());
  $("#ctp-stale-input").addEventListener("change", () => refreshCtpMonitor());
  $("#refresh-button").addEventListener("click", async () => {
    await loadInitialState();
  });
  $("#open-report-button").addEventListener("click", () => {
    if (state.currentReport?.reportUrl) {
      window.open(state.currentReport.reportUrl, "_blank", "noopener");
    }
  });
  $("#config-select").addEventListener("change", (event) => {
    const option = event.target.selectedOptions[0];
    setMode(option?.dataset.modeHint || "backtest");
  });
  for (const button of document.querySelectorAll(".segment")) {
    button.addEventListener("click", () => setMode(button.dataset.mode));
  }
  for (const button of document.querySelectorAll(".tab")) {
    button.addEventListener("click", () => {
      state.currentTable = button.dataset.table;
      for (const tab of document.querySelectorAll(".tab")) {
        tab.classList.toggle("active", tab === button);
      }
      renderTable();
    });
  }
}

function renderLucideIcons() {
  if (window.lucide) {
    window.lucide.createIcons();
  }
}

wireEvents();
renderMetrics({});
initializeWatchlist();
drawEmptyChart($("#equity-chart").getContext("2d"), $("#equity-chart"));
scheduleCtpAutoRefresh();
loadInitialState().catch((error) => {
  setStatus("错误", "error");
  $("#connection-state").textContent = error.message;
});

window.addEventListener("load", () => {
  renderLucideIcons();
});
