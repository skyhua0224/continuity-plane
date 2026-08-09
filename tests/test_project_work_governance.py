import unittest
from pathlib import Path


class ProjectWorkGovernanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(__file__).parents[1]
        cls.contract = (
            cls.root / "docs" / "architecture" / "project-work-governance.md"
        ).read_text(encoding="utf-8")
        cls.gitignore = (cls.root / ".gitignore").read_text(encoding="utf-8")

    def test_project_has_one_master_and_personal_work_is_a_projection(self):
        self.assertIn("`MASTER.md` 只有一个项目级治理实例", self.contract)
        self.assertIn("`My Work`", self.contract)
        self.assertIn("个人视图不得使用 `MASTER` 命名", self.contract)

    def test_owner_worker_and_direction_axes_are_independent(self):
        self.assertIn("governance_owner_mode", self.contract)
        self.assertIn("single-owner | multi-owner", self.contract)
        self.assertIn("execution_worker_mode", self.contract)
        self.assertIn("single-worker | multi-worker", self.contract)
        self.assertIn("direction_state", self.contract)
        self.assertIn("discovery | governed | operational", self.contract)

    def test_issue_backlog_remains_a_task_source_without_master_duplication(self):
        self.assertIn("issue-backed", self.contract)
        self.assertIn("禁止把完整 Issue backlog 复制进 MASTER", self.contract)
        self.assertIn("source_revision", self.contract)

    def test_discovery_bootstrap_does_not_fabricate_a_roadmap(self):
        self.assertIn("Project Charter", self.contract)
        self.assertIn("明确未知项", self.contract)
        self.assertIn("mainline_authority: false", self.contract)

    def test_local_personal_projections_are_ignored_by_git(self):
        for pattern in (
            "STATUS.local.md",
            "MY_WORK.md",
            ".context-control-plane/local/",
            ".claude/worktrees/",
        ):
            with self.subTest(pattern=pattern):
                self.assertIn(pattern, self.gitignore)

    def test_shared_work_ledger_prevents_unaware_duplicate_work(self):
        self.assertIn("Work Ledger", self.contract)
        self.assertIn("其他协作者不可见", self.contract)
        self.assertIn("第二个未协调 claim", self.contract)

    def test_non_modular_projects_use_the_same_collaboration_core(self):
        self.assertIn("repository_topology: modular | monolith | mixed", self.contract)
        self.assertIn("模块边界不构成准入条件", self.contract)
        self.assertIn("Foundation Sunshine 当前为非模块化仓库", self.contract)
        for scope in ("repo", "directory", "file", "symbol", "capability", "effect"):
            with self.subTest(scope=scope):
                self.assertIn(scope, self.contract)


if __name__ == "__main__":
    unittest.main()
