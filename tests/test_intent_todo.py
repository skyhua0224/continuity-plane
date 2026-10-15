from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from continuity_plane.intent_todo import (
    IntentTodoError,
    MAX_ITEMS,
    _write,
    queue_path,
    block_item,
    compile_queue,
    complete_item,
    load_queue,
)


class IntentTodoTests(unittest.TestCase):
    def test_explicit_single_checklist_item_can_append_without_inventing_a_second(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertIsNone(
                compile_queue(directory, "just a question", data_root=directory)
            )
            first = compile_queue(directory, "- fix first", data_root=directory)
            appended = compile_queue(directory, "- add second", data_root=directory)
            self.assertEqual(len(appended["items"]), 2)
            self.assertEqual(
                appended["items"][0]["item_id"], first["items"][0]["item_id"]
            )
            self.assertEqual(sum(i["status"] == "active" for i in appended["items"]), 1)

    def test_fourteen_items_are_not_silently_truncated(self):
        with tempfile.TemporaryDirectory() as directory:
            queue = compile_queue(
                directory,
                "\n".join(f"{i}. work {i}" for i in range(1, 15)),
                data_root=directory,
            )
            self.assertEqual(len(queue["items"]), 14)

    def test_overflow_and_oversized_items_preserve_existing_queue(self):
        with tempfile.TemporaryDirectory() as directory:
            original = compile_queue(
                directory, "- first\n- second", data_root=directory
            )
            with self.assertRaisesRegex(IntentTodoError, "capacity"):
                compile_queue(
                    directory,
                    "\n".join(f"{i}. work {i}" for i in range(MAX_ITEMS + 1)),
                    data_root=directory,
                )
            self.assertEqual(load_queue(directory, directory), original)
            with self.assertRaisesRegex(IntentTodoError, "exceeds"):
                compile_queue(
                    directory, "- " + "中" * 180 + "\n- final", data_root=directory
                )
            self.assertEqual(load_queue(directory, directory), original)

    def test_replaying_legacy_truncated_prompt_restores_only_missing_items(self):
        with tempfile.TemporaryDirectory() as directory:
            prompt = "\n".join(f"{i}. work {i}" for i in range(1, 15))
            queue = compile_queue(directory, prompt, data_root=directory)
            queue = complete_item(
                directory,
                queue["items"][0]["item_id"],
                result="verified proof",
                data_root=directory,
            )
            queue["items"] = queue["items"][:12]
            _write(queue_path(directory, directory), queue)
            repaired = compile_queue(directory, prompt, data_root=directory)
            self.assertEqual(len(repaired["items"]), 14)
            self.assertEqual(repaired["items"][0]["status"], "completed")
            self.assertEqual(repaired["items"][0]["result"], "verified proof")
            self.assertEqual(
                sum(item["status"] == "active" for item in repaired["items"]), 1
            )
            self.assertEqual(
                compile_queue(directory, prompt, data_root=directory), repaired
            )

    def test_compile_is_bounded_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "repo"
            data = Path(directory) / "data"
            prompt = """修复桌面版卡片样式
            - 补充 iOS 中文说明
            - 修复首次读取失败
            - 加速二维码和新机启动
            - 修正 PR message 格式"""
            first = compile_queue(root, prompt, data_root=data)
            second = compile_queue(root, prompt, data_root=data)
            self.assertIsNotNone(first)
            self.assertEqual(first, second)
            self.assertEqual(first["status"], "active")
            self.assertEqual(len(first["items"]), 4)
            self.assertEqual(first["items"][0]["status"], "active")

    def test_completion_advances_and_terminal_completion_is_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "repo"
            data = Path(directory) / "data"
            queue = compile_queue(root, "- fix UI\n- add docs", data_root=data)
            first = complete_item(root, queue["items"][0]["item_id"], data_root=data)
            self.assertEqual(first["items"][1]["status"], "active")
            final = complete_item(root, first["items"][1]["item_id"], data_root=data)
            self.assertEqual(final["status"], "completed")
            self.assertEqual(load_queue(root, data_root=data), final)

    def test_block_stops_queue(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "repo"
            data = Path(directory) / "data"
            queue = compile_queue(root, "- fix UI\n- add docs", data_root=data)
            blocked = block_item(
                root,
                queue["items"][0]["item_id"],
                "needs user decision",
                data_root=data,
            )
            self.assertEqual(blocked["status"], "blocked")
            self.assertEqual(blocked["items"][0]["result"], "needs user decision")


if __name__ == "__main__":
    unittest.main()
