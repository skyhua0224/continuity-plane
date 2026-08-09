# External Skill and MCP Catalog Assessment

版本：1  
日期：2026-08-09  
状态：research / candidate catalog  
范围：Codex/OpenAI、Claude/Anthropic、Agent Skills、GitHub Copilot、MCP 官方 registry 和 Skills.sh

## 结论

外部 Skill 与 MCP catalog 可以显著扩展项目初始化和协作能力，但 catalog 条目只提供候选发现。任何条目都必须记录 publisher、source URL、commit/version、content hash、license、依赖、工具权限、适用范围和验证结果；未经批准不得 active。运行时禁止从未固定 revision 的远端地址直接加载 Skill 正文或脚本。

## 官方来源快照

| 来源 | 2026-08-09 只读快照 | 许可边界 | 控制面用途 |
|---|---|---|---|
| [OpenAI Build skills](https://learn.chatgpt.com/docs/build-skills) | Codex 支持显式/隐式激活、repo/user/admin/system scope、progressive disclosure；初始 Skill 列表最多占上下文 2% 或 8,000 字符 | 具体 Skill/Plugin 单独审查 | Provider adapter、metadata budget、implicit invocation policy |
| [openai/plugins](https://github.com/openai/plugins) | commit `11c74d6ba24d3a6d48f54a194cd00ef3beea18f9`；182 plugins、607 `SKILL.md` | manifest 或 plugin license 单独记录 | Codex 官方当前 plugin/skill catalog |
| [openai/skills](https://github.com/openai/skills) | commit `49f948faa9258a0c61caceaf225e179651397431`；44 `SKILL.md`；README 已标记 deprecated | 每个 Skill 目录独立 `LICENSE.txt` | 历史兼容和迁移参考，不作为新接入主入口 |
| [anthropics/skills](https://github.com/anthropics/skills) | commit `f17010c9bb483898c1d9c9f42dde2b3a98889434`；17 `SKILL.md` | 多数示例 Apache-2.0；文档类 Skill 含 source-available 条款；逐目录审查 | Claude adapter、Skill creator/MCP builder/web testing 模式 |
| [Agent Skills specification](https://agentskills.io/specification) | repo commit `217be548739f21d6008915c29aefe320ea1a90af` | 代码 Apache-2.0；文档 CC-BY-4.0 | Provider-neutral 最低兼容格式与三阶段 disclosure |
| [github/awesome-copilot](https://github.com/github/awesome-copilot) | commit `ab7544d03d4c49fdd07f5958e1888ad39c4118e2`；419 Skills、192 instructions、226 agents | repo MIT；条目仍需依赖和远端引用审查 | 治理、证据、供应链、评估和代码检索候选 |
| [Official MCP Registry](https://registry.modelcontextprotocol.io/) | registry repo commit `f36b7dd4afe2d540a4ceb9b64d3627085bf5db03`；v0.1 API freeze，仍处 preview 演进期 | 每个 server 独立许可和 publisher ownership | MCP 发现源；不能等同生产认可 |
| [MCP reference servers](https://github.com/modelcontextprotocol/servers) | commit `76d64c822f5125032f89eb71dbdb94e42b434821`；7 个 reference servers | Apache-2.0/MIT 迁移中 | 协议和故障测试参考；官方明确提示不保证 production-ready |
| [Skills.sh](https://skills.sh/) | 提供 official、topic、agent、install ranking 和 audits 页面 | 聚合条目许可各异；站点明确不保证所有 Skill 的质量或安全 | 市场发现和热度信号；trust tier 不高于 community candidate |

## 优先候选

| 候选 | 来源地址 | 许可 | 适用计划 | 建议 |
|---|---|---|---|---|
| `verification-before-completion`、`systematic-debugging`、`test-driven-development` | [OpenAI plugins / superpowers](https://github.com/openai/plugins/tree/main/plugins/superpowers/skills) | MIT | M7 Verification Profile、Executor/Verifier workflow | 逐 Skill 抽取并做冲突/replay A/B，不整包自动激活 |
| `agent-supply-chain` | [GitHub awesome-copilot](https://github.com/github/awesome-copilot/tree/main/skills/agent-supply-chain) | MIT | M4 external Skill hash/provenance/admission | 优先 pilot；复用完整性 manifest 思路 |
| `build-evidence-map` | [GitHub awesome-copilot](https://github.com/github/awesome-copilot/tree/main/skills/build-evidence-map) | MIT | M7 assertion/evidence、M9 Evidence Matrix | pilot；保留 current provenance 和独立 validator |
| `codebase-memory-mcp` | [GitHub awesome-copilot](https://github.com/github/awesome-copilot/tree/main/skills/codebase-memory-mcp) | MIT | M6 code graph/MCP | reference/pilot；图结果必须由当前源码或 `rg/LSP` 复核 |
| `acquire-codebase-knowledge` | [GitHub awesome-copilot](https://github.com/github/awesome-copilot/tree/main/skills/acquire-codebase-knowledge) | MIT | Project initialization、M6 项目接入 | pilot；生成 proposal，禁止自动写 governance state |
| `node-link-and-diagram-layout` | [OpenAI plugins / data visualization](https://github.com/openai/plugins/tree/main/plugins/build-web-data-visualization/skills/node-link-and-diagram-layout) | MIT | M9-07 Docmost force-directed view | UI 实施阶段 pilot |
| `build-macos-apps` | [OpenAI plugins](https://github.com/openai/plugins/tree/main/plugins/build-macos-apps) | MIT | AlkaidLab Project Profile | 仅在 Moonlight/macOS operation 匹配时启用 |
| `skill-creator`、`mcp-builder`、`webapp-testing` | [Anthropic skills](https://github.com/anthropics/skills/tree/main/skills) | Apache-2.0（逐目录确认） | M4 初始化提案、M2 State MCP、M9 UI 验收 | 对照 OpenAI 版本做双 provider replay |
| `ai-team-orchestration` | [GitHub awesome-copilot](https://github.com/github/awesome-copilot/tree/main/skills/ai-team-orchestration) | MIT | M8 多协作者设计参考 | 只作比较；其 normal trust/branch workflow 不能替代 claim/lease/CAS |
| `codex-security` | [OpenAI plugins](https://github.com/openai/plugins/tree/main/plugins/codex-security) | Proprietary | M7 安全验证 | 保持 link-only/quarantine，需 entitlement 和许可确认 |
| `doc-coauthoring` | [Anthropic skills](https://github.com/anthropics/skills/tree/main/skills/doc-coauthoring) | 快照未含独立 license 文件 | 治理文档 reader testing 参考 | quarantine；可研究模式，不复制内容 |

## Catalog Admission 合同

外部条目进入 catalog 时至少记录：

```yaml
catalog_entry_id: stable-id
publisher: string
source_url: https-url
source_revision: commit-or-version
source_path: relative-path
content_sha256: sha256
license_ref: SPDX-id | source-url
trust_tier: official | standard | verified-org | marketplace | community
capabilities: [read, write, network, subprocess, mcp]
applicability: [project, repo, path, operation, role, provider]
verification_refs: [test-report | audit-url]
status: candidate | approved | active | quarantined | deprecated | rejected
```

发现流程按 `official -> standard -> verified organization -> marketplace/community` 排序。Stars、安装量和市场 audit 只能提供候选优先级；许可、hash、依赖、脚本、远端下载、MCP authorization、prompt injection 和副作用权限必须由控制面单独验证。

项目初始化时 resolver 只向模型暴露通过 applicability 预筛选的 manifest 摘要和直接来源 URL。OpenAI 当前初始 Skill 列表预算说明了在 host 加载前预筛选的必要性；目录规模不能依赖 provider 自动截断。批准后的内容固定到 commit/version 和 content hash，完整正文按 S3 bounded lookup 获取。
