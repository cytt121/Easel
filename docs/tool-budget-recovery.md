# 恢复执行时继承工具预算

恢复控制器可以把未结束任务的预算从旧 `runId` 转交给新 `runId`，前提是 `agentId` 和
`sessionKey` 完全一致。预算的规范身份仍是第一次执行的账本行；额度、截止时间和已预占
调用 ID 都不复制、不清零。SQLite 事务只切换当前允许执行的绑定，最多一个并发恢复者成功。
新执行必须使用原预算策略；修改额度或时限会关闭任务。

本功能是可信本地控制操作，不能暴露为模型可调用工具，也不能根据模型提供的任务身份自动恢复。
控制器必须先核对任务归属和恢复意图，停止旧执行并对可能仍在进行的远端副作用做对账；然后在
启动新执行之前转交预算。转交不能撤销已经开始的工具或恢复模型对话本身。

在项目根目录使用 Node 24.19：

```bash
node scripts/transfer_tool_budget.mjs --ledger /absolute/path/budget.sqlite --agent main --session agent:main:task --previous OLD_RUN_ID --next NEW_RUN_ID
```

退出码 0 表示转交已提交，1 表示拒绝，2 表示参数或账本不可用。命令只输出决定与剩余额度，
不发送模型或工具请求。必须在新的原生执行开始前运行；新执行使用的真实 `runId` 必须与
`--next` 一致。OpenClaw 2026.10.2 的 `chat.send` 使用 `idempotencyKey` 作为执行 ID，
其他版本和自动恢复流程不能据此推断，需要重新验收。

旧身份的 `begin`、`reserve` 和 `finish` 均返回 `execution_superseded`，不影响新身份。
插件迟到的停止请求仍只针对旧 runId。已结束、超时、归属不符、目标已存在或重复转交都不能
创建新预算。账本保留全部绑定，最多 10000 个预算、20000 个执行绑定，满额拒绝，不淘汰旧记录。

## 旧账本升级

新账本为 schema v2，运行时不会自动升级 v1。先停止所有读写此账本的旧版本 Gateway/进程，
保留离线备份，再显式升级；不能在旧代码仍持有数据库连接时热升级：

```bash
node --input-type=module -e "import { DurableToolStore } from './agents/tool-budget/durable-store.mjs'; new DurableToolStore('/absolute/path/budget.sqlite', {upgrade: true}).close()"
```

迁移在单个事务中给原有任务补充活动绑定，保留调用记录、终态、已用额度与截止时间。升级后
只能使用支持 v2 的执行端；旧版新连接会拒绝此账本。升级失败时不要删除账本或重新初始化。

原生验收使用本地假模型和无害计数工具：

```bash
node scripts/verify_native_tool_budget.mjs /path/to/openclaw --durable --recovery
node scripts/verify_native_tool_budget.mjs /path/to/openclaw --durable --recovery --tool-search
```

验收预存一次已花额度，转交给预分配的新原生执行 ID，并检查剩余额度真正约束原生工具。
它不代表产品已经自动恢复整个 Agent；进程崩溃、事务回滚、并发转交和旧回调隔离另由账本及
插件测试覆盖。尚不提供 Web 自动恢复入口或跨账号迁移。
