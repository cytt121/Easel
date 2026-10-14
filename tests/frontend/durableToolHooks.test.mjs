import assert from 'node:assert/strict';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { DatabaseSync } from 'node:sqlite';
import test from 'node:test';
import { DurableToolStore } from '../../agents/tool-budget/durable-store.mjs';
import { openBudgetStore } from '../../agents/tool-budget/open-store.mjs';
import { installBudgetHooks } from '../../agents/tool-budget/hooks.mjs';

const ctx = { agentId: 'agent', sessionKey: 'session', runId: 'run' };
const drain = () => new Promise(resolve => setImmediate(resolve));
function fixture(t) {
  const root = mkdtempSync(join(tmpdir(), 'easel-durable-hooks-'));
  const path = join(root, 'budget.sqlite');
  new DurableToolStore(path, { create: true }).close();
  const stores = [];
  let wall = 100;
  t.after(() => { for (const store of stores) { try { store.close(); } catch {} } rmSync(root, { recursive: true }); });
  function install(options = {}) {
    const store = options.store ?? new DurableToolStore(path, { clock: () => wall });
    stores.push(store);
    let mono = 0;
    const hooks = {}, stops = [], logs = [], timers = [];
    installBudgetHooks({ pluginConfig: { maxCalls: 2, timeoutMs: 1000, ...options.policy },
      on: (name, fn) => { hooks[name] = fn; }, logger: { info: raw => logs.push(JSON.parse(raw)) } },
    { store, clock: () => mono, schedule: (fn, delay) => { timers.push({ fn, delay }); return {}; }, cancel() {},
      stop: target => { stops.push(target); return { aborted: true, runIds: [target.runId] }; } });
    return { store, hooks, stops, logs, timers, advance: value => { mono = value; },
      begin: (target = ctx) => hooks.before_agent_run({}, target),
      call: (id, target = ctx) => hooks.before_tool_call({ toolCallId: id }, target) };
  }
  return { path, install, advanceWall: value => { wall = value; } };
}

test('plugin reinstallation restores spent quota instead of granting a fresh budget', async t => {
  const f = fixture(t), first = f.install();
  assert.equal(first.begin().outcome, 'pass');
  assert.equal(first.call('first'), undefined);
  first.hooks.after_tool_call({ toolCallId: 'first', error: 'failure still spends' }, ctx);
  first.store.close();
  f.advanceWall(600);
  const resumed = f.install();
  assert.equal(resumed.begin().outcome, 'pass');
  assert.equal(resumed.timers[0].delay, 500);
  assert.equal(resumed.call('second'), undefined);
  assert.equal(resumed.call('third').block, true);
  await drain();
  assert.equal(resumed.logs[0].stopReason, 'call_budget_exhausted');
  assert.equal(resumed.logs[0].rejected, 1);
  assert.deepEqual(resumed.stops, [{ ...ctx, preserveSideRuns: true }]);
  assert.equal(f.install().begin().outcome, 'block');
});

test('duplicate calls and changed policy are denied after reinstall', t => {
  const f = fixture(t), first = f.install();
  first.begin();
  first.call('first');
  first.store.close();
  const resumed = f.install();
  resumed.begin();
  assert.match(resumed.call('first').blockReason, /duplicate_call_identity/);
  const other = { ...ctx, runId: 'other' };
  resumed.begin(other);
  assert.equal(f.install({ policy: { maxCalls: 3 } }).begin(other).reason, 'policy_mismatch');
});

test('monotonic thinking deadline persists even if the wall clock does not advance', async t => {
  const f = fixture(t), first = f.install();
  first.begin();
  first.advance(1000);
  first.timers[0].fn();
  await drain();
  assert.equal(first.logs[0].stopReason, 'time_budget_exhausted');
  assert.equal(f.install().begin().reason, 'time_budget_exhausted');
  const other = { ...ctx, runId: 'other' };
  assert.equal(f.install().begin(other).outcome, 'pass');
});

test('finished runs cannot reopen and late tools remain blocked', t => {
  const f = fixture(t), first = f.install();
  first.begin();
  first.hooks.agent_end({}, ctx);
  assert.equal(first.call('late').block, true);
  assert.equal(f.install().begin().reason, 'run_finished');
});

test('time spent reopening storage does not extend the original deadline', t => {
  const f = fixture(t), owner = f.install();
  let resumed;
  const wrapper = {
    begin: (...args) => { const saved = owner.store.begin(...args); resumed.advance(1000); return saved; },
    finish: (...args) => owner.store.finish(...args), close() {},
  };
  resumed = f.install({ store: wrapper });
  assert.equal(resumed.begin().reason, 'time_budget_exhausted');
  assert.equal(resumed.timers.length, 0);
  assert.equal(f.install().begin().reason, 'time_budget_exhausted');
});

test('missing configured store denies lifecycle admission without in-memory fallback', t => {
  const f = fixture(t);
  assert.equal(openBudgetStore(undefined), null);
  for (const path of [f.path + '.missing', '', null]) {
    const broken = f.install({ store: openBudgetStore(path) });
    assert.equal(broken.begin().reason, 'storage_unavailable');
    assert.equal(broken.call('tool').block, true);
  }
});

test('storage failure after begin blocks tools and requests only the owning run stop', async t => {
  const f = fixture(t), first = f.install();
  first.begin();
  const lock = new DatabaseSync(f.path);
  try {
    lock.exec('BEGIN IMMEDIATE');
    assert.match(first.call('tool').blockReason, /storage_unavailable/);
    lock.exec('ROLLBACK');
    assert.equal(first.call('retry').block, true);
    await drain();
    assert.deepEqual(first.stops, [{ ...ctx, preserveSideRuns: true }]);
    assert.equal(first.logs[0].admitted, 0);
    assert.equal(first.logs[0].rejected, 2);
    assert.equal(first.logs[0].stopReason, 'storage_unavailable');
  } finally { lock.close(); }
});
