# 给人和 AI 的开工入口

[English](agent-start.en.md) · [安装与初始化](../USAGE.md) · [部署](deployment.md)

先找到当前任务、下一步和验证入口。CLI、SSH 与 MCP 都能用；已有卡片与交接足够时，无需另外维护 Todo、重复 claim 或发送收件箱消息。

## 已注册项目：四条命令

存在 `.continuity/collaboration.toml` 且 backend 为 gitea-issues 时，先按[Gitea 开工流程](gitea-collaboration.md)检查版本、自己的认证和 backend sync。身份来自人类 Gitea 账号，不使用下方本地 agent assignee 认领，不要求固定主控。缺版本只能经用户授权安装，不能索取 Token。

主控交接或更换 Claude／Codex 主控时，先运行 `continuity controller start --project-id PROJECT --member LOCAL_MEMBER --format brief --max-output-bytes 4096`。它合并有界计划、下一步、报告／提问／工具 profile 和获批记忆；私人来源仍需本机授权，共享服务器只用 `--public-only`。详见[主控连续性](controller-memory.md)。

在项目目录执行，把 demo、agent-1、controller 换成实际项目、独立执行者实例和主控。并发 worker 不共用身份。

```bash
continuity --version
continuity whoami --root . --assignee agent-1 --controller controller
continuity collaboration packet --project-id demo --assignee agent-1
continuity handoff show --project-id demo
```

Alpha36 的 CLI 是 `0.1.0a36`，插件是 `0.1.0-alpha.36`。未注册的项目先走[初始化](../USAGE.md#初始化项目)，不覆盖已有 `.continuity/`。whoami 不登录 provider，也不授予主控租约。

## 可直接贴进简报

> 使用本项目的 Continuity 卡片与共享交接。先核对项目、稳定执行者身份和派给我的任务。已有 source-fresh、checkpoint-verified 恢复包时从中接续；否则只读有界 packet/handoff 及必要引用，不重读完整历史。不要拿别人的 queued 卡、强行 claim 或 force。符号检索先试一次 bounded lookup，空命中后窄范围搜索。实现前读适用的一手资料，记录版本、章节和采用理由；导入索引不代表已读。收尾挂最终报告、commit、实际检查与未验证项；交接下一步、在途工作和待决事项。元数据状态不授权推送、部署或动密钥。

不要求每个回复都宣告“用了工具”；只在事件变化、阻塞或需决策时提醒。无卡时按已约定的任务继续或请求分配，不凭工具猜任务。

## 按需展开

```bash
# 按当前任务选择，不要求逐项执行
continuity help task.update
continuity context lookup --root . --query YOUR_SYMBOL --format brief --max-output-bytes 1024
continuity rules export --project-id demo --audience worker --format text
continuity todo from-cards --project-id demo
```

替换符号名。规则使用完整导出和哈希附件；卡片 Todo 只作视图。来源索引可用 reference import-tsv/export-tsv 显式往返，冲突不自动覆盖；真实阅读／采用再用 reference record，不捏造证据。[角色工作流](workflows.md)包含派发和收尾示例。

MCP 用 continuity_help 查同名操作；continuity_plan 使用 action 与 parameters，例如：

```json
{"action":"task.list","project_id":"demo","format":"brief","parameters":{"assignee":"agent-1"}}
```

每次使用相同 executor_id，或在 server 环境配置 CONTINUITY_ASSIGNEE；connection/session ID 不作为执行者。MCP 未连接就用 CLI/SSH，普通计划元数据不要求 State claim。

## 安装验收

- 分别检查 CLI、插件缓存与新 MCP 握手。pip 升级不自动刷新插件缓存；旧 tag 需按[部署步骤](deployment.md#codex-plugin-与-mcp)重设。
- 每个实际使用的 Claude profile 单独配置、验收 hook 和 MCP；不能只更新默认目录。
- 旧会话可能需重连／新开。不要为升级杀掉在途 worker；配置存在不等于自然压缩事件已验证。
- 原生账号、会话和使用量在所属机器／profile 原地处理；只共享获准项目资料，不搬账号目录。

按[能力矩阵](validation.md)区分已验证与未知范围。
