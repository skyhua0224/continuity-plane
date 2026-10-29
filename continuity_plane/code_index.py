"""Incremental, provider-neutral code index for bounded symbol lookup."""

from __future__ import annotations

import bisect
import copy
import hashlib
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any


INDEX_SCHEMA_VERSION = "context.code-index/v1alpha1"
EXTRACTOR_REVISION = 4
LOOKUP_SCHEMA_VERSION = "context.code-index-lookup/v1alpha1"
MAX_QUERY_BYTES = 1024
MAX_FILES = 50_000
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_RESULTS = 200
MAX_OUTPUT_BYTES = 64 * 1024

_INDEX_FIELDS = {
    "schema_version",
    "repository_revision",
    "index_revision",
    "cache_status",
    "tracked_files",
    "indexed_files",
    "reused_files",
    "rehashed_files",
    "removed_files",
    "cache_key",
    "max_files",
    "state_write_authority",
    "memory_authority",
    "receipt_sha256",
}
_LOOKUP_FIELDS = {
    "schema_version",
    "repository_revision",
    "index_revision",
    "cache_status",
    "query_sha256",
    "matches",
    "match_count",
    "matched_before_limit",
    "truncated",
    "max_results",
    "max_output_bytes",
    "returned_bytes",
    "state_write_authority",
    "memory_authority",
    "receipt_sha256",
}
_ENTRY_FIELDS = {
    "path",
    "size_bytes",
    "mtime_ns",
    "sha256",
    "language",
    "symbols",
    "terms",
}
_SKIPPED_FIELDS = {"path", "size_bytes", "mtime_ns"}
_SYMBOL_FIELDS = {"kind", "name", "line", "qualname"}
_MATCH_FIELDS = {"path", "line", "kind", "name", "file_sha256"}

_LANGUAGE_BY_SUFFIX = {
    ".c": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".cxx": "cpp",
    ".cs": "csharp",
    ".go": "go",
    ".h": "c",
    ".hpp": "cpp",
    ".java": "java",
    ".js": "javascript",
    ".jsx": "javascript",
    ".m": "objective-c",
    ".mm": "objective-cpp",
    ".py": "python",
    ".rb": "ruby",
    ".rs": "rust",
    ".swift": "swift",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".vue": "vue",
}
_KEY_VALUE_SUFFIXES = {".toml", ".yaml", ".yml", ".json", ".cmake"}
_SYMBOL_PATTERNS = (
    ("class", re.compile(r"^\s*(?:export\s+)?class\s+([A-Za-z_$][\w$]*)")),
    (
        "function",
        re.compile(r"^\s*(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)"),
    ),
    ("function", re.compile(r"^\s*(?:async\s+)?def\s+([A-Za-z_]\w*)")),
    ("function", re.compile(r"^\s*(?:pub\s+)?(?:async\s+)?fn\s+([A-Za-z_]\w*)")),
    ("struct", re.compile(r"^\s*(?:pub\s+)?struct\s+([A-Za-z_]\w*)")),
    ("trait", re.compile(r"^\s*(?:pub\s+)?trait\s+([A-Za-z_]\w*)")),
    ("interface", re.compile(r"^\s*(?:export\s+)?interface\s+([A-Za-z_$][\w$]*)")),
    ("type", re.compile(r"^\s*(?:export\s+)?type\s+([A-Za-z_$][\w$]*)")),
    (
        "namespace",
        re.compile(r"^\s*namespace\s+([A-Za-z_]\w*)\s*(?:::[A-Za-z_]\w*)*\s*(?:\{|=)"),
    ),
    (
        "c-function",
        re.compile(
            r"^\s*((?:(?:static|inline|extern|const|constexpr|virtual|explicit|unsigned|signed"
            r"|long|short|struct\s+[A-Za-z_]\w*|enum\s+[A-Za-z_]\w*|class\s+[A-Za-z_]\w*)\s+"
            r"|[\w:<>*&]+\s+)*)"
            r"\s*(?:[*&]\s*)*([A-Za-z_]\w*(?:::[A-Za-z_]\w*)*)\s*\(([^;{}]*)\)\s*"
            r"(?:const\s*)?(?:noexcept\s*)?(?:override\s*)?(?:final\s*)?"
            r"(?:=\s*(?:default|delete|0)\s*)?"
            r"([{|;]|$)"
        ),
    ),
    (
        "constant",
        re.compile(
            r"^\s*(?:(?:static|constexpr|const|inline)\s+)+"
            r"(?:[A-Za-z_]\w*(?:::[A-Za-z_]\w*)*[\s*&]+)?"
            r"([A-Z][A-Z0-9_]*)\s*(?:=|\[)"
        ),
    ),
    ("struct", re.compile(r"^\s*(?:typedef\s+)?struct\s+([A-Za-z_]\w*)\b")),
    (
        "enum",
        re.compile(r"^\s*(?:typedef\s+)?enum(?:\s+(?:class|struct))?\s+([A-Za-z_]\w*)\b"),
    ),
    ("union", re.compile(r"^\s*(?:typedef\s+)?union\s+([A-Za-z_]\w*)\b")),
    ("macro", re.compile(r"^\s*#\s*define\s+([A-Za-z_]\w*)\b")),
)
_C_FAMILY = {"c", "cpp", "objective-c", "objective-cpp"}
_C_STATEMENT_KEYWORDS = {
    "if",
    "while",
    "switch",
    "for",
    "return",
    "else",
    "case",
    "delete",
    "throw",
    "co_return",
    "new",
    "sizeof",
    "void",
    "int",
    "char",
    "bool",
    "float",
    "double",
    "auto",
}
_C_TYPE_KEYWORDS = {
    "return",
    "else",
    "case",
    "delete",
    "throw",
    "co_return",
    "new",
    "sizeof",
    "typeof",
    "defined",
}
_SCOPE_DECL = re.compile(
    r"^\s*(?:typedef\s+)?(class|struct|union|enum(?:\s+class)?|namespace)\s+([A-Za-z_]\w*)"
)
_ENUM_CONSTANT_INLINE = re.compile(
    r"(?<![A-Za-z0-9_])([A-Z][A-Z0-9_]*)\s*(?:,|=|;|\})"
)
_C_USING_ALIAS = re.compile(r"\busing\s+([A-Za-z_]\w*)\s*=")
_C_TYPEDEF = re.compile(r"\btypedef\b")
_C_ALIAS_KEYWORDS = frozenset(
    {
        "auto",
        "bool",
        "char",
        "char8_t",
        "char16_t",
        "char32_t",
        "class",
        "const",
        "decltype",
        "double",
        "enum",
        "extern",
        "float",
        "inline",
        "int",
        "long",
        "noexcept",
        "register",
        "restrict",
        "short",
        "signed",
        "static",
        "struct",
        "template",
        "typedef",
        "typename",
        "union",
        "unsigned",
        "void",
        "volatile",
        "wchar_t",
    }
)
_SYMBOL_GATES = {
    "class": "class",
    "function": "def",
    "struct": "struct",
    "trait": "trait",
    "interface": "interface",
    "type": "type",
    "namespace": "namespace",
    "c-function": "(",
    "constant": "=",
    "enum": "enum",
    "union": "union",
    "macro": "#",
}
_FUNCTION_GATES = {"def": "def", "function": "function", "fn": "fn"}

