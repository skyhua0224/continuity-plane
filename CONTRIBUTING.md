# 贡献指南

[English](CONTRIBUTING.en.md)

使用聚焦的分支，并根据改动风险提供相应测试或证据。

```bash
python -m venv .venv
.venv/bin/python -m pip install --editable '.[dev]'
.venv/bin/python -m unittest discover -s tests -p 'test_*.py'
```

状态、事件、checkpoint、claim、Skill 或证据合同的改动必须保持可回放和
版本化。Pull Request 需要说明故障模式、权限边界、执行过的验证和兼容性影响。

提交使用 Conventional Commits。禁止加入自动生成的 Agent 署名、原始对话、
凭据、个人文件系统路径或私有项目标识。

## 贡献授权

向项目提交贡献即表示同意该贡献按 Apache-2.0 提供。Commit 必须包含
Developer Certificate of Origin sign-off：

```text
Signed-off-by: Your Name <your-public-email@example.com>
```

产品名是 Continuity Plane。独立集成可以使用自己的名称，但不能暗示其为
官方发行版。
