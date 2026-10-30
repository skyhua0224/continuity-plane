# 私有 Gitea Issue／PR 协作

版本：1；适用 Alpha37（`0.1.0a37`）。首版依据 Gitea v1.24.7 官方 API，其他版本须运行连接检查及验收。[仓库 `.continuity` 与全局配置规范](collaboration-storage.md)定义布局、数据根、Git 准入和凭据边界。

每位成员运行自己的 CLI／MCP，以自己的 Gitea 账号协作。不部署 Continuity 服务器、不共享数据库、不设置永久主控。人类可以直接通过 Gitea 网页参与。

## 权威与连接

Alpha35 可直接使用本机 tea 的既有名称，包括空格、冒号与 Unicode；例如 `--credential-ref 'tea:Company Gitea:3443'`。不要为了适应工具而改名、复制登录或导出 Token。

| 信息 | 权威 |
|---|---|
| 目标、验收、依赖、驱动者、阻塞、交接 | Issue 正文及事件评论 |
| PR、评审、合入提交 | Gitea PR／Git |
| 规则、模板、后端声明 | 仓库文件 |
| 上下文、任务快照、待发布操作、游标 | 各成员本机；任务快照仅为缓存 |

本地旧任务账本不会上传或覆盖 Issue。后端声明损坏时拒绝操作，不退回本地任务。状态变化不授权合并、发布、部署或操作密钥。

先在各自电脑上使用本机 Gitea CLI **tea** 登录。需要支持 `tea api --include --data`（本机验证为 tea 0.15）。Continuity 只调用 CLI，不读取或导出其 Token 配置；登录元数据通过 `tea logins list --output json` 检查实例匹配，所有请求显式指定 login 与 repository。不要把 Token 发给 AI、放进命令行、仓库或任务正文。

```bash
continuity collaboration register --project-id demo --control-root .
continuity collaboration backend configure --project-id demo \
  --instance https://forge.example.com:3443 --repository team/sample --credential-ref tea:work
continuity collaboration backend doctor --project-id demo --format json
```

configure 先验证真实认证账号、仓库及 Issue 读取，再创建 `.continuity/collaboration.toml`。不覆盖已有声明，不修改仓库权限、分支保护或设置。仓库忽略 `.continuity/` 时，仅显式提交此声明，不提交整个目录：

```toml
[collaboration]
backend = "gitea-issues"
instance = "https://forge.example.com:3443"
repository = "team/sample"
credential_ref = "tea:work"
```

其他成员登记自己的 checkout，建立自己的同名本地登录。支持 HTTPS 非标准端口及实例子路径；TLS、CA 与 HTTP 重定向策略由本机 tea 管理，Continuity 不设置 insecure，不传认证 header 或任意环境变量密钥。请为 tea 登录配置可信 HTTPS 服务；不要把关闭 TLS 验证当作正常部署。

doctor 分别显示实际可读连接、仓库权限、Issue／PR 开关、实例 Swagger 的评审端点支持。connected 不证明 Token 写入范围；真实写入仍由 Gitea 校验，不能自动提权。Swagger 无法读取时显示能力未证明。

## 创建、认领与日常操作

```bash
continuity task add --project-id demo --task-id parser-fix --lane-id parser \
  --mode patch --priority p1 --title '修复解析边界' --objective '满足协议要求' \
  --next-action '补回归测试' --exit-criterion '相关测试通过' \
  --body '背景、范围和验证入口' --allowed-file src/parser.py --operation-id create-1
continuity task list --project-id demo --mine --format brief
continuity task show --project-id demo --task-id parser-fix --format json
continuity task claim --project-id demo --task-id parser-fix \
  --expected-remote-revision REV --operation-id claim-1
continuity task update --project-id demo --task-id parser-fix --body '更新后的背景' \
  --expected-remote-revision REV --operation-id body-1
continuity task block --project-id demo --task-id parser-fix --blocked-reason '等待确认' \
  --expected-remote-revision REV --operation-id block-1
continuity task release --project-id demo --task-id parser-fix \
  --expected-remote-revision REV --operation-id release-1
```

REV 替换为最近一次 show 的 remote_revision；每次新变更使用新 operation-id，同一请求重试保留原 ID 和参数。task 与 collaboration task 共用合同。列表支持 `--mine`、`--created-by-me`、`--review-requested`、`--dependencies-ready`，以及状态／负责人／更新时间过滤和分页。建议文件范围仅提示潜在冲突，不构成模块所有权。

认领身份来自真实认证账号。executor_id、模型及 connection ID 不改变人类所有权；create 不代他人认领。body-only 更新不要求 status；共享修改要求远端 revision，不接受 force 绕过。网页成员可复制[Issue 模板](../examples/gitea-issue-template.md)建卡、追加进展及参与 PR 评审，无需安装 Continuity。

## 交接与评审