_SYMBOL_TABLE: list[tuple[str, re.Pattern[str], str]] = []
for _kind, _pattern in _SYMBOL_PATTERNS:
    if _kind == "function":
        _gate = next(
            _token
            for _token in ("function", "def", "fn")
            if _token in _pattern.pattern
        )
    else:
        _gate = _SYMBOL_GATES[_kind]
    _SYMBOL_TABLE.append((_kind, _pattern, _gate))
_SYMBOL_TABLE_PLAIN = [
    item
    for item in _SYMBOL_TABLE
    if item[0]
    not in {"c-function", "macro", "union", "enum", "namespace", "constant"}
]


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _digest(value: dict[str, Any], field: str) -> str:
    unsigned = copy.deepcopy(value)
    unsigned.pop(field, None)
    return hashlib.sha256(_canonical(unsigned)).hexdigest()


def _git(root: Path, *arguments: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", "-C", str(root), *arguments],
        capture_output=True,
        check=False,
    )


def _repository_revision(root: Path) -> str:
    result = _git(root, "rev-parse", "HEAD")
    if result.returncode != 0:
        raise ValueError("repository revision is unavailable")
    try:
        revision = result.stdout.decode("ascii").strip()
    except UnicodeDecodeError as exc:
        raise ValueError("repository revision is invalid") from exc
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("repository revision is invalid")
    return revision


def _tracked_paths(root: Path) -> list[Path]:
    result = _git(root, "ls-files", "-z")
    if result.returncode != 0:
        raise ValueError("tracked file list is unavailable")
    paths: list[Path] = []
    for value in result.stdout.split(b"\0"):
        if not value:
            continue
        try:
            relative = Path(value.decode("utf-8"))
        except UnicodeDecodeError:
            continue
        if relative.is_absolute() or ".." in relative.parts:
            continue
        paths.append(relative)
    return sorted(paths, key=lambda path: path.as_posix())


def default_code_index_path(root: str | Path) -> Path:
    """Return a user-local cache path that cannot write into the project root."""
    root = Path(root).resolve()
    configured = os.environ.get("CONTINUITY_CODE_INDEX_CACHE")
    if configured:
        return Path(configured).expanduser() / f"{_cache_key(root)}.json"
    cache_root = os.environ.get("XDG_CACHE_HOME") or os.environ.get("LOCALAPPDATA")
    base = Path(cache_root).expanduser() if cache_root else Path.home() / ".cache"
    return base / "continuity-plane" / "code-index" / f"{_cache_key(root)}.json"


def _cache_key(root: Path) -> str:
    return hashlib.sha256(
        (str(root) + ":symbols-language-v4").encode("utf-8")
    ).hexdigest()[:32]


def _language(path: Path) -> str | None:
    return _LANGUAGE_BY_SUFFIX.get(path.suffix.lower())


def _matching_open(value: str, close: int, opener: str, closer: str) -> int | None:
    """Return the opener index that matches the closer at ``close``."""
    depth = 0
    for index in range(close, -1, -1):
        character = value[index]
        if character == closer:
            depth += 1
        elif character == opener:
            depth -= 1
            if depth == 0:
                return index
    return None


def _last_identifier(value: str) -> re.Match[str] | None:
    matches = list(re.finditer(r"[A-Za-z_]\w*", value))
    return matches[-1] if matches else None


def _typedef_alias_name(declaration: str) -> tuple[int, str] | None:
    """Return the declared name offset and text for a ``typedef`` statement."""
    body = declaration.rstrip()
    if not body.endswith(";"):
        return None
    body = body[:-1].rstrip()
    while body.endswith("]"):
        open_index = _matching_open(body, len(body) - 1, "[", "]")
        if open_index is None:
            return None
        body = body[:open_index].rstrip()
    if not body or body.endswith("}"):
        return None
    if body.endswith(")"):
        close = len(body) - 1
        open_index = _matching_open(body, close, "(", ")")
        if open_index is None:
            return None
        inner = body[open_index + 1 : close].strip()
        before = body[:open_index].rstrip()
        if before.endswith(")"):
            group_open = _matching_open(before, len(before) - 1, "(", ")")
            if group_open is None:
                return None
            declarator = before[group_open + 1 : -1]
            match = _last_identifier(declarator)
            if match is None or match.group(0) in _C_ALIAS_KEYWORDS:
                return None
            return group_open + 1 + match.start(), match.group(0)
        match = _last_identifier(before)
        if match is None:
            return None
        if (
            re.fullmatch(r"[A-Za-z_]\w*", inner) is not None
            and match.group(0) in _C_ALIAS_KEYWORDS
            and inner not in _C_ALIAS_KEYWORDS
        ):
            # ``typedef int (alias);`` is a parenthesised declarator, not a
            # function type.
            return body.index(inner, open_index + 1), inner
        if match.group(0) in _C_ALIAS_KEYWORDS:
            return None
        return match.start(), match.group(0)
    match = _last_identifier(body)
    if match is None or match.group(0) in _C_ALIAS_KEYWORDS:
        return None
    return match.start(), match.group(0)


