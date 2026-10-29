# Continuity Plane Alpha36 · Agents use it on their own, and it costs fewer tokens

Alpha36 is about two things: AI agents now use Continuity by themselves instead of falling back to shell commands or ignoring it, and using it costs fewer tokens. In our controller test the same job took 48 % fewer tokens than with Alpha35. Coordinators who run several agents benefit most.

Alpha36 让 AI Agent 自己用起 Continuity，而且用得更省：同样的主控任务，token 比 Alpha35 少 48%。同时带多个 AI 干活的人收益最大。

> **升级后请重连 MCP 或新开会话**，宿主才会加载新工具。默认工具列表不再包含高级 State 工具，需要时设置 `CONTINUITY_MCP_TOOLSET=full`；按名字调用旧工具仍然有效。
> **Reconnect MCP or start a new session after upgrading.** Advanced State tools are no longer listed by default; set `CONTINUITY_MCP_TOOLSET=full` to list them. Calling old tools by name still works.

### 🤖 Agent 主动使用 · Agents use it on their own

- 新增 15 个按用途划分的 MCP 工具，例如 `continuity_task_add`、`continuity_worker_dispatch`、`continuity_report_final`；参数有完整类型，项目和身份可以省略，结果里附带下一步该调用的工具。
  Added 15 task-shaped MCP tools such as `continuity_task_add`, `continuity_worker_dispatch` and `continuity_report_final`, with typed parameters, project and identity defaults, and a `next` hint in every result.
- MCP 服务器自带使用说明，工具描述改为"什么时候用"。在 shell 里运行已有 MCP 工具的 `continuity` 命令时，会提醒一次对应的工具。
  Both MCP servers now explain when to use them, and tool descriptions say when to call each tool. Running a `continuity` command in the shell that has an MCP equivalent gets one reminder.
- 派给执行者的提示词、会话开始提示和 skills 先写工具名，CLI 作为后备。
  Worker prompts, session-start hints and skills name the MCP tools first, with the CLI as fallback.
- 新增 `continuity observe adoption`，按会话统计通过 MCP 和 CLI 使用 Continuity 的次数，只记次数不存内容。
  Added `continuity observe adoption`, which counts per session how agents reached Continuity through MCP or the CLI; it stores counts only.

### 💰 更省 token · Fewer tokens

- 等待执行者不再占用模型：`continuity worker wait` 作为后台任务运行，执行者结束时才唤醒主控。
  Waiting costs no model turns: `continuity worker wait` runs as a background task and wakes the controller only when a worker ends.
- 读取执行者报告默认只返回前 2 KB、标题列表和总长度，需要时再翻页。
  Worker reports return the first 2 KB, the headings and the total size by default; page for more.
- 默认 MCP 工具定义加使用说明为 17.8 KB，比 Alpha35 的 18.4 KB 还小，同时多了 15 个工具。
  Default MCP tool definitions plus instructions are 17.8 KB, smaller than Alpha35's 18.4 KB, with 15 more tools.
- 上下文压缩后的恢复信息改成一段简短文本，每个会话的 hook 注入有总预算。
  The post-compaction resume packet is a short text block, and hook injections have a per-session budget.

- 新增 `continuity-mcp --proxy ssh://主机`：本机代理缓存工具列表，远端断开后自动重连，工具列表在整个会话中保持不变，避免宿主因为工具列表变化而重建提示缓存。
  Added `continuity-mcp --proxy ssh://HOST`: a local proxy caches the tool list and reconnects after the remote drops, so the list never changes during a session and the host does not rebuild its prompt cache.
- 派给执行者的提示词把固定的规则和用法放在最前面、任务内容放在后面，不同执行者可以共用提供商的前缀缓存。
  Worker prompts lead with the fixed rules and usage text and put the task last, so different workers share the provider's prefix cache.

### 🔍 代码定位 · Code lookup

- 检索先返回定义，再返回用法；支持 `Class.method`、`ns::Class::method` 这类限定名。
  Lookups return the definition first, then usages, and accept qualified names such as `Class.method` and `ns::Class::method`.
