const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

const source = fs.readFileSync(path.join(__dirname, '../src/token_dashboard/static/app.js'), 'utf8');
function harness(fetch) {
  const elements = new Map();
  const getElement = (id) => {
    if (!elements.has(id)) elements.set(id, {
      textContent: '', innerHTML: '', hidden: false,
      classList: { toggle() {}, remove() {}, add() {} },
      addEventListener() {}, setAttribute() {}, getAttribute: () => "",
    });
    return elements.get(id);
  };
  const charts = [];
  const ready = [];
  const intervals = [];
  const context = vm.createContext({
    fetch, AbortController, DOMException, console: { error() {} },
    localStorage: { getItem: () => null },
    document: {
      documentElement: { getAttribute: () => 'light', setAttribute() {} },
      getElementById: getElement, querySelectorAll: () => [],
      addEventListener: (_, fn) => ready.push(fn),
    },
    window: { addEventListener() {} },
    getComputedStyle: () => ({ getPropertyValue: () => '#333' }),
    setInterval: (fn) => intervals.push(fn), setTimeout,
    echarts: { init: () => ({ setOption: (option) => charts.push(option), clear() {} }) },
  });
  vm.runInContext(source, context);
  return { context, elements, charts, ready, intervals, run: (code) => vm.runInContext(code, context) };
}
function data(url, label = 'current') {
  const endpoint = url.split('?')[0];
  return {
    '/api/summary': { today: {}, last_7d: {}, last_30d: {}, all_time: {}, meta: {} },
    '/api/heatmap': { combined: [], providers: {}, start_day: '2026-06-01', end_day: '2026-06-30' },
    '/api/punchcard': { punchcard: [] },
    '/api/burn': { block_5h: {}, week: {}, rate: {}, forecast_30d: {} },
    '/api/models': { models: [{ model: label, provider: 'claude' }] },
    '/api/projects': { projects: [] },
    '/api/sessions': { sessions: [] },
    '/api/turns': { turns: [] },
  }[endpoint];
}
const response = (body) => ({ ok: true, json: async () => body });
function stubCharts(h) {
  h.run('renderHeatmaps = () => {}; renderSeriesToggle = () => {}; renderLine = () => {}; renderPunchcard = () => {};');
}

test('seven-day average includes six quiet days and waits for a full window', () => {
  const h = harness();
  h.run(`renderLine(Array.from({length: 7}, (_, i) => ({day: '2026-06-' + (14+i), cost: i === 0 ? 70 : 0})))`);
  assert.deepEqual(Array.from(h.charts[0].series[1].data), [null, null, null, null, null, null, 10]);
});

test('calendar uses server timezone bounds even when last activity is older', () => {
  const h = harness();
  h.run(`lastHeatmap = {start_day: '2026-06-01', end_day: '2026-06-30'};
    wireHoverSync = () => {};
    renderHeatmapInto('combined', [{day: '2026-06-10', cost: 1, tokens: 1}]);`);
  assert.deepEqual(Array.from(h.charts[0].calendar.range), ['2026-06-01', '2026-06-30']);
});

test('late responses cannot overwrite a newer range or filter', async () => {
  const pending = [];
  let delay = true;
  const h = harness((url) => {
    // Deliberately ignore AbortSignal to simulate a response already in flight.
    if (delay) return new Promise((resolve) => pending.push(() => resolve(response(data(url, 'obsolete')))));
    return Promise.resolve(response(data(url, 'selected')));
  });
  stubCharts(h);
  const first = h.run('loadAll()');
  delay = false;
  await h.run('state.days = 30; state.filter = {type: "model", value: "selected"}; loadAll()');
  pending.forEach((resolve) => resolve());
  await first;
  assert.match(h.elements.get('models').innerHTML, /selected/);
  assert.doesNotMatch(h.elements.get('models').innerHTML, /obsolete/);
  assert.match(h.elements.get('models-note').textContent, /last 30 days/);
});

test('scheduled polling refreshes all eight panels', async () => {
  const urls = [];
  const h = harness(async (url) => { urls.push(url.split('?')[0]); return response(data(url)); });
  stubCharts(h);
  h.ready[0]();
  await h.run('loadAll()');
  urls.length = 0;
  await h.intervals[0]();
  assert.equal(urls.length, 8);
  assert.equal(new Set(urls).size, 8);
  assert.ok(urls.includes('/api/models') && urls.includes('/api/punchcard') && urls.includes('/api/turns'));
});