def _c_alias_declarations(
    text: str, lines: list[str]
) -> dict[int, list[tuple[str, str]]]:
    """Map line numbers to ``(kind, name)`` C/C++ type alias declarations."""
    aliases: dict[int, list[tuple[str, str]]] = {}
    typedef_matches = list(_C_TYPEDEF.finditer(text))
    using_matches = list(_C_USING_ALIAS.finditer(text))
    if not typedef_matches and not using_matches:
        return aliases
    line_offsets = [0]
    for index, character in enumerate(text):
        if character == "\n":
            line_offsets.append(index + 1)
    regions: list[tuple[int, int]] = []
    in_block_comment = False
    for line in lines:
        start = 0
        end = len(line)
        if in_block_comment:
            close = line.find("*/")
            if close < 0:
                regions.append((end, end))
                continue
            start = close + 2
            in_block_comment = False
        index = start
        while index < len(line):
            if line.startswith("//", index):
                end = index
                break
            if line.startswith("/*", index):
                close = line.find("*/", index + 2)
                if close < 0:
                    end = index
                    in_block_comment = True
                    break
                index = close + 2
                continue
            index += 1
        regions.append((start, end))
    preprocessor: list[bool] = []
    continued = False
    for line in lines:
        directive = continued or line.lstrip().startswith("#")
        preprocessor.append(directive)
        continued = directive and line.rstrip().endswith("\\")

    def in_code(position: int) -> bool:
        line = bisect.bisect_right(line_offsets, position)
        column = position - line_offsets[line - 1]
        start, end = regions[line - 1]
        return start <= column < end and not preprocessor[line - 1]

    def record(position: int, kind: str, name: str) -> None:
        line = bisect.bisect_right(line_offsets, position)
        bucket = aliases.setdefault(line, [])
        if (kind, name) not in bucket:
            bucket.append((kind, name))

    for match in using_matches:
        if in_code(match.start(1)):
            record(match.start(1), "using", match.group(1))
    for match in typedef_matches:
        if not in_code(match.start()):
            continue
        depth = 0
        end: int | None = None
        for index in range(match.end(), len(text)):
            character = text[index]
            if character in "{(":
                depth += 1
            elif character in "})":
                depth -= 1
                if depth < 0:
                    break
            elif character == ";" and depth == 0:
                end = index
                break
        if end is None:
            continue
        parsed = _typedef_alias_name(text[match.start() : end + 1])
        if parsed is None:
            continue
        offset, name = parsed
        record(match.start() + offset, "typedef", name)
    return aliases


