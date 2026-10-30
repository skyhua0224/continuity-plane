# 当前能力、实测与已知限制

[English](validation.en.md) · [工作流](workflows.md) · [历史实验](benchmarks.md)

范围：Alpha37（`0.1.0a37`）。证据分为本轮合成 CLI、已有自动回归、匿名使用反馈和未覆盖项，不能互相替代。

## 本轮可复现实测

Alpha27 已用本机 tea 0.15.0／HTTPS Gitea 1.24.7 双合成账号私有仓库验收；Alpha36 未另建真实 Gitea 现场，协作相关变化由合成回归覆盖。下方采用者脚本在仅安装 Alpha37 wheel 的新持久 venv 与全新工作目录重跑；receipt 只含允许公开的合成聚合数据，完整发行构建仍以 tag CI 为准。

运行公开脚本；使用一个**新建、空的持久目录**，不指向自己的注册表或 provider profile：

```bash
python examples/verify_adopter_workflows.py --workspace /path/to/new-persistent-fixture --cli /path/to/continuity
```

源码开发树中的脚本位于 `public/examples/`；公开 checkout 位于 `examples/`。脚本保留工作目录，不删除原有数据。它创建合成 Git 仓库、独立注册表和 Python 子进程；不会调用模型、读取原生账号／会话、操作真实项目或发送网络请求。

脱敏结果：[adopter-workflows.json](../benchmarks/adopter-workflows.json)。只输出允许的布尔项、版本、字节数与退出码，没有硬件／主机／profile 标识、私有路径、项目名或原始输出。

| 检查 | 本轮结果 |
|---|---|
| 初始化、注册、身份与按 assignee 取卡 | 通过 |
| brief 符号检索与 1024-byte 上限 | 通过；该合成命中返回 32 bytes |
| 完整规则导出、镜像对比、规则／正文组装 | 通过 |
| 收件箱发送、读取、ack、归档与重复抑制 | 通过；只归档已确认消息 |
| 两个 Python worker 的 supervisor 运行与正常报告 | 通过 |
| 失败子进程 | 原退出码 3 被保存；dispatch CLI 非零（2） |
| 单次 merged 元数据与 commit 绑定检查 | 通过；不执行真实 Git 合入 |
| shared handoff、lesson、第二主控只读 | 通过 |
| 分域 profile、批准记忆与 `controller start` | 通过；固定样例返回 1,443 JSON bytes，预算 4,096 bytes |
| 逐条事件翻页、多事件 wait、无变化读取 | 通过；该 fixture 无变化响应 216 bytes |
| worker 提前改为待评审后的报告 | 自动挂卡并由 report 读取；不接受手工挂载替代此检查 |

14 个正向检查通过，另验证提前改卡后的自动报告交付。字节数只描述该固定样例，不是生产项目吞吐、模型 token 降幅或费用节省。

## 核心能力与证据范围