- 热缓存查询从约 590 ms 降到约 84 ms。
  A warm-cache lookup dropped from about 590 ms to about 84 ms.

- 现在也能找到 C/C++ 的 `using` 别名和 `typedef` 定义。
  C/C++ `using` aliases and `typedef` definitions are now found.

### 🧰 任务卡与派发 · Cards and dispatch

- 本地任务卡可以修改正文、标题和验收条件，每次修改记录版本号和哈希；没有正文的卡在创建时就会被拒绝。
  Local cards can revise body, title and exit criteria with a recorded revision and hash; a card without a body is rejected at creation.
- 创建卡片时可以声明尚未完成的依赖，卡片会等依赖完成后才能开工。
  Cards can declare unfinished dependencies at creation and wait until they complete.
- 新增 `continuity worker handoff --to-model`：换模型续做，新会话会带上工作区、分支、未提交改动和上一份报告。
  Added `continuity worker handoff --to-model`, which continues a card on another model with the worktree, branch, uncommitted changes and last report.
- 每张卡可以设置最低模型档位和禁用模型；短时限流先在同一模型上重试，再考虑回退。
  Cards can set a minimum model tier and denied models; short rate limits retry the same model before any fallback.
- 主控通知按事件列出下一步工具，并覆盖主控负责的其他项目。
  Controller notices name the next tool per event and cover the other projects the controller leads.

- 执行者事件更准确：同一任务内部重试记为"重试中"，不再触发"执行者已结束"通知；结束时只发一次 `job.completed` / `failed` / `interrupted` / `lost`。
  Worker events are precise: retries inside a job are reported as retrying and no longer trigger an "ended" notice; each job ends once with `job.completed`, `failed`, `interrupted` or `lost`.
- 进程已经不在的"运行中"任务会被标记为 `lost` 并释放容量；报告会标明是最终报告还是中途报告。
  "Running" jobs whose process is gone become `lost` and release their capacity; reports say whether they are final or interim.
- 新增 `continuity collaboration reconcile`：提示已经合入、可以合入或长期没动的卡片，默认只读。
  Added `continuity collaboration reconcile`, which points out cards that are merged, ready to merge or stale; read-only by default.
- 主控交接的计划从 640 字节放宽到 4 KB。
  The handoff plan limit is raised from 640 bytes to 4 KB.

### 🐛 修复 · Fixes

- 修复执行者因 429 限流结束时，被误判为内容策略拒绝的问题（原因是分类器读到了执行者刚读过的源码）。
  Fixed workers stopped by a 429 rate limit being classified as content-policy refusals because the classifier read source code from the worker's output.
- 修复"本周额度用尽，将于某时重置"未被解析，导致模型被无限期暂停的问题。
  Fixed "limit exhausted, resets at …" messages not being parsed, which paused the model indefinitely.
- 修复 `controller acquire --token` 忽略传入的令牌的问题；卡片指定的工作目录不存在时，现在在启动前给出明确错误。
  Fixed `controller acquire --token` ignoring the given token; a missing card worktree now fails before launch with a clear message.
- 读取最近变更、主控交接和卡片列表在大项目上快了 2–13 倍，输出与之前逐字节相同。
  Recent changes, the controller handoff and card lists read 2–13× faster on large projects, with byte-identical output.

### 📊 实测 · Measurements

**Agent 是否主动使用、花多少 token**：真实 Claude Code（Sonnet 5.5）会话，隔离数据目录，用户提示不提 Continuity，每个场景每个版本 3 次。工具 `tools/run_agent_adoption_eval.py`。

| 场景 | 版本 | Continuity 操作走 MCP 的比例 | 成功 | 中位成本 | 中位 token |
|---|---|---:|---:|---:|---:|
| 主控拆分 3 件事并分派、写交接 | Alpha35 | 5 % / 82 %（两轮） | 3/3 | $0.185 | 342,725 |
| | Alpha36 | 100 % | 3/3 | **$0.128** | **177,317** |
| 压缩后接手，说出下一步 | Alpha35 | 0 % | 3/3 | $0.063 | 68,048 |
| | Alpha36 | 100 % | 3/3 | **$0.055** | 64,061 |
| 在 2 个文件的小仓库里找定义和用法 | Alpha35 | — | 3/3 | $0.039 | 39,997 |
| | Alpha36 | 100 % | 3/3 | $0.045 | 61,954 |

