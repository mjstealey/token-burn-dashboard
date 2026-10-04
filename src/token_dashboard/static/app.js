/* Token Burn dashboard — fetches JSON metrics and renders a Tufte-style
   calendar heat map plus supporting breakdowns. No build step. */

const state = { metric: "cost", days: 365, filter: null }; // filter: {type:"model"|"project", value}
const heatmapCharts = {}; // panel key ("combined"|"claude"|"openai"...) -> ECharts instance
const heatmapDayIndex = {}; // panel key -> { "2026-06-18": dataIndex } for cross-panel hover
let lineChart = null;
let punchChart = null;
let lastHeatmap = null; // last /api/heatmap payload, kept so toggles re-render without refetch
let lastHeatmapSig = null; // render signature; unchanged 60s polls skip the re-render
let seriesEnabled = loadEnabledPanels(); // { key: bool } or null until first load fills it

// Distinct base hue per series so each provider — and the combined total — reads as
// its own color. Ramps and chip colors are derived from these and adapt to the theme.
const SERIES_HUE = { combined: 222, claude: 16, openai: 168, local: 38 }; // indigo / coral / teal / amber
function hueFor(key) { return SERIES_HUE[key] ?? 205; }

function currentTheme() {
  return document.documentElement.getAttribute("data-theme") === "dark" ? "dark" : "light";
}
function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

// 7-stop sequential ramp for a series hue. Light mode runs pale -> deep; dark mode
// runs dark -> bright so cells stay legible on either background.
function rampFor(key) {
  const h = hueFor(key);
  const dark = currentTheme() === "dark";
  const N = 7;
  const stops = [];
  for (let i = 0; i < N; i++) {
    const t = i / (N - 1);
    const L = dark ? 20 + t * 50 : 94 - t * 58;
    const S = dark ? 38 + t * 42 : 32 + t * 43;
    stops.push(`hsl(${h} ${S}% ${L}%)`);
  }
  return stops;
}
// Solid representative color for the toggle chip dot.
function seriesColor(key) {
  return `hsl(${hueFor(key)} 60% ${currentTheme() === "dark" ? 58 : 40}%)`;
}

// Light/dark theme. The initial attribute is set by an inline script in <head>
// (so there's no flash); this syncs the button and persists changes.
function applyTheme(theme) {
  document.documentElement.setAttribute("data-theme", theme);
  try { localStorage.setItem("td.theme", theme); } catch {}
  const btn = document.getElementById("theme-toggle");
  if (btn) {
    btn.textContent = theme === "dark" ? "☀" : "☾";
    btn.setAttribute("aria-label", theme === "dark" ? "Switch to light mode" : "Switch to dark mode");
    btn.title = btn.getAttribute("aria-label");
  }
}
function toggleTheme() {
  applyTheme(currentTheme() === "dark" ? "light" : "dark");
  // Colors (chart ramps, provider pills, cache bars) are baked at render time, so
  // re-render everything to follow the new theme.
  loadAll();
}
function seriesLabel(key) {
  return key === "combined" ? "Combined"
    : key === "openai" ? "Codex"
    : key === "claude" ? "Claude"
    : key;
}
function loadEnabledPanels() {
  try { return JSON.parse(localStorage.getItem("td.heatmaps") || "null"); } catch { return null; }
}
function saveEnabledPanels() {
  try { localStorage.setItem("td.heatmaps", JSON.stringify(seriesEnabled || {})); } catch {}
}