| 能力 | 当前依据 | 未证明／边界 |
|---|---|---|
| 卡片状态、branch/commit/verification、body-only 更新 | CLI 实测 + 自动回归 | 元数据不替代实际测试 |
| Todo from-cards、开放状态筛选 | 自动回归 + 合成路径 | 不要求卡片用户再维护独立队列 |
| dispatched worker 与报告 | 正常路径实测、提前改卡／重放回归；有匿名采用反馈 | 迟到 attempt 与重新指派仍受保护 |
| timeout、idle timeout、effort、原线程 resume | 适配器／supervisor 回归 | 本轮不调用原生模型验证每个 provider 的参数 |
| 暂时错误、策略拒绝、fallback／延期 | 分类、预留与迟到结果回归 | 不因拒绝自动换渠道；不是账号额度预测器 |
| changes、分页、OR wait | CLI 实测 + 并发／预算回归 | 无法唤醒所有宿主；超时不是 worker 失败 |
| 规则、受众、附件与完整导出 | CLI 实测 + 版本／附件回归 | 指针／hash 不证明 worker 实际读过 |
| reference 与 lesson | 记录、TSV 往返／冲突、更正回归；lesson CLI 实测 | 显式文件交换；提醒不等于采用 |
| 主控 profile／记忆与原生笔记 | 分域、锁字段、候选、撤销、origin/symlink/权限门及有界 start 回归 | 原生写回和真实全量 memory 类型未实测；同 OS 用户需 sandbox |
| inbox 归档、额度暂停、预留诊断 | 合成端到端与确定性回归 | 只消费已观察错误，不轮询 provider 额度 |
| worker queue、handoff 优先级、assigned／running、batch、integration_base | 单实例／去重／容量门／host pause／transient backoff、低预算交接、状态投影、JSON batch 与 prompt 回归 | 不提供跨机器强锁、强制进程隔离或自动唤醒 |
| inbox 无 ID 发送、批量确认与 MCP 结构化错误 | CLI／MCP 生成 ID、显式 ID 幂等、ack 返回值、before／all 批量确认、archive 与 schema／argparse 条件字段回归 | ack 不等于宿主唤醒；archive 不删除；单批最多 1000 条 |
| 主机重启 reservation、本地依赖、统一列表、cooldown 原因 | boot/host stale 判定、容量忽略 stale、reconcile、dependency gating、paged envelope、非代码 decision、健康原因和大历史追加回归 | 容量仅按项目+host+profile+client-family；MCP 生命周期仍由宿主管理 |
| 代码索引、brief、依赖与提示词路标 | 合成符号命中 + 语言／路标回归 | 本轮样例为 Python；非完整编译器调用图 |
| 批次、决定、metrics、一步关卡 | schema／来源／fence 回归；有使用反馈 | 记录声明，不自动执行 Git 或批准例外 |
| controller lease、容量与未知操作确认 | CLI 第二主控实测 + 并发回归 | 仅协调经过工具的合作客户端 |
| 共享交接、权威集成 ref、session/batch 绑定 | CLI handoff + 回归 | 绑定不追溯分摊全部历史；新 MCP 连接不是永久身份 |
| Codex／Claude 压缩保存与恢复 | 受控回调／去重回归；历史原生手动压缩记录 | 本轮未重新运行自然压缩、自动阈值或长上下文模型实验 |
| Claude 原生子代理观察 | 官方 schema + 受控回调回归 | 自然发生的完整原生子代理生命周期仍未实证；不推断退出码／成功 |
| CLI／hook／MCP SSH bridge | 既有握手验证、隔离与映射协议回归 | 本轮未重新实测双机；映射须显式选择 bridge_config |
| token/cache／hook 开销／任务增量 | 原地导入与计数回归 | 本轮不采集真实账号；无统一费用／因果节省结论 |
| 漂移与未了结承诺 | 当前解析与规则回归；匿名反馈 | 不是对所有文档与外部世界的真实性证明 |

