# 发布、版本与说明约定

[English](releases.en.md) · [变更记录](../CHANGELOG.md) · [部署与升级](deployment.md)

## Release 分类与 alpha 版本号

GitHub 上已发布的版本统一使用普通 **Release**，当前版本 Alpha26。包版本为 `0.1.0a26`，tag 为 `v0.1.0-alpha.26`；新版本发布时标为 Latest。这不把代码宣称为生产稳定版，也不改变 PyPI 的预发行排序规则。

升级使用明确版本约束，或在 pipx/pip 中显式允许 `--pre`。旧 tag、commit、发行资产与包内容不因分类／标题整理而重写。历史 Release 只修正分类和面向用户的说明，原发布年代与兼容范围保留。

## 标题

格式：`Continuity Plane Alpha N · 用户可感知的主题`。近期版本：

| 版本 | 主题 | 说明 |
|---|---|---|
| Alpha26 | 顺手协作与可靠报告 | [本版本](release-alpha26.md) |
| Alpha25 | 主控交接与可靠派发 | [本版本](release-alpha25.md) |
| Alpha24 | Worker 闭环与规则交付 | [本版本](release-alpha24.md) |
| Alpha23 | 双端接续与执行建议 | [本版本](release-alpha23.md) |
| Alpha22 | 有界检索与协作视图 | [本版本](release-alpha22.md) |
| Alpha21 | 计划录入与消息交接 | [本版本](release-alpha21.md) |

标题不放设备、私有项目或内部任务编号；不堆砌 commit 名或夸大“全面最优”。

## Release note 模板

1. **这一版带来什么**：从用户动作写变化，说明适合谁。
2. **改进与修复**：列可核对行为，避免把设计计划写成已实现。
3. **升级前注意**：兼容、schema、宿主重连、数据和回滚范围。
4. **安装／升级**：本版本的明确命令，区分包和插件。
5. **验证与限制**：本版本证据、失败／跳过、真实回归与合成／历史实验分别标注。
6. **文档入口**：链接部署、使用、当前能力和完整 changelog。

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
