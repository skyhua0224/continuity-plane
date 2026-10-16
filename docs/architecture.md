# 架构与权限边界

[English](architecture.en.md) · [部署](deployment.md) · [验证范围](validation.md)

Continuity 是 provider-neutral 的开发工具，不是业务项目的运行时依赖。CLI 是共同入口，MCP、插件、hook 和现有 SSH 脚本可调用同一组能力。

```text
CLI / MCP / host hook
  ├─ collaboration registry → cards, changes, rules, reports, handoff
  ├─ supervisor             → managed attempts, output capture, resume
  ├─ retrieval / references → bounded code routes and source declarations
  └─ optional State service → authorization, revision/CAS, claim, checkpoint
```

## 两类状态，不混用门禁

协作卡、Todo、收件箱、规则、经验和批次是项目侧协作记录。它们不要求先建立 State claim，也不自动授权推送、合入或部署。主控 fence 可在项目上显式启用，协调工具控制的派发与集成。

Typed State 是另一条权威操作边界：source、可信请求上下文、expected revision/CAS、validator、claim 与 checkpoint 用于其受控提交。检索、报告和模型结论只能提供候选证据。它不应被误用为每次普通项目操作前的强制仪式。

## 本地优先与远端执行

默认 State 使用项目内 SQLite；协作数据与缓存可在用户数据根。无需服务进程。远端使用明确的 SSH CLI／MCP 端点；hook bridge 映射的是项目路径，不迁移 native account/session 状态。

默认安装不包含完整 hosted shared-strong 服务、Docmost 控制台或 OS sandbox。PostgreSQL、forge、Docmost 和 Obsidian 相关 adapter／投影有各自的部分实现与部署条件，见 [State](state-workflows.md)、[项目视图](project-views.md)及[图形产品](visual-products.md)。

## 运行与证据

supervisor 包装已配置适配器，记录 attempt 的开始、心跳、结束、退出码和报告产物。主控自己决定任务适配、批准与合入。原生 Claude 子代理回调是观察；无法据此控制其进程、证明心跳或推断成功。

有界 packet 是启动入口，详细产物按引用读取。规则交付、卡片正文和代码路标分别标注来源；附件 hash 证明内容身份，不证明内容正确。共享计划和 lesson 也不提升为新的用户授权。

## 故障与隔离

- 原子写在目标目录暂存并同步；持久文件系统仍需要备份。
- attempt 结束与卡片投影有迟到结果保护；I/O 补写保留记录，但不覆盖新的执行绑定。
- 主控租约和操作预留只保护经过工具的合作客户端，不阻止任意 Git、脚本或原生 Agent 创建。
- 缺失 metadata 时普通授权工作继续；失去 fence 的受保护写入被拒绝并保留原因。
- 凭据、原生会话与 provider 查询留在所属主机/profile；公开证据只输出经过允许的聚合字段。
- 当前已知的报告挂载、重连归属、来源 TSV 和检索自动映射缺口在验证矩阵列出，不以架构图宣称完成。
