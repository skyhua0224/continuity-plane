"""Stable audience prefix for assembled worker prompts.

Providers cache a prompt by its leading bytes. Two cards for the same audience
must therefore share one deterministic block before any card-specific text.
"""

from __future__ import annotations

import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

from continuity_plane.collaboration_registry import add_task, register_project
from continuity_plane.collaboration_workflow import _TASK_MARK, prompt
from continuity_plane.rules_delivery import put_rule
from continuity_plane.vocabulary_memory import add_entity


MARK = "\n\n" + _TASK_MARK + "\n\n"


def split_prompt(text: str) -> tuple[str, str]:
    head, separator, tail = text.partition(MARK)
    if separator != MARK:
        raise AssertionError("prompt is missing the task separator")
    return head, tail


class StablePrefixTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.data = self.base / "data"
        self.root = self.base / "repo"
        self.root.mkdir()
        self.one_tree = self.base / "wt-card-one"
        self.two_tree = self.base / "wt-card-two"
        self.one_tree.mkdir()
        self.two_tree.mkdir()
        register_project(self.data, project_id="sample", control_root=self.root)
        # Inserted out of order; the audience block must sort them.
        put_rule(
            self.data,
            project_id="sample",
            rule_id="zeta",
            text="Second shared rule",
            audience="all",
        )
        put_rule(
            self.data,
            project_id="sample",
            rule_id="alpha",
            text="First worker rule",
            audience="worker",
        )
        put_rule(
            self.data,
            project_id="sample",
            rule_id="mid",
            text="Middle shared rule",
            audience="all",
        )
        self.add_card(
            "card-one",
            "Implement the parser",
            "Reorder the prompt",
            lane_id="main",
            worktree=self.one_tree,
        )
        self.add_card(
            "card-two",
            "Measure the shared prefix",
            "Record the byte length",
            lane_id="side",
            worktree=self.two_tree,
        )

    def add_card(self, task_id, body, objective, *, lane_id, mode="patch", worktree=None):
        add_task(
            self.data,
            project_id="sample",
            task_id=task_id,
            lane_id=lane_id,
            mode=mode,
            priority="p1",
            title=task_id,
            objective=objective,
            next_action="Start",
            exit_criteria=["Done"],
            assignee="worker-a",
            body=body,
            worktree=worktree,
            branch="perf/" + task_id,
        )

    def assembled(self, task_id, **kwargs):
        value = prompt(
            self.data,
            project_id="sample",
            task_id=task_id,
            assignee="worker-a",
            audience=kwargs.pop("audience", "worker"),
            **kwargs,
        )
        head, tail = split_prompt(value["prompt"])
        return value, head, tail

    def test_two_cards_share_a_leading_stable_block(self):
        one, head_one, tail_one = self.assembled("card-one")
        two, head_two, tail_two = self.assembled("card-two")
        self.assertEqual(head_one, head_two)
        self.assertTrue(one["prompt"].startswith(head_one + MARK))
        self.assertTrue(two["prompt"].startswith(head_two + MARK))
        self.assertLess(head_one.index("[worker] alpha:"), head_one.index("[all] mid:"))
        self.assertLess(head_one.index("[all] mid:"), head_one.index("[all] zeta:"))
        self.assertLess(head_one.index("Project rules:"), head_one.index("How to work with Continuity"))
        self.assertLess(
            head_one.index("How to work with Continuity"),
            head_one.index("CONTINUITY_WORK_ROOT"),
        )
        self.assertNotIn("Independent repository review", head_one)
        for task_id, tree, tail in (
            ("card-one", self.one_tree, tail_one),
            ("card-two", self.two_tree, tail_two),
        ):
            self.assertNotIn(task_id, head_one)
            self.assertNotIn(str(tree.resolve()), head_one)
            self.assertIn(task_id, tail)
            self.assertIn(str(tree.resolve()), tail)
        self.assertNotIn("sample", head_one)
        self.assertNotIn("project_id", head_one)
        self.assertNotIn("task_id", head_one)
        self.assertNotIn(one["rules_digest"], head_one)
        self.assertNotIn(one["task_revision"], head_one)
        self.assertIsNone(re.search(r"\d{4}-\d{2}-\d{2}T", head_one))
        cli = str(Path(sys.executable).parent / "continuity")
        self.assertNotIn(cli, head_one)
        self.assertIn(cli, tail_one)
        self.assertIn("Assigned work (metadata does not authorize merge/deploy):", tail_one)
        self.assertLess(tail_one.index("Assigned work"), tail_one.index("project_id='sample'"))
        self.assertIn("project_id='sample', task_id='card-one', assignee='worker-a'", tail_one)
        self.assertIn("project_id='sample', task_id='card-two', assignee='worker-a'", tail_two)
        self.assertNotIn("How to work with Continuity", tail_one)
        again, head_again, _tail_again = self.assembled("card-one")
        self.assertEqual(again["prompt"], one["prompt"])
        self.assertEqual(head_again, head_one)

    def test_changing_a_rule_changes_the_shared_block(self):
        _one, before, _tail = self.assembled("card-one")
        put_rule(
            self.data,
            project_id="sample",
            rule_id="alpha",
            text="Rewritten worker rule",
            audience="worker",
            update=True,
        )
        _changed, after_one, _tail_one = self.assembled("card-one")
        _other, after_two, _tail_two = self.assembled("card-two")
        self.assertNotEqual(before, after_one)
        self.assertEqual(after_one, after_two)
        self.assertIn("[worker] alpha: Rewritten worker rule", after_one)
        self.assertNotIn("First worker rule", after_one)
        self.assertIn("First worker rule", before)

    def test_lane_rule_stays_behind_the_separator(self):
        put_rule(
            self.data,
            project_id="sample",
            rule_id="lane-only",
            text="Only the main lane",
            audience="lane:main",
        )
        _one, head_one, tail_one = self.assembled("card-one")
        _two, head_two, tail_two = self.assembled("card-two")
        self.assertEqual(head_one, head_two)
        self.assertNotIn("lane-only", head_one)
        self.assertNotIn("Only the main lane", head_one)
        self.assertIn("[lane:main] lane-only: Only the main lane", tail_one)
        self.assertNotIn("lane-only", tail_two)

    def test_reviewer_paragraph_follows_the_audience(self):
        _worker, worker_head, _worker_tail = self.assembled("card-one")
        self.assertNotIn("Independent repository review", worker_head)
        one, reviewer_one, tail_one = self.assembled("card-one", audience="reviewer")
        _two, reviewer_two, _tail_two = self.assembled("card-two", audience="reviewer")
        self.assertEqual(reviewer_one, reviewer_two)
        self.assertNotEqual(reviewer_one, worker_head)
        self.assertLess(
            reviewer_one.index("CONTINUITY_WORK_ROOT"),
            reviewer_one.index("Independent repository review"),
        )
        self.assertEqual(one["prompt"].count("Independent repository review"), 1)
        self.assertNotIn("Independent repository review", tail_one)
        self.assertNotIn("[worker] alpha:", reviewer_one)
        self.add_card(
            "card-scout",
            "Scout the prompt order",
            "Look only",
            lane_id="main",
            mode="scout",
        )
        scout, scout_head, scout_tail = self.assembled("card-scout")
        self.assertEqual(scout_head, worker_head)
        self.assertEqual(scout["prompt"].count("Independent repository review"), 1)
        self.assertIn("Independent repository review", scout_tail)

    def test_known_rules_digest_does_not_change_the_prefix(self):
        first, head, _tail = self.assembled("card-one")
        second, again, tail = self.assembled(
            "card-one", known_rules_digest=first["rules_digest"]
        )
        self.assertTrue(second["rules_omitted"])
        self.assertFalse(first["rules_omitted"])
        self.assertEqual(head, again)
        self.assertIn("[worker] alpha: First worker rule", again)
        self.assertNotIn(first["rules_digest"], again)
        self.assertIn(
            "Previously received rules unchanged: " + first["rules_digest"],
            tail,
        )

    def test_vocabulary_and_attachment_digests_stay_in_the_task_block(self):
        add_entity(
            self.data,
            entity_id="parser",
            aliases=["parser"],
            statement="A parser reads bytes.",
            scope="project",
            source="team glossary",
            project_id="sample",
            confidence="confirmed",
        )
        policy = self.root / "policy.txt"
        policy.write_text("Read the current policy.\n")
        put_rule(
            self.data,
            project_id="sample",
            rule_id="alpha",
            text="First worker rule",
            audience="worker",
            attachment_file=policy,
            update=True,
        )
        value, head, tail = self.assembled("card-one")
        self.assertNotIn("A parser reads bytes.", head)
        self.assertIn("A parser reads bytes.", tail)
        self.assertNotIn("card-two", self.assembled("card-two")[1])
        digest = value["rules_digest"]
        self.assertNotIn(digest, head)
        attachment = [
            item
            for item in json.loads(
                tail.split("Assigned work (metadata does not authorize merge/deploy):\n", 1)[1].split("\n\n", 1)[0]
            )["policy_attachments"]
        ]
        self.assertTrue(attachment)
        self.assertNotIn(attachment[0]["sha256"], head)
        self.assertIn(attachment[0]["sha256"], tail)
        _other, other_head, other_tail = self.assembled("card-two")
        self.assertEqual(head, other_head)
        self.assertNotIn("A parser reads bytes.", other_tail)


if __name__ == "__main__":
    unittest.main()