最后一个场景 Alpha36 多花 15 %：仓库只有两个文件，直接读文件比调用检索便宜。大仓库的检索收益见下表。

**大项目读取速度**：i7-13700KF、31 GB、Linux；一个 1,101 张卡、237,434 条事件（72.7 MB）的项目副本；`hyperfine -N`，预热 2 次，3 轮 × 20 次。测试时机器负载 17–19，绝对值偏慢，前后对比在同一条件下。

| 操作 | Alpha35 中位数 / p95 | Alpha36 中位数 / p95 |
|---|---:|---:|
| 读取最近 50 条变更 | 812 / 3,609 ms | **61 / 82 ms** |
| 读取主控交接 | 466 / 2,691 ms | **174 / 200 ms** |
| 列出待评审的卡 | 333 / 381 ms | **168 / 191 ms** |
| 查看一张卡 | 256 / 1,574 ms | **139 / 175 ms** |
| 读取最近 5 条执行事件 | 130 / 268 ms | **63 / 86 ms** |

**代码定位**：48 条人工标注查询，覆盖一个 807 个文件、约 11.5 万行的 C/C++ 仓库和本项目的 Python 代码；每条查询 10 次。另用 28 个调优时没出现过的符号复核。

| 指标 | Alpha35 | Alpha36 |
|---|---:|---:|
| 第一条结果就是定义（C/C++，标注集） | 13.8 % | **100 %** |
| 第一条结果就是定义（C/C++，复核集） | 1/8 | **7/8** |
| 用法召回（前 10 条，C/C++） | 13.2 % | **88.5 %** |
| 热缓存查询，进程内中位数 | 589 ms | **84 ms** |

**压缩后接续**：模拟会话（30 次提问、200 次工具调用，压缩一次），统计 hook 注入的字节数和恢复准确度。

| 指标 | Alpha35 | Alpha36 |
|---|---:|---:|
| 压缩后注入 | 3,974 B | **1,150 B** |
| 整个会话的 hook 注入 | 7,792 B | **2,950 B** |
| 恢复准确度（当前卡、下一步、约束、待决事项、验证入口） | — | **5/5，3 次均正确** |

**等待执行者**：模拟主控等待 10 分钟、3 个执行者分别在第 2、5、9 分钟结束。循环调用等待工具需要唤醒主控 15 次，后台 `worker wait` 只需 6 次（每次结束 1 次，加读报告）。

**MCP 连接恢复**：同机模拟远端进程被强制结束 10 次，重连并完成一次调用的中位数 69 ms、p95 94 ms，工具列表 0 次变化；真实 SSH（两台机器之间）强制结束远端后 1.4 秒恢复，工具列表逐字节一致。

**提示词共享前缀**：同一项目两张不同任务卡组装的提示词，共同前缀从 2,371 字节增加到 3,972 字节。

### ⚠️ 已知限制 · Known limits

- Codex 执行者的真实会话评测还没完成（评测期间 Codex 额度用尽），上面的 Agent 数据只来自 Claude Code。
- MCP 代理对缓存重建的实际效果需要上线后用真实会话复测；静默断网要等 SSH 心跳（约 45 秒）才能发现。
- "后台任务只在结束时唤醒模型"依赖宿主的后台任务机制，Claude Code 之外未验证。
- 两项既有基准测试（M7-05 收据绑定旧环境、M8-10 延迟门）在 Alpha35 上同样失败。

### 升级 · Upgrade

```bash
pipx install --force 'continuity-plane==0.1.0a36'
```

完整变更：[CHANGELOG](../CHANGELOG.md) · [命令与工具参考](commands.md) · [部署说明](deployment.md)
