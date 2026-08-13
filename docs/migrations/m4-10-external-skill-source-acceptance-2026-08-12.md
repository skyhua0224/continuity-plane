# M4-10 External Skill Source Acceptance

版本：2  
日期：2026-08-12  
状态：accepted offline source snapshot contract / production acquisition authorization pending M7/M8

```yaml
document_id: context.m4-10-external-skill-source-acceptance
document_revision: 2
change_type: evidence
authority_ref: repository-tests-synthetic-fixture-and-current-source-review-2026-08-12
supersedes: context.m4-10-external-skill-source-acceptance@1
affected_tasks: [M4-07, M4-10, M6-06, M7-01, M8-05]
next_review: M7-01
```

## 范围

M4-10 注册 `context.external-skill-source-snapshot/v1alpha1` strict schema 与 runtime validator。adapter policy 按 version/hash 注册，在外部 callback 前冻结 source class、trust tier、publisher、canonical URL、允许 host/path、revision kind、resource kind 和 license；调用方只提交 source request，不能声明 official、publisher、license 或任意 adapter version。当前 policy 覆盖已验证的 OpenAI `node-link-and-diagram-layout`、Agent Skills specification、GitHub `awesome-copilot` Skill 路径和 Skills.sh dynamic index。

每个来源接受一次最大 4 MiB 的内容 observation callback；Skill 来源另接受一次固定 commit 的 Git tree observation callback。tree evidence 独立派生 `SKILL.md`、scripts、references 与 assets 的期望路径，再与下载资源比较。正文、license evidence、publisher evidence、tree evidence 和资源 manifest 经内存 staging 后进入 `LocalArtifactStore`；所有可预见的输入、时间、tree、manifest 和 schema 校验在发布前完成。snapshot 保存 artifact refs、content hash/size、asset tree digest、final URL、retrieval time 和 acquisition。missing/unlisted resource、symlink、submodule、跨 Skill 目录资产、非规范路径、URL/path traversal、cross-host redirect、mutable revision 和缺失 license/publisher/tree evidence 均 fail-closed 或 quarantine。底层生产 HTTP request count、redirect 和 streaming byte enforcement 由 M8 acquisition adapter 验收，当前 callback `max_bytes` 只构成调用合同。

离线 replay 接受 source requests、snapshot、固定 generator/time 与本地 artifact store，不接收 retriever。M4-07 projection 强制 `source_kind=external`、manifest `status=proposed`、entry `status=candidate`、approval refs 为空且六项 authority permission 为 `false`；投影前必须复验 CAS。只有 immutable Git commit 的 candidate Skill 可以投影。Standard、catalog index、quarantined source 和证据不匹配 source 的投影为 0。

## 验证

| 门 | 结果 |
|---|---|
| strict schema/runtime | schema hash `0c64c7af279ab955a7a546c8f71e15612d5ca7c970767929683715e6d6f29cb6` 已注册；27/27 定向测试通过；metadata validation 与 CAS-backed admission 名称和权限分离 |
| fixed adapter policy | policy hash `a6b3d6c1da73ceac16f2ec2db07ca260494a342c0569c1f870075f686b561776`；adapter version/hash 精确绑定；retrieval callback 修改可变模块对象不影响冻结 policy；unknown adapter、caller-declared identity、错误 resource kind/license、endpoint、非规范路径和重复 source identity 在 callback 前拒绝 |
| staged CAS | 四类 fixture 内容 observation callback `4` 次、两个 Skill tree callback `2` 次；内容、证据、tree 和资源 manifest 均按实际 bytes 写入 CAS；已知失败在 publication 前留下对象 `0`；missing/corrupt/hash mismatch 100% 拒绝 |
| pinned asset tree | multi-file Skill 的 `SKILL.md`、scripts 和 references `3/3` 入 manifest；期望集合由固定 commit 的独立 tree evidence 派生；missing、unlisted、symlink、submodule、跨 scope 和 tree 缺失/截断均 quarantine |
| deterministic replay | synthetic fixture hash `d0043e4340bfa5d2069f0f091df5eb43897c2ac947750dea205652646fd423c4`；40 次持续测试 replay mismatch `0`，追加 callback `0`；本机 200 次 replay p50 `1.5769 ms`、p95 `1.7772 ms`、max `2.5655 ms` |
| safe projection | immutable candidate Skill `2/2` 投影；standard/index `0/2`；permission true `0`；approval refs 非空 `0` |
| source review | OpenAI、Agent Skills、GitHub 三个 pinned content hash 复核；Skills.sh API 缺 revision/license/path/hash，保持 quarantine；详见 `docs/research/external-skill-and-mcp-catalog-assessment-2026-08-09.md` |
| repository gate | 全库 637 tests 通过、28 个无 DSN PostgreSQL live tests skipped；repository verifier、compileall、Ruff 和 `git diff --check` 通过 |

Fixture 标记为 `synthetic-contract`，用于验证 contract、CAS 与 replay，不构成真实外部内容采集证据。真实 revision/hash 只记录在研究文档；Git 不保存本次外部 Skill 正文。

## 后续边界

M7-01/M7-06 提供生产 assertion provenance、publisher ownership、license validity 和 freshness verifier；M8-05 提供受控 streaming acquisition、底层 request-count/redirect/byte enforcement、authorization/audit 和 publication failure orphan GC/retention；M4-07 与 M7/M8 的 approval/promotion gate 决定 active。M6-06 单独处理 MCP Registry 与 tool authorization。当前实现不执行 provider Skill 安装，不调用 provider process，不提交 Typed State，external active Skill 数为 `0`。
