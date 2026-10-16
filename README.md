# Continuity Plane

为 Codex、Claude Code 和其他 Agent 保存任务、规则、交接与验证证据；管理 worker 的派发、续跑和结束通知，并用有界检索减少重复阅读。

[English](README.en.md) · [快速开始](USAGE.md) · [部署与升级](docs/deployment.md) · [工作流](docs/workflows.md) · [能力与验证](docs/validation.md) · [下载](https://github.com/skyhua0224/continuity-plane/releases/latest)

[![PyPI 版本](https://img.shields.io/pypi/v/continuity-plane?include_prereleases)](https://pypi.org/project/continuity-plane/)
[![Apache-2.0 许可](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE)
[![由 Continuity Plane 管理](docs/assets/managed-with-continuity-plane.svg)](https://github.com/skyhua0224/continuity-plane)

## 它解决哪些问题

| 遇到的情况 | 现在可以怎么做 | 详情 |
|---|---|---|
| 压缩后忘了下一步，接手者又从头探索 | 有界接续点与主控交接：计划、批次、在途 worker、待决事项和验证入口 | [接续与 hook](docs/lifecycle-parity.md) |
| 多个 Agent 不知道彼此做到哪里 | 任务卡、收件箱、增量变更流；用 `wait` 等结束事件 | [协作](docs/workflows.md) |
| worker 悄悄退出，报告埋在日志里 | `dispatch` / `resume` / `report`；supervisor 记录 attempt、退出码和挂卡报告 | [派发参考](docs/field-workflows.md#dispatch-resume-and-reports) |
| 规则散落、反复复制，执行方漏读 | 全局／项目规则、受众、哈希附件、完整导出、提示词组装与一手资料登记 | [规则与资料](docs/workflows.md#规则资料与教训) |
| 找一个符号读入几千行代码 | 增量索引、brief 输出、有界依赖查询；派卡自动附代码路标 | [检索边界](docs/validation.md) |
| 计划漂移、承诺丢失、合入证据说不清 | 卡片驱动 Todo、commit 绑定验证、批次／决定、commitment、lesson 与漂移检查 | [工作流](docs/workflows.md) |

核心是一份 CLI。没有插件也能使用任务卡、检索和协作；默认不需要云账号、PostgreSQL、Docmost 或常驻 daemon。原生 Agent 的登录仍由自己的客户端管理。

## 五分钟开始

需要 Python 3.11+。以下为 macOS/Linux shell；Windows 路径见[部署指南](docs/deployment.md)。

```bash
python3 -m venv .venv
.venv/bin/python -m pip install 'continuity-plane==0.1.0a26'
.venv/bin/continuity init --root . --project-id demo
.venv/bin/continuity collaboration register --project-id demo --control-root . --repository-root .
.venv/bin/continuity whoami --root . --assignee controller --controller controller
.venv/bin/continuity collaboration packet --project-id demo --assignee controller
```

在目标仓库运行，为真实项目选择稳定 ID。初始化拒绝覆盖已有 `.continuity/`；已有治理文档按[接入指南](docs/state-workflows.md)处理。后续示例假定 `continuity` 已在 PATH 中。

| 用途 | 从哪里开始 |
|---|---|
| 一个人，防止压缩与交接丢信息 | [快速开始](USAGE.md)，再接入宿主 hook |
| 主控 + 多个执行方 | [两个 worker 的完整路径](docs/workflows.md#一个主控与两个执行方) |
| 多机器、团队共享状态 | [SSH 部署与隔离](docs/deployment.md#控制端与状态端分离)，再考虑主控租约 |

## 核心能力

- **工作现场**：session 绑定任务／批次；交接、规则、承诺与 lesson 随包出现。权威集成 ref 与检出 HEAD 分开标注。
- **派发闭环**：从规则与卡片组装提示词，在 supervisor 下运行已配置适配器；新 attempt 默认总时长 24 小时、空闲 90 分钟，支持覆盖和 effort 透传。
- **故障与续跑**：按错误类别处理，有原生会话引用时可原线程续跑。策略拒绝不自动换渠道；失去租约或名额时保留原结果并明确延期。
- **协作与互斥**：筛选卡片、分页变更、多事件等待和持久消息；显式主控 fence 与原子容量预留保护经过工具的操作。
- **规则与证据**：规则不静默裁剪，长文使用哈希附件；资料登记、报告、结构化验证、批次指标与批准来源均可追溯。
- **可核对计量**：实际返回／注入字节、宿主 token 与提示词缓存分别报告；净估算允许为负，不把缓存命中冒充节省。

完整参数以 `continuity help` 和各命令 `--help` 为准；不要求先建立 State claim 才能使用普通协作命令。

## Codex 与 Claude Code

| 入口 | 提供什么 | 注意事项 |
|---|---|---|
| Codex plugin `continuity-plane` | 有界恢复与生命周期 hook | core 不注册 State MCP；安装不等于 hook 已被信任和触发 |
| Codex plugin `continuity-plane-search` | `continuity_context_lookup` | 可独立启用；空命中后继续窄范围搜索 |
| State plugin／显式 `continuity-mcp` | State 工具与 `continuity_plan` 等协作工具 | 协作元数据无需 State resume；权威写入仍有授权门 |
| Claude Code plugin | MCP 与 8 类宿主 hook | SubagentStart/Stop 是观察，不是原生进程控制 |
| 其他 Agent | CLI 或 stdio MCP | 不限定 provider 与模型 |

安装、工具发现、hook 验收、重复注册排查和卸载见[部署指南](docs/deployment.md)。运行中的会话不会热加载新 MCP；升级后重连或新开会话。

## 已测结果

[验证矩阵](docs/validation.md)区分合成 CLI 实测、自动回归、使用反馈和尚未覆盖项，覆盖派发、失败退出、报告、规则、事件、交接与互斥。

历史压缩、检索、Skill 和协作对照保留在[实测方法](docs/benchmarks.md)，不作为最新版或任意项目的统一降幅。窗口有效利用率、长期每任务费用和因果 token 节省仍需匹配真实会话数据；未知保持未知。

## 数据与操作边界

- 凭据、私有 provider 配置和原生会话库留在所属机器／profile。远端访问执行远端命令，不搬账号到另一台机器查询。
- 仅共享获准的项目记录、产物引用与带来源计数。可恢复数据放持久盘，临时目录只放可重建缓冲。
- 收件箱与 hook 不保证唤醒任意会话；报告不自动授予合入、推送或部署权限。
- 主控租约协调合作客户端，不拦截绕过工具的 Git／脚本，不是 OS sandbox。
- 首次 `controller acquire` 启用该项目的 guarded ledger v4；先升级所有参与派发的客户端。安装本身不启用它。

## 文档

| 需要什么 | 入口 |
|---|---|
| 安装、升级、跨系统、远端 hook、回滚 | [部署与升级](docs/deployment.md) |
| 个人／主控／团队三条路径 | [使用说明](USAGE.md) · [工作流](docs/workflows.md) |
| 当前能力、实测方法、未验证项 | [验证矩阵](docs/validation.md) · [历史实验](docs/benchmarks.md) |
| 协作全参数、State 高级用法 | [命令参考](docs/field-workflows.md) · [State](docs/state-workflows.md) |
| 变更、兼容与发布约定 | [变更记录](CHANGELOG.md) · [发布规范](docs/releases.md) |
| 架构、配置、API 与可选视图 | [文档索引](docs/README.md) · [项目视图](docs/project-views.md) · [可选图形产品](docs/visual-products.md) · [使用场景](docs/use-cases.md) |

## 反馈与许可证

[提交 Issue](https://github.com/skyhua0224/continuity-plane/issues)时附版本、脱敏命令、预期／实际行为及最小复现。不要上传原始会话、账号文件、设备标识或私有项目数据。

采用 [Apache-2.0](LICENSE)，归属要求见 [NOTICE](NOTICE)。README badge 与遥测均非强制。
