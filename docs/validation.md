# 当前能力、实测与已知限制

[English](validation.en.md) · [工作流](workflows.md) · [历史实验](benchmarks.md)

范围：Alpha26（`0.1.0a26`）。证据分为本轮合成 CLI、已有自动回归、匿名使用反馈和未覆盖项，不能互相替代。

## 本轮可复现实测

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
| 收件箱发送、读取、ack | 通过 |
| 两个 Python worker 的 supervisor 运行与正常报告 | 通过 |
| 失败子进程 | 原退出码 3 被保存；dispatch CLI 非零（2） |
| 单次 merged 元数据与 commit 绑定检查 | 通过；不执行真实 Git 合入 |
| shared handoff、lesson、第二主控只读 | 通过 |
| 逐条事件翻页、多事件 wait、无变化读取 | 通过；该 fixture 无变化响应 196 bytes |
| worker 提前改为待评审后的报告 | 自动挂卡并由 report 读取；不接受手工挂载替代此检查 |

13 个正向检查通过，另验证提前改卡后的自动报告交付。字节数只描述该固定样例，不是生产项目吞吐、模型 token 降幅或费用节省。

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

## Alpha26 采用问题回归

| 修复 | 本轮证据 | 保留边界 |
|---|---|---|
| 提前改卡仍挂报告 | 待评审／完成／merged／blocked、迟到与重放回归；CLI 自动读取 | 旧 attempt、换执行者不覆盖新附件 |
| 稳定执行者 | 两个全新 MCP 进程；不同连接 ID 的卡片 Todo 激活 | 明确配置 executor_id/CONTINUITY_ASSIGNEE；不自动接管旧身份 |
| 可执行提示 | 命令经真实 parser 校验、去重、卡片不建第二队列 | 未知写参数不自动推断；不证明 worker 已采纳 |
| 来源 TSV | Unicode、扩展列、幂等、冲突／无效行无部分写入、MCP 接口 | 显式导入导出；不覆盖已有文件、不自动同步或验证已读 |
| 映射远端检索 | 独立合成仓库经 CLI 接收协议检索；映射失败／超预算拒绝 | 未做真实双机新实测；无任意远端文件／进程管理 |

回归入口：[test_a26_adoption.py](../tests/test_a26_adoption.py)，13 项合成检查。[部署](deployment.md)与[版本说明](release-alpha26.md)给出配置及兼容事项。

## 隐私与效果声明

公开证据只输出允许的聚合；原始会话、凭据、设备标识、私有项目与 profile 目录不进入 Git。宿主 usage 在所属机器／profile 原地读取，不能搬到别处查询。

高提示词缓存命中不等于 Continuity 导致的省 token。净字节为负也必须保留。长期每任务成本、窗口利用率和压缩间工作量需要可比较的真实会话、同源计数与明确质量门；当前未知项不填数字。
