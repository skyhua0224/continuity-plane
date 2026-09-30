# M10-17 多项目 Session binding 验收

## 范围

本验收固定一个 Agent Session 同时服务多个独立 Project State 的边界。项目身份不再
由进程 `cwd` 推断；Session 必须通过成功的显式 `continuity_resume(root=...)` 建立或
切换项目 binding。治理根、实现仓和外部 delivery workspace 仍各自保持 repository、
claim、lease、revision 与副作用权限边界。

## 合同

- binding 使用 `context.codex-session-project-bindings/v1alpha1`，保存 Session 摘要、
  已绑定项目集合、active root、每个项目 profile digest 和整体 binding digest；原始
  Session ID 与对话正文不进入 State 或 Git。
- MCP 服务启动时不从 `cwd` 预绑定。`continuity_resume` 的显式绝对 root 成功后才
  加入项目集合，并成为 active root；同一 Session 可再次显式 resume 另一已安装项目。
- 相对 root 只能相对上一次成功的 active root 解析。未绑定 root、project profile
  缺失、digest 失配或 binding 损坏在调用 CLI/State/Effect 前返回稳定拒绝，不回退到
  当前 `cwd`。
- 生命周期写操作始终使用请求中的已绑定 root；切换项目不共享另一项目的 claim、
  checkpoint 或 revision。一个项目内的治理根与外部 delivery workspace 由该项目
  的 workspace registry 进一步绑定。

## 验收证据

| 门 | 结果 |
|---|---|
| strict binding schema、registry hash、公开构建清单 | 通过 |
| MCP 显式 root 绑定、跨 root 切换和未绑定写拒绝 | `46/46` focused tests 通过 |
| hook binding 的 `0600`、profile digest、legacy 单根迁移和损坏 fail-closed | 通过 |
| packaged MCP 与 plugin MCP 合同一致 | 通过 |
| 只读请求不触发 State 写入或额外副作用 | 通过 |
| 真实项目状态、业务仓库和 SQLite | 本验收未写入、未修改 |

## 使用入口

在跨项目 Session 中，先对每个需要工作的治理根执行一次显式 resume：

```text
continuity_resume(root=/path/to/project-a)
continuity_resume(root=/path/to/project-b)
```

之后的 Work、claim、checkpoint 和 effect 请求必须带对应项目 root。不要依赖当前终端
目录来切换项目，也不要把一个项目的 claim ID 复制到另一个项目。

## 未宣称事项

本验收证明路由和权限隔离，不证明跨项目 Work 自动合并、跨项目唯一 claim 或任何业务
仓的完成门。共享唯一 claim 仍需项目选择 `shared-strong` 或受支持的 forge adapter。
