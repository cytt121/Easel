import { DurableToolStore } from './durable-store.mjs';

/** A requested but unavailable ledger must never become the in-memory fallback. */
export function openBudgetStore(path) {
  if (path === undefined) return null;
  try { return new DurableToolStore(path); }
  catch {
    const denied = () => ({ allowed: false, reason: 'storage_unavailable' });
    return { begin: denied, reserve: denied, finish: denied, isAvailable: () => false, close() {} };
  }
}