/* ---------- formatting ---------- */
// Model names, project paths, and session ids come from local log files (a cwd can
// contain quotes or angle brackets) — escape everything log-derived before innerHTML.
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}
function fmtMoney(v) {
  v = v || 0;
  if (v >= 1000) return "$" + v.toLocaleString(undefined, { maximumFractionDigits: 0 });
  if (v >= 1) return "$" + v.toFixed(2);
  if (v >= 0.001 || v === 0) return "$" + v.toFixed(3);
  return "$" + v.toPrecision(2); // sub-tenth-of-a-cent bucket edges stay distinguishable
}
function fmtTokens(v) {
  v = v || 0;
  if (v >= 1e9) return (v / 1e9).toFixed(2) + "B";
  if (v >= 1e6) return (v / 1e6).toFixed(2) + "M";
  if (v >= 1e3) return (v / 1e3).toFixed(1) + "k";
  return String(Math.round(v));
}
const metricVal = (d) => (state.metric === "cost" ? d.cost : d.tokens) || 0;
const fmtMetric = (v) => (state.metric === "cost" ? fmtMoney(v) : fmtTokens(v));
function pct(n) { return (100 * (n || 0)).toFixed(0) + "%"; }
function localTime(s) {
  if (!s) return "—";
  try { return new Date(s).toLocaleString(); } catch { return s; }
}
function localDate(s) {
  if (!s) return "—";
  try { return new Date(s).toLocaleDateString(); } catch { return s; }
}
function weekday(day) {
  try { return new Date(day + "T00:00:00").toLocaleDateString(undefined, { weekday: "short" }); }
  catch { return ""; }
}
function dateAdd(iso, delta) {
  const d = new Date(iso + "T00:00:00Z");
  d.setUTCDate(d.getUTCDate() + delta);
  return d.toISOString().slice(0, 10);
}
function rangeLabel() {
  return { 30: "last 30 days", 90: "last 90 days", 180: "last 180 days", 365: "last 365 days" }[state.days]
    || `last ${state.days} days`;
}

async function getJSON(url, context) {
  const r = await fetch(url, { signal: context.signal });
  if (!r.ok) throw new Error(url + " -> " + r.status);
  const data = await r.json();
  if (context.signal.aborted) throw new DOMException("Superseded refresh", "AbortError");
  return data;
}

/* ---------- KPIs / freshness ---------- */
// Period-over-period delta against the previous window of the same length.
function deltaLine(cur, prev) {
  if (!prev || !(prev.cost > 0)) return "";
  const d = ((cur.cost || 0) - prev.cost) / prev.cost;
  const sign = d >= 0 ? "+" : "−";
  return `<div class="delta">${sign}${Math.abs(d * 100).toFixed(0)}% vs prior ${fmtMoney(prev.cost)}</div>`;
}

async function loadSummary(context) {
  const s = await getJSON("/api/summary", context);
  const cards = [
    ["Today", s.today, null], ["7 days", s.last_7d, s.prev_7d],
    ["30 days", s.last_30d, s.prev_30d], ["All time", s.all_time, null],
  ];
  document.getElementById("kpis").innerHTML = cards
    .map(([label, w, prev]) => `
      <div class="kpi">
        <div class="label">${label}</div>
        <div class="big">${fmtMoney(w.cost)}</div>
        <div class="small">${fmtTokens(w.tokens)} tok · ${(w.events || 0).toLocaleString()} req</div>
        ${deltaLine(w, prev)}
      </div>`)
    .join("");

  const last = s.meta && s.meta.last_ts ? localTime(s.meta.last_ts) : "no data";
  const ing = s.last_ingest_at ? localTime(s.last_ingest_at) : "—";
  document.getElementById("freshness").textContent =
    `Latest activity ${last} · last ingest ${ing} · timezone ${s.timezone}`;

  // Models with usage but an all-zero rate mean every $ on the page undercounts.
  const warn = document.getElementById("warnings");
  const unpriced = s.unpriced_models || [];
  const failures = Object.values(s.last_ingest || {}).reduce((n, v) => n + (v.files_failed || 0), 0);
  const messages = [];
  if (failures) messages.push(`⚠ ${failures} log file(s) failed to ingest; totals may be incomplete`);
  if (s.last_error) messages.push("⚠ latest ingest failed; showing previously ingested data");
  if (unpriced.length) {
    messages.push("⚠ no pricing.yaml rate for " +
      unpriced.map((m) => `${m.model || "(unknown)"} (${fmtTokens(m.tokens)} tok)`).join(", ") +
      " — $ figures undercount until rates are added");
  }
  warn.hidden = messages.length === 0;
  warn.textContent = messages.join(" · ");

  // Providers table.
  const provs = s.providers || [];
  document.getElementById("providers").innerHTML = provs.length
    ? table(["Provider", "Tokens", "$", "Last seen"],
        provs.map((p) => [
          providerPill(p.provider), fmtTokens(p.tokens), fmtMoney(p.cost), localTime(p.last_ts),
        ]))
    : `<p class="empty">No data yet.</p>`;

  document.getElementById("source-note").textContent =
    "Sources: ~/.claude (Claude Code), ~/.codex (Codex). Read-only.";
}

