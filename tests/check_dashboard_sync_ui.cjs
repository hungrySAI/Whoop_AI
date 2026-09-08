// Execute the actual status renderer with fabricated responses, without a browser or health data.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assets = path.join(__dirname, '../src/whoop_copilot/web_assets');
const html = fs.readFileSync(path.join(assets, 'index.html'), 'utf8');
const elements = new Map([...html.matchAll(/id="([^"]+)"/g)].map((match) => [match[1], {
  textContent: '', classList: { toggle() {} }, addEventListener() {},
}]));
const context = vm.createContext({
  document: {
    getElementById(id) { assert.ok(elements.has(id), `Missing HTML target: ${id}`); return elements.get(id); },
    querySelector() { return { content: 'synthetic-csrf' }; }, querySelectorAll() { return []; },
  },
  Intl, Date, performance: { now: () => 0 }, clearTimeout() {}, setTimeout() {},
  fetch() { assert.fail('Rendering must not request WHOOP or the local API'); },
});
const source = fs.readFileSync(path.join(assets, 'app.js'), 'utf8');
vm.runInContext(source.replace(/initialize\(\);\s*$/, ''), context);
const end = '2026-09-07T12:00:00Z';
const plan = {
  request: { start: '2026-07-19T00:00:00Z', end }, reason: 'since_success',
  expanded: true, lookback_limited: false, lookback_days: 366,
};
const status = {
  environment: 'synthetic', sync_enabled: true, can_sync: true, running: false,
  checked_at: end, freshness: { 7: { state: 'stale' }, 30: { state: 'stale' } },
  sync_plan: { 7: plan, 30: plan }, last_run: null, last_success_window: null,
};
function render(value, section = 'overview') {
  context.input = value;
  context.section = section;
  vm.runInContext('state.section = section; state.status = input; renderStatus(input)', context);
}
const text = id => elements.get(id).textContent;
render(status);
assert.match(text('planned-window'), /2026\/07\/19/);
assert.match(text('catchup-note'), /扩展查询/);
assert.match(text('sync-button'), /同步并补取/);
assert.match(text('sync-policy'), /不定时采集/);

render({ ...status, sync_plan: { 7: { ...plan, reason: 'history_gap' } } });
assert.match(text('freshness-note'), /区间之间仍有待补取/);

render({ ...status, sync_plan: { 7: {
  ...plan, lookback_limited: true, request: { start: '2025-09-06T12:00:00Z', end },
} } });
assert.match(text('planned-window'), /2025/);
assert.match(text('catchup-note'), /366 天；更早历史未核查/);

render({ ...status, last_run: {
  run_id: 'synthetic', status: 'paging', resources_completed: 4,
  request: { start: '2026-06-01T00:00:00Z', end: '2026-08-01T00:00:00Z' }, catch_up: null,
} });
assert.equal(text('planned-window-label'), '当前请求范围');
assert.match(text('planned-window'), /2026\/06\/01/);
assert.match(text('resume-window'), /不包含此后新增的数据/);
assert.equal(elements.get('resume-button').hidden, false);

render(status, 'weekly');
assert.match(text('freshness-note'), /补取范围可早于所选视图/);
assert.doesNotMatch(text('freshness-note'), /不会回填更早周/);

render({ ...status, sync_enabled: false, can_sync: false });
assert.equal(text('planned-window'), '—');
assert.equal(text('catchup-note'), '合成演示不访问 WHOOP。');
console.log('6 synthetic status-renderer scenarios passed; no network or browser accessed.');
