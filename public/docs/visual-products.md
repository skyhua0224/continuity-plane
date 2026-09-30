# 图形化产品计划

[English](visual-products.en.md)

Continuity Plane 的图形化产品服务于大型项目定位、复盘、影响分析和受控治理。
所有页面读取同一 revision 的签名 projection。视图可以筛选、布局和下钻；权威
状态变更始终通过 State MCP 的 authorization、expected revision/CAS 和 validator。

## 当前交付边界

| 能力 | 内部合同 | alpha wheel | 用户界面 |
|---|---|---|---|
| Project Graph / Work Ledger | verified | included | adapter 待实现 |
| Decision Timeline / Evidence Matrix | verified | included | adapter 待实现 |
| Relationship / Impact | verified | included | adapter 待实现 |
| Obsidian signed Markdown vault | verified | included | 生成器可调用；CLI export 待实现 |
| Context Health / Replay | verified | 未进入 alpha wheel | provider-neutral canary SPI 待实现 |
| Docmost console | projection/action contract verified | connector 未发布 | preview |
| Obsidian Canvas / Bases | planned | 未发布 | planned |

已验证的 projection 证明数据完整性、同 revision 绑定、容量限制和篡改拒绝。它们
不等同于浏览器或 Obsidian 中已经完成的图形界面。

## Docmost 控制台

Docmost 提供适合持续操作的共享控制台：

- **Project Map**：确定性任务 DAG 与 Relationship/Impact 探索模式；显示主线、
  dependency、owner、claim/lease、blocker、过期分支和 scope overlap；
- **Work Ledger**：active/completed Work、并行认领、PR/CI/deploy intent、冲突和
  离线补发状态；
- **Decision & Evidence**：决定时间线、supersedes 链、Constraint 和 Evidence
  Matrix；
- **Context Health & Replay**：compaction、Skill、retrieval、reference freshness、
  continuation cursor、effect watermark 和 canary drilldown；
- **Governance Inbox**：审批、纠偏、promotion、review 和审计回执。

浏览器只接收有界 projection 和 artifact reference。SSE 是默认实时通知传输，
WebSocket 由部署 capability 显式启用。所有治理动作都携带 trusted session、请求
identity、expected revision 和 idempotency key；页面无数据库直写路径。

## Obsidian 图形化 vault

Obsidian 保持零服务、离线、只读的观察面。现有 Markdown vault 将扩展为：

- Project、Work、Decision、Constraint 和 Evidence 的稳定双向链接；
- 生成的 Canvas 项目图与影响图；
- Bases 定义的 Work Ledger、Evidence Matrix 和健康状态表；
- 按 owner、状态、scope、revision 和 evidence validity 过滤；
- 带 source revision、template version、content hash 和签名的 manifest。

生成目录与用户自有笔记使用独立 namespace。刷新采用临时目录生成、完整验证和
原子替换；人工修改生成文件、增加未管理文件或混用不同 revision 时拒绝发布。用户
笔记可以链接生成对象，但不能反向提交 active state。

## 实施顺序

1. **统一 Presentation SPI**：提取 provider-neutral Context Health/Replay 输入，固定
   view manifest、route、pagination、filter、artifact authorization 和 stale-view
   合同。
2. **Obsidian graphical export**：提供 CLI export、Markdown、Canvas、Bases、
   backlinks、增量刷新与离线验证。
3. **Docmost read console**：实现 Project Map、Work Ledger、Decision/Evidence、
   Context Health/Replay 和通知 inbox。
4. **Docmost controlled actions**：接入审批、纠偏、promotion 与 review；所有动作
   经过 State MCP 和 audit chain。
5. **跨前端验收**：同 revision 可见对象、筛选结果、artifact links 和 health
   findings 在 Docmost 与 Obsidian 保持一致。

## 完成门

| 指标 | 门槛 |
|---|---:|
| projection 与 State revision/digest 对应 | `100%` |
| stale、tampered、mixed-revision view 被拒绝 | `100%` |
| UI/生成器直接 State 或数据库写入 | `0` |
| 2,000 nodes / 5,000 edges projection 完整性 | `100%` |
| reference desktop 首次可交互 p95 | `< 2 s` |
| 已加载视图 filter/focus p95 | `< 100 ms` |
| SSE duplicate delivery 与离线漏事件 | `0` |
| Docmost/Obsidian 可见对象一致率 | `100%` |
| 键盘主流程与 WCAG 2.2 AA 自动检查 | pass |
| 浏览器/Electron screenshot、布局和重叠检查 | desktop/mobile target pass |
| 默认本地模式新增 daemon/数据库 | `0` |

性能结论必须绑定设备类别、数据规模、浏览器或 Obsidian 版本和至少 25 个性能样本。
完整 Docmost 界面需要真实 connector、认证和浏览器测试通过后才能移出 preview。