// Soft background + readable foreground for a provider, from its series hue, so the
// pill matches that provider's heat map color (and adapts to the theme).
function providerColors(p) {
  const h = hueFor(p);
  return currentTheme() === "dark"
    ? { fg: `hsl(${h} 65% 70%)`, bg: `hsl(${h} 38% 18%)` }
    : { fg: `hsl(${h} 60% 32%)`, bg: `hsl(${h} 68% 93%)` };
}
function providerPill(p) {
  const name = p === "openai" ? "Codex" : p === "claude" ? "Claude" : p === "local" ? "Local" : p;
  const c = providerColors(p);
  return `<span class="pill" style="background:${c.bg};color:${c.fg}">${esc(name)}</span>`;
}

/* ---------- heat map ---------- */
// Log-spaced buckets from the panel max. Only the end buckets carry text labels —
// the middle swatches show the ramp; exact values live in the tooltip.
function buckets(max, ramp, fmt) {
  if (!(max > 0)) return [{ lte: 0, color: ramp[0], label: "0" }];
  const th = [];
  let v = max;
  for (let i = 0; i < ramp.length - 1; i++) { th.unshift(v); v = v / 3; } // geometric (log-ish)
  const pieces = [{ lt: th[0], color: ramp[0], label: "< " + fmt(th[0]) }];
  for (let i = 0; i < th.length - 1; i++) {
    pieces.push({ gte: th[i], lt: th[i + 1], color: ramp[i + 1], label: " " });
  }
  pieces.push({ gte: th[th.length - 1], color: ramp[ramp.length - 1], label: "≥ " + fmt(th[th.length - 1]) });
  return pieces;
}

function filterQuery() {
  return state.filter ? `&${state.filter.type}=${encodeURIComponent(state.filter.value)}` : "";
}

async function loadHeatmap(context) {
  const data = await getJSON(`/api/heatmap?days=${state.days}&metric=${state.metric}${filterQuery()}`, context);
  lastHeatmap = data;
  // Skip the re-render when nothing changed (60s poll) — a full setOption redraw
  // closes any open tooltip and flickers the canvas.
  const sig = JSON.stringify([data.start_day, data.end_day, data.combined, data.providers, state.metric, state.days, state.filter, currentTheme()]);
  if (sig === lastHeatmapSig && Object.keys(heatmapCharts).length) return;
  lastHeatmapSig = sig;
  document.getElementById("heatmap-note").textContent =
    `color = daily ${state.metric === "cost" ? "$" : "tokens"} · totals & scale = ${rangeLabel()} per panel · hover for detail`;
  renderFilterChip();
  renderSeriesToggle(data);
  renderHeatmaps(data);
  renderLine(data.combined || data.series || []);
}

/* ---------- click-to-filter ---------- */
function renderFilterChip() {
  const el = document.getElementById("heatmap-filter");
  if (!state.filter) { el.hidden = true; el.innerHTML = ""; return; }
  const label = state.filter.type === "project" ? shortPath(state.filter.value) : state.filter.value;
  el.hidden = false;
  el.innerHTML =
    `<span title="${esc(state.filter.value)}">only ${esc(state.filter.type)} <b class="mono">${esc(label)}</b></span>` +
    `<button id="filter-clear" title="Clear filter" aria-label="Clear filter">✕</button>`;
  document.getElementById("filter-clear").addEventListener("click", () => setFilter(null));
}

function setFilter(f) {
  state.filter = f;
  updateFilteredRows();
  loadAll();
}

// Keep row highlighting in sync without re-fetching the tables.
function updateFilteredRows() {
  document.querySelectorAll("tr[data-fkind]").forEach((tr) => {
    const on = state.filter
      && state.filter.type === tr.dataset.fkind
      && state.filter.value === tr.dataset.fval;
    tr.classList.toggle("filtered", !!on);
  });
}

// Clicking a Models/Projects row scopes the Daily burn card to it; again to clear.
function wireRowFilters(containerId) {
  document.querySelectorAll(`#${containerId} tr[data-fkind]`).forEach((tr) => {
    tr.addEventListener("click", () => {
      const f = { type: tr.dataset.fkind, value: tr.dataset.fval };
      const same = state.filter
        && state.filter.type === f.type && state.filter.value === f.value;
      setFilter(same ? null : f);
    });
  });
}

function rowFilterAttr(kind, value) {
  const on = state.filter && state.filter.type === kind && state.filter.value === value;
  return `data-fkind="${esc(kind)}" data-fval="${esc(value)}"` +
    `${on ? ' class="filtered"' : ""} title="Filter the daily burn to this ${kind}"`;
}

