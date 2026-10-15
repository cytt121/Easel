# 任务用量与版本化成本估算

`scripts/agent_task_cost.py` 离线读取多任务评测 JSON 和显式价格配置，不调用模型、不查询账单、
不自动获取或推测供应商价格。结果是使用声明单价的估算，不是实际费用或执行期货币预算。

新版 `agent_task_eval.py` 报告包含 `deployment_fingerprint`：对配置的完整 Chat Completions
地址和请求模型名称计算 SHA-256，不包含 Key。价格文件必须匹配这个指纹；旧报告没有指纹时
拒绝估算，不能猜测模型身份。这只绑定请求配置，不证明提供商实际路由的模型或单价正确；
同一部署上的账号套餐差异仍需操作者选择正确价格版本。指纹不是秘密配置的加密存储。

价格 JSON 仅允许以下字段。下列数字是测试示例，不代表任何模型的真实报价：

```json
{
  "schema_version": 1,
  "version": "example-flat-v1",
  "effective_date": "2026-10-15",
  "currency": "USD",
  "deployment_fingerprint": "从本次评测报告复制的64位小写十六进制指纹",
  "input_per_million": "2.5",
  "output_per_million": "10"
}
```

版本必填；日期必须为有效的 `YYYY-MM-DD`；币种为三位大写标记，不进行汇率换算。单价以
十进制字符串声明，0–999999.999999999，最多九位小数，不接受浮点数、负数、指数或非有限值。
操作者负责核对价格来源、适用日期、账号和模型；脚本不声称该配置已获得供应商认证。

```bash
python scripts/agent_task_cost.py --report task-report.json --pricing pricing.json
```

每任务估算为 `(prompt_tokens × input_per_million + completion_tokens × output_per_million) / 1000000`。
使用 Decimal 运算并输出十进制字符串，不按分四舍五入，从而保留微小成本。这里采用统一输入/
输出费率，不模拟缓存折扣、分层计价、推理专属费率、税费、免费额度或套餐。价格为零也不能让
缺失的用量变成已知；提供商没有报告的超时后消耗同样无法估算。

报告分别给出：

- 每个已尝试任务的 `estimated_cost`，用量缺失或响应不完整时为 `null`。
- `known_subtotal_estimate`：已知任务小计，明确不能当作完整总额。
- `attempted_total_estimate`：全部已尝试任务均可估算时才给出，否则 `null`。
- `planned_total_estimate`：计划内任务全部已运行，且全部用量已知时才给出。
- `failed_task_estimate`：未通过任务的估算消耗；其中任何任务用量未知时为 `null`。
  未运行的任务不算失败，也不计为已发生费用；失败消耗不等于全部可以避免的浪费。

源报告的场景、样本数、重复任务、完成标记、请求数和 token 类型会校验，冲突时报错，不
输出看似完整的金额。无任务时已尝试小计为零、计划总额仍未知。价格配置复制到输出中，便于
复现；原始报告不会被改写。输入文件上限 1 MiB，拒绝重复 JSON 键。

退出码 0：计划总额可估算；1：存在未运行或未知费用；2：报告或价格配置无效。
默认不附带真实价格，本项目已有真实模型 smoke test 的成本仍保持未知，不能套用示例单价。
