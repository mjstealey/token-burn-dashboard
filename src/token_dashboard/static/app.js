/* Token Burn dashboard — fetches JSON metrics and renders a Tufte-style
   calendar heat map plus supporting breakdowns. No build step. */

const state = { metric: "cost", days: 365 };
const heatmapCharts = {}; // panel key ("combined"|"claude"|"openai"...) -> ECharts instance
let lineChart = null;
let lastHeatmap = null; // last /api/heatmap payload, kept so toggles re-render without refetch
let seriesEnabled = loadEnabledPanels(); // { key: bool } or null until first load fills it

// Muted sequential ramp (light -> dark teal-green).
const RAMP = ["#eef3f1", "#d6e6df", "#a9cfc2", "#6fae9b", "#3f8f78", "#226b58", "#0f4536"];

// Per-panel accent used on the toggle chip dot; combined keeps the primary accent.
const SERIES_COLORS = { combined: "#226b58", claude: "#226b58", openai: "#5a3fb0", local: "#7a6a4f" };
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
function fmtMoney(v) {
  v = v || 0;
  if (v >= 1000) return "$" + v.toLocaleString(undefined, { maximumFractionDigits: 0 });
  if (v >= 1) return "$" + v.toFixed(2);
  return "$" + v.toFixed(3);
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
function dateAdd(iso, delta) {
  const d = new Date(iso + "T00:00:00Z");
  d.setUTCDate(d.getUTCDate() + delta);
  return d.toISOString().slice(0, 10);
}

async function getJSON(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(url + " -> " + r.status);
  return r.json();
}

/* ---------- KPIs / freshness ---------- */
async function loadSummary() {
  const s = await getJSON("/api/summary");
  const cards = [
    ["Today", s.today], ["7 days", s.last_7d], ["30 days", s.last_30d], ["All time", s.all_time],
  ];
  document.getElementById("kpis").innerHTML = cards
    .map(([label, w]) => `
      <div class="kpi">
        <div class="label">${label}</div>
        <div class="big">${fmtMoney(w.cost)}</div>
        <div class="small">${fmtTokens(w.tokens)} tok · ${(w.events || 0).toLocaleString()} req</div>
      </div>`)
    .join("");

  const last = s.meta && s.meta.last_ts ? localTime(s.meta.last_ts) : "no data";
  const ing = s.last_ingest_at ? localTime(s.last_ingest_at) : "—";
  document.getElementById("freshness").textContent =
    `Latest activity ${last} · last ingest ${ing} · timezone ${s.timezone}`;

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

function providerPill(p) {
  const cls = p === "openai" ? "openai" : p === "local" ? "local" : "";
  const name = p === "openai" ? "Codex" : p === "claude" ? "Claude" : p;
  return `<span class="pill ${cls}">${name}</span>`;
}

/* ---------- heat map ---------- */
function buckets(max) {
  if (!(max > 0)) return [{ lte: 0, color: RAMP[0] }];
  const th = [];
  let v = max;
  for (let i = 0; i < RAMP.length - 1; i++) { th.unshift(v); v = v / 3; } // geometric (log-ish)
  const pieces = [{ lt: th[0], color: RAMP[0] }];
  for (let i = 0; i < th.length - 1; i++) pieces.push({ gte: th[i], lt: th[i + 1], color: RAMP[i + 1] });
  pieces.push({ gte: th[th.length - 1], color: RAMP[RAMP.length - 1] });
  return pieces;
}

async function loadHeatmap() {
  const data = await getJSON(`/api/heatmap?days=${state.days}&metric=${state.metric}`);
  lastHeatmap = data;
  document.getElementById("heatmap-note").textContent =
    `color = daily ${state.metric === "cost" ? "$" : "tokens"} · each panel scaled to its own range · hover for detail`;
  renderSeriesToggle(data);
  renderHeatmaps(data);
  renderLine(data.combined || data.series || []);
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
    const color = SERIES_COLORS[k] || "#226b58";
    const dot = on ? ` style="background:${color}"` : "";
    return `<button data-key="${k}" class="${on ? "on" : ""}" aria-pressed="${on}">` +
      `<span class="dot"${dot}></span>${seriesLabel(k)}</button>`;
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
        `<div class="heatmap-label"><span class="name">${seriesLabel(k)}</span>` +
        `<span class="note panel-stat" id="stat-${k}"></span></div>` +
        `<div class="chart heatmap-chart" id="hm-${k}"></div>`;
    }
    host.appendChild(panel);
  });

  enabled.forEach((k) => {
    const series = k === "combined" ? (data.combined || []) : ((data.providers || {})[k] || []);
    renderHeatmapInto(k, series);
  });
}