// "combined" first, then each provider alphabetically.
function orderedKeys(data) {
  return ["combined", ...Object.keys(data.providers || {}).sort()];
}

function renderSeriesToggle(data) {
  const keys = orderedKeys(data);
  if (!seriesEnabled) seriesEnabled = {};
  // Default newly-seen panels to on.
  keys.forEach((k) => { if (!(k in seriesEnabled)) seriesEnabled[k] = true; });

  const host = document.getElementById("series-toggle");
  host.innerHTML = keys.map((k) => {
    const on = seriesEnabled[k] !== false;
    const dot = on ? ` style="background:${seriesColor(k)}"` : "";
    return `<button data-key="${esc(k)}" class="${on ? "on" : ""}" aria-pressed="${on}">` +
      `<span class="dot"${dot}></span>${esc(seriesLabel(k))}</button>`;
  }).join("");

  host.querySelectorAll("button").forEach((btn) => {
    btn.addEventListener("click", () => {
      const k = btn.dataset.key;
      seriesEnabled[k] = !(seriesEnabled[k] !== false); // toggle
      saveEnabledPanels();
      renderSeriesToggle(lastHeatmap);
      renderHeatmaps(lastHeatmap);
    });
  });
}

function renderHeatmaps(data) {
  const keys = orderedKeys(data);
  const host = document.getElementById("heatmaps");
  const placeholder = host.querySelector(".empty");
  if (placeholder) placeholder.remove();

  // Tear down panels that are now disabled or gone.
  Array.from(host.querySelectorAll(".heatmap-panel")).forEach((panel) => {
    const k = panel.dataset.key;
    if (!keys.includes(k) || seriesEnabled[k] === false) {
      if (heatmapCharts[k]) { heatmapCharts[k].dispose(); delete heatmapCharts[k]; }
      delete heatmapDayIndex[k];
      panel.remove();
    }
  });

  const enabled = keys.filter((k) => seriesEnabled[k] !== false);
  if (!enabled.length) {
    host.innerHTML = `<p class="empty">No panels selected — pick one above.</p>`;
    return;
  }

  // Create any missing panels, then re-append in order so layout is stable.
  enabled.forEach((k) => {
    let panel = host.querySelector(`.heatmap-panel[data-key="${k}"]`);
    if (!panel) {
      panel = document.createElement("div");
      panel.className = "heatmap-panel";
      panel.dataset.key = k;
      panel.innerHTML =
        `<div class="heatmap-label"><span class="name">${esc(seriesLabel(k))}</span>` +
        `<span class="note panel-stat" id="stat-${esc(k)}"></span></div>` +
        `<div class="chart heatmap-chart" id="hm-${esc(k)}"></div>`;
    }
    host.appendChild(panel);
  });

  enabled.forEach((k) => {
    const series = k === "combined" ? (data.combined || []) : ((data.providers || {})[k] || []);
    renderHeatmapInto(k, series);
  });
}

// The panels share a date window, so hovering a day in one highlights the same day
// in the others — that's the comparison the aligned calendars exist for.
function wireHoverSync(key, chart) {
  if (chart.__hoverSynced) return;
  chart.__hoverSynced = true;
  chart.on("mouseover", (p) => {
    if (!p || !p.data || !p.data.raw) return;
    const day = p.data.raw.day;
    Object.entries(heatmapCharts).forEach(([k, c]) => {
      if (k === key) return;
      const idx = (heatmapDayIndex[k] || {})[day];
      if (idx != null) c.dispatchAction({ type: "showTip", seriesIndex: 0, dataIndex: idx });
      else c.dispatchAction({ type: "hideTip" });
    });
  });
  chart.on("globalout", () => {
    Object.entries(heatmapCharts).forEach(([k, c]) => {
      if (k !== key) c.dispatchAction({ type: "hideTip" });
    });
  });
}

