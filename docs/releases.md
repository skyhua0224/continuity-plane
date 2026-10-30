# 发布、版本与说明约定

[English](releases.en.md) · [变更记录](../CHANGELOG.md) · [部署与升级](deployment.md)

## Release 分类与 alpha 版本号

GitHub 上已发布的版本统一使用普通 **Release**，当前版本 Alpha37。包版本为 `0.1.0a37`，tag 为 `v0.1.0-alpha.37`；新版本发布时标为 Latest。这不把代码宣称为生产稳定版，也不改变 PyPI 的预发行排序规则。

升级使用明确版本约束，或在 pipx/pip 中显式允许 `--pre`。旧 tag、commit、发行资产与包内容不因分类／标题整理而重写。历史 Release 只修正分类和面向用户的说明，原发布年代与兼容范围保留。

## 标题

格式：`Continuity Plane Alpha N · 用户可感知的主题`。近期版本：

| 版本 | 主题 | 说明 |
|---|---|---|
| Alpha37 | 换模型不丢进度、队列真正运行 | [本版本](release-alpha37.md) |
| Alpha36 | Agent 主动使用、更省 token | [本版本](release-alpha36.md) |
| Alpha35 | 可观察派发与可恢复等待 | [本版本](release-alpha35.md) |
| Alpha34 | HTTP 拦截与模型内容拒绝分流 | [本版本](release-alpha34.md) |
| Alpha33 | 派发热路径与历史规模解耦 | [本版本](release-alpha33.md) |
| Alpha32 | 主机恢复、依赖与可解释调度 | [本版本](release-alpha32.md) |
| Alpha31 | 可靠 inbox 契约与结构化 MCP 错误 | [本版本](release-alpha31.md) |
| Alpha30 | 可靠派发队列与可行动交接 | [本版本](release-alpha30.md) |
| Alpha29 | 主控连续性与分域记忆 | [本版本](release-alpha29.md) |
| Alpha28 | 兼容现有 Gitea 登录名称 | [本版本](release-alpha28.md) |
| Alpha27 | 私有 Issue／PR 协作与可靠接手 | [本版本](release-alpha27.md) |
| Alpha26 | 顺手协作与可靠报告 | [本版本](release-alpha26.md) |
| Alpha25 | 主控交接与可靠派发 | [本版本](release-alpha25.md) |
| Alpha24 | Worker 闭环与规则交付 | [本版本](release-alpha24.md) |
| Alpha23 | 双端接续与执行建议 | [本版本](release-alpha23.md) |
| Alpha22 | 有界检索与协作视图 | [本版本](release-alpha22.md) |
| Alpha21 | 计划录入与消息交接 | [本版本](release-alpha21.md) |

标题不放设备、私有项目或内部任务编号；不堆砌 commit 名或夸大“全面最优”。

## Release note 模板

从 Alpha36 起，Release 说明按下面的格式写。读者是准备升级的用户：先让他们知道这一版解决了什么，再告诉他们要做什么。

````markdown
# Continuity Plane Alpha N · 一句话主题

一段话：这一版主要解决什么问题，谁最受益。

> **升级后请重连 MCP 或新开会话。**（以及其他必须知道的事，加粗，放最前）

### 🤖 主题一（如“Agent 主动使用”）

- 新增 X：做什么的时候，现在可以怎样。
  Added X: when you …, you can now ….
- X 现在会 Y（以前 Z）。
  X now does Y (it used to Z).
- 修复 在什么情况下 出现什么问题。
  Fixed … when ….

### 🧰 主题二 …

### 📊 实测

环境、数据规模、方法（工具、次数）、中位数和 p95；有对照组时写清对照。复现命令或脚本路径。

### ⚠️ 已知限制

- 每条一句。

### 升级

```bash
pipx install --force 'continuity-plane==0.1.0aN'
```

完整变更：CHANGELOG 链接 · 文档链接
````

规则：

- 每条更新中文一句、英文一句，事实一致；主题用 emoji 小标题分组。
- 写用户能感知的行为。内部文件名、类名、实现路径放到 CHANGELOG 或提交记录。
- 实测只放本版本新测的数据；旧实验写明版本和日期，放 [历史实验](benchmarks.md)。

中英文说明保持相同事实；安装和限制不得只出现在一种语言。最新文档链接指向 main；历史 tag 中没有的新文档不得伪造 tag 链接。

## 维护者发布检查

- 测试与构建绑定同一源码；公开导出通过隐私、链接、命令和打包检查。
- 核对 CLI、MCP、插件版本与安装入口；安装不等于原生 hook 已触发。
- 通过普通 commit/tag 发布，不 force 移动 tag；保留失败证据，不覆写已经发布的 PyPI 包。
- 创建新 GitHub Release 不加 prerelease 标记；将该新版本显式设为 Latest。
- 用已有 OIDC Trusted Publisher 发布；不把账号密钥写入源码或 Release note。
- 分类／说明修订只 PATCH 现有 Release，不删除重建或重新上传资产；保留修改前元数据，并核对 ID、tag、published_at、资产 ID／摘要未变。
- 观察 workflow 结果。旧版本已存在时不通过重复上传“修复”分类；如触发重复发布，应确认是已存在同版本，不覆盖包。
- 安装说明、回滚和已知限制先更新，再宣称版本可采用。无真实数据时不造节省率或成本。

API 与事件参考：[GitHub Releases API](https://docs.github.com/en/rest/releases/releases#update-a-release)、[Release workflow events](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#release)。
