"""Independent verification for non-authoritative CodeGraph clues."""

from __future__ import annotations

import copy
import hashlib
import json
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "context.codegraph-verification-receipt/v1alpha1"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,511}$")
_QUALIFIED_SYMBOL_RE = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+$"
)
_CLUE_FIELDS = {
    "clue_id",
    "source_repository",
    "target_repository",
    "source_symbol",
    "target_symbol",
    "relation",
    "index_revision",
    "index_sha256",
}
_EVIDENCE_FIELDS = {
    "clue_id",
    "verifier",
    "source_symbol",
    "target_symbol",
    "revision",
    "source_sha256",
    "target_sha256",
    "tool_version",
    "command",
    "command_sha256",
    "output",
    "output_sha256",
}
_OUTPUT_FIELDS = {
    "source_path",
    "source_line",
    "query_line",
    "query_column",
    "target_path",
    "target_line",
    "target_column",
}
_RECEIPT_FIELDS = {
    "schema_version",
    "receipt_id",
    "verified_at",
    "clues",
    "verifier_evidence",
    "verified_clues",
    "verifier_counts",
    "codegraph_authority",
    "state_write_authority",
    "receipt_sha256",
}


class CodeGraphVerificationError(ValueError):
    """Raised when a graph clue lacks exact current-code corroboration."""


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _digest(receipt: dict[str, Any]) -> str:
    unsigned = copy.deepcopy(receipt)
    unsigned.pop("receipt_sha256", None)
    return hashlib.sha256(_canonical(unsigned)).hexdigest()


def _safe(value: Any, field: str) -> str:
    if not isinstance(value, str) or _SAFE_ID_RE.fullmatch(value) is None:
        raise CodeGraphVerificationError(f"{field} is invalid")
    return value


def _text(value: Any, field: str, maximum: int = 2048) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value.encode("utf-8")) > maximum
        or any(character in value for character in "\x00\r\n")
    ):
        raise CodeGraphVerificationError(f"{field} is invalid")
    return value


