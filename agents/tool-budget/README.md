# Native tool budgets (opt in)

This plugin adds a per-run admission limit to the OpenClaw embedded agent used by
Easel. The limit is reserved synchronously **before** a tool executes. Failed
tools and later policy vetoes consume their reservation. A monotonic deadline
also runs while the model is thinking. Retries with the same run identity cannot
reset either limit within the process. Optional durable reservations retain the
same run identity's quota and original deadline across restarts.

Trusted recovery controllers can also transfer an unfinished budget to a new native run ID,
fencing the old execution while retaining quota and deadline. See
[budget recovery](../../docs/tool-budget-recovery.md) for the explicit command and offline v1-to-v2 upgrade.

The unit of admission is a native `before_tool_call` invocation. Tool Search
dispatchers and their target tools each consume a reservation; a budget of two
can execute one target through Tool Search or two directly exposed tools. A
parallel dispatcher burst can exhaust capacity before its targets begin. Limits
are conservative and are not a count of external API requests.

The plugin is optional; installing Easel does not enable it or alter existing
OpenClaw settings. It does not cover Codex or ACP backends, CLI child processes,
or external side effects performed inside one admitted tool.

## Configuration

The native integration is tested against **OpenClaw 2026.10.2, Node 24.19**.
Use the same OpenClaw version for the CLI and gateway. Other versions and
alternative harnesses need their own acceptance run before enabling protection.

Merge the following entries into the existing **Easel profile** configuration
(normally `~/.openclaw-easel/openclaw.json`). Replace the path with the absolute
path to this directory. Preserve existing plugin allow entries, paths, and
configuration; do not replace the entire config with this fragment.

```json
{
  "plugins": {
    "allow": ["easel-tool-budget"],
    "load": { "paths": ["/absolute/path/to/Easel/agents/tool-budget"] },
    "entries": {
      "easel-tool-budget": {
        "enabled": true,
        "hooks": { "allowConversationAccess": true },
        "config": { "maxCalls": 40, "timeoutMs": 120000 }
      }
    }
  }
}
```

`maxCalls` must be an integer from 0 to 10000; `timeoutMs` must be an integer
from 1 to 3600000. There are no implicit unlimited defaults. Missing/invalid
run or call identities deny execution. Lifecycle hooks need OpenClaw's
`allowConversationAccess` setting; this plugin does not read or log messages,
tool parameters, or results. Without that setting, OpenClaw blocks those hooks
and every attempted tool is denied because its run was never admitted.

After saving, restart the Easel gateway and inspect the **same profile**:

```sh
openclaw --profile easel plugins inspect easel-tool-budget --runtime --json
```

Verify status `loaded`, no hook-blocking diagnostics, and all four typed hooks:
`before_agent_run`, `before_tool_call`, `after_tool_call`, and `agent_end`.
An enabled entry alone does not prove runtime enforcement. Keep the gateway's
own run timeout enabled as a separate bound.

## Stop semantics and limits

Admission closes immediately when a limit is exceeded. A separate, bounded
`chat.abort` request targets the exact agent, session, and run, with
`preserveSideRuns: true`. It never retries against a whole session. Only a reply
confirming that exact run is reported as `stopStatus: confirmed`; transport
errors, identity mismatches, and already-ended runs are `unconfirmed`.
An unconfirmed stop still denies further tools, but cannot guarantee that the
model has stopped. The logs distinguish these outcomes rather than reporting a
successful cancellation. An already-running tool may finish after the deadline;
the plugin cannot roll back its effects or forcibly terminate arbitrary tools.

The plugin logs aggregate admitted/pending/succeeded/failed/rejected counts and
the stop reason. It keeps at most 10000 run records per gateway process and
refuses new runs once full. Completed call identities are discarded after their
results settle. Retained run records prevent delayed callbacks from reopening
completed runs. Restarting a drained gateway clears only the memory registry.
Without `stateFile`, budgets do not survive restart; with it, the durable
registry remains and still refuses new identities when full. Neither mode is
an exactly-once mechanism. Logged counters describe this plugin instance, not
the lifetime accounting in the persistent ledger.

Do not hot-reload the plugin during active work. Resumed/recovered native runs
may have identities that cannot be cancelled through the original chat request;
their abort result remains unconfirmed. These limitations must be considered
before enabling the plugin on an existing long-running gateway.

## Optional durable reservations

First initialize a dedicated database once, while no gateway uses it. Run this
from the repository root with an absolute path in an existing private directory:

```sh
node --input-type=module -e "import { DurableToolStore } from './agents/tool-budget/durable-store.mjs'; new DurableToolStore('/absolute/path/budget.sqlite', {create: true}).close()"
```

Then add `"stateFile": "/absolute/path/budget.sqlite"` beside `maxCalls` and
`timeoutMs` in this plugin's config. Keep that file across restarts. Runtime
opening never creates a missing database: unavailable storage blocks both run
admission and tools instead of falling back to memory. The existing no-file
configuration remains explicitly in-memory.

Each tool needs both a committed SQLite reservation and the in-process budget's
approval. Failed tools, later policy vetoes and crashes after reservation do not
refund calls. Reopening the same agent/session/run restores the remaining quota
and deadline; changing policy for that identity closes it. The persisted wall
clock includes downtime; a monotonic timer still bounds in-process thinking.
Storage errors request a scoped stop but do not prove the model has stopped.

Changed native run IDs require an explicit trusted recovery transfer; they are not
automatically linked and interrupted work is not automatically resumed. It assumes trustworthy local storage and system time;
restoring old database backups or undetected clock adjustments are outside the
guarantee. Do not delete/reset the ledger to clear capacity while old identities
can return. See [ledger semantics](../../docs/durable-tool-reservations.md).

## Local checks

```sh
node --test tests/frontend/toolBudget.test.mjs tests/frontend/toolBudgetHooks.test.mjs
node --test tests/frontend/durableTool*.test.mjs
```

The core and hook-adapter tests require no model, credentials, network, or
OpenClaw installation. They cover concurrent reservations, failed calls,
deadline boundaries, identity errors, cancellation failure, late callbacks,
and registry exhaustion. They do not substitute for native gateway acceptance.

The acceptance script starts an isolated native gateway, a loopback fake model,
and a harmless counter tool. It asserts that a five-call burst executes exactly
two effects, stopping that run leaves a concurrent run able to finish, and the
deadline aborts a model response that never completes. It requires no real
credentials or model service. Run it with the pinned Node version:

```sh
npm install --prefix /tmp/easel-budget-runtime --ignore-scripts openclaw@2026.10.2
node scripts/verify_native_tool_budget.mjs /tmp/easel-budget-runtime/node_modules/openclaw
node scripts/verify_native_tool_budget.mjs /tmp/easel-budget-runtime/node_modules/openclaw --tool-search
node scripts/verify_native_tool_budget.mjs /tmp/easel-budget-runtime/node_modules/openclaw --durable
node scripts/verify_native_tool_budget.mjs /tmp/easel-budget-runtime/node_modules/openclaw --durable --tool-search
```

Use an equivalent temporary directory on Windows. Each invocation creates its
own temporary state and loopback ports; it never changes the user's profile.
Only aggregate acceptance results go to stdout. Failed runs identify a local
diagnostic directory. CI runs both direct tools and Tool Search, in memory and
durable modes, on Linux and Windows; it does not upload raw gateway logs or
transcripts. These native checks use fresh isolated state. Same-identity
reopening is tested separately with hook reinstallation and real child-process
death; the fixture does not assert native recovery preserves run IDs.
