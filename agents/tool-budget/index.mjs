import { callGatewayFromCli } from 'openclaw/plugin-sdk/gateway-runtime';
import { dispatchGatewayMethod } from 'openclaw/plugin-sdk/gateway-method-runtime';
import { installBudgetHooks } from './hooks.mjs';
import { openBudgetStore } from './open-store.mjs';
import { registerBudgetCapability } from './capability.mjs';
import { registerBudgetSend } from './send.mjs';

export default {
  id: 'easel-tool-budget',
  name: 'Easel per-run tool budget',
  register(api) {
    const store = openBudgetStore(api.pluginConfig?.stateFile);
    const available = () => store === null || store.isAvailable();
    const policy = installBudgetHooks(api, { store,
      stop: params => callGatewayFromCli('chat.abort',
      { timeout: '5000', json: true }, params, { progress: false }) });
    const instanceId = registerBudgetCapability(api, policy, available);
    registerBudgetSend(api, policy, instanceId, dispatchGatewayMethod, available);
  },
};