def _symbols(payload: bytes, language: str | None = None) -> list[dict[str, Any]]:
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        return []
    result: list[dict[str, Any]] = []
    lines = text.splitlines()
    seen: set[tuple[str, str, int]] = set()
    python_stack: list[tuple[int, str]] = []
    c_scopes: list[tuple[int, str, str]] = []
    c_depth = 0
    pending_scope: tuple[str, str] | None = None
    name_words: dict[str, re.Pattern[str]] = {}
    alias_lines = (
        _c_alias_declarations(text, lines) if language in _C_FAMILY else {}
    )
    for line_number, line in enumerate(lines, 1):
        before_depth = c_depth
        if language == "python":
            if line.strip():
                # Blank lines do not end a Python block.
                indent = len(line) - len(line.lstrip(" \t"))
                while python_stack and indent <= python_stack[-1][0]:
                    python_stack.pop()
        elif language in _C_FAMILY:
            scope_match = _SCOPE_DECL.match(line)
            if scope_match is not None:
                pending_scope = (
                    scope_match.group(2),
                    scope_match.group(1).split()[0],
                )
                if pending_scope[1] == "enum" and "{" in line:
                    for constant_match in _ENUM_CONSTANT_INLINE.finditer(
                        line.split("{", 1)[1]
                    ):
                        qualname = pending_scope[0] + "::" + constant_match.group(1)
                        key = ("enum-constant", qualname, line_number)
                        if key not in seen:
                            result.append(
                                {
                                    "kind": "enum-constant",
                                    "name": constant_match.group(1),
                                    "line": line_number,
                                    "qualname": qualname,
                                }
                            )
                            seen.add(key)
            elif (
                pending_scope is not None
                and pending_scope[1] == "enum"
                and "{" in line
            ) or (c_scopes and c_scopes[-1][2] == "enum"):
                body = line
                if pending_scope is not None and "{" in line:
                    body = line.split("{", 1)[1]
                for constant_match in _ENUM_CONSTANT_INLINE.finditer(body):
                    qualname = constant_match.group(1)
                    if c_scopes:
                        qualname = (
                            "::".join(scope for _, scope, _ in c_scopes)
                            + "::"
                            + qualname
                        )
                    elif pending_scope is not None:
                        qualname = pending_scope[0] + "::" + qualname
                    key = ("enum-constant", qualname, line_number)
                    if key not in seen:
                        result.append(
                            {
                                "kind": "enum-constant",
                                "name": constant_match.group(1),
                                "line": line_number,
                                "qualname": qualname,
                            }
                        )
                        seen.add(key)
        candidates = [line]
        if language in _C_FAMILY:
            candidates.extend(
                " ".join(lines[line_number - 1 : line_number + width])
                for width in range(1, min(8, len(lines) - line_number + 1))
            )
        table = _SYMBOL_TABLE if language in _C_FAMILY else _SYMBOL_TABLE_PLAIN
        matched = False
        for candidate_index, candidate in enumerate(candidates):
            for kind, pattern, gate in table:
                if gate not in candidate:
                    continue
                match = pattern.match(candidate)
                if match is not None:
                    name = match.group(1)
                    start = match.start(1)
                    if kind == "c-function":
                        name = match.group(2)
                        start = match.start(2)
                    prefix = candidate[:start].strip()
                    prefix_tail = prefix.split()[-1] if prefix else ""
                    if kind == "c-function" and (
                        name in _C_STATEMENT_KEYWORDS
                        or prefix_tail in _C_TYPE_KEYWORDS
                        or (not prefix and ("::" not in name or match.group(4) != "{"))
                    ):
                        continue
                    if kind == "c-function":
                        tail = "\n".join(lines[line_number - 1 : line_number + 15])
                        opening = re.search(
                            r"\b" + re.escape(name) + r"\s*\(", tail
                        )
                        if opening:
                            depth = 1
                            end = opening.end()
                            while end < len(tail) and depth:
                                depth += (tail[end] == "(") - (tail[end] == ")")
                                end += 1
                            marker = (
                                re.search(r"[;{]", tail[end:]) if depth == 0 else None
                            )
                            if marker:
                                kind = (
                                    "c-function-definition"
                                    if marker[0] == "{"
                                    else "c-function-declaration"
                                )
                            chain = name.split("::")
                            if len(chain) > 1 and len(set(chain)) == 1:
                                # Constructors collide with their own class
                                # name; rank them below real definitions.
                                kind = "c-constructor"
                    qualname = name
                    qualified_text = "::" in name
                    if qualified_text:
                        # Match and display the bare method name; the qualified
                        # spelling stays available for qualified queries.
                        name = name.rsplit("::", 1)[-1]
                    if language in _C_FAMILY and c_scopes and not qualified_text:
                        qualname = (
                            "::".join(scope for _, scope, _ in c_scopes) + "::" + name
                        )
                    elif language == "python" and kind == "function" and python_stack:
                        qualname = python_stack[-1][1] + "." + name
                    recorded = line_number
                    if candidate_index > 0:
                        # A joined multi-line candidate starts at this line, but
                        # the reported line should be where the name appears.
                        full_name = qualname if "::" in qualname else name
                        name_word = name_words.get(full_name)
                        if name_word is None:
                            name_word = re.compile(
                                r"(?<![A-Za-z0-9_])"
                                + re.escape(full_name)
                                + r"(?![A-Za-z0-9_])"
                            )
                            name_words[full_name] = name_word
                        for offset in range(0, candidate_index + 1):
                            probe = lines[line_number - 1 + offset]
                            if name_word.search(probe) is not None:
                                recorded = line_number + offset
                                break
                        if recorded == line_number and qualname != name:
                            # Scope-stack qualnames never appear verbatim in
                            # the source; fall back to the bare name.
                            bare_word = name_words.get(name)
                            if bare_word is None:
                                bare_word = re.compile(
                                    r"(?<![A-Za-z0-9_])"
                                    + re.escape(name)
                                    + r"(?![A-Za-z0-9_])"
                                )
                                name_words[name] = bare_word
                            for offset in range(0, candidate_index + 1):
                                probe = lines[line_number - 1 + offset]
                                if bare_word.search(probe) is not None:
                                    recorded = line_number + offset
                                    break
                    key = (kind, qualname, recorded)
                    if key not in seen:
                        result.append(
                            {
                                "kind": kind,
                                "name": name,
                                "line": recorded,
                                "qualname": qualname,
                            }
                        )
                        seen.add(key)
                    matched = True
                    break
            if matched:
                break
        if language == "python":
            class_match = re.match(
                r"^\s*(?:export\s+)?class\s+([A-Za-z_$][\w$]*)", line
            )
            if class_match is not None:
                indent = len(line) - len(line.lstrip(" \t"))
                python_stack.append((indent, class_match.group(1)))
        elif language in _C_FAMILY:
            c_depth += line.count("{") - line.count("}")
            if pending_scope is not None and c_depth > before_depth:
                c_scopes.append((c_depth, pending_scope[0], pending_scope[1]))
                pending_scope = None
            elif pending_scope is not None and c_depth == before_depth and "}" in line:
                # A one-line scope such as ``struct foo { int x; };`` opens and
                # closes without leaving a scope on the stack.
                pending_scope = None
            elif pending_scope is not None and ";" in line and "{" not in line:
                pending_scope = None
            while c_scopes and c_depth < c_scopes[-1][0]:
                c_scopes.pop()
            scope_prefix = "::".join(scope for _, scope, _ in c_scopes)
            for alias_kind, alias_name in alias_lines.get(line_number, ()):
                qualname = (
                    scope_prefix + "::" + alias_name if scope_prefix else alias_name
                )
                key = (alias_kind, qualname, line_number)
                if key not in seen:
                    result.append(
                        {
                            "kind": alias_kind,
                            "name": alias_name,
                            "line": line_number,
                            "qualname": qualname,
                        }
                    )
                    seen.add(key)
        if not result or result[-1]["line"] != line_number:
            key = re.match(r"^\s*([A-Za-z][A-Za-z0-9_.-]*)\s*(?:=|:)", line)
            if key and line.lstrip().split("=", 1)[0].strip() == key.group(1):
                result.append(
                    {
                        "kind": "config-key",
                        "name": key.group(1),
                        "line": line_number,
                        "qualname": key.group(1),
                    }
                )
    return result


def _read_cache(path: Path, root: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != INDEX_SCHEMA_VERSION
        or value.get("extractor_revision") != EXTRACTOR_REVISION
        or value.get("cache_key") != _cache_key(root)
        or not isinstance(value.get("entries"), list)
        or not isinstance(value.get("skipped"), list)
    ):
        return None
    entries: dict[str, dict[str, Any]] = {}
    for entry in value["entries"]:
        if (
            isinstance(entry, dict)
            and set(entry) == _ENTRY_FIELDS
            and isinstance(entry.get("path"), str)
        ):
            entries[entry["path"]] = entry
    value["entries"] = entries
    value["skipped"] = {
        item["path"]: item
        for item in value["skipped"]
        if isinstance(item, dict)
        and set(item) == _SKIPPED_FIELDS
        and isinstance(item.get("path"), str)
    }
    return value


