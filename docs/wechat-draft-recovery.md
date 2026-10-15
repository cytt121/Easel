# 微信草稿的未知结果

普通图文和贴图草稿的 `draft/add` 是创建操作。超时、连接中断或 HTTP 5xx 不能证明微信
没有创建草稿，因此这两个入口只发送一次请求，也不跟随重定向。

- HTTP 200 且包含合法 `media_id`：返回创建回执。
- 明确的微信应用层错误码：报告拒绝，不自动重试，也不输出原始错误正文。
- 传输失败、非 200、无法解析或缺少合法回执：抛出 `DraftOutcomeUnknown`，提示先在目标
  公众号核对，不能当成“没有创建”而自动重放。

原有 `publish.py` / `add_draft` 入口仅修复进程内的自动重试，重新运行仍可能创建新草稿。
需要跨进程保护的官方 API 任务应使用下述显式操作入口。封面和正文图片上传的原有重试策略不在此次改动范围内。此限制不能表述为远端
恰好一次执行。后续对账应绑定明确的任务、账号和远端回执；不能凭“暂时查不到”推断可以重试。

测试使用离线 HTTP fixture，覆盖两种创建入口的超时、断连、限流、重定向、服务端错误、
应用层拒绝及异常成功响应，不使用真实微信凭据或创建真实草稿。

## 持久化提交和回执核对

`draft_operation.py` 是官方 HTTP API 的低层操作入口，接收已经准备好的 `articles` JSON
（包括预先上传的封面素材 ID），不负责写作、内容审核、图片上传或浏览器后台发布。
使用前必须完成既有内容检查与发布授权；该入口不会自动把浏览器会话切换成官方 API。

以下命令从仓库根目录执行，账本使用绝对路径；父目录需已存在。先显式初始化一次，之后
不得为解决超时而删除账本或重新初始化。账本最多保存 10000 项操作，满额拒绝，不自动清理。

```bash
python skills/builtin/skill-wechat-publisher/scripts/draft_operation.py init --ledger /absolute/path/drafts.sqlite
python skills/builtin/skill-wechat-publisher/scripts/draft_operation.py submit --ledger /absolute/path/drafts.sqlite --operation task-001 --account main --payload draft.json
python skills/builtin/skill-wechat-publisher/scripts/draft_operation.py status --ledger /absolute/path/drafts.sqlite --operation task-001 --account main --payload draft.json
python skills/builtin/skill-wechat-publisher/scripts/draft_operation.py reconcile --ledger /absolute/path/drafts.sqlite --operation task-001 --account main --payload draft.json
```

`draft.json` 的结构为 `{"articles":[{"title":"标题","content":"已检查的正文","thumb_media_id":"已有素材ID"}]}`。
同一任务必须保留相同操作 ID、账号和原始 payload；账号以配置中的真实 `app_id` 绑定，账号
别名变更不生成新任务。同一 ID 换账号或输入会拒绝；不能通过自动换 ID 绕过未知结果。

提交前通过 SQLite 事务写入 `unknown`。收到合法回执后写入 `acknowledged`，明确应用层拒绝
写入 `failed`。三个状态都不会被重复 `submit` 再次发送。包括发送前后进程崩溃、请求超时、
回执保存失败在内的模糊窗口保留 `unknown`，需要人工核对，不能自动重放。

`status` 只读本地；`reconcile` 没有回执时返回 `unknown_no_receipt`，不发创建或列表请求。
有回执时，只调用官方 [获取草稿详情接口](https://developers.weixin.qq.com/doc/service/api/draftbox/draftmanage/api_getdraft)。
`receipt_found` 仅表示这次查询在绑定账号下找到该回执对应的草稿，不表示正文未经编辑、
没有重复草稿或已经群发；微信会规范化内容，不能直接拿输入全文哈希与查询结果比较。
查询超时、草稿不存在或异常响应均返回 `unknown`，绝不将其转换为重新提交许可。
远端查询结果是当时观测，不覆盖已持久化的提交回执。

标准输出仅含状态、是否启动本次请求及核对结论；账本仅存操作/账号/输入哈希、状态和
远端回执，不存 Key、token、正文或原始响应。退出码 0 表示初始化成功、已收到提交回执或
本次查询找到回执；1 表示失败或未知；2 表示配置、归属或存储错误。所有非零结果都不是
自动重试许可。数据库仅用于可信本地文件系统；多机、回滚备份、删除文件或更换 ID 不在保障内。
