# M0-11 Git Admission Acceptance

版本：1  
日期：2026-08-14  
状态：verified offline Git admission contract

## 范围

`context.git-collaboration-packet/v1alpha1` 固定 branch、commit 和 pull-request 元数据。`context.git-admission-receipt/v1alpha1` 固定 staged、regular-merge 和 first-commit 离线审计结果。Git 只保存协作、审查与发布证据；运行时 Typed State、claim、lease、checkpoint、effect 和 authorization 保持独立权限边界。

## 验收结果

- branch ref 使用 provider-neutral work ID；provider thread ID、private path、secret、PII 和 machine material 被拒绝；
- commit subject 使用 Conventional Commit，body 包含 `Why`、`Task`、`State-Revision`、`Evidence` 和 `Tests`；
- pull-request body 包含 why、scope、state/ownership、evidence、validation、rollback 和 open questions；work ID、title、revision、parent、scope、return point、exit criteria、attempt budget、expiry 和 promotion target 与 branch/commit 合同一致；`mainline_authority=false`；
- staged audit 仅读取 Git index；unstaged worktree 内容不影响 receipt；empty index、raw transcript、secret、private path 和超过 8 MiB 的 staged blob 被拒绝；
- staged replay fixture 同时包含 validation receipt，并使用 M1-04 independent validator 重验 source provenance、sanitization、evidence、fixture hash 和 receipt hash；
- first-commit audit 重放 root author、commit contract 和 root tree admission；regular merge audit 要求恰好两个 parent commits；
- 所有 receipt 固定 `runtime_state_authority=false`，不构成 State MCP commit、claim、promotion 或 effect authorization；
- 两份 strict schema 已登记，M0-11 定向合同测试 `25/25` 通过；完整 SHA-256 integrity metadata、Git commit 和版本化证据引用通过 admission，secret、PII、provider ID 和 private path 继续拒绝。
- canonical 全仓 `731` tests 通过，`28` 个无 DSN PostgreSQL live tests skipped；repository verifier、Python compile 和 diff check 通过。

## 验证命令

```bash
.venv/bin/python -m unittest tests.test_m0_11_git_admission -v
.venv/bin/python -m unittest discover -s tests -p 'test_*.py'
.venv/bin/python -m compileall -q context_control_plane tests tools
.venv/bin/python tools/verify_repository.py --root .
git diff --check
```

M8-05 负责真实 actor authorization、tenant/project isolation 和 audit persistence。M8-08 负责 GitHub、Gitea 和 GitLab forge adapter、remote expected-ref checks 与 offline conflict handling。Git admission 不提供跨设备唯一 claim，也不替代 Typed State 的 revision/CAS。
