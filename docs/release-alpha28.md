# Alpha28 · 兼容现有 Gitea 登录名称

Existing Gitea CLI login-name compatibility

包 `0.1.0a28`；tag／插件 `v0.1.0-alpha.28`／`0.1.0-alpha.28`。普通 Release；保持 alpha 兼容阶段。

## 改进与升级

Alpha27 原生 Gitea 后端的 credential_ref 校验过窄，拒绝了本机 tea 已有的空格、冒号或 Unicode 登录名称。本版允许 1–256 UTF-8 bytes 的实际别名，拒绝空值、首尾空白及控制字符；CLI 仍精确匹配已登录的实例，使用单独、显式的参数值。以连字符开头的名称也不会被解释为命令选项。

无需修改已有 tea 登录名，更无需复制 Token、账号配置或原生会话。项目 TOML 可直接使用原有别名：

```toml
credential_ref = "tea:Company Gitea:3443"
```

```bash
python -m pip install --upgrade 'continuity-plane==0.1.0a28'
continuity --version
continuity collaboration backend doctor --project-id demo --format json
```

已有 pipx 环境可执行 `pipx upgrade continuity-plane --pip-args=--pre`；插件跟进同一 tag 后刷新宿主／重连 MCP。不覆盖已发布 Alpha27 包或 tag，不自动更改项目权限、保护或驱动者。

回归覆盖空格／冒号／Unicode／连字符名称、原机实例绑定和无效字符拒绝；完整测试、构建与本版采用者 receipt 见 CI／验证文档。跨成员任务权威、无强锁认领、离线未知结果等边界延续[Alpha27](release-alpha27.md)。[配置规范](collaboration-storage.md)与[Gitea 工作流](gitea-collaboration.md)适用。

## English

Alpha27 rejected existing tea login aliases containing spaces, colons or Unicode. Alpha28 accepts real 1–256 UTF-8-byte aliases, rejects empty/surrounding whitespace/control characters, keeps exact origin binding, and passes explicit argument values so leading hyphens cannot become CLI options. No login rename, token extraction, account transfer, server permission change or schema migration is required.

Install the exact pin above, or upgrade pipx with --pip-args=--pre; refresh matching plugins and reconnect MCP. Existing Alpha27 packages/tags remain immutable. Regression coverage includes alias compatibility and invalid-input rejection; CI and the refreshed adopter receipt record full validation. [Storage contract](collaboration-storage.en.md), [Gitea workflow](gitea-collaboration.en.md), and Alpha27's coordination/offline guarantees remain applicable.
