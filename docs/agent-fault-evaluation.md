# Agent 确定性故障回归报告

从已有 pytest 的 JUnit XML 生成可比较的 JSON 证据，不运行模型、不调用平台、不重放工具。
首批四个场景覆盖原生会话身份恢复、并发会话所有权与取消清理、原生致命错误通知，以及进程死亡后的会话恢复。
场景 ID、故障阶段、预期行为、禁止效果及具体测试映射位于报告脚本的 `SCENARIOS`。

```bash
python -m pytest -q tests/test_agent_harnesses.py tests/test_agent_fault_measurements.py -o junit_family=legacy --junitxml=agent-tests.xml
python scripts/agent_fault_report.py agent-tests.xml --revision YOUR_TESTED_COMMIT > agent-report.json
```

测试命令失败时也可以生成报告，但必须保留 pytest 的退出码与原始 XML；生成报告不改变测试结果。
报告命令退出码：0 表示登记场景全部通过，1 表示至少一个失败、错误、跳过或未运行，
2 表示输入文件不可读取或格式无效。0 只覆盖登记场景，不代表整个测试套件或产品全部通过。
部分测试文件、`-k` 筛选或测试更名可能产生 `not_run`，不会被当成成功。

输出包含输入 XML 的 SHA-256、调用者提供的版本标签及每个场景的结果与测试用时。
版本标签不被脚本独立验证；应由执行测试的 CI 记录当时的提交，而不是事后猜测。
失败堆栈、stdout/stderr、测试参数值和工具输出不会复制进报告。原始 XML 仍需妥善保管。

`test_duration_seconds` 是关联测试的执行用时，不等于任务恢复延迟。
模型调用数、重复外部效果数及恢复延迟目前没有独立采集，明确输出 `null`，不伪装成零。
测试断言通过也不等于真实用户任务成功率。相同 XML 和版本标签会生成相同报告。

待任务账本、检查点及调用预算功能合入后，再分小 PR 登记对应场景、采集专门指标，
并接入 CI artifact。真实模型任务评测需要独立的任务集、费用预算和多次采样，当前命令不提供。

进程恢复场景用真实子进程运行协议 fixture，在两轮之间杀死空闲进程，再恢复同一原生会话。
JUnit properties 记录两次明确发起的 prompt 提交数、超出预期的重复提交数，以及从下一轮
开始到恢复完成的单调时钟耗时；这是模拟协议的恢复延迟，不是模型推理或真实业务恢复耗时。
每个后端保留独立样本，不把少量样本平均成可靠性结论。测量字段缺失会产生
`missing_measurements`，不能判为完整通过；非法计数和非有限数值拒绝生成报告。
其他场景未测量的字段仍为 null，重复 prompt 不等于重复外部副作用。
## 完整恢复回归集合

默认 `--suite core` 检查四个原生协议场景。`--suite recovery` 在其上增加九个场景组：
日志损坏/截断、流结束顺序、快照归属、检查点进程死亡、孤儿任务核对、排队取消、
终态回放、待授权取消和客户端请求并发保护。清单固定每个所需参数化变体，不使用前缀模糊匹配。

```bash
python scripts/agent_fault_report.py agent-tests.xml --suite recovery --revision YOUR_TESTED_COMMIT > recovery-report.json
```

恢复集合用于包含对应功能分支的组合审计。前置功能未合入时，默认 CI 仍运行 core；
主动选择 recovery 而缺少对应测试会明确失败为 not_run，不会跳过后假报完成。
完整报告仅表示所选集合的登记断言和必要测量满足要求。它不表示未实现的严格调用预算已完成。
工具临时错误分类/预算场景应随对应实现新增；通用失败自动重试不属于当前能力。
## CI 与验收边界

Linux / Windows 的根目录 pytest 写出 JUnit（legacy family 保留测试 properties）。即使测试失败，
后续仍尝试生成报告；原测试退出状态不被覆盖。输入无效时报告步骤失败，缺失产物也不能显示成功。
CI 仅归档白名单字段的 JSON，保留 14 天，不上传原始 JUnit 中的堆栈、stdout 和测试输入。
产物名包含操作系统与 run attempt；版本标签使用实际 checkout 的 GitHub SHA（PR 通常为合并测试提交）。
手动 CI 的 fault_suite 可选 recovery，用于组合分支；日常 PR 使用 core，避免要求尚未合入的功能。

确定性评测的验收：固定场景清单、缺失证据失败、真实子进程故障注入、必要指标校验、
相同 XML 的稳定报告、两种操作系统的 CI 与可下载 JSON。本集合不测试真实模型质量，
不测真实平台副作用，不保证模型成本或成功率；这些不应出现在本工作的量化结论中。