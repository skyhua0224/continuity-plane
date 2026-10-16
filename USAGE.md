# 使用说明

[English](USAGE.en.md) · [README](README.md) · [部署与升级](docs/deployment.md) · [完整文档](docs/README.md)

## 先选你的路径

| 想做什么 | 起点 |
|---|---|
| 一个人，压缩后接着做 | 下方初始化与接续 |
| 主控派发、等待与收报告 | [两个执行方示例](docs/workflows.md#一个主控与两个执行方) |
| 多人或多机共享项目 | [团队路径](docs/workflows.md#一个团队)与[SSH 部署](docs/deployment.md#控制端与状态端分离) |
| 已有 MASTER／STATUS 或需要 State claim | [State 高级接入](docs/state-workflows.md) |

## 安装

Python 3.11+。推荐隔离安装；不要把系统 Python 的权限问题当作项目故障。

```bash
python3 -m venv .venv
.venv/bin/python -m pip install 'continuity-plane==0.1.0a26'
```

Windows 使用 `.venv\Scripts\python.exe` 和 `.venv\Scripts\continuity.exe`。以下用 `continuity` 表示环境内的命令；可激活环境或使用完整可执行路径。全局 pipx、升级、离线 wheel 与回滚见[部署指南](docs/deployment.md)。

## 初始化项目

在自己的仓库执行：

```bash
continuity init --root . --project-id demo
continuity collaboration register --project-id demo --control-root . --repository-root .
continuity whoami --root . --assignee controller --controller controller
continuity collaboration packet --project-id demo --assignee controller
```

`init` 创建项目内 `.continuity/`，含 project.yaml、State SQLite 和治理模板；`register` 让项目进入用户级协作注册表。两者用途不同。不要重复 init 或覆盖已有计划；既有项目先阅读 State 接入指南。

`whoami` 显示本次身份，不会替你创建租约或登录 provider。长期 shell 可设置 `CONTINUITY_ASSIGNEE`、`CONTINUITY_CONTROLLER`；每名并发执行方使用独立名字或实例名。

## Agent 接入

给人或 AI 的四条开工命令与可粘贴简报见[开工入口](docs/agent-start.md)。

CLI 能用不代表 hook 已启用。按[部署指南](docs/deployment.md)选择 Codex、Claude Code 或通用 MCP，检查实际工具表与 hook 事件。保持 provider 的原有登录／路由，不复制整份配置。

## 压缩与接续

把接续需要的增量写进共享现场，而不必维护长篇状态报告：

```bash
continuity handoff write --project-id demo --plan '先完成当前回归，再处理下一项' --next-action '运行当前任务的测试'
continuity handoff show --project-id demo
continuity lesson add --project-id demo --lesson-id check-logs --text '先核对失败检查，再判断是否负载问题' --source '本次回归记录'
```

安装并实际启用 hook 后，压缩前保存 session 接续点，压缩后按宿主合同恢复。Claude 在 SessionStart(compact) 注入；PostCompact 只验证。缺少 hook 时不会自动发生。共享交接刷新失败会告警，不能把旧共享计划当作新快照。

## 协作

卡片是工作清单，Todo 可从卡片生成；无需维护第二份过期队列。

```bash
continuity todo from-cards --project-id demo
continuity collaboration task list --project-id demo --status dispatched --format brief
continuity collaboration changes --project-id demo --since 0
continuity collaboration wait --project-id demo --since 0 --event attempt.finished --event attempt.timeout --timeout-seconds 30
```

保存返回的 `next_cursor`，后续用它增量读取；`truncated` 为真时继续翻页。`wait` 超时非零不代表 worker 失败。收件方必须读取并 ack；信箱不是进程唤醒器。完整派卡、收尾、报告与合入证据见[工作流](docs/workflows.md)。

## 少读代码

```bash
continuity context index --root .
continuity context lookup --root . --query YOUR_SYMBOL --format brief --max-output-bytes 1024
continuity context dependents --root . --query YOUR_HEADER --format brief
```

替换符号／头文件名。检索基于当前 Git tracked 文件；空命中后做窄范围搜索。反向依赖是已索引关系，不是完整编译器调用图。不是每个旧子命令都接受同一组参数，先查 `--help`，不要给 packet 添加未提供的 budget 参数。

## 规则与资料

使用规则作为唯一来源，脚本中的 common.txt 只当生成物；长文走哈希附件。

```bash
continuity rules export --project-id demo --audience worker --format text
continuity reference list --root .
continuity help reference.add
```

登记当前版本的标准、OS／库官方文档、已读章节与采用理由。资料记录是来源声明，不证明实现已正确；读不到来源就标明未验证范围，不凭模型记忆宣称最优。

## 计量与验收

```bash
continuity observe report --root .
continuity help
```

报告区分返回字节、hook 开销、估计替代量和宿主 token／缓存。负收益保留；费用、窗口或来源不明时不补造数字。当前能力和本轮合成实测见[验证矩阵](docs/validation.md)。

## 存储

State、协作注册表、索引缓存和宿主会话不是同一份数据。默认路径与 `--data-root`、环境变量、远端隔离见[部署指南](docs/deployment.md)。不要用复制账号目录代替远端执行，不把可恢复日志、报告和会话引用放在易失临时盘。

## 备份与移除

先备份项目 `.continuity/`、用户级协作数据根与自定义产物，再卸载软件；卸载软件不等于删除记录。State bundle 的 export/import/rollback 仅覆盖其声明范围，不是整套协作与原生账号的备份，见[State 指南](docs/state-workflows.md)。

## Release 与 PyPI

当前版本：`0.1.0a26`；Git tag：`v0.1.0-alpha.26`。[GitHub Releases](https://github.com/skyhua0224/continuity-plane/releases/latest) 统一使用普通 Release；alpha 版本号仍表示兼容性可能变化，PyPI 选择它仍需明确 pin 或 `--pre`。

[变更记录](CHANGELOG.md) · [发布与升级约定](docs/releases.md)