function renderHeatmapInto(key, series) {
  const el = document.getElementById(`hm-${key}`);
  if (!el) return;
  let chart = heatmapCharts[key];
  if (!chart) { chart = echarts.init(el, null, { renderer: "canvas" }); heatmapCharts[key] = chart; }
  wireHoverSync(key, chart);

  const statEl = document.getElementById(`stat-${key}`);
  if (!series.length) {
    chart.clear();
    heatmapDayIndex[key] = {};
    if (statEl) statEl.textContent = "no activity in range";
    return;
  }
  if (statEl) {
    const tCost = series.reduce((a, d) => a + (d.cost || 0), 0);
    const tTok = series.reduce((a, d) => a + (d.tokens || 0), 0);
    statEl.textContent = `${fmtMoney(tCost)} · ${fmtTokens(tTok)} tok`;
  }

  const cells = series.map((d) => ({ value: [d.day, metricVal(d)], raw: d }));
  heatmapDayIndex[key] = Object.fromEntries(series.map((d, i) => [d.day, i]));
  const maxV = Math.max(...series.map(metricVal));
  // The API anchors every panel to today in the configured timezone.
  const lastDay = lastHeatmap.end_day;
  const start = lastHeatmap.start_day;

  const ink = cssVar("--ink"), muted = cssVar("--muted"), panel = cssVar("--panel"),
    rule = cssVar("--rule"), faint = cssVar("--faint"), bg = cssVar("--bg");

  chart.setOption({
    tooltip: {
      borderColor: rule,
      backgroundColor: panel,
      textStyle: { color: ink, fontSize: 12 },
      formatter: (p) => {
        const d = p.data.raw;
        return `<b>${weekday(d.day)} ${d.day}</b><br/>` +
          `${fmtMoney(d.cost)} · ${fmtTokens(d.tokens)} tok<br/>` +
          `<span style="color:${muted}">in ${fmtTokens(d.input)} · out ${fmtTokens(d.output)} · ` +
          `cache rd ${fmtTokens(d.cache_read)} · cache wr ${fmtTokens(d.cache_creation)}</span>`;
      },
    },
    visualMap: {
      type: "piecewise",
      pieces: buckets(maxV, rampFor(key), fmtMetric),
      orient: "horizontal",
      left: "center",
      bottom: 0,
      itemWidth: 12,
      itemHeight: 12,
      itemGap: 4,
      textStyle: { color: muted, fontSize: 10 },
    },
    calendar: {
      top: 20,
      left: 30,
      right: 12,
      bottom: 40,
      cellSize: ["auto", 13],
      range: [start, lastDay],
      splitLine: { show: false },
      itemStyle: { color: faint, borderColor: bg, borderWidth: 2 },
      yearLabel: { show: false },
      monthLabel: { color: muted, fontSize: 11 },
      dayLabel: { color: muted, fontSize: 10, firstDay: 0 },
    },
    series: [{ type: "heatmap", coordinateSystem: "calendar", data: cells }],
  }, true);
}

function renderLine(series) {
  const el = document.getElementById("dailyline");
  if (!lineChart) lineChart = echarts.init(el, null, { renderer: "canvas" });
  if (!series.length) { lineChart.clear(); return; }

  const days = series.map((d) => d.day);
  const vals = series.map(metricVal);
  // Series contains every calendar day, including zeros. The first six
  // points have insufficient history for a full seven-day average.
  const ma = vals.map((_, i) => {
    if (i < 6) return null;
    const s = i - 6;
    const w = vals.slice(s, i + 1);
    return w.reduce((a, b) => a + b, 0) / w.length;
  });

  const ink = cssVar("--ink"), muted = cssVar("--muted"), panel = cssVar("--panel"),
    rule = cssVar("--rule"), faint = cssVar("--faint");
  const ramp = rampFor("combined");

  lineChart.setOption({
    grid: { top: 18, left: 48, right: 12, bottom: 22 },
    tooltip: {
      trigger: "axis", backgroundColor: panel, borderColor: rule,
      textStyle: { color: ink, fontSize: 12 },
      valueFormatter: (v) => fmtMetric(v),
    },
    xAxis: {
      type: "category", data: days, boundaryGap: false,
      axisLine: { lineStyle: { color: rule } },
      axisLabel: { color: muted, fontSize: 10 },
      axisTick: { show: false },
    },
    yAxis: {
      type: "value",
      splitLine: { lineStyle: { color: faint } },
      axisLabel: { color: muted, fontSize: 10, formatter: (v) => fmtMetric(v) },
    },
    series: [
      { name: "daily", type: "bar", data: vals, itemStyle: { color: ramp[2] }, barMaxWidth: 6 },
      { name: "7-day avg", type: "line", data: ma, smooth: true, symbol: "none",
        lineStyle: { color: ramp[5], width: 2 } },
    ],
  }, true);
}

/* ---------- punchcard ---------- */
const DOW_LABELS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

