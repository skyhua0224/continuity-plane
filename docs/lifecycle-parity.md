# 压缩接续与本地计量 / Compaction continuation and local accounting

核心包、Codex 三个插件与 Claude 插件统一为 alpha23。先更新核心，再用宿主的插件安装
流程启用相同 tag；运行中的进程仍可能持有旧代码，需要检查工具表与事件记录并重载。
只看到 CLI 版本不能证明 hook 生效。不要为升级覆盖 provider key 或整份宿主配置。

Codex 和 Claude 使用同一 session handoff 合同：压缩前保存下一步、约束、验证和 Git
返回点；压缩后有界注入并静默继续。同一个压缩边界重复回调只注入一次；不同 Session
不会复用返回点。没有可验证的会话记录时保留宿主摘要与当前用户意图，不重放旧 Work。
Claude 的发布目录为 `integrations/claude/continuity-plane`，包含 plugin manifest、MCP 和
hooks。`continuity-host-hook` 使用安装核心的解释器；压缩前后执行 checkpoint create/verify。

```bash
continuity observe report --root PROJECT_ROOT --data-root DATA_ROOT
continuity context lookup --root PROJECT_ROOT --query SYMBOL --format brief --data-root DATA_ROOT
```

`retrieval_usage` 显示实际返回字节、hook 注入字节、按命令和 Session 分组的净估算。
净值 = 估计替代的原始输出字节 − 实际返回字节 − hook 注入字节，允许为负。
替代基线是估计，不是实际 token 计费或缓存节省率。只记录数字和匿名 Session 标识，
不保存正文、源码或凭据；默认本地开启，可设 `CONTINUITY_METER=0` 关闭。

## 执行建议 / Executor preferences

场景目录提供主控的判断起点：快速排查、日常实现、安全边界、对抗审查、深度设计、
独立域调查、回归验证。场景不决定唯一执行器，显式用途、复杂度和时效参数覆盖预设。

```bash
continuity collaboration scenarios
continuity collaboration recommend --scenario quick-triage --busy-model grok
continuity collaboration recommend --scenario security-boundary --busy-model glm --unavailable-model sol
continuity collaboration recommend --scenario deep-design --complexity normal --urgency normal
```

`--busy-model` 可重复，表示主控观察到拥挤，降低该模型优先级并允许切到其他适合的池；
`--unavailable-model` 可重复，排除确实不可用的模型。这里不探测真实 provider 队列。
排队、容量错误、超时或验证失败后，主控可以更新条件重算建议，不需要等待原模型恢复。
多个任务挤向同一模型时，先考虑独立范围拆分或移交；每部分须可独立验证、写入范围
无交叉、worktree 隔离。共享状态或证据耦合、协调成本较高时保留整体。
建议不自动启动执行器、重试、扩大范围或改变任务优先级；没有候选也不冻结主控本地工作。

路由按任务用途选择候选池，与主控模型、Claude Code 或 Codex 无关：

- 日常调查、修改和验证：Sonnet / Sol / Grok / GLM 四者同池分摊。
- 速度优先的有界任务：同等计数时 Grok 优先。
- 安全边界任务：同等计数时 GLM 优先；Sol 不可用时排除 Sol，并优先考虑 GLM。
- 对抗审查、难题且无紧迫时限：Astra / Opus 同池分摊；不为凑比例派发昂贵模型。

GLM 建议 `max`；Sol / Sonnet / Grok 默认 `high`，显式要求时可提高到 `max`；
Astra / Opus 建议 `high`。这些是可读的路由别名和偏好，执行器必须核验实际模型、
可用性和档位支持。OpenAI 的 [Codex 配置文档](https://developers.openai.com/codex/config-reference/)
明确说明 reasoning effort 的可用档位依赖所选模型与客户端；不将通用偏好直接当作 API 参数。
此策略不修改宿主配置，不提供调用额度承诺，不授予派发、合入或部署权限。

```bash
continuity collaboration recommend --project-id PROJECT --task-id TASK \
  --complexity high --urgency low --domain transport --parallel-safe --save
continuity collaboration recommend --purpose security --blocked-model sol
continuity collaboration recommend --purpose adversarial-review
continuity collaboration recommend --urgency high --thinking max
```

只有显式 `--save` 才在项目共享锁内按已保存任务建议的首选次数分摊同池模型。
同一任务重复保存相同条件保持首选不变；预览、packet 与 hook 读取均不消耗分摊次数。
四者均适配且可用时，优先选择已保存首选次数最少者，用途只用于同次数平局排序；
从零开始的任一分配前缀最大差不超过 1。历史 `5:1:1:0` 先补 GLM 等较少者，
直到追平；不为了均分使用 Astra/Opus。确有适配差异时用可重复的 `--eligible-model`
声明适合的候选，均分仅在该范围内进行；忙碌/不可用条件可暂时产生差距。
尚未保存路由的卡片按已有建议次数排序，同次数使用任务 ID 的稳定散列打破平局，
避免每份开场包都优先同一个模型；读取不创建路由记录。
这是计划分摊，尚未等同于真实派发次数、token 或费用均衡。独立数据根不会共享计数。
保存的候选与思考档位由 packet 及两种宿主的会话 handoff 读取；Todo 携带默认建议。
既有任务建议需要重新保存才采用新策略。模型切换接续同一计划与验证，不重做已完成项。
模型暂不可用时可交给可用执行者处理独立子任务；并行要求范围、worktree 和验证隔离。
建议缺失、额度不足或档位不支持均不构成普通工作的只读门。

Routing is controller- and host-independent. Balance Sonnet/Sol/Grok/GLM for general work and
Astra/Opus for hard flexible-deadline work or adversarial review; prefer Grok for speed
and GLM for security boundaries or unavailable Sol when planned counts tie. GLM uses max guidance; routine
models default to high with explicit max escalation; specialist models use high.
Only saved recommendations count toward pool balancing, atomically and idempotently.
Reads do not change allocation. This measures planned recommendations, not real usage.
Least-planned-first allocation keeps a fresh eligible four-model pool within a gap of one
and repairs historical skew. Use repeatable `--eligible-model` to narrow task suitability.
Adapters validate aliases, availability and effort support; no provider is launched.
Scenario presets are overridable starting points. Caller-reported congestion lowers a
model's priority; unavailable models are excluded. Queue delay, capacity errors, timeouts
and failed validation warrant reconsideration. Split only independently verifiable scopes
with isolated worktrees and non-overlapping writes; retain coupled tasks as a whole.

The core and both host adapters share a bounded session handoff. Save before compaction;
verify and quietly continue afterward. Repeated callbacks do not reinject the same handoff,
and sessions cannot read each other's return points. Claude ships a manifest, MCP config and
hooks using the core interpreter. Host-specific plugin enabling/reload remains necessary.

Accounting includes actual returned bytes and hook-injected bytes, grouped by command and
anonymous session. Signed net byte estimates can be negative. They do not establish measured
token savings. Executor preferences follow the current task and verification requirements;
availability and process dispatch remain the host's responsibility.