Alpha25 的[公开发行 CI](https://github.com/skyhua0224/continuity-plane/actions/runs/38845950775)通过 268 项测试。文档新增脚本和链接／命令检查另随当前主分支 CI 运行；不改写已发布 wheel 或历史 test count。

## Alpha37 验证

- 回归：`tests/test_a37_*.py`；完整源码套件 2,897 项，仅 M7-05 收据与 M8-10 延迟门失败，与 Alpha35 相同。`self-update` 只做过预演。更新检测已对真实 PyPI 验证：后台查询、终端提示、管道不输出、标准输出不变、关闭开关。

## Alpha36 验证

- 真实 Agent 会话：Claude Code（Sonnet 5.5）在隔离数据目录中运行主控、接手、代码查找三个场景，每个场景每个版本 3 次，用户提示不提 Continuity；用 `tools/run_agent_adoption_eval.py` 统计 MCP／CLI 调用、token 与成本。Codex 执行者场景因评测期间额度用尽未完成。
- 大项目读取、代码定位、压缩后接续、等待执行者、MCP 代理重连：方法与数字见[版本说明](release-alpha36.md#-实测--measurements)。
- 回归：`tests/test_agent_tools.py`、`test_agent_steering.py`、`test_adoption_telemetry.py` 及 `tests/test_a36_*.py`；完整源码套件 2,764 项，仅 M7-05 收据与 M8-10 延迟门失败，与 Alpha35 相同。

## Alpha35 采用问题回归

| 修复 | 本轮证据 | 保留边界 |
|---|---|---|
| worker 事件漏接与轮询窗口 | `worker events --follow` 在进程启动后追加事件、从 cursor 输出 NDJSON；多项目 cursor 解析回归 | 本地 CLI 流不是远程唤醒服务；`--all-projects` 使用 project:cursor 对 |
| inbox 完成通知 | `inbox wait` 从持久 change 读取新消息，`inbox unread --count` 与同一未确认事实一致 | ack 仍是接收方动作；不保证另一台宿主的 AI 被唤醒 |
| job 终态与现场 | `job.json`／索引 running→completed、exit_code=0、实际 cwd／HEAD、结束 cursor 和 metadata 失败非阻塞回归 | 旧 job 缺失字段不补造；没有 heartbeat 的 native child 仍是观察 |
| 派发热路径与输出 | 无 running binding 时禁止历史扫描；有 binding 从启动 cursor 增量读；heartbeat 标记 build-lock idle；task show 默认 64 KiB | 超长单记录仍有明确预算失败；不会杀等待锁的进程 |
| 容量、quota 与 fallback | 跨注册项目 host gate、旧 boot 排除、expired cooldown 放行；项目 fallback 仅 transient 后换 sonnet 并改派 | host capacity 是 operator 声明；认证／内容策略拒绝不 fallback；不是账号额度查询 |
| 卡片语义与安全 | queued 保留 assignee、显式 unassign、controller lease 改派原因、superseded 依赖；PATH Git guard；异步 completion hook | Git wrapper 不是安全沙箱；绝对路径可绕过；hook 不是远程唤醒 |

聚焦 Alpha35 回归位于 `tests/test_alpha35_control_observability.py`；原有 supervisor、dispatch、project-context、controller-recovery 与 adoption 回归同时运行。

## Alpha34 采用问题回归

| 修复 | 本轮证据 | 保留边界 |
|---|---|---|
| HTTP 拦截与内容拒绝 | 401/403、Cloudflare、cf-ray、代理页与明确 `cyber_policy` 的优先序回归；旧 job 自动分类更正 | 明确模型安全标记仍阻断自动重放；复核解除需要理由与精确 job |
| 自动与人工续跑 | 403 场景同一 thread 的 dry-run resume；exact-job 解除、幂等事件和结果更正 | `operator-cleared` 是本地审计决定，不是 provider 鉴权或新的内容安全批准 |

## Alpha33 采用问题回归

| 修复 | 本轮证据 | 保留边界 |
|---|---|---|
| 派发热路径 | 增量 safety index、损坏重建、只消费追加尾部、真实大项目首次/后续耗时 | 首次升级仍需一次顺序回填；change log 仍是权威 |
| 输入与输出预算 | 空 body 在策略历史前失败；`batch-list` 超大预算封顶并返回 cursor | 单条记录无法放入 131072 字节时仍明确报错，不静默丢记录 |
| token 诊断 | 过期、owner、缺 token、错项目 token 分别说明 | controller token 是协作 fence，不是原生账号认证 |

## Alpha32 采用问题回归

| 修复 | 本轮证据 | 保留边界 |
|---|---|---|
| 重启遗留 reservation | host/boot 指纹、旧 reservation 的 boot 时间判断、reservation inventory、dry-run/apply reconcile、clear 结果和容量忽略 stale 回归 | 只清理能证明 stale 的记录；unknown/live 保留 |
| 依赖与列表 | 本地 Gitea 同构 dependencies、dependencies-ready、claim/active/dispatch 门和统一 paged envelope | 依赖是排序提示和运行门，不构成模块所有权 |
| 决定、健康与性能 | 非代码 decision 无 commit、dispatch-health 原因/task/attempt、无 event-id 不扫完整历史 | 当前项目锁定仍可能等待其他 writer；不是无限并发性能保证 |
| MCP 主机重启 | collaboration doctor 明确宿主管理与 CLI／SSH 降级路径 | Continuity 不自动拉起宿主 stdio 进程 |

## Alpha31 采用问题回归

| 修复 | 本轮证据 | 保留边界 |
|---|---|---|
| MCP send 缺 message_id | CLI／MCP 缺省生成并返回 ID；显式 ID 重发不重复投递；返回 ID 可 ack | 自动 ID 不提供重试幂等；需要重发幂等时显式传 ID |
| MCP 裸异常 | inbox 缺 sender 与 planning 非法参数均返回 isError／structuredContent／failed_gate | JSON-RPC 协议错误仍用于未知工具或非法 envelope |
| 大批未确认 inbox | 时间窗或 all 的受限批量 ack，随后 archive 已确认消息 | 使用者仍需决定确认范围；不自动确认历史消息 |
| root／project 漂移 | 错误显示请求 root／project 与两边注册身份及纠正命令 | 本地注册不会与远端权威状态自动合并 |

## Alpha30 采用问题回归

| 修复 | 本轮证据 | 保留边界 |
|---|---|---|
| 持久派发队列 | 单实例锁、优先级、重复抑制、host pause、容量门、quota／rate-limit 退避和状态回归 | 锁覆盖同一项目／数据根；不是远端队列或 provider SLA |
| handoff 截断 | 1 KiB 预算下仍保留 pending decision、blocked 与 failed attempt | 普通 change 历史可省略；完整细节需提高预算 |
| Todo 状态 | assigned 与实际 running binding 分开，blocked 带原因 | 无 heartbeat 的 native child 仍是观察，不是受控运行证明 |
| usage limit | quota 优先于泛化 policy 文案；自动 quota 记录、暂停与旧 job 分类更正回归 | 只使用 provider 错误中的 reset；未知 reset 不伪造 |
| shell-safe batch | 文件与 stdin JSON、首错停止、本地／共享字段路径回归 | 不是跨 Issue 全有或全无事务 |
| reservation 与 integration base | live／stale／unknown 诊断；worker prompt 带非授权 base | 诊断不自动终止或接管进程；base 不授权合入 |

## Alpha29 采用问题回归

| 修复 | 本轮证据 | 保留边界 |
|---|---|---|
| 提前改卡仍挂报告 | 待评审／完成／merged／blocked、迟到与重放回归；CLI 自动读取 | 旧 attempt、换执行者不覆盖新附件 |
| 稳定执行者 | 两个全新 MCP 进程；不同连接 ID 的卡片 Todo 激活 | 明确配置 executor_id/CONTINUITY_ASSIGNEE；不自动接管旧身份 |
| 可执行提示 | 命令经真实 parser 校验、去重、卡片不建第二队列 | 未知写参数不自动推断；不证明 worker 已采纳 |
| 来源 TSV | Unicode、扩展列、幂等、冲突／无效行无部分写入、MCP 接口 | 显式导入导出；不覆盖已有文件、不自动同步或验证已读 |
| 映射远端检索 | 独立合成仓库经 CLI 接收协议检索；映射失败／超预算拒绝 | 未做真实双机新实测；无任意远端文件／进程管理 |

回归入口：[test_a26_adoption.py](../tests/test_a26_adoption.py)，13 项 Alpha26 合成检查。Alpha35 可观察性用例位于 `tests/test_alpha35_control_observability.py`；Alpha34 分类与复核解除用例保留在 `tests/test_alpha34_policy_classification.py`。[版本说明](release-alpha35.md)列出本版边界。

## 隐私与效果声明

公开证据只输出允许的聚合；原始会话、凭据、设备标识、私有项目与 profile 目录不进入 Git。宿主 usage 在所属机器／profile 原地读取，不能搬到别处查询。

高提示词缓存命中不等于 Continuity 导致的省 token。净字节为负也必须保留。长期每任务成本、窗口利用率和压缩间工作量需要可比较的真实会话、同源计数与明确质量门；当前未知项不填数字。