function renderHeatmapInto(key, series) {
  const el = document.getElementById(`hm-${key}`);
  if (!el) return;
  let chart = heatmapCharts[key];
  if (!chart) { chart = echarts.init(el, null, { renderer: "canvas" }); heatmapCharts[key] = chart; }

  const statEl = document.getElementById(`stat-${key}`);
  if (!series.length) {
    chart.clear();
    if (statEl) statEl.textContent = "no activity in range";
    return;
  }
  if (statEl) {
    const tCost = series.reduce((a, d) => a + (d.cost || 0), 0);
    const tTok = series.reduce((a, d) => a + (d.tokens || 0), 0);
    statEl.textContent = `${fmtMoney(tCost)} · ${fmtTokens(tTok)} tok`;
  }

  const cells = series.map((d) => ({ value: [d.day, metricVal(d)], raw: d }));
  const maxV = Math.max(...series.map(metricVal));
  // All panels share the same date window (combined's latest day) so they line up.
  const all = (lastHeatmap && lastHeatmap.combined && lastHeatmap.combined.length)
    ? lastHeatmap.combined : series;
  const lastDay = all[all.length - 1].day;
  const start = dateAdd(lastDay, -(state.days - 1));

  chart.setOption({
    tooltip: {
      borderColor: "#ddd9d0",
      backgroundColor: "#fff",
      textStyle: { color: "#1b1b1a", fontSize: 12 },
      formatter: (p) => {
        const d = p.data.raw;
        return `<b>${d.day}</b><br/>` +
          `${fmtMoney(d.cost)} · ${fmtTokens(d.tokens)} tok<br/>` +
          `<span style="color:#6f6c66">in ${fmtTokens(d.input)} · out ${fmtTokens(d.output)} · ` +
          `cache rd ${fmtTokens(d.cache_read)} · cache wr ${fmtTokens(d.cache_creation)}</span>`;
      },
    },
    visualMap: {
      type: "piecewise",
      pieces: buckets(maxV),
      orient: "horizontal",
      left: "center",
      bottom: 0,
      itemWidth: 12,
      itemHeight: 12,
      textStyle: { color: "#6f6c66", fontSize: 10 },
      formatter: (a, b) => {
        const f = state.metric === "cost" ? (x) => fmtMoney(x) : (x) => fmtTokens(x);
        if (a === -Infinity || a == null) return "< " + f(b);
        if (b === Infinity || b == null) return "≥ " + f(a);
        return f(a) + "–" + f(b);
      },
    },
    calendar: {
      top: 20,
      left: 30,
      right: 12,
      bottom: 40,
      cellSize: ["auto", 13],
      range: [start, lastDay],
      splitLine: { show: false },
      itemStyle: { color: "#f3f1ec", borderColor: "#faf9f7", borderWidth: 2 },
      yearLabel: { show: false },
      monthLabel: { color: "#6f6c66", fontSize: 11 },
      dayLabel: { color: "#b6b2a8", fontSize: 10, firstDay: 0 },
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
  // 7-day trailing moving average.
  const ma = vals.map((_, i) => {
    const s = Math.max(0, i - 6);
    const w = vals.slice(s, i + 1);
    return w.reduce((a, b) => a + b, 0) / w.length;
  });

  lineChart.setOption({
    grid: { top: 18, left: 48, right: 12, bottom: 22 },
    tooltip: {
      trigger: "axis", backgroundColor: "#fff", borderColor: "#ddd9d0",
      textStyle: { color: "#1b1b1a", fontSize: 12 },
      valueFormatter: (v) => fmtMetric(v),
    },
    xAxis: {
      type: "category", data: days, boundaryGap: false,
      axisLine: { lineStyle: { color: "#ddd9d0" } },
      axisLabel: { color: "#6f6c66", fontSize: 10 },
      axisTick: { show: false },
    },
    yAxis: {
      type: "value",
      splitLine: { lineStyle: { color: "#efece6" } },
      axisLabel: { color: "#6f6c66", fontSize: 10, formatter: (v) => fmtMetric(v) },
    },
    series: [
      { name: "daily", type: "bar", data: vals, itemStyle: { color: "#cfe2da" }, barMaxWidth: 6 },
      { name: "7-day avg", type: "line", data: ma, smooth: true, symbol: "none",
        lineStyle: { color: "#226b58", width: 2 } },
    ],
  }, true);
}

/* ---------- burn ---------- */
async function loadBurn() {
  const b = await getJSON("/api/burn");
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
    rows.push(plainRow("Time to 5h limit", b.block_5h.hours_to_limit.toFixed(1) + " h"));
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
function table(headers, rows) {
  const head = headers.map((h) => `<th>${h}</th>`).join("");
  const body = rows.map((r) => "<tr>" + r.map((c) => `<td>${c}</td>`).join("") + "</tr>").join("");
  return `<table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
}
function miniBar(frac) {
  const w = Math.round(80 * Math.min(1, Math.max(0, frac)));
  return `<span class="bartrack"><span class="bar" style="width:${w}px"></span></span>`;
}

async function loadModels() {
  const { models } = await getJSON("/api/models");
  if (!models.length) { document.getElementById("models").innerHTML = `<p class="empty">No data yet.</p>`; return; }
  const rows = models.map((m) => {
    const inputSide = (m.input || 0) + (m.cache_read || 0) + (m.cache_creation || 0);
    const eff = inputSide ? (m.cache_read || 0) / inputSide : 0;
    return [
      providerPill(m.provider) + " <span class='mono'>" + (m.model || "(unknown)") + "</span>",
      fmtTokens(m.tokens), fmtMoney(m.cost),
      miniBar(eff) + " " + pct(eff), (m.events || 0).toLocaleString(),
    ];
  });
  document.getElementById("models").innerHTML =
    table(["Model", "Tokens", "$", "Cache hit", "Req"], rows);
}

async function loadProjects() {
  const { projects } = await getJSON("/api/projects");
  if (!projects.length) { document.getElementById("projects").innerHTML = `<p class="empty">No data yet.</p>`; return; }
  const rows = projects.map((p) => [
    `<span class="mono" title="${p.project}">${shortPath(p.project)}</span>`,
    fmtMoney(p.cost), fmtTokens(p.tokens), p.sessions,
  ]);
  document.getElementById("projects").innerHTML = table(["Project", "$", "Tokens", "Sess"], rows);
}

async function loadSessions() {
  const { sessions } = await getJSON("/api/sessions");
  if (!sessions.length) { document.getElementById("sessions").innerHTML = `<p class="empty">No data yet.</p>`; return; }
  const rows = sessions.map((s) => [
    `<span class="mono" title="${s.session_id}">${(s.session_id || "").slice(0, 8)}</span> ` +
      `<span class="dim">${shortPath(s.project)}</span>`,
    fmtMoney(s.cost), fmtTokens(s.tokens), s.turns,
  ]);
  document.getElementById("sessions").innerHTML = table(["Session", "$", "Tokens", "Turns"], rows);
}

async function loadTurns() {
  const { turns } = await getJSON("/api/turns");
  if (!turns.length) { document.getElementById("turns").innerHTML = `<p class="empty">No data yet.</p>`; return; }
  const rows = turns.map((t) => [
    providerPill(t.provider) + " <span class='mono'>" + (t.model || "") + "</span>",
    fmtMoney(t.cost),
    `<span class="dim">in ${fmtTokens(t.input)} · out ${fmtTokens(t.output)} · rd ${fmtTokens(t.cache_read)} · wr ${fmtTokens(t.cache_creation)}</span>`,
    `<span class="dim">${localTime(t.ts)}</span>`,
  ]);
  document.getElementById("turns").innerHTML = table(["Model", "$", "Tokens", "When"], rows);
}

function shortPath(p) {
  if (!p) return "(none)";
  const parts = p.split("/").filter(Boolean);
  return parts.length <= 2 ? p : ".../" + parts.slice(-2).join("/");
}

/* ---------- wiring ---------- */
function loadAll() {
  return Promise.all([
    loadSummary(), loadHeatmap(), loadBurn(),
    loadModels(), loadProjects(), loadSessions(), loadTurns(),
  ]).catch((e) => console.error(e));
}

function wireToggle(id, key, cast) {
  document.querySelectorAll(`#${id} button`).forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(`#${id} button`).forEach((b) => b.classList.remove("on"));
      btn.classList.add("on");
      state[key] = cast(btn.dataset[key]);
      if (key === "metric") loadHeatmap();
      else loadHeatmap();
    });
  });
}

document.addEventListener("DOMContentLoaded", () => {
  wireToggle("metric-toggle", "metric", (v) => v);
  wireToggle("range-toggle", "days", (v) => parseInt(v, 10));
  document.getElementById("refresh").addEventListener("click", async (e) => {
    e.target.disabled = true;
    e.target.textContent = "↻ ingesting…";
    try { await fetch("/api/ingest", { method: "POST" }); await loadAll(); }
    finally { e.target.disabled = false; e.target.textContent = "↻ refresh"; }
  });
  loadAll();
  window.addEventListener("resize", () => {
    Object.values(heatmapCharts).forEach((c) => c.resize());
    if (lineChart) lineChart.resize();
  });
  // Lightweight polling so the dashboard stays current while open.
  setInterval(() => { loadSummary(); loadHeatmap(); loadBurn(); }, 60000);
});
