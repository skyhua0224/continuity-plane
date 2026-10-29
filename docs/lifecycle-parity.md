# 压缩接续与计量 / Lifecycle and accounting

[文档索引](README.md) · [Deployment](deployment.en.md) · [验证边界](validation.md)

## 当前版本与采用状态

本页适用 Alpha36。安装包、宿主插件、MCP 连接和原生 hook 采用必须分别核对；旧会话不会因为 pip 升级而热加载新代码。检查工具表、serverInfo.version 和实际事件，而不是只看 CLI 版本。不要更换 provider 凭据或整体覆盖宿主配置。

## 保存与恢复

Codex 与 Claude 使用有界 session handoff：压缩前保存下一步、约束、验证入口和检出信息；恢复读取当前用户意图与可验证接续点，同一代恢复只交付一次。共享主控现场包含计划、批次、worker、待决事项；worker 压缩不能覆盖主控计划。

权威集成 ref 和 checkout HEAD 分开。默认集成 ref 是注册根的 refs/heads/main；缺失就返回 unknown，不以另一个检出 HEAD 代替。可用 `collaboration context-set` 显式声明真实集成根与 ref。

| 宿主 | 保存 | 验证与注入 |
|---|---|---|
| Codex | PreCompact | 按该客户端支持的 PostCompact 恢复路径；需要实际 hook 信任 |
| Claude Code | PreCompact | PostCompact 验证；SessionStart(compact) 注入 |
| 无 hook 的执行方 | 显式 handoff／卡片收尾 | 启动时读取 packet／handoff，不承诺自动发生 |

共享交接因租约或 I/O 无法刷新时，本地保存继续，返回 `saved-with-warning`；恢复包保留这一事实。不能把仍在盘上的旧共享交接宣称为本次成功刷新。

Claude 的 SubagentStart/Stop 仅提供原机／profile 作用域的观察；没有 PID、心跳、退出码或成功推断。受控回调测试与真实原生子代理现场使用分别记录。

## 低噪声提醒

结束事件、阻塞和规则变化有实际用途；重复的“请先使用某功能”不应被当作采用成功。使用卡片即可工作的主控不必再维护第二份 Todo。Alpha35 的提示继续使用已知上下文生成完整命令，超长清单提示去重；缺少的写入参数不会被捏造。提示可执行不代表模型已经执行或采纳。

`CONTINUITY_DISABLED=1` 可临时关闭 hook；`CONTINUITY_HOOK_DEBUG=1` 用于桥接排障。普通元数据失效不授予新权限，也不应冻结普通源码工作。

## 真实计量与估算分开

```bash
continuity observe report --root . --data-root /path/to/data
continuity context lookup --root . --query YOUR_SYMBOL --format brief --max-output-bytes 1024
```

- 返回与注入字节：实际输出／hook emission point 计数。
- 净字节估算：估计替代的原始输出 − 返回 − hook 注入，允许负值；它不是计费差额。
- Provider token：在所属机器／profile 原地读取被明确指定的宿主 usage，保存计数与来源；不迁移原始会话。
- 提示词缓存与索引缓存：不同指标，不相互替代，也不等于 Continuity 导致的节省。
- Session 绑定：`session bind` 只从绑定基线之后归属任务／批次增量，不追溯摊分全历史，不合并不同 origin，不虚构价格。

未知 usage、窗口、费用和真实计费数据继续显示未知。公开历史实验不能替代长期真实会话的匹配对照。

## 派发建议

当前建议参考实际运行记录、时长与已记录 cooldown，而不只统计保存过的建议。显式模型选择和任务适配范围优先；忙碌／不可用可以使比例暂时不同。日常池与高成本专家池分开，思考档位是适配器实际支持的参数，不是所有模型通用的承诺。

```bash
continuity collaboration scenarios
continuity collaboration recommend --scenario quick-triage --busy-model grok
continuity collaboration recommend --scenario independent-review
```

推荐只是判断起点。派发必须使用已配置、原机/profile 匹配的适配器；认证错误与策略拒绝不通过换渠道自动重试。安全评审应准确描述授权范围和测试目标，不能通过隐藏真实性质规避审查。

## English summary

Alpha35 separates installation from observed host adoption. Claude verifies at PostCompact and injects at SessionStart(compact); Codex uses its supported native lifecycle. Shared-handoff failure is a visible warning, not a successful refresh. Native child callbacks are observations, not proven success. Actionable notices use known context, omit invented write arguments and deduplicate oversized-checklist advice; they do not prove model adoption.

Actual response/injection bytes, replacement estimates, provider usage, prompt-cache counters and index-cache hits have different meanings. Prospective task/batch bindings do not retroactively allocate historical usage or infer prices. Routing uses recorded runs/cooldowns, not merely saved recommendations. See [validation](validation.en.md) for controlled tests, user feedback and remaining gaps.