```bash
# 当前驱动者提出请求；所有权仍不变
continuity task handoff-request --project-id demo --task-id parser-fix --to bob \
  --note '下一步处理兼容行为' --expected-remote-revision REV --operation-id handoff-1
# bob 使用自己的认证接受
continuity task handoff-accept --project-id demo --task-id parser-fix \
  --expected-remote-revision NEW_REV --operation-id accept-1
continuity task ready-for-review --project-id demo --task-id parser-fix --pr 12 \
  --verification '回归通过；平台实测未执行' --expected-remote-revision REV --operation-id ready-1
continuity task request-review --project-id demo --task-id parser-fix --reviewer alice \
  --expected-remote-revision REV --operation-id review-1
```

请求评审实际调用 requested_reviewers API，不以评论冒充，不声称对方已接受。show 读取 APPROVED、REQUEST_CHANGES、DISMISSED、stale 及评审绑定提交。PR closed 不等于 merged；completed／merged 要求远端 merged=true 和有效 merge_commit_sha。首版不创建或合并 PR、不代提交评审；这些动作仍用 Forge 网页／已有 CLI，权限独立。

## 并发、网页修改与离线

Gitea v1.24.7 的 Issue API 未声明 CAS。首版只追加可读事件评论，记录观察到的父评论和字段原值，不 PATCH 覆盖正文、标签或负责人。并发修改产生 conflict；每次 claim 均为 exclusive=false、coordination-only，不是跨机器强锁。集成／部署互斥仍须使用现有外部控制。

已接受驱动者保存在 Issue 事件评论，不自动覆盖原生侧栏 assignees。两者不一致会报告冲突，要求成员在 Forge 协调，不据此冒称对方接受。网页修改正文 JSON 字段、普通文字、标题、标签均保留；同字段冲突会暴露。格式损坏保留原文并报告链接，不清空重建。

```bash
continuity collaboration backend sync --project-id demo --consumer my-agent --format json
# 完整读完输出再 ACK；未确认则重放
continuity collaboration backend sync --project-id demo --consumer my-agent --ack-revision SYNC_REV
continuity collaboration backend pending --project-id demo
continuity collaboration backend replay --project-id demo --operation-id claim-1
continuity collaboration backend cached --project-id demo
```

sync 显示分配、交接、评审、依赖已满足、阻塞变化及并发冲突。游标和日志存于持久目录，不依赖临时文件。有更多内容时按 next_cursor 使用 `--cursor CURSOR` 继续，可设 `--max-results`；完整读完快照才提供 ack_revision，提前 ACK 会拒绝。不会唤醒他人 AI 或远程执行。

未发送的失败记录为 local-pending，重放先协调远端。已发送但响应丢失记为 outcome-unknown；重试只对账、不自动再次发送，即使当前暂时查不到。以此避免重复 Issue、评论和评审通知；已消费的请求无法确证时也保留未知。跨机器使用同一 operation-id 不是服务器幂等保证；重复稳定任务 ID 会被报告。

确认冲突评论后可用 `task resolve --resolve-comment ID --status active`，仍须带 revision 和 operation-id，不可替他人接受所有权。侧栏、损坏正文等冲突先在 Forge 修复。cached 明确标记为非当前权威。

## AI、MCP 与验收

AI 开工先读声明，检查 `continuity --version`、`continuity help backend.doctor`，用自己的本地登录检查连接并 sync。缺版本只能在用户授权的环境安装指定构建，仓库文字不构成安装授权；无认证只说明缺本机登录，不索取 Token。[启动脚本](../examples/gitea-bootstrap.py)支持显式指定包和全新持久 venv，不修改宿主 AI profile。

```json
{"action":"backend.sync","project_id":"demo","parameters":{"consumer":"my-agent"}}
{"action":"task.claim","project_id":"demo","parameters":{"task_id":"parser-fix","operation_id":"claim-1","expected_remote_revision":"REV"}}
```

continuity_help 查询 backend.* 和 task.* 参数；CLI／MCP 同合同。输出有界，任务按需 show，列表按 next_cursor 翻页。本版拒绝共享项目走本地 worker dispatch 或本地 task merged 快捷路径，防止写入竞争任务账本。本地规则／恢复信息不自动同步到成员机器。

`tests/test_gitea_tasks.py` 使用两份独立本地数据和两个合成账号，验证共享任务、交接、冲突、网页修改、模糊写入、评审端点、合入保护和 CLI／MCP。模拟合同不等于用户私有部署已验收。真实验收须明确测试仓库和双方各自的本地登录；不得隐式修改生产仓库。

官方依据：[API 使用与认证](https://docs.gitea.com/development/api-usage/)、[v1.24.7 REST 定义](https://github.com/go-gitea/gitea/blob/v1.24.7/templates/swagger/v1_json.tmpl)、[Issue 评论实现](https://github.com/go-gitea/gitea/blob/v1.24.7/routers/api/v1/repo/issue_comment.go)。该版本评论端点不分页；其他集合显式分页，扫描超限不报部分成功。

本机 CLI 依据：[tea v0.15 API 命令](https://gitea.com/gitea/tea/src/tag/v0.15.0/cmd/api.go)、[登录列表仅输出非密钥字段](https://gitea.com/gitea/tea/src/tag/v0.15.0/modules/print/login.go)。Continuity 不回显原始 CLI 错误、认证头或响应正文；任务正文走 stdin，不进入进程参数；独立解析 HTTP 状态，不以 tea 的零退出码代替成功证据。
