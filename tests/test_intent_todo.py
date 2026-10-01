from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from context_control_plane.intent_todo import (
    block_item,
    compile_queue,
    complete_item,
    load_queue,
)


class IntentTodoTests(unittest.TestCase):
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
            blocked = block_item(root, queue["items"][0]["item_id"], "needs user decision", data_root=data)
            self.assertEqual(blocked["status"], "blocked")
            self.assertEqual(blocked["items"][0]["result"], "needs user decision")


if __name__ == "__main__":
    unittest.main()
