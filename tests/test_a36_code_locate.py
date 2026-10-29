from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from continuity_plane.code_index import (
    build_code_index,
    lookup_code_index,
    validate_code_index_receipt,
)


class A36CodeLocateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name) / "repo"
        self.root.mkdir()
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        subprocess.run(
            ["git", "-C", str(self.root), "config", "user.email", "a36@example.invalid"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(self.root), "config", "user.name", "A36 Test"],
            check=True,
        )
        (self.root / "stack.py").write_text(
            "class Stack:\n"
            "    def push(self, item):\n"
            "        self._items.append(item)\n"
            "\n"
            "    def pop(self):\n"
            "        return self._items.pop()\n"
            "\n"
            "\n"
            "def push_all(stack, items):\n"
            "    for item in items:\n"
            "        stack.push(item)\n",
            encoding="utf-8",
        )
        (self.root / "caller.py").write_text(
            "from stack import Stack\n"
            "\n"
            "def run():\n"
            "    s = Stack()\n"
            "    s.push(1)\n"
            "    return s.pop()\n",
            encoding="utf-8",
        )
        (self.root / "widget.h").write_text(
            "#ifndef WIDGET_H\n"
            "#define WIDGET_H\n"
            "\n"
            "namespace ui {\n"
            "class Widget {\n"
            "public:\n"
            "  void render();\n"
            "  static Widget *load(const char *name);\n"
            "};\n"
            "}  // namespace ui\n"
            "\n"
            "#endif\n",
            encoding="utf-8",
        )
        (self.root / "widget.cpp").write_text(
            '#include "widget.h"\n'
            "\n"
            "namespace ui {\n"
            "\n"
            "void Widget::render() {\n"
            "  repaint();\n"
            "}\n"
            "\n"
            "Widget *Widget::load(const char *name) {\n"
            "  return new Widget();\n"
            "}\n"
            "\n"
            "}  // namespace ui\n",
            encoding="utf-8",
        )
        (self.root / "main.cpp").write_text(
            '#include "widget.h"\n'
            "\n"
            "int main() {\n"
            "  ui::Widget *widget = ui::Widget::load(\"x\");\n"
            "  widget->render();\n"
            "  return 0;\n"
            "}\n",
            encoding="utf-8",
        )
        (self.root / "limits.h").write_text(
            "#ifndef LIMITS_H\n"
            "#define LIMITS_H\n"
            "\n"
            "namespace cfg {\n"
            "constexpr int MAX_SESSIONS = 12;\n"
            "enum mode_t { MODE_FAST, MODE_SLOW = 4 };\n"
            "}  // namespace cfg\n"
            "\n"
            "#endif\n",
            encoding="utf-8",
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(self.root),
                "add",
                "stack.py",
                "caller.py",
                "widget.h",
                "widget.cpp",
                "main.cpp",
                "limits.h",
            ],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(self.root), "commit", "-qm", "test: a36 fixture"],
            check=True,
        )
        self.cache = Path(self.directory.name) / "cache" / "index.json"

    def tearDown(self) -> None:
        self.directory.cleanup()

    def _lookup(self, query: str, **kwargs):
        receipt = lookup_code_index(
            self.root,
            query=query,
            cache_path=self.cache,
            record_usage=False,
            max_results=kwargs.pop("max_results", 10),
            max_output_bytes=kwargs.pop("max_output_bytes", 8192),
            **kwargs,
        )
        validate_code_index_receipt(receipt, root=self.root)
        return receipt

    def test_exact_symbol_ranks_before_prefix_and_substring(self) -> None:
        receipt = self._lookup("push")
        first = receipt["matches"][0]
        self.assertEqual((first["path"], first["name"]), ("stack.py", "push"))
        names = [item["name"] for item in receipt["matches"]]
        self.assertIn("push_all", names)
        self.assertLess(names.index("push"), names.index("push_all"))

    def test_python_qualified_method_query(self) -> None:
        receipt = self._lookup("Stack.push")
        self.assertEqual(receipt["matches"][0]["path"], "stack.py")
        self.assertEqual(receipt["matches"][0]["line"], 2)
        self.assertEqual(receipt["matches"][0]["name"], "push")
        kinds = [item["kind"] for item in receipt["matches"]]
        self.assertIn("usage", kinds)

    def test_cpp_qualified_method_and_namespace_scope(self) -> None:
        receipt = self._lookup("Widget::render")
        self.assertEqual(receipt["matches"][0]["path"], "widget.cpp")
        self.assertEqual(receipt["matches"][0]["line"], 5)
        self.assertEqual(receipt["matches"][0]["kind"], "c-function-definition")
        scoped = self._lookup("ui::Widget::load")
        paths = [(item["path"], item["kind"]) for item in scoped["matches"]]
        self.assertIn(("widget.cpp", "c-function-definition"), paths)
        self.assertIn(("widget.h", "c-function-declaration"), paths)

    def test_definitions_rank_before_usages(self) -> None:
        receipt = self._lookup("render")
        kinds = [item["kind"] for item in receipt["matches"]]
        first_usage = kinds.index("usage") if "usage" in kinds else len(kinds)
        self.assertLess(0, first_usage)
        self.assertEqual(kinds[0], "c-function-definition")
        usage_paths = {
            item["path"] for item in receipt["matches"] if item["kind"] == "usage"
        }
        self.assertTrue(usage_paths)

    def test_header_query_returns_file_and_includes(self) -> None:
        receipt = self._lookup("widget.h")
        first = receipt["matches"][0]
        self.assertEqual((first["path"], first["kind"]), ("widget.h", "file"))
        includes = [
            item
            for item in receipt["matches"]
            if item["kind"] == "include" and item["path"] != "widget.h"
        ]
        self.assertEqual(
            {item["path"] for item in includes}, {"widget.cpp", "main.cpp"}
        )

    def test_python_module_file_query_returns_importers(self) -> None:
        receipt = self._lookup("stack.py")
        self.assertEqual(receipt["matches"][0]["kind"], "file")
        self.assertEqual(receipt["matches"][0]["path"], "stack.py")
        imports = [item for item in receipt["matches"] if item["kind"] == "include"]
        self.assertEqual(imports[0]["path"], "caller.py")

    def test_constants_and_enum_constants_are_indexed(self) -> None:
        receipt = self._lookup("MAX_SESSIONS")
        self.assertEqual(receipt["matches"][0]["kind"], "constant")
        self.assertEqual(receipt["matches"][0]["line"], 5)
        enum_value = self._lookup("MODE_SLOW")
        self.assertEqual(enum_value["matches"][0]["kind"], "enum-constant")
        self.assertEqual(enum_value["matches"][0]["line"], 6)

    def test_usage_results_respect_extension_filters(self) -> None:
        receipt = self._lookup("Widget::load", extensions=["h"])
        paths = {item["path"] for item in receipt["matches"]}
        self.assertEqual(paths, {"widget.h"})
        self.assertTrue(
            all(item["kind"] != "usage" for item in receipt["matches"])
        )

    def test_warm_lookup_keeps_cache_file_untouched(self) -> None:
        first = build_code_index(self.root, cache_path=self.cache)
        self.assertEqual(first["cache_status"], "miss")
        stat_before = self.cache.stat()
        second = build_code_index(self.root, cache_path=self.cache)
        self.assertEqual(second["cache_status"], "hit")
        self.assertEqual(second["index_revision"], first["index_revision"])
        stat_after = self.cache.stat()
        self.assertEqual(
            (stat_before.st_mtime_ns, stat_before.st_size),
            (stat_after.st_mtime_ns, stat_after.st_size),
        )

    def test_mcp_brief_reply_stays_within_one_kilobyte(self) -> None:
        server = Path(__file__).resolve().parents[1] / (
            "continuity_plane/context_mcp_server.py"
        )
        request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "continuity_context_lookup",
                "arguments": {
                    "root": str(self.root),
                    "query": "push",
                    "format": "brief",
                    "cache_path": str(self.cache),
                },
            },
        }
        environment = dict(os.environ, CONTINUITY_METER="0")
        completed = subprocess.run(
            [sys.executable, str(server)],
            input=json.dumps(request) + "\n",
            text=True,
            capture_output=True,
            check=True,
            timeout=30,
            cwd=str(self.root.parent),
            env=environment,
        )
        reply = json.loads(completed.stdout.splitlines()[0])
        text = reply["result"]["content"][0]["text"]
        self.assertLessEqual(len(text.encode()), 1024)
        self.assertIn("stack.py:2", text)


if __name__ == "__main__":
    unittest.main()