async function loadPunchcard(context) {
  const { punchcard } = await getJSON(`/api/punchcard?days=${state.days}${filterQuery()}`, context);
  const filt = state.filter
    ? ` · only ${state.filter.type === "project" ? shortPath(state.filter.value) : state.filter.value}`
    : "";
  document.getElementById("punchcard-note").textContent =
    `when the burn happens · local hour × weekday · color = ${state.metric === "cost" ? "$" : "tokens"} · ${rangeLabel()}${filt}`;
  renderPunchcard(punchcard || []);
}

function renderPunchcard(rows) {
  const el = document.getElementById("punchcard");
  if (!punchChart) punchChart = echarts.init(el, null, { renderer: "canvas" });
  if (!rows.length) { punchChart.clear(); return; }

  // Fill all 7x24 cells so quiet hours render as (palest) tiles, not holes.
  const byKey = {};
  rows.forEach((r) => { byKey[`${r.dow}-${r.hour}`] = r; });
  const cells = [];
  for (let d = 0; d < 7; d++) {
    for (let h = 0; h < 24; h++) {
      const r = byKey[`${d}-${h}`] || null;
      cells.push({ value: [h, d, r ? metricVal(r) : 0], raw: r });
    }
  }
  const maxV = Math.max(...rows.map(metricVal));

  const ink = cssVar("--ink"), muted = cssVar("--muted"), panel = cssVar("--panel"),
    rule = cssVar("--rule"), bg = cssVar("--bg");

  punchChart.setOption({
    tooltip: {
      borderColor: rule,
      backgroundColor: panel,
      textStyle: { color: ink, fontSize: 12 },
      formatter: (p) => {
        const [h, d] = p.data.value;
        const win = `${DOW_LABELS[d]} ${String(h).padStart(2, "0")}:00–${String((h + 1) % 24).padStart(2, "0")}:00`;
        const r = p.data.raw;
        if (!r) return `<b>${win}</b><br/>no activity`;
        return `<b>${win}</b><br/>${fmtMoney(r.cost)} · ${fmtTokens(r.tokens)} tok · ` +
          `${(r.events || 0).toLocaleString()} req`;
      },
    },
    grid: { top: 8, left: 40, right: 12, bottom: 46 },
    xAxis: {
      type: "category",
      data: Array.from({ length: 24 }, (_, h) => h),
      axisLine: { show: false },
      axisTick: { show: false },
      axisLabel: { color: muted, fontSize: 10, interval: 2 },
    },
    yAxis: {
      type: "category",
      data: DOW_LABELS,
      inverse: true, // Sunday on top, matching the calendar rows
      axisLine: { show: false },
      axisTick: { show: false },
      axisLabel: { color: muted, fontSize: 10 },
    },
    visualMap: {
      type: "piecewise",
      pieces: buckets(maxV, rampFor("combined"), fmtMetric),
      orient: "horizontal",
      left: "center",
      bottom: 0,
      itemWidth: 12,
      itemHeight: 12,
      itemGap: 4,
      textStyle: { color: muted, fontSize: 10 },
    },
    series: [{
      type: "heatmap",
      data: cells,
      itemStyle: { borderColor: bg, borderWidth: 2 },
    }],
  }, true);
}

/* ---------- burn ---------- */
async function loadBurn(context) {
  const b = await getJSON("/api/burn", context);
  const rows = [];
  rows.push(burnRow("Last 5 hours", fmtTokens(b.block_5h.tokens) + " · " + fmtMoney(b.block_5h.cost),
    b.block_5h.utilization, b.block_5h.limit_tokens));
  rows.push(burnRow("Last 7 days", fmtTokens(b.week.tokens) + " · " + fmtMoney(b.week.cost),
    b.week.utilization, b.week.limit_tokens));
  rows.push(plainRow("Burn rate (24h avg)",
    fmtTokens(b.rate.tokens_per_hour_24h) + " tok/h · " + fmtMoney(b.rate.cost_per_hour_24h) + "/h"));
  rows.push(plainRow("30-day forecast",
    fmtTokens(b.forecast_30d.tokens) + " · " + fmtMoney(b.forecast_30d.cost)));
  if (b.block_5h.hours_to_limit != null) {
    // An estimate, not a promise: trailing-5h consumption at the trailing-24h rate.
    const h = b.block_5h.hours_to_limit;
    rows.push(plainRow("Time to 5h limit",
      h > 24 ? "> 24 h at 24h-avg rate" : `≈ ${h.toFixed(1)} h at 24h-avg rate`));
  }
  document.getElementById("burn").innerHTML = rows.join("");
}
function plainRow(k, v) {
  return `<div class="burnrow"><span class="k">${k}</span><span class="v">${v}</span></div>`;
}
function burnRow(k, v, util, limit) {
  let gauge = "";
  if (limit && util != null) {
    const w = Math.min(100, util * 100);
    const hot = util >= 0.85 ? "hot" : "";
    gauge = `<div class="gauge"><span class="${hot}" style="width:${w}%"></span></div>`;
  }
  return `<div class="burnrow" style="flex-wrap:wrap">
    <span class="k">${k}${limit ? ` · ${pct(util)} of limit` : ""}</span>
    <span class="v">${v}</span>${gauge ? `<div style="flex-basis:100%">${gauge}</div>` : ""}</div>`;
}

