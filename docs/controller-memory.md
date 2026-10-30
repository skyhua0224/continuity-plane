# 主控连续性与分域记忆

版本：1；适用 Alpha37。此功能保存可恢复的工作方式、交流习惯和获批项目知识，不读取或复制原生账号、Token、完整会话或目录树。

## 开工入口

主控替换时先运行：

```bash
continuity controller start --project-id demo --member alice --format brief --max-output-bytes 4096
```

返回 `controller-start/v1`：handoff revision、计划、下一步、待决事项、有效 profile、获批私有记忆、仓库共享知识、冲突和指令边界。它不刷新大仓库现场或启动 provider；当前 worker／报告事实用 `handoff show` 等有界查询。

内容超出预算返回 `needs-expansion`，不写恢复 ACK。完整且仍是当前版本时可传 `--ack-revision REV --profile-sha256 HASH`。ACK 表示已接收，不表示理解、批准或获得外部操作权限；并发更新后旧 revision 不可确认。

## 分域

| 信息 | 位置 | 生效规则 |
|---|---|---|
| 项目工作约定 | 仓库 `.continuity/controller-profile.json` | `defaults` 生效，`locked_fields` 禁止个人覆盖 |
| 成员私人习惯 | 本机 `private-memory/<owner>/profiles/` | global 再 project，服从项目锁定 |
| 获批项目知识 | 私人记录；显式导出到 `.continuity/knowledge/` 后人工评审 | Git 提交后才成为团队视图 |

固定字段：language、report_format、report_triggers、question_policy、tool_preferences、model_preferences、integration_base。它们是工作方式提示，不能授予工具、合并、部署、账号或凭据权限；宿主和现行项目指令优先。

```json
{
  "defaults": {"language": "zh", "report_format": "结论先行，区分已验证与未验证", "question_policy": "仅阻塞选择使用宿主问答工具"},
  "locked_fields": ["report_format"]
}
```

```bash
continuity profile set --project-id demo --member alice --patch-file profile.json --expected-revision 0
continuity profile show --project-id demo --member alice --format brief
```

`--public-only` 只读仓库 profile，适合共享服务器／SSH bridge；私人读取只能在所属机器执行。

## 记忆记录

记录先为 `candidate`，不自动注入。成员审批后才进入恢复包；撤销或过期立即退出。历史 revision 保存在成员本机，使用 owner-only 文件权限。

```bash
continuity memory note --project-id demo --member alice --record-id report-style \
  --kind preference --scope private-project --source user-instruction \
  --text '阶段完成先给结论，再区分已验证、未验证和待决项'
continuity memory adopt --project-id demo --member alice --record-id report-style \
  --expected-revision 1 --reason '用户明确采用于本项目'
continuity memory notes --project-id demo --member alice --format brief
continuity memory share --project-id demo --member alice --record-id report-style \
  --expected-revision 2 --reason '团队共同遵守的交流要求'
```

`private-global` 在该成员所有项目可见；`private-project` 仅当前项目。不同 member／项目默认隔离。同 OS 用户的 Agent 仍需宿主 sandbox 才能形成进程隔离；`.gitignore` 是准入规则，不是访问控制。

share 只创建带内容哈希的 `.continuity/knowledge/RECORD.HASH.json`，返回 `exported-for-review`，不 stage、commit 或推送。人工检查后才显式提交该文件。

## 原生笔记

当前互通是只读候选导入。授权绑定当前成员、项目、原生机 profile 和具体 `.md`／`.txt`；不递归发现、不跟随链接。auth、settings、config、session、rollout、history、secret、token 等账号／会话来源禁止。

```bash
continuity memory grant --project-id demo --member alice --consent-id notes-v1 \
  --provider codex --source-file /owning/profile/approved-note.md
continuity memory ingest --project-id demo --member alice --consent-id notes-v1
continuity memory notes --project-id demo --member alice --include-candidates --format brief
continuity memory consent-revoke --project-id demo --member alice --consent-id notes-v1
```

一次授权后，所属机器上的 `controller start` 会自动增量刷新这些精确文件，无需每次手动 ingest；刷新失败只报告来源不可用，不阻塞计划恢复。导入内容始终是候选 `lesson`，不是可执行指令。撤销后不再读取或注入。写回原生记忆、跨设备／成员迁移和内容分享都要分别授权；Alpha35 仍不修改原生文件，也不宣称双向写回。

本地 stdio MCP 默认拒绝私人 memory/profile/controller.start。所属主机明确设置 `CONTINUITY_PRIVATE_MEMORY_MCP=1` 才可调用，且不能指定其他 member。共享服务器只用 `--public-only`。

## 交接与故障

`handoff write` 默认快速保存声明，不同步扫描大仓库；`handoff show` 现算当前事实，`--auto` 才生成观察草稿。`--capture-member alice` 把私人 profile 快照存入成员本机，不进入共享 handoff。

`collaboration inbox archive` 只移动已确认消息，未确认消息保留；归档按 message ID 保留，重复 send 不再制造通知。额度／限流／容量只据已观察 worker 错误暂停对应 origin／model，未知 quota reset 不自动重试或伪造时间。

```bash
continuity dispatch-health show --project-id demo --format brief
continuity dispatch-health clear --project-id demo --provider codex --model MODEL --reason '已核对新的运行结果'
```

预留冲突报告 job、owner、时间和 duplicate/stale 类型。`task batch-update --file` 顺序处理最多 50 项，逐项 revision 与 operation ID，失败即停，不声称整批事务。

验收看可观察行为：新主控继续同一 next_action，沿用报告／提问／工具习惯，不重复已答问题，不把候选或过期记忆变成批准。不能承诺模型内部状态逐字复现。记忆不能扩大外部权限、改变人类负责人或替代 Issue／PR 权威。
