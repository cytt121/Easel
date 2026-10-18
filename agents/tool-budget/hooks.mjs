import { ToolBudget } from './budget.mjs';

const identity = ctx => [ctx?.agentId, ctx?.sessionKey, ctx?.runId].every(
  value => typeof value === 'string' && value.trim() && value.length <= 512);
const key = ctx => JSON.stringify([ctx.agentId, ctx.sessionKey, ctx.runId]);
const denied = reason => ({ block: true, blockReason: `Easel tool budget: ${reason}` });

/** Dependency-injected transport keeps admission synchronous; stop never widens its target. */
export function installBudgetHooks(api, { stop, clock, store = null, schedule = setTimeout, cancel = clearTimeout }) {
  const config = Object.freeze({ maxCalls: api.pluginConfig.maxCalls, timeoutMs: api.pluginConfig.timeoutMs,
    ...(api.pluginConfig.stateFile === undefined ? {} : { stateFile: api.pluginConfig.stateFile }) });
  const monotonic = clock ?? (() => performance.now());
  // Validate configuration before registering any partial protection.
  new ToolBudget({ maxCalls: config.maxCalls, timeoutMs: config.timeoutMs, clock });
  const runs = new Map();
  const maxTrackedRuns = 10000;
  const emit = (state, event) => {
    const snapshot = state.budget.snapshot();
    api.logger.info(JSON.stringify({ event, ...snapshot,
      rejected: snapshot.rejected + (state.durableRejected ?? 0),
      stopReason: state.blockReason ?? snapshot.stopReason, stopStatus: state.stopStatus }));
  };
  const halt = state => {
    if (state.stopStatus !== 'not_requested') return;
    state.blockReason ??= state.budget.expire() ?? 'run_finished';
    state.budget.finish();
    if (store?.finish(state.target, state.blockReason).reason === 'storage_unavailable') {
      state.blockReason = 'storage_unavailable';
    }
    state.stopStatus = 'pending';
    // Do not await a stop from inside the hook: the host may await this hook
    // during cancellation. Capacity stays closed regardless of RPC outcome.
    Promise.resolve().then(() => stop({ ...state.target, preserveSideRuns: true })).then(
      result => {
        state.stopStatus = result?.aborted === true && Array.isArray(result.runIds) && result.runIds.includes(state.target.runId)
          ? 'confirmed' : 'unconfirmed';
        emit(state, 'easel_budget_stop');
      }, () => { state.stopStatus = 'unconfirmed'; emit(state, 'easel_budget_stop'); });
  };
  api.on('before_agent_run', (_event, ctx) => {
    const block = reason => ({ outcome: 'block', reason, message: `Easel tool budget: ${reason}` });
    if (!identity(ctx)) return block('missing_run_identity');
    const existing = runs.get(key(ctx));
    if (!existing && runs.size >= maxTrackedRuns) return block('tracking_capacity_exhausted');
    const beforeRead = monotonic();
    const saved = store?.begin(ctx, config);
    if (saved && !saved.allowed) {
      if (existing && !existing.finished) { existing.blockReason = saved.reason; halt(existing); }
      return block(saved.reason);
    }
    if (existing) {
      const reason = existing.blockReason ?? existing.budget.expire();
      if (reason && !existing.finished) halt(existing);
      return reason ? block(reason) : { outcome: 'pass' };
    }
    const elapsed = monotonic() - beforeRead;
    const remaining = saved ? Math.floor(saved.remainingMs - elapsed) : config.timeoutMs;
    if (!Number.isFinite(elapsed) || elapsed < 0 || remaining <= 0) {
      const reason = !Number.isFinite(elapsed) || elapsed < 0 ? 'clock_invalid' : 'time_budget_exhausted';
      store?.finish(ctx, reason);
      return block(reason);
    }
    const state = { budget: new ToolBudget({ maxCalls: saved?.remainingCalls ?? config.maxCalls,
      timeoutMs: remaining, clock }),
      target: { runId: ctx.runId, sessionKey: ctx.sessionKey, agentId: ctx.agentId },
      stopStatus: 'not_requested', timer: null, finished: false };
    const arm = () => {
      state.timer = schedule(() => {
        if (state.finished) return;
        if (state.budget.expire()) halt(state);
        else arm(); // Timers may fire early; never silently lose the deadline.
      }, state.budget.remainingMs());
      state.timer?.unref?.();
    };
    arm();
    runs.set(key(ctx), state);
    return { outcome: 'pass' };
  });
  api.on('before_tool_call', (event, ctx) => {
    if (!identity(ctx) || (event.runId && event.runId !== ctx.runId) ||
        (event.toolCallId && ctx.toolCallId && event.toolCallId !== ctx.toolCallId)) {
      return denied('mismatched_run_identity');
    }
    const state = runs.get(key(ctx));
    if (!state) return denied('run_not_admitted');
    const reason = state.blockReason ?? state.budget.expire();
    if (reason) {
      state.budget.admit(event.toolCallId ?? ctx.toolCallId); // Count the rejected attempt.
      if (!state.finished) halt(state);
      return denied(reason);
    }
    const reserved = store?.reserve(ctx, event.toolCallId ?? ctx.toolCallId);
    if (reserved && !reserved.allowed) {
      state.durableRejected = (state.durableRejected ?? 0) + 1;
      state.blockReason = reserved.reason;
      halt(state);
      return denied(reserved.reason);
    }
    const decision = state.budget.admit(event.toolCallId ?? ctx.toolCallId);
    if (!decision.allowed) {
      if (decision.reason !== 'run_finished') halt(state);
      return denied(decision.reason);
    }
  }, { priority: 1000 });
  api.on('after_tool_call', (event, ctx) => {
    if (!identity(ctx) || (event.runId && event.runId !== ctx.runId) ||
        (event.toolCallId && ctx.toolCallId && event.toolCallId !== ctx.toolCallId)) return;
    runs.get(key(ctx))?.budget.settle(event.toolCallId ?? ctx.toolCallId, Boolean(event.error));
  });
  api.on('agent_end', (event, ctx) => {
    const target = { ...ctx, runId: ctx?.runId ?? event.runId };
    if (!identity(target) || (event.runId && event.runId !== target.runId)) return;
    const state = runs.get(key(target));
    if (!state) return;
    state.finished = true;
    cancel(state.timer);
    state.budget.finish();
    if (store?.finish(target).reason === 'storage_unavailable') state.blockReason = 'storage_unavailable';
    emit(state, 'easel_budget_end');
  });
  return config;
}
