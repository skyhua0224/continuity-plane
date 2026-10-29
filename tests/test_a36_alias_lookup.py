from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from continuity_plane.code_index import (
    build_code_index,
    lookup_code_index,
    validate_code_index_receipt,
)


class A36AliasLookupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name) / "repo"
        self.root.mkdir()
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(self.root),
                "config",
                "user.email",
                "a36@example.invalid",
            ],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(self.root), "config", "user.name", "A36 Alias Test"],
            check=True,
        )
        (self.root / "aliases.h").write_text(
            "#pragma once\n"
            "#include <memory>\n"
            "#include <vector>\n"
            "\n"
            "namespace safe {\n"
            "\n"
            "// Alias inside a namespace.\n"
            "using mail_t = std::shared_ptr<class mail_raw_t>;\n"
            "\n"
            "template <class T>\n"
            "using box_t = std::vector<T>;\n"
            "\n"
            "using typename_t = typename std::vector<int>::value_type;\n"
            "\n"
            "using prepare_fn =\n"
            "    void (*)(int);\n"
            "\n"
            "typedef unsigned int uint_t;\n"
            "typedef struct { int x; } point_t;\n"
            "typedef struct named_tag { int y; } named_alias_t;\n"
            "typedef enum { ALPHA, BETA } status_t;\n"
            "typedef char uuid_t[16];\n"
            "typedef void (*callback_t)(int);\n"
            "typedef void* (API_CALL *proc_t)(const char*);\n"
            "\n"
            "struct user {\n"
            "  using user_id_t = long;\n"
            "  typedef long legacy_id_t;\n"
            "};\n"
            "\n"
            "}  // namespace safe\n"
            "\n"
            "typedef struct {\n"
            "  int a;\n"
            "  int b;\n"
            "} multi_line_t;\n"
            "\n"
            "using namespace_alias_t = safe::mail_t;\n"
            "\n"
            "using std::size_t;\n"
            "\n"
            "#define DECLARE_ALIAS(alias_name) \\\n"
            "  typedef int alias_name;\n"
            "// using commented_t = int;\n",
            encoding="utf-8",
        )
        subprocess.run(
            ["git", "-C", str(self.root), "add", "aliases.h"], check=True
        )
        subprocess.run(
            ["git", "-C", str(self.root), "commit", "-qm", "test: a36 alias fixture"],
            check=True,
        )
        self.cache = Path(self.directory.name) / "cache" / "index.json"

    def tearDown(self) -> None:
        self.directory.cleanup()

    def _lookup(self, query: str):
        receipt = lookup_code_index(
            self.root,
            query=query,
            cache_path=self.cache,
            record_usage=False,
            max_results=10,
            max_output_bytes=8192,
        )
        validate_code_index_receipt(receipt, root=self.root)
        return receipt

    def _first(self, query: str) -> dict[str, object]:
        receipt = self._lookup(query)
        self.assertTrue(receipt["matches"], f"no match for {query}")
        return receipt["matches"][0]

    def test_using_alias_in_namespace_is_a_definition(self) -> None:
        first = self._first("mail_t")
        self.assertEqual(
            (first["path"], first["line"], first["kind"]),
            ("aliases.h", 8, "using"),
        )
        self.assertEqual(first["name"], "mail_t")
        receipt = self._lookup("mail_t")
        self.assertTrue(any(item["kind"] == "usage" for item in receipt["matches"]))

    def test_qualified_alias_query(self) -> None:
        first = self._first("safe::mail_t")
        self.assertEqual((first["line"], first["kind"]), (8, "using"))
        member = self._first("safe::user::user_id_t")
        self.assertEqual((member["line"], member["kind"]), (27, "using"))

    def test_template_and_typename_aliases(self) -> None:
        self.assertEqual(self._first("box_t")["line"], 11)
        self.assertEqual(self._first("typename_t")["line"], 13)

    def test_multiline_using_alias_records_its_declaring_line(self) -> None:
        first = self._first("prepare_fn")
        self.assertEqual((first["line"], first["kind"]), (15, "using"))

    def test_typedef_forms_are_definitions(self) -> None:
        expected = {
            "uint_t": 18,
            "point_t": 19,
            "named_alias_t": 20,
            "status_t": 21,
            "uuid_t": 22,
            "callback_t": 23,
            "proc_t": 24,
            "multi_line_t": 36,
        }
        for query, line in expected.items():
            with self.subTest(query=query):
                first = self._first(query)
                self.assertEqual((first["line"], first["kind"]), (line, "typedef"))
                self.assertEqual(first["name"], query)

    def test_named_struct_tag_stays_a_struct_definition(self) -> None:
        first = self._first("named_tag")
        self.assertEqual((first["line"], first["kind"]), (20, "struct"))

    def test_typedef_inside_class_body_is_scoped(self) -> None:
        first = self._first("safe::user::legacy_id_t")
        self.assertEqual((first["line"], first["kind"]), (28, "typedef"))

    def test_using_declaration_is_not_indexed_as_an_alias(self) -> None:
        receipt = self._lookup("safe::size_t")
        self.assertEqual(receipt["matches"], [])

    def test_macro_and_comment_aliases_are_not_indexed(self) -> None:
        self.assertEqual(self._lookup("alias_name")["matches"], [])
        self.assertEqual(self._lookup("commented_t")["matches"], [])

    def test_cache_round_trip_keeps_aliases(self) -> None:
        build = build_code_index(self.root, cache_path=self.cache)
        self.assertEqual(build["cache_status"], "miss")
        warm = build_code_index(self.root, cache_path=self.cache)
        self.assertEqual(warm["cache_status"], "hit")
        first = self._first("mail_t")
        self.assertEqual((first["line"], first["kind"]), (8, "using"))


if __name__ == "__main__":
    unittest.main()