def _write_cache(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(
        prefix=".code-index-", suffix=".tmp", dir=path.parent
    )
    try:
        with open(handle, "w", encoding="utf-8") as stream:
            json.dump(
                document,
                stream,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _entry_for(root: Path, relative: Path) -> dict[str, Any] | None:
    path = (root / relative).resolve()
    try:
        path.relative_to(root)
        stat = path.stat()
        if not path.is_file() or stat.st_size > MAX_FILE_BYTES:
            return None
        payload = path.read_bytes()
    except (OSError, ValueError):
        return None
    if b"\0" in payload[:8192]:
        return None
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        return None
    language = _language(path)
    return {
        "path": relative.as_posix(),
        "size_bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "sha256": hashlib.sha256(payload).hexdigest(),
        "language": language or "text",
        "symbols": _symbols(payload, language),
        "terms": sorted(
            set(re.findall(r"[A-Za-z_][A-Za-z0-9_-]{2,}", text.casefold()))
        )[:4096],
    }


def _index_revision(
    repository_revision: str,
    entries: dict[str, dict[str, Any]],
    skipped: dict[str, dict[str, Any]],
) -> str:
    stable = {
        "repository_revision": repository_revision,
        "entries": [entries[path] for path in sorted(entries)],
        "skipped": [skipped[path] for path in sorted(skipped)],
    }
    return hashlib.sha256(_canonical(stable)).hexdigest()


def _load_index(
    root: str | Path,
    *,
    cache_path: str | Path | None = None,
    max_files: int = 50_000,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Incrementally index tracked text files into disposable user-local storage."""
    root = Path(root).resolve()
    if type(max_files) is not int or not 1 <= max_files <= MAX_FILES:
        raise ValueError("max_files is invalid")
    cache = (
        Path(cache_path).expanduser().resolve()
        if cache_path is not None
        else default_code_index_path(root).resolve()
    )
    if root == cache or root in cache.parents:
        raise ValueError("code index cache must be outside the project root")
    revision = _repository_revision(root)
    tracked = _tracked_paths(root)
    if len(tracked) > max_files:
        raise ValueError("tracked file count exceeds max_files")
    previous = _read_cache(cache, root)
    old_entries = previous["entries"] if previous is not None else {}
    old_skipped = previous["skipped"] if previous is not None else {}
    entries: dict[str, dict[str, Any]] = {}
    skipped: dict[str, dict[str, Any]] = {}
    reused = 0
    rehashed = 0
    for relative in tracked:
        key = relative.as_posix()
        old = old_entries.get(key)
        path = root / relative
        try:
            stat = path.stat()
        except OSError:
            continue
        if (
            isinstance(old, dict)
            and old.get("size_bytes") == stat.st_size
            and old.get("mtime_ns") == stat.st_mtime_ns
        ):
            entries[key] = old
            reused += 1
            continue
        old_skip = old_skipped.get(key)
        if (
            isinstance(old_skip, dict)
            and old_skip.get("size_bytes") == stat.st_size
            and old_skip.get("mtime_ns") == stat.st_mtime_ns
        ):
            skipped[key] = old_skip
            reused += 1
            continue
        entry = _entry_for(root, relative)
        rehashed += 1
        if entry is not None:
            entries[key] = entry
        else:
            skipped[key] = {
                "path": key,
                "size_bytes": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
            }
    removed = (
        len((set(old_entries) | set(old_skipped)) - (set(entries) | set(skipped)))
        if previous is not None
        else 0
    )
    status = (
        "miss"
        if previous is None
        else "hit"
        if rehashed == 0 and removed == 0
        else "partial"
    )
    document = {
        "schema_version": INDEX_SCHEMA_VERSION,
        "extractor_revision": EXTRACTOR_REVISION,
        "cache_key": _cache_key(root),
        "repository_revision": revision,
        "index_revision": "",
        "entries": entries,
        "skipped": skipped,
    }
    if status == "hit" and previous is not None:
        # Nothing changed: reuse the stored revision and keep the cache file
        # untouched so a warm lookup costs one read instead of a full rewrite.
        index_revision = previous["index_revision"]
    else:
        index_revision = _index_revision(revision, entries, skipped)
        _write_cache(
            cache,
            {
                "schema_version": INDEX_SCHEMA_VERSION,
                "extractor_revision": EXTRACTOR_REVISION,
                "cache_key": _cache_key(root),
                "repository_revision": revision,
                "index_revision": index_revision,
                "entries": [entries[key] for key in sorted(entries)],
                "skipped": [skipped[key] for key in sorted(skipped)],
            },
        )
    document["index_revision"] = index_revision
    receipt = {
        "schema_version": INDEX_SCHEMA_VERSION,
        "repository_revision": revision,
        "index_revision": index_revision,
        "cache_status": status,
        "tracked_files": len(tracked),
        "indexed_files": len(entries),
        "reused_files": reused,
        "rehashed_files": rehashed,
        "removed_files": removed,
        "cache_key": _cache_key(root),
        "max_files": max_files,
        "state_write_authority": False,
        "memory_authority": False,
        "receipt_sha256": "",
    }
    receipt["receipt_sha256"] = _digest(receipt, "receipt_sha256")
    validate_code_index_receipt(receipt)
    return receipt, document


def build_code_index(
    root: str | Path,
    *,
    cache_path: str | Path | None = None,
    max_files: int = 50_000,
) -> dict[str, Any]:
    """Incrementally index tracked text files into disposable user-local storage."""
    receipt, _ = _load_index(root, cache_path=cache_path, max_files=max_files)
    return receipt


def _query(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or "\0" in value
        or "\n" in value
        or "\r" in value
        or len(value.encode("utf-8")) > MAX_QUERY_BYTES
    ):
        raise ValueError("query is invalid")
    return value.casefold()


def _query_terms(value: str) -> list[str]:
    return [
        term
        for term in re.findall(r"[\w][\w.-]*", value.casefold(), re.UNICODE)
        if term
    ]


_DEFINITION_RANK = {
    "function": 0,
    "c-function-definition": 0,
    "class": 0,
    "struct": 0,
    "enum": 0,
    "macro": 0,
    "typedef": 0,
    "using": 0,
    "interface": 0,
    "trait": 0,
    "union": 0,
    "namespace": 0,
    "type": 0,
    "c-function-declaration": 1,
}


_LANGUAGE_SUFFIXES = {
    "c": {
        ".c",
        ".cc",
        ".cpp",
        ".cxx",
        ".h",
        ".hh",
        ".hpp",
        ".inl",
        ".inc",
    },
    "cpp": {
        ".c",
        ".cc",
        ".cpp",
        ".cxx",
        ".h",
        ".hh",
        ".hpp",
        ".inl",
        ".inc",
        ".m",
        ".mm",
    },
    "objective-c": {".m", ".h"},
    "objective-cpp": {".mm", ".h", ".hpp"},
    "python": {".py"},
    "javascript": {".js", ".jsx", ".mjs", ".cjs", ".vue", ".ts", ".tsx"},
    "typescript": {".ts", ".tsx", ".js", ".jsx"},
    "go": {".go"},
    "rust": {".rs"},
    "java": {".java"},
    "ruby": {".rb"},
    "swift": {".swift"},
    "csharp": {".cs"},
    "vue": {".vue", ".js", ".ts"},
}


def _path_penalty(path: str) -> int:
    penalty = (
        4
        if Path(path).name.casefold() == "cmakelists.txt"
        or Path(path).suffix == ".cmake"
        else 0
    )
    lowered = path.casefold()
    if any(
        value in lowered
        for value in ("archive/", "generated/", "third_party/", "vendor/", "benchmarks/")
    ):
        penalty += 6
    if Path(path).suffix in {".md", ".txt", ".rst"}:
        penalty += 3
    return penalty


def _usage_matches(
    root: Path,
    *,
    seed: str,
    language: str,
    allowed_extensions: set[str],
    path_prefix: str | None,
    exclude: set[tuple[str, int]],
    limit: int,
) -> tuple[list[dict[str, Any]], int]:
    """Collect bounded usage lines with word matches and diverse file coverage."""
    suffixes = _LANGUAGE_SUFFIXES.get(language)
    if suffixes is None or limit <= 0:
        return [], 0
    if allowed_extensions:
        suffixes = suffixes & allowed_extensions
    if not suffixes:
        return [], 0
    result = _git(root, "grep", "-n", "-I", "-w", "-F", "-m", "4", "--", seed)
    if result.returncode not in {0, 1}:
        return [], 0
    per_path: dict[str, list[dict[str, Any]]] = {}
    hashes: dict[str, str | None] = {}
    payloads: dict[str, bytes | None] = {}
    gathered = 0
    for raw in result.stdout.splitlines():
        parts = raw.split(b":", 2)
        if len(parts) != 3:
            continue
        try:
            path = parts[0].decode("utf-8")
            line = int(parts[1].decode("ascii"))
            text = parts[2].decode("utf-8", "replace")
        except (UnicodeDecodeError, ValueError):
            continue
        if Path(path).suffix not in suffixes:
            continue
        if path_prefix and not (
            path == path_prefix or path.startswith(path_prefix.rstrip("/") + "/")
        ):
            continue
        if (path, line) in exclude:
            continue
        gathered += 1
        if gathered > 80:
            break
        if path not in payloads:
            try:
                payloads[path] = (root / path).read_bytes()
                hashes[path] = hashlib.sha256(payloads[path]).hexdigest()
            except OSError:
                payloads[path] = None
                hashes[path] = None
        if hashes[path] is None:
            continue
        kind = "usage"
        stripped = text.lstrip()
        if stripped.startswith(("#include", "#import")) or stripped.startswith(
            ("import ", "from ")
        ):
            kind = "include"
        per_path.setdefault(path, []).append(
            {
                "path": path,
                "line": line,
                "kind": kind,
                "name": seed,
                "file_sha256": hashes[path],
            }
        )
    selected: list[dict[str, Any]] = []
    order = sorted(per_path)
    while len(selected) < limit and any(per_path.values()):
        for path in order:
            bucket = per_path[path]
            if bucket:
                selected.append(bucket.pop(0))
                if len(selected) >= limit:
                    break
    return selected, gathered


def lookup_code_index(
    root: str | Path,
    *,
    query: str,
    cache_path: str | Path | None = None,
    max_results: int = 20,
    max_output_bytes: int = 8192,
    data_root: str | Path | None = None,
    record_usage: bool = True,
    path_prefix: str | None = None,
    extensions: list[str] | None = None,
) -> dict[str, Any]:
    """Return bounded symbol/path references without returning source bodies."""
    _query(query)
    if extensions is not None and (
        not isinstance(extensions, list)
        or not all(isinstance(v, str) for v in extensions)
    ):
        raise ValueError("extensions must be a string list")
    if path_prefix is not None and not isinstance(path_prefix, str):
        raise ValueError("path_prefix must be a string")
    if path_prefix and (
        Path(path_prefix).is_absolute() or ".." in Path(path_prefix).parts
    ):
        raise ValueError("path_prefix must be a repository-relative path")
    allowed_extensions = {"." + v.lstrip(".") for v in (extensions or [])}
    terms = _query_terms(query)
    if type(max_results) is not int or not 1 <= max_results <= MAX_RESULTS:
        raise ValueError("max_results is invalid")
    if (
        type(max_output_bytes) is not int
        or not 1024 <= max_output_bytes <= MAX_OUTPUT_BYTES
    ):
        raise ValueError("max_output_bytes is invalid")
    root_path = Path(root).resolve()
    index, document = _load_index(root_path, cache_path=cache_path)
    full_query = query.strip()
    lower_query = full_query.casefold()
    query_suffix = Path(full_query).suffix
    path_like = (
        "/" in full_query
        or query_suffix in _LANGUAGE_BY_SUFFIX
        or query_suffix in _KEY_VALUE_SUFFIXES
        or query_suffix in {".inl", ".inc", ".md", ".txt", ".rst"}
    )
    identifier_segments = (
        re.findall(r"[A-Za-z_]\w*", full_query) if not path_like else []
    )
    last_segment_raw = (
        Path(full_query).name
        if path_like and Path(full_query).name
        else identifier_segments[-1]
        if identifier_segments
        else full_query
    )
    last_segment = last_segment_raw.casefold()
    qualified_query = (
        not path_like
        and "::" in full_query
        or not path_like
        and re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+", full_query)
        is not None
    )
    qualifier_set = (
        {segment.casefold() for segment in identifier_segments[:-1]}
        if qualified_query
        else set()
    )
    word_pattern = (
        re.compile(
            r"(?<![A-Za-z0-9])" + re.escape(last_segment) + r"(?![A-Za-z0-9])"
        )
        if last_segment
        else None
    )
    multi_term = len(terms) > 1 and not qualified_query and not path_like
    # (tier, definition rank, path penalty, path, line, kind, name, sha, language)
    candidates: list[tuple[Any, ...]] = []
    for entry in document["entries"].values():
        path = entry["path"]
        if path_prefix and not (
            path == path_prefix or path.startswith(path_prefix.rstrip("/") + "/")
        ):
            continue
        if allowed_extensions and Path(path).suffix not in allowed_extensions:
            continue
        path_lower = path.casefold()
        termset = set(entry["terms"]) if multi_term else None
        entry_has_terms = (
            all(term in termset or term in path_lower for term in terms)
            if termset is not None
            else True
        )
        for symbol in entry["symbols"]:
            name = symbol["name"]
            name_lower = name.casefold()
            qual_lower = (symbol.get("qualname") or name).casefold()
            tier = None
            if qualified_query:
                if symbol.get("qualname") == full_query:
                    tier = 0
                elif qual_lower == lower_query:
                    tier = 1
                elif name_lower == last_segment:
                    tier = (
                        3
                        if qualifier_set
                        and qualifier_set
                        <= set(re.split(r"[.:]+", qual_lower))
                        else 6
                    )
            elif name == last_segment_raw:
                tier = 1
            elif name_lower == lower_query:
                tier = 2
            if tier is None and last_segment and not path_like:
                qualifier_ok = not qualifier_set or qualifier_set <= set(
                    re.split(r"[.:]+", qual_lower)
                )
                if qualifier_ok:
                    if name_lower.startswith(last_segment) or qual_lower.endswith(
                        ("::" + last_segment, "." + last_segment)
                    ):
                        tier = 6
                    elif word_pattern is not None and word_pattern.search(qual_lower):
                        tier = 7
                    elif last_segment in name_lower and not path_like:
                        tier = 8
            if tier is None:
                continue
            if termset is not None and not entry_has_terms:
                haystack = name_lower + " " + qual_lower
                if not all(term in haystack for term in terms):
                    continue
            candidates.append(
                (
                    tier,
                    _DEFINITION_RANK.get(symbol["kind"], 2),
                    _path_penalty(path),
                    path,
                    symbol["line"],
                    symbol["kind"],
                    name,
                    entry["sha256"],
                    entry["language"],
                )
            )
        if path_lower == lower_query or Path(path).name.casefold() == lower_query:
            candidates.append(
                (
                    9,
                    0,
                    _path_penalty(path),
                    path,
                    1,
                    "file",
                    path,
                    entry["sha256"],
                    entry["language"],
                )
            )
        elif last_segment and re.search(
            r"(?:^|/)"
            + re.escape(lower_query if qualified_query else last_segment),
            path_lower,
        ):
            candidates.append(
                (
                    10,
                    1,
                    _path_penalty(path),
                    path,
                    1,
                    "path",
                    path,
                    entry["sha256"],
                    entry["language"],
                )
            )
        if termset is not None and entry_has_terms:
            candidates.append(
                (
                    11,
                    2,
                    _path_penalty(path),
                    path,
                    1,
                    "text",
                    path,
                    entry["sha256"],
                    entry["language"],
                )
            )

    candidates.sort(key=lambda item: item[:7])

    def _emit(items: list[tuple[Any, ...]], *, cap_symbols: bool) -> list[dict[str, Any]]:
        emitted: set[tuple[str, str]] = set()
        definitions_emitted = 0
        output: list[dict[str, Any]] = []
        for item in items:
            is_symbol = item[5] not in {"file", "path", "text"}
            identity = (item[3], item[6])
            if cap_symbols and is_symbol:
                # Keep the reply definition-first without letting forward
                # declarations crowd out the usage lines agents ask for next.
                if identity in emitted or definitions_emitted >= 5:
                    continue
                definitions_emitted += 1
                emitted.add(identity)
            if any(
                existing["path"] == item[3]
                and existing["line"] == item[4]
                and existing["name"] == item[6]
                for existing in output
            ):
                continue
            output.append(
                {
                    "path": item[3],
                    "line": item[4],
                    "kind": item[5],
                    "name": item[6],
                    "file_sha256": item[7],
                }
            )
        return output

    # Definitions and exact file targets first; fuzzy path and text matches
    # only after the usage lines that motivated the lookup.
    primary = [item for item in candidates if item[5] not in {"path", "text"}]
    all_matches: list[dict[str, Any]] = []
    all_matches.extend(_emit(primary, cap_symbols=True))
    top = candidates[0] if candidates else None
    symbol_seed = (
        top is not None
        and top[0] <= 4
        and top[5] not in {"file", "path", "text"}
    )
    file_seed = (
        top is not None
        and path_like
        and top[0] <= 9
        and top[5] in {"file", "path"}
    )
    if (symbol_seed or file_seed) and len(all_matches) < max_results:
        if symbol_seed:
            seed = full_query if qualified_query else top[6]
            language = top[8]
        else:
            seed = (
                Path(full_query).stem
                if Path(full_query).suffix == ".py"
                else Path(full_query).name
            )
            language = top[8]
        seeds = [seed]
        if qualified_query and last_segment_raw != seed:
            # Python call sites spell bare names even when the query is
            # qualified; fall back after the exact spelling finds nothing.
            seeds.append(last_segment_raw)
        for candidate_seed in seeds:
            exclude = {
                (item[3], item[4])
                for item in candidates
                if item[6] in {candidate_seed, last_segment_raw}
                or item[5] == "file"
            }
            usages, _ = _usage_matches(
                root_path,
                seed=candidate_seed,
                language=language,
                allowed_extensions=allowed_extensions,
                path_prefix=path_prefix,
                exclude=exclude,
                limit=max_results - len(all_matches),
            )
            all_matches.extend(usages)
            if usages:
                break
    all_matches.extend(
        _emit(
            [item for item in candidates if item[5] in {"path", "text"}],
            cap_symbols=False,
        )
    )
    pool_size = len(all_matches)
    receipt = {
        "schema_version": LOOKUP_SCHEMA_VERSION,
        "repository_revision": index["repository_revision"],
        "index_revision": index["index_revision"],
        "cache_status": index["cache_status"],
        "query_sha256": hashlib.sha256(query.encode("utf-8")).hexdigest(),
        "matches": all_matches[:max_results],
        "match_count": min(len(all_matches), max_results),
        "matched_before_limit": pool_size,
        "truncated": pool_size > max_results,
        "max_results": max_results,
        "max_output_bytes": max_output_bytes,
        "returned_bytes": 0,
        "state_write_authority": False,
        "memory_authority": False,
        "receipt_sha256": "",
    }
    for _ in range(8):
        receipt["receipt_sha256"] = _digest(receipt, "receipt_sha256")
        size = len(_canonical(receipt))
        if receipt["returned_bytes"] == size:
            break
        receipt["returned_bytes"] = size
    while receipt["returned_bytes"] > max_output_bytes and receipt["matches"]:
        receipt["matches"].pop()
        receipt["match_count"] -= 1
        receipt["truncated"] = True
        receipt["returned_bytes"] = 0
        for _ in range(8):
            receipt["receipt_sha256"] = _digest(receipt, "receipt_sha256")
            size = len(_canonical(receipt))
            if receipt["returned_bytes"] == size:
                break
            receipt["returned_bytes"] = size
    if receipt["returned_bytes"] > max_output_bytes:
        raise ValueError("max_output_bytes cannot contain the lookup receipt")
    validate_code_index_receipt(receipt, root=root)
    try:
        from .usage_meter import record

        if record_usage:
            record(
                root,
                command="context lookup",
                response_bytes=receipt["returned_bytes"],
                estimated_grep_bytes=sum(
                    len(item["path"].encode()) + len(item["name"].encode()) + 32 for item in receipt['matches']
                ),
                cache_status=index["cache_status"],
                data_root=data_root,
            )
    except OSError:
        pass
    return receipt


def find_dependents(
    root: str | Path, *, query: str, max_results: int = 50
) -> dict[str, Any]:
    """Return bounded reverse references for headers, symbols and build keys."""
    root = Path(root).resolve()
    result = _git(root, "grep", "-n", "-I", "-w", "-F", "--", query)
    paths: dict[str, int] = {}
    lines: list[dict[str, Any]] = []
    for raw in result.stdout.splitlines():
        parts = raw.split(b":", 2)
        if len(parts) != 3:
            continue
        try:
            path = parts[0].decode()
            line = int(parts[1])
            text = parts[2].decode("utf-8", "replace")[:240]
        except (UnicodeDecodeError, ValueError):
            continue
        paths[path] = paths.get(path, 0) + 1
        lines.append({"path": path, "line": line, "excerpt": text})
    lines.sort(key=lambda item: (item["path"], item["line"]))
    return {
        "schema_version": "context.reverse-dependents/v1alpha1",
        "query": query,
        "paths": [
            {"path": path, "matches": count}
            for path, count in sorted(
                paths.items(), key=lambda item: (-item[1], item[0])
            )
        ],
        "matches": lines[:max_results],
        "truncated": len(lines) > max_results,
    }


def validate_code_index_receipt(value: Any, *, root: str | Path | None = None) -> None:
    if not isinstance(value, dict):
        raise ValueError("code index receipt is invalid")
    schema = value.get("schema_version")
    fields = (
        _INDEX_FIELDS
        if schema == INDEX_SCHEMA_VERSION
        else _LOOKUP_FIELDS
        if schema == LOOKUP_SCHEMA_VERSION
        else None
    )
    if fields is None or set(value) != fields:
        raise ValueError("code index receipt fields are invalid")
    if value["receipt_sha256"] != _digest(value, "receipt_sha256"):
        raise ValueError("code index receipt digest mismatch")
    for field in ("repository_revision", "index_revision", "receipt_sha256"):
        length = 40 if field == "repository_revision" else 64
        if not isinstance(value[field], str) or not re.fullmatch(
            rf"[0-9a-f]{{{length}}}", value[field]
        ):
            raise ValueError(f"{field} is invalid")
    if (
        value["state_write_authority"] is not False
        or value["memory_authority"] is not False
    ):
        raise ValueError("code index cannot carry authority")
    if schema == INDEX_SCHEMA_VERSION:
        if value["cache_status"] not in {"miss", "hit", "partial"}:
            raise ValueError("cache_status is invalid")
        for field in (
            "tracked_files",
            "indexed_files",
            "reused_files",
            "rehashed_files",
            "removed_files",
            "max_files",
        ):
            if type(value[field]) is not int or value[field] < 0:
                raise ValueError(f"{field} is invalid")
        return
    if value["cache_status"] not in {"miss", "hit", "partial"}:
        raise ValueError("cache_status is invalid")
    matches = value["matches"]
    if not isinstance(matches, list) or value["match_count"] != len(matches):
        raise ValueError("lookup matches are invalid")
    if type(value["matched_before_limit"]) is not int or value[
        "matched_before_limit"
    ] < len(matches):
        raise ValueError("matched_before_limit is invalid")
    if value["truncated"] is not (value["matched_before_limit"] > len(matches)):
        raise ValueError("truncated is invalid")
    if (
        value["returned_bytes"] != len(_canonical(value))
        or value["returned_bytes"] > value["max_output_bytes"]
    ):
        raise ValueError("lookup output budget is invalid")
    for match in matches:
        if not isinstance(match, dict) or set(match) != _MATCH_FIELDS:
            raise ValueError("lookup match fields are invalid")
        if type(match["line"]) is not int or match["line"] < 1:
            raise ValueError("lookup match line is invalid")
        for field in ("path", "kind", "name"):
            if not isinstance(match[field], str) or not match[field]:
                raise ValueError(f"lookup match {field} is invalid")
        if not isinstance(match["file_sha256"], str) or not re.fullmatch(
            r"[0-9a-f]{64}", match["file_sha256"]
        ):
            raise ValueError("lookup match file hash is invalid")
        if root is not None:
            path = (Path(root).resolve() / match["path"]).resolve()
            try:
                path.relative_to(Path(root).resolve())
                payload = path.read_bytes()
            except (OSError, ValueError) as exc:
                raise ValueError("lookup match path is invalid") from exc
            if hashlib.sha256(payload).hexdigest() != match["file_sha256"]:
                raise ValueError("lookup match file hash drifted")


__all__ = [
    "build_code_index",
    "default_code_index_path",
    "lookup_code_index",
    "validate_code_index_receipt",
]
