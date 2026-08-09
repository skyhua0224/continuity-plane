import unittest
from pathlib import Path


class AutonomousProgressPlanTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(__file__).parents[1]
        cls.master = (root / "MASTER.md").read_text(encoding="utf-8")
        cls.idea_continuity = (
            root / "docs" / "architecture" / "idea-continuity.md"
        ).read_text(encoding="utf-8")
        cls.dogfood_policy = (
            root / "docs" / "policies" / "project-dogfooding-observation.md"
        ).read_text(encoding="utf-8")

    def test_plan_has_input_deliberation_and_unattended_dispatch_leaves(self):
        self.assertIn("| M3-08 |", self.master)
        self.assertIn("| M8-09 |", self.master)

        routing = next(line for line in self.master.splitlines() if "| M3-08 |" in line)
        dispatcher = next(line for line in self.master.splitlines() if "| M8-09 |" in line)
        self.assertIn("blocking decision", routing)
        self.assertIn("unattended", dispatcher)
        self.assertIn("required/conditional/optional", dispatcher)
        self.assertIn("claim/lease", dispatcher)

    def test_non_blocking_inputs_do_not_displace_the_active_leaf(self):
        self.assertIn("`discussion_request`", self.idea_continuity)
        self.assertIn("`blocking_decision`", self.idea_continuity)
        self.assertIn("non-blocking input", self.idea_continuity)
        self.assertIn("bounded escalation", self.idea_continuity)

    def test_dogfood_requires_visible_compaction_interrupt_idea_and_agent_events(self):
        for phrase in (
            "visible compaction",
            "interrupt",
            "Idea",
            "multi-Agent dispatch/handoff",
        ):
            self.assertIn(phrase, self.dogfood_policy)


if __name__ == "__main__":
    unittest.main()
