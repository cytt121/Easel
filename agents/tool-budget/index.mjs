import { callGatewayFromCli } from 'openclaw/plugin-sdk/gateway-runtime';
import { installBudgetHooks } from './hooks.mjs';
import { openBudgetStore } from './open-store.mjs';

export default {
  id: 'easel-tool-budget',
  name: 'Easel per-run tool budget',
  register(api) {
    installBudgetHooks(api, { store: openBudgetStore(api.pluginConfig?.stateFile),
      stop: params => callGatewayFromCli('chat.abort',
      { timeout: '5000', json: true }, params, { progress: false }) });
  },
};
