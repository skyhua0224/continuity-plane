# Continuous Integration Policy

版本：1  
日期：2026-08-09  
状态：implemented locally / live Gitea verification pending

## 权限边界

CI 是独立 Verifier，权威状态写权限为 0。CI 读取 Git revision、schema、fixture、测试和治理投影，产生可引用的 verification evidence；它不能直接修改 active task、owner、claim、Decision、Checkpoint、promotion 状态或 MASTER governance authority。

分支保护、State MCP validator 或人类治理动作可以要求 CI evidence。CI 成功只证明该 revision 通过已声明的 Verification Profile，不单独构成任务完成、promotion 或 production release。

## 触发与作业

`.gitea/workflows/ci.yml` 在所有 branch push 和 pull request 上运行两个独立 required job：

| Job | 门禁 | 失败效果 |
|---|---|---|
| `repository-verification` | unit/contract tests、fixture privacy、benchmark determinism、Python compile、JSON/YAML parse、schema artifact hash、MASTER/STATUS/target-state revision、active leaf、raw transcript admission、Markdown link 与 style | revision 不得合并或晋升 |
| `secret-scan` | Gitleaks 默认规则、完整 Git history、精确 reviewed-test-vector allowlist | revision 不得合并或发布 |

Gitleaks 使用开源 CLI，不使用依赖 GitHub API 的 action wrapper。版本固定为 `8.30.1`，Linux x64 release archive 固定 SHA-256 为 `551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb`。allowlist 只能按已复核的非秘密值精确匹配；不得按 `tests/`、`docs/` 或 fixture 目录整体放行。

## 本地等价入口

```bash
python3 --version
python3 -m venv .venv
.venv/bin/python -m pip install --requirement requirements-dev.txt
.venv/bin/python -m unittest discover -s tests -p "test_*.py"
.venv/bin/python -m compileall -q context_control_plane tests tools
.venv/bin/python tools/verify_repository.py --root .
```

本地 Gitleaks 使用同一 version、archive checksum 和 `.gitleaks.toml`。本地通过不能替代 Gitea checkout、push 和 pull-request execution evidence；Gitea 失败也不能由本地结果覆盖。

## 可复现性与资源边界

- runner 提供 `python3`，workflow 记录实际版本并在项目 `.venv` 中隔离依赖；最低 Python 版本和多版本矩阵必须由后续兼容 fixture 确定，不从 runner label 推导。
- 直接依赖使用精确版本；Python 或依赖升级单独通过兼容与 replay 验证。
- benchmark 使用确定性 fixture、固定 budget 和 byte-equivalent result；provider live A/B 由后续阶段独立运行。
- 大型日志不写入 Git 或 PR；CI 只输出失败摘要和 artifact reference。
- job 不写生产数据库、不调用 State MCP commit、不部署 Product、不使用项目运行时 secret。
- 不同性能设备可以运行同一 verifier；性能门在 Project Profile 中声明硬件 class、样本数和基线，功能与一致性 veto 不因设备降级。

## 参考证据

ProjectCompute `.gitea/workflows/ci.yml` 的 2026-08-09 snapshot SHA-256 为 `bc8a2ce9c1964ba5d2a9055cd5ef621d5129664cebd16b5a8e6c16dd15aa8f90`。该证据确认当前 Gitea 环境采用 `ubuntu-latest`、`actions/checkout@v4`、push/PR required jobs 和独立 Gitleaks CLI job。Context Control Plane 只采用 Git/Gitea 集成事实；CI 仍遵守 provider-neutral typed-state、claim、CAS、checkpoint 和 evidence 权限边界。

## 验收门

- verifier 的 positive/negative regression tests 全部通过；
- 本地全量 test、compile、structured-data、schema、projection、privacy、link、style 和 secret scan 失败数为 0；
- Gitea `main` push 与 pull request 的两个 required job 均产生成功 evidence；
- branch protection 将两个 job 设为 required，未经审计的 bypass 为 0；
- CI 配置、dependency、allowlist 或 Verification Profile 变化触发 replay 与治理审阅。
