// Explicit local recovery action. This command never dispatches a model or tool.
import { parseArgs } from 'node:util';
import { DurableToolStore } from '../agents/tool-budget/durable-store.mjs';

let store;
try {
  const { values } = parseArgs({ options: {
    ledger: { type: 'string' }, agent: { type: 'string' }, session: { type: 'string' },
    previous: { type: 'string' }, next: { type: 'string' },
  } });
  if (['ledger', 'agent', 'session', 'previous', 'next'].some(key => !values[key])) throw new Error();
  store = new DurableToolStore(values.ledger);
  const owner = { agentId: values.agent, sessionKey: values.session };
  const result = store.transfer({ ...owner, runId: values.previous }, { ...owner, runId: values.next });
  console.log(JSON.stringify(result));
  process.exitCode = result.allowed ? 0 : 1;
} catch {
  console.error('Invalid recovery configuration or unavailable supported ledger. No task was dispatched.');
  process.exitCode = 2;
} finally { store?.close(); }