/* ---------- tables ---------- */
function table(headers, rows, rowAttrs) {
  const head = headers.map((h) => `<th>${h}</th>`).join("");
  const body = rows.map((r, i) =>
    `<tr${rowAttrs && rowAttrs[i] ? " " + rowAttrs[i] : ""}>` +
    r.map((c) => `<td>${c}</td>`).join("") + "</tr>").join("");
  return `<table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
}
function miniBar(frac, color) {
  const w = Math.round(80 * Math.min(1, Math.max(0, frac)));
  const style = color ? `width:${w}px;background:${color}` : `width:${w}px`;
  return `<span class="bartrack"><span class="bar" style="${style}"></span></span>`;
}

async function loadModels(context) {
  const data = await getJSON(`/api/models?days=${state.days}`, context);
  const models = data.models || [];
  const savings = data.cache_savings || 0;
  document.getElementById("models-note").textContent =
    `$ and cache efficiency · ${rangeLabel()}` +
    (savings >= 0.01 ? ` · caching saved ≈ ${fmtMoney(savings)}` : "");
  if (!models.length) { document.getElementById("models").innerHTML = `<p class="empty">No data yet.</p>`; return; }
  const rows = models.map((m) => {
    const inputSide = (m.input || 0) + (m.cache_read || 0) + (m.cache_creation || 0);
    const eff = inputSide ? (m.cache_read || 0) / inputSide : 0;
    const reasoning = m.reasoning && m.output ? pct(m.reasoning / m.output) : `<span class="dim">—</span>`;
    return [
      providerPill(m.provider) + " <span class='mono'>" + esc(m.model || "(unknown)") + "</span>",
      fmtTokens(m.tokens), fmtMoney(m.cost),
      miniBar(eff, seriesColor(m.provider)) + " " + pct(eff),
      reasoning, (m.events || 0).toLocaleString(),
    ];
  });
  document.getElementById("models").innerHTML = table(
    ["Model", "Tokens", "$", "Cache hit", "Reasoning", "Req"], rows,
    models.map((m) => rowFilterAttr("model", m.model || "(unknown)")),
  );
  wireRowFilters("models");
}

async function loadProjects(context) {
  const { projects } = await getJSON(`/api/projects?days=${state.days}`, context);
  document.getElementById("projects-note").textContent = `by directory · ${rangeLabel()}`;
  if (!projects.length) { document.getElementById("projects").innerHTML = `<p class="empty">No data yet.</p>`; return; }
  const rows = projects.map((p) => [
    providerPill(p.provider) +
      ` <span class="mono" title="${esc(p.project)}">${esc(shortPath(p.project))}</span>`,
    fmtMoney(p.cost), fmtTokens(p.tokens), p.sessions,
  ]);
  document.getElementById("projects").innerHTML = table(
    ["Project", "$", "Tokens", "Sess"], rows,
    projects.map((p) => rowFilterAttr("project", p.project || "(none)")),
  );
  wireRowFilters("projects");
}

async function loadSessions(context) {
  const { sessions } = await getJSON(`/api/sessions?days=${state.days}`, context);
  document.getElementById("sessions-note").textContent = `most expensive · ${rangeLabel()}`;
  if (!sessions.length) { document.getElementById("sessions").innerHTML = `<p class="empty">No data yet.</p>`; return; }
  const rows = sessions.map((s) => [
    `<span class="mono" title="${esc(s.session_id)}">${esc((s.session_id || "").slice(0, 8))}</span> ` +
      `<span class="dim" title="${esc(s.project)}">${esc(shortPath(s.project))}</span>`,
    fmtMoney(s.cost), fmtTokens(s.tokens), s.turns,
    `<span class="dim">${localDate(s.last_ts)}</span>`,
  ]);
  document.getElementById("sessions").innerHTML =
    table(["Session", "$", "Tokens", "Turns", "When"], rows);
}

async function loadTurns(context) {
  const { turns } = await getJSON(`/api/turns?days=${state.days}`, context);
  document.getElementById("turns-note").textContent = `where the burn went · ${rangeLabel()}`;
  if (!turns.length) { document.getElementById("turns").innerHTML = `<p class="empty">No data yet.</p>`; return; }
  const rows = turns.map((t) => [
    providerPill(t.provider) + " <span class='mono'>" + esc(t.model || "") + "</span>",
    `<span class="dim" title="${esc(t.project)}">${esc(shortPath(t.project))}</span>`,
    fmtMoney(t.cost),
    `<span class="dim">in ${fmtTokens(t.input)} · out ${fmtTokens(t.output)} · rd ${fmtTokens(t.cache_read)} · wr ${fmtTokens(t.cache_creation)}</span>`,
    `<span class="dim">${localTime(t.ts)}</span>`,
  ]);
  document.getElementById("turns").innerHTML =
    table(["Model", "Project", "$", "Tokens", "When"], rows);
}

function shortPath(p) {
  if (!p) return "(none)";
  const parts = p.split("/").filter(Boolean);
  return parts.length <= 2 ? p : ".../" + parts.slice(-2).join("/");
}

/* ---------- wiring ---------- */
// A refresh owns every request. A new selection cancels the previous generation
// so late responses cannot render against newer labels, filters, or theme state.
let refreshController = null;
let refreshGeneration = 0;
async function loadAll() {
  if (refreshController) refreshController.abort();
  refreshController = new AbortController();
  const context = { signal: refreshController.signal };
  const generation = ++refreshGeneration;
  const results = await Promise.allSettled([
    loadSummary(context), loadHeatmap(context), loadPunchcard(context), loadBurn(context),
    loadModels(context), loadProjects(context), loadSessions(context), loadTurns(context),
  ]);
  if (generation !== refreshGeneration) return true;
  const failed = results.filter((r) => r.status === "rejected");
  const freshness = document.getElementById("freshness");
  freshness.classList.toggle("error", failed.length > 0);
  if (failed.length) {
    failed.forEach((r) => console.error(r.reason));
    freshness.textContent = "⚠ some panels failed to refresh — showing last loaded data";
  }
  return failed.length === 0;
}

function wireToggle(id, key, cast, onChange) {
  document.querySelectorAll(`#${id} button`).forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(`#${id} button`).forEach((b) => b.classList.remove("on"));
      btn.classList.add("on");
      state[key] = cast(btn.dataset[key]);
      onChange();
    });
  });
}