def _sha256(value: Any, field: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise CodeGraphVerificationError(f"{field} is invalid")
    return value


def _qualified_symbol(value: Any, field: str) -> str:
    if not isinstance(value, str) or _QUALIFIED_SYMBOL_RE.fullmatch(value) is None:
        raise CodeGraphVerificationError(f"{field} is not a qualified symbol")
    return value


def _python_module_for_path(relative_path: str, field: str) -> str:
    path = Path(relative_path)
    if path.suffix != ".py":
        raise CodeGraphVerificationError(
            f"current repository {field} module/path binding is invalid"
        )
    parts = list(path.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    if not parts or any(
        re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", part) is None for part in parts
    ):
        raise CodeGraphVerificationError(
            f"current repository {field} module/path binding is invalid"
        )
    return ".".join(parts)


def _validate_symbol_path_binding(
    symbol: str, relative_path: str, field: str
) -> None:
    module = _python_module_for_path(relative_path, field)
    if not symbol.startswith(f"{module}."):
        raise CodeGraphVerificationError(
            f"current repository {field} module/path binding is invalid"
        )


def _relative_path(value: Any, field: str) -> str:
    value = _text(value, field, 2048)
    path = Path(value)
    if path.is_absolute() or ".." in path.parts or "\\" in value:
        raise CodeGraphVerificationError(f"{field} is invalid")
    return path.as_posix()


def _positive_int(value: Any, field: str) -> int:
    if type(value) is not int or value <= 0:
        raise CodeGraphVerificationError(f"{field} must be a positive integer")
    return value


def _nonnegative_int(value: Any, field: str) -> int:
    if type(value) is not int or value < 0:
        raise CodeGraphVerificationError(f"{field} must be a non-negative integer")
    return value


def _command_digest(command: list[str]) -> str:
    return hashlib.sha256(_canonical(command)).hexdigest()


def _output_digest(output: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical(output)).hexdigest()


def codegraph_clue_evidence_sha256(
    verifier_evidence: list[dict[str, Any]],
) -> str:
    """Bind a clue to the exact persisted rg and LSP corroboration records."""
    if not isinstance(verifier_evidence, list) or len(verifier_evidence) != 2:
        raise CodeGraphVerificationError("clue evidence must contain rg and lsp")
    by_verifier: dict[str, dict[str, Any]] = {}
    for index, evidence in enumerate(verifier_evidence):
        _validate_evidence(evidence, index)
        by_verifier[evidence["verifier"]] = evidence
    if set(by_verifier) != {"rg", "lsp"}:
        raise CodeGraphVerificationError("clue evidence must contain rg and lsp")
    material = [
        {
            "verifier": verifier,
            "revision": by_verifier[verifier]["revision"],
            "source_sha256": by_verifier[verifier]["source_sha256"],
            "target_sha256": by_verifier[verifier]["target_sha256"],
            "tool_version": by_verifier[verifier]["tool_version"],
            "command_sha256": by_verifier[verifier]["command_sha256"],
            "output_sha256": by_verifier[verifier]["output_sha256"],
        }
        for verifier in ("rg", "lsp")
    ]
    return hashlib.sha256(_canonical(material)).hexdigest()


def _rg_match_records(stdout: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    try:
        for line in stdout.splitlines():
            record = json.loads(line)
            if isinstance(record, dict) and record.get("type") == "match":
                records.append(record)
    except json.JSONDecodeError as exc:
        raise CodeGraphVerificationError("rg returned invalid JSON evidence") from exc
    if not records:
        raise CodeGraphVerificationError("rg returned no match evidence")
    return records


def _timestamp(value: Any) -> str:
    if not isinstance(value, str):
        raise CodeGraphVerificationError("verified_at is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CodeGraphVerificationError("verified_at is invalid") from exc
    if parsed.tzinfo is None:
        raise CodeGraphVerificationError("verified_at requires a timezone")
    return value


def _validate_clue(clue: Any, index: int) -> None:
    if not isinstance(clue, dict) or set(clue) != _CLUE_FIELDS:
        raise CodeGraphVerificationError(f"clue {index} fields are invalid")
    for field in (
        "clue_id",
        "source_repository",
        "target_repository",
        "index_revision",
    ):
        _safe(clue[field], f"clues[{index}].{field}")
    for field in ("source_symbol", "target_symbol"):
        _qualified_symbol(clue[field], f"clues[{index}].{field}")
    if clue["relation"] != "references":
        raise CodeGraphVerificationError(
            "clue relation is not proved by the rg and LSP reference contract"
        )
    _sha256(clue["index_sha256"], f"clues[{index}].index_sha256")


def _validate_evidence(evidence: Any, index: int) -> None:
    if not isinstance(evidence, dict) or set(evidence) != _EVIDENCE_FIELDS:
        raise CodeGraphVerificationError(f"verifier evidence {index} fields are invalid")
    _safe(evidence["clue_id"], "evidence.clue_id")
    if evidence["verifier"] not in {"rg", "lsp"}:
        raise CodeGraphVerificationError("verifier must be rg or lsp")
    for field in ("source_symbol", "target_symbol"):
        _qualified_symbol(evidence[field], f"evidence.{field}")
    _safe(evidence["revision"], "evidence.revision")
    for field in ("source_sha256", "target_sha256"):
        _sha256(evidence[field], f"evidence.{field}")
    _text(evidence["tool_version"], "evidence.tool_version", 512)
    command = evidence["command"]
    if not isinstance(command, list) or not command or len(command) > 32:
        raise CodeGraphVerificationError("evidence command is invalid")
    for command_index, argument in enumerate(command):
        _text(argument, f"evidence.command[{command_index}]", 2048)
    _sha256(evidence["command_sha256"], "evidence.command_sha256")
    if evidence["command_sha256"] != _command_digest(command):
        raise CodeGraphVerificationError("evidence command digest mismatch")
    output = evidence["output"]
    if not isinstance(output, dict) or set(output) != _OUTPUT_FIELDS:
        raise CodeGraphVerificationError("evidence output fields are invalid")
    for field in ("source_path", "target_path"):
        _relative_path(output[field], f"evidence.output.{field}")
    for field in ("source_line", "query_line", "target_line"):
        _positive_int(output[field], f"evidence.output.{field}")
    for field in ("query_column", "target_column"):
        _nonnegative_int(output[field], f"evidence.output.{field}")
    _sha256(evidence["output_sha256"], "evidence.output_sha256")
    if evidence["output_sha256"] != _output_digest(output):
        raise CodeGraphVerificationError("evidence output digest mismatch")


def _validate_evidence_pairs(
    clues: list[dict[str, Any]], verifier_evidence: list[dict[str, Any]]
) -> None:
    clue_by_id = {clue["clue_id"]: clue for clue in clues}
    by_clue: dict[str, dict[str, dict[str, Any]]] = {}
    for evidence in verifier_evidence:
        clue_id = evidence["clue_id"]
        verifier = evidence["verifier"]
        if clue_id not in clue_by_id:
            raise CodeGraphVerificationError("evidence references an unknown clue")
        if verifier in by_clue.setdefault(clue_id, {}):
            raise CodeGraphVerificationError("every clue requires rg and lsp evidence")
        by_clue[clue_id][verifier] = evidence
    for clue_id, clue in clue_by_id.items():
        evidence_set = by_clue.get(clue_id, {})
        if set(evidence_set) != {"rg", "lsp"}:
            raise CodeGraphVerificationError("every clue requires rg and lsp evidence")
        for evidence in evidence_set.values():
            if (
                evidence["source_symbol"] != clue["source_symbol"]
                or evidence["target_symbol"] != clue["target_symbol"]
            ):
                raise CodeGraphVerificationError("qualified symbol mismatch")
            if evidence["revision"] != clue["index_revision"]:
                raise CodeGraphVerificationError("verifier revision does not match clue index")
        if clue["index_sha256"] != codegraph_clue_evidence_sha256(
            [evidence_set["rg"], evidence_set["lsp"]]
        ):
            raise CodeGraphVerificationError("clue index digest does not match verifier evidence")


def _validate_current_repository_evidence(
    evidence: dict[str, Any], *, clue: dict[str, Any], root: Path
) -> None:
    output = evidence["output"]
    source_path = (root / output["source_path"]).resolve()
    target_path = (root / output["target_path"]).resolve()
    resolved_root = root.resolve()
    repository_identity = resolved_root.name
    if (
        clue["source_repository"] != repository_identity
        or clue["target_repository"] != repository_identity
    ):
        raise CodeGraphVerificationError(
            "clue repository identity does not match current repository paths"
        )
    if resolved_root not in source_path.parents or resolved_root not in target_path.parents:
        raise CodeGraphVerificationError("tool evidence is outside current repository")
    _validate_symbol_path_binding(
        evidence["source_symbol"], output["source_path"], "source symbol"
    )
    if evidence["verifier"] == "lsp":
        _validate_symbol_path_binding(
            evidence["target_symbol"], output["target_path"], "target symbol"
        )
    try:
        source_bytes = source_path.read_bytes()
        target_bytes = target_path.read_bytes()
        source_lines = source_bytes.decode("utf-8").splitlines()
        target_lines = target_bytes.decode("utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise CodeGraphVerificationError("tool evidence does not match current repository") from exc
    if (
        hashlib.sha256(source_bytes).hexdigest() != evidence["source_sha256"]
        or hashlib.sha256(target_bytes).hexdigest() != evidence["target_sha256"]
    ):
        raise CodeGraphVerificationError("tool evidence does not match current repository")
    try:
        source_definition = source_lines[output["source_line"] - 1]
        query_line = source_lines[output["query_line"] - 1]
        target_definition = target_lines[output["target_line"] - 1]
    except IndexError as exc:
        raise CodeGraphVerificationError("tool evidence does not match current repository") from exc
    source_name = evidence["source_symbol"].rsplit(".", 1)[-1]
    target_name = evidence["target_symbol"].rsplit(".", 1)[-1]
    query_column = output["query_column"]
    if (
        f"def {source_name}" not in source_definition
        or query_line[query_column : query_column + len(target_name)] != target_name
    ):
        raise CodeGraphVerificationError("tool evidence does not match current repository")
    if evidence["verifier"] == "rg":
        if target_path != source_path or output["target_line"] != output["query_line"]:
            raise CodeGraphVerificationError("rg evidence does not match current repository")
    elif f"def {target_name}" not in target_definition:
        raise CodeGraphVerificationError("lsp evidence does not match current repository")


def _read_lsp_message(stream: Any) -> dict[str, Any]:
    headers: dict[str, str] = {}
    while True:
        line = stream.readline()
        if not line:
            raise CodeGraphVerificationError("pylsp terminated before returning evidence")
        if line in (b"\r\n", b"\n"):
            break
        key, value = line.decode("ascii").split(":", 1)
        headers[key.lower()] = value.strip()
    try:
        length = int(headers["content-length"])
        message = json.loads(stream.read(length))
    except (KeyError, ValueError, json.JSONDecodeError) as exc:
        raise CodeGraphVerificationError("pylsp returned an invalid message") from exc
    if not isinstance(message, dict):
        raise CodeGraphVerificationError("pylsp returned an invalid message")
    return message


def _send_lsp_message(stream: Any, message: dict[str, Any]) -> None:
    payload = _canonical(message)
    stream.write(f"Content-Length: {len(payload)}\r\n\r\n".encode("ascii") + payload)
    stream.flush()


def _lsp_definition(
    *, root: Path, source_path: Path, source_text: str, line: int, column: int
) -> tuple[str, dict[str, Any]]:
    executable = shutil.which("pylsp")
    if executable is None:
        adjacent = Path(sys.executable).with_name("pylsp")
        executable = str(adjacent) if adjacent.is_file() else None
    if executable is None:
        raise CodeGraphVerificationError("pylsp is unavailable")
    version_run = subprocess.run(
        [executable, "--version"], capture_output=True, text=True, check=True
    )
    tool_version = version_run.stdout.strip().replace(" v", "/", 1)
    process = subprocess.Popen(
        [executable],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if process.stdin is None or process.stdout is None:
        raise CodeGraphVerificationError("pylsp streams are unavailable")
    try:
        _send_lsp_message(
            process.stdin,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "processId": None,
                    "rootUri": root.resolve().as_uri(),
                    "capabilities": {},
                },
            },
        )
        while _read_lsp_message(process.stdout).get("id") != 1:
            pass
        _send_lsp_message(
            process.stdin, {"jsonrpc": "2.0", "method": "initialized", "params": {}}
        )
        _send_lsp_message(
            process.stdin,
            {
                "jsonrpc": "2.0",
                "method": "textDocument/didOpen",
                "params": {
                    "textDocument": {
                        "uri": source_path.resolve().as_uri(),
                        "languageId": "python",
                        "version": 1,
                        "text": source_text,
                    }
                },
            },
        )
        _send_lsp_message(
            process.stdin,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "textDocument/definition",
                "params": {
                    "textDocument": {"uri": source_path.resolve().as_uri()},
                    "position": {"line": line, "character": column},
                },
            },
        )
        while True:
            response = _read_lsp_message(process.stdout)
            if response.get("id") == 2:
                break
        result = response.get("result")
        if not isinstance(result, list) or len(result) != 1 or not isinstance(result[0], dict):
            raise CodeGraphVerificationError("pylsp did not resolve one definition")
        return tool_version, result[0]
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        process.stdin.close()
        process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()


def probe_codegraph_relation(
    *,
    root: Path,
    clue_id: str,
    source_repository: str,
    target_repository: str,
    source_symbol: str,
    target_symbol: str,
    relation: str,
    source_path: str,
    symbol_name: str,
    index_revision: str,
    verified_at: str,
) -> dict[str, Any]:
    """Run ripgrep and pylsp against current files and seal their exact locations."""
    root = root.resolve()
    relative_source = _relative_path(source_path, "source_path")
    source_file = (root / relative_source).resolve()
    if root not in source_file.parents:
        raise CodeGraphVerificationError("source_path is outside current repository")
    source_bytes = source_file.read_bytes()
    source_text = source_bytes.decode("utf-8")
    source_lines = source_text.splitlines()
    source_name = source_symbol.rsplit(".", 1)[-1]
    source_line = next(
        (index for index, text in enumerate(source_lines) if f"def {source_name}" in text),
        None,
    )
    if source_line is None:
        raise CodeGraphVerificationError("source symbol definition is missing")
    query_line = next(
        (
            index
            for index, text in enumerate(source_lines[source_line + 1 :], source_line + 1)
            if symbol_name in text
        ),
        None,
    )
    if query_line is None:
        raise CodeGraphVerificationError("target symbol reference is missing")
    query_column = source_lines[query_line].index(symbol_name)
    rg_executable = shutil.which("rg")
    if rg_executable is None:
        raise CodeGraphVerificationError("rg is unavailable")
    rg_command = [rg_executable, "--json", symbol_name, relative_source]
    rg_run = subprocess.run(
        rg_command, cwd=root, capture_output=True, text=True, check=True
    )
    rg_matches = _rg_match_records(rg_run.stdout)
    if not any(
        record.get("data", {}).get("line_number") == query_line + 1
        for record in rg_matches
    ):
        raise CodeGraphVerificationError("rg output does not contain the target reference")
    rg_version = subprocess.run(
        [rg_executable, "--version"], capture_output=True, text=True, check=True
    ).stdout.splitlines()[0]
    lsp_version, definition = _lsp_definition(
        root=root,
        source_path=source_file,
        source_text=source_text,
        line=query_line,
        column=query_column + 1,
    )
    try:
        target_file = Path(definition["uri"].removeprefix("file://")).resolve()
        target_range = definition["range"]["start"]
        target_line = int(target_range["line"])
        target_column = int(target_range["character"])
        relative_target = target_file.relative_to(root).as_posix()
    except (KeyError, TypeError, ValueError) as exc:
        raise CodeGraphVerificationError("pylsp definition is outside current repository") from exc
    target_bytes = target_file.read_bytes()
    source_sha256 = hashlib.sha256(source_bytes).hexdigest()
    target_sha256 = hashlib.sha256(target_bytes).hexdigest()
    base_output = {
        "source_path": relative_source,
        "source_line": source_line + 1,
        "query_line": query_line + 1,
        "query_column": query_column,
    }
    rg_output = {
        **base_output,
        "target_path": relative_source,
        "target_line": query_line + 1,
        "target_column": query_column,
    }
    lsp_output = {
        **base_output,
        "target_path": relative_target,
        "target_line": target_line + 1,
        "target_column": target_column,
    }
    common = {
        "clue_id": clue_id,
        "source_symbol": source_symbol,
        "target_symbol": target_symbol,
        "revision": index_revision,
        "source_sha256": source_sha256,
        "target_sha256": target_sha256,
    }
    rg_evidence = {
        **common,
        "target_sha256": source_sha256,
        "verifier": "rg",
        "tool_version": rg_version,
        "command": ["rg", "--json", symbol_name, relative_source],
        "command_sha256": "",
        "output": rg_output,
        "output_sha256": _output_digest(rg_output),
    }
    rg_evidence["command_sha256"] = _command_digest(rg_evidence["command"])
    lsp_command = [
        "pylsp",
        "textDocument/definition",
        relative_source,
        f"{query_line + 1}:{query_column}",
    ]
    lsp_output_with_probe = {**lsp_output}
    lsp_evidence = {
        **common,
        "verifier": "lsp",
        "tool_version": lsp_version,
        "command": lsp_command,
        "command_sha256": _command_digest(lsp_command),
        "output": lsp_output_with_probe,
        "output_sha256": _output_digest(lsp_output_with_probe),
    }
    clue = {
        "clue_id": clue_id,
        "source_repository": source_repository,
        "target_repository": target_repository,
        "source_symbol": source_symbol,
        "target_symbol": target_symbol,
        "relation": relation,
        "index_revision": index_revision,
        "index_sha256": codegraph_clue_evidence_sha256(
            [rg_evidence, lsp_evidence]
        ),
    }
    receipt = verify_codegraph_clues(
        clues=[clue],
        verifier_evidence=[rg_evidence, lsp_evidence],
        verified_at=verified_at,
    )
    validate_codegraph_receipt(receipt, root=root)
    return receipt


def verify_codegraph_clues(
    *,
    clues: list[dict[str, Any]],
    verifier_evidence: list[dict[str, Any]],
    verified_at: str,
) -> dict[str, Any]:
    """Accept graph clues only when rg and LSP agree on qualified symbols."""
    if not isinstance(clues, list) or not clues or len(clues) > 256:
        raise CodeGraphVerificationError("clues are invalid")
    for index, clue in enumerate(clues):
        _validate_clue(clue, index)
    clue_ids = [clue["clue_id"] for clue in clues]
    if len(set(clue_ids)) != len(clue_ids):
        raise CodeGraphVerificationError("clue IDs must be unique")
    if not isinstance(verifier_evidence, list) or len(verifier_evidence) > len(clues) * 2:
        raise CodeGraphVerificationError("verifier_evidence is invalid")
    for index, evidence in enumerate(verifier_evidence):
        _validate_evidence(evidence, index)

    _validate_evidence_pairs(clues, verifier_evidence)

    receipt = {
        "schema_version": SCHEMA_VERSION,
        "receipt_id": "receipt/m6-02/codegraph-verification",
        "verified_at": _timestamp(verified_at),
        "clues": copy.deepcopy(clues),
        "verifier_evidence": copy.deepcopy(verifier_evidence),
        "verified_clues": len(clues),
        "verifier_counts": {"rg": len(clues), "lsp": len(clues)},
        "codegraph_authority": False,
        "state_write_authority": False,
        "receipt_sha256": "",
    }
    receipt["receipt_sha256"] = _digest(receipt)
    validate_codegraph_receipt(receipt)
    return receipt


def validate_codegraph_receipt(receipt: Any, *, root: Path | None = None) -> None:
    if not isinstance(receipt, dict) or set(receipt) != _RECEIPT_FIELDS:
        raise CodeGraphVerificationError("receipt fields are invalid")
    if receipt["schema_version"] != SCHEMA_VERSION:
        raise CodeGraphVerificationError("receipt schema_version is invalid")
    _safe(receipt["receipt_id"], "receipt_id")
    _timestamp(receipt["verified_at"])
    clues = receipt["clues"]
    evidence = receipt["verifier_evidence"]
    if receipt["verified_clues"] != len(clues):
        raise CodeGraphVerificationError("verified_clues is inaccurate")
    if receipt["verifier_counts"] != {"rg": len(clues), "lsp": len(clues)}:
        raise CodeGraphVerificationError("verifier_counts are inaccurate")
    if receipt["codegraph_authority"] is not False or receipt["state_write_authority"] is not False:
        raise CodeGraphVerificationError("CodeGraph verification cannot grant authority")
    for index, clue in enumerate(clues):
        _validate_clue(clue, index)
    for index, item in enumerate(evidence):
        _validate_evidence(item, index)
    if len(evidence) != len(clues) * 2:
        raise CodeGraphVerificationError("receipt lacks independent verifier evidence")
    _validate_evidence_pairs(clues, evidence)
    if root is not None:
        clue_by_id = {clue["clue_id"]: clue for clue in clues}
        for item in evidence:
            _validate_current_repository_evidence(
                item, clue=clue_by_id[item["clue_id"]], root=root
            )
    _sha256(receipt["receipt_sha256"], "receipt_sha256")
    if receipt["receipt_sha256"] != _digest(receipt):
        raise CodeGraphVerificationError("receipt digest mismatch")


def canonical_codegraph_receipt_bytes(receipt: dict[str, Any]) -> bytes:
    validate_codegraph_receipt(receipt)
    return _canonical(receipt)