document.addEventListener("DOMContentLoaded", () => {
  applyTheme(currentTheme()); // sync the toggle button to the attribute set in <head>
  document.getElementById("theme-toggle").addEventListener("click", toggleTheme);
  wireToggle("metric-toggle", "metric", (v) => v, loadAll);
  wireToggle("range-toggle", "days", (v) => parseInt(v, 10), loadAll);
  document.getElementById("refresh").addEventListener("click", async (e) => {
    const btn = e.target;
    btn.disabled = true;
    btn.textContent = "↻ ingesting…";
    try {
      const r = await fetch("/api/ingest", { method: "POST" });
      if (!r.ok) throw new Error("ingest failed: " + r.status);
      if (!await loadAll()) throw new Error("Panel refresh failed");
      btn.textContent = "↻ refresh";
    } catch (err) {
      console.error(err);
      btn.textContent = "⚠ refresh failed";
      setTimeout(() => { btn.textContent = "↻ refresh"; }, 4000);
    } finally {
      btn.disabled = false;
    }
  });
  document.getElementById("export").addEventListener("click", () => {
    // Raw events for the current range (and active filter, if any).
    window.location.href = `/api/export.csv?days=${state.days}${filterQuery()}`;
  });
  loadAll();
  window.addEventListener("resize", () => {
    Object.values(heatmapCharts).forEach((c) => c.resize());
    if (lineChart) lineChart.resize();
    if (punchChart) punchChart.resize();
  });
  // Keep charts, tables, and totals current together.
  setInterval(loadAll, 60000);
});
