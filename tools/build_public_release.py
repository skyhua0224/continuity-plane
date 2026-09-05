#!/usr/bin/env python3
"""Build and validate a release-neutral fresh-history public mirror."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

PUBLIC_RELEASE_VERSION = "0.1.0-alpha.12"

_ROOT_FILES = (
    "README.md",
    "README.en.md",
    "USAGE.md",
    "USAGE.en.md",
    "LICENSE",
    "LICENSE.zh-CN.md",
    "NOTICE",
    "THIRD_PARTY_NOTICES.md",
    "THIRD_PARTY_NOTICES.en.md",
    "BRANDING.md",
    "BRANDING.en.md",
    "SECURITY.md",
    "SECURITY.en.md",
    "CONTRIBUTING.md",
    "CONTRIBUTING.en.md",
    "CHANGELOG.md",
    "CHANGELOG.en.md",
    "pyproject.toml",
    ".gitignore",
    ".gitattributes",
    ".gitleaks.toml",
)
_BANNED = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"alkaidlab",
        r"projectcompute",
        r"deepseek",
        r"claude",
        r"provider://cursor",
        r"moonlight",
        r"sunshine",
        r"skyindows",
        r"shunwang",
        r"netbird",
        r"foundation account",
        r"gitea\.sky-hua",
        r"/home/[a-z0-9._-]+/",
        r"/users/[a-z0-9._-]+/",
        r"01[0-9a-f]{6}-[0-9a-f-]{27}",
    )
)
_LEGACY_PUBLIC_MARKERS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"context control plane",
        r"context-control-plane",
        r"context_control_plane",
        r"\.context-control-plane",
    )
)
_TEXT_SUFFIXES = {
    "",
    ".c",
    ".cfg",
    ".cpp",
    ".h",
    ".hpp",
    ".ini",
    ".json",
    ".md",
    ".py",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}
_PUBLIC_MODULE_ROOTS = {
    "__init__",
    "artifact_store",
    "checkpoint",
    "code_index",
    "cli",
    "codex_hook_launcher",
    "codex_mcp_server",
    "context_mcp_server",
    "collaboration_notifications",
    "decision_evidence_projection",
    "durable_operation",
    "execution_packet",
    "external_state_provider",
    "human_governance",
    "light_observability",
    "obsidian_vault",
    "postgres_state_store",
    "project_graph_projection",
    "relationship_impact_projection",
    "retrieval_routing",
    "shared_state_mcp",
    "skill_resolver",
    "sqlite_state_store",
    "state_mcp",
    "verification_profile",
}
_PUBLIC_SCHEMA_FILES = (
    "m10-01/canonical-attach-proposal.schema.json",
    "m2-01/typed-state.schema.json",
    "m2-02/state-event.schema.json",
    "m2-05/state-mcp.schema.json",
    "m2-06/checkpoint-manifest.schema.json",
    "m4-01/skill-manifest-set.schema.json",
    "m4-01/spdx-license-ids-3.28.0.json",
    "m5-01/execution-packet.schema.json",
    "m7-03/verification-profile.schema.json",
    "m8-02/state-mcp-v3alpha1.schema.json",
    "m8-10/agent-inbox-item.schema.json",
    "m8-10/collaboration-delivery-batch.schema.json",
    "m8-10/collaboration-notification-publish.schema.json",
    "m8-10/collaboration-notification.schema.json",
    "m8-10/collaboration-subscription-cursor.schema.json",
    "m8-10/collaboration-subscription-request.schema.json",
    "m8-10/collaboration-subscription.schema.json",
    "m10-11/local-work-activation-request.schema.json",
    "m10-11/status-projection.schema.json",
    "m10-15/delivery-workspace-registry.schema.json",
    "m10-15/codex-session-project-bindings.schema.json",
    "m10-16/observability-policy.schema.json",
    "m10-16/state-mcp-observation.schema.json",
)
_PUBLIC_IGNORED_PARTS = {".git", "__pycache__", ".ruff_cache", "build", "dist"}


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _text_violations(path: Path, relative: str) -> list[str]:
    if path.suffix.lower() not in _TEXT_SUFFIXES:
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return []
    violations = []
    for pattern in _BANNED:
        if pattern.search(text) or pattern.search(relative):
            violations.append(f"{relative}: banned marker {pattern.pattern}")
    return violations


def _legacy_public_violations(path: Path, relative: str) -> list[str]:
    if path.suffix.lower() not in _TEXT_SUFFIXES:
        return []
    try:
        content = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return []
    return [
        f"{relative}: legacy public marker {pattern.pattern}"
        for pattern in _LEGACY_PUBLIC_MARKERS
        if pattern.search(content) or pattern.search(relative)
    ]


def scan_public_release(root: Path) -> list[str]:
    root = root.resolve()
    violations: list[str] = []
    for forbidden in ("MASTER.md", "STATUS.md", "AGENTS.md"):
        if (root / forbidden).exists():
            violations.append(f"root development document is public: {forbidden}")
    for path in sorted(root.rglob("*")):
        if not path.is_file() or ".git" in path.relative_to(root).parts:
            continue
        relative = path.relative_to(root).as_posix()
        if path.suffix in {".jsonl", ".sqlite", ".sqlite3", ".db"}:
            violations.append(f"runtime/archive artifact is public: {relative}")
        violations.extend(_text_violations(path, relative))
        violations.extend(_legacy_public_violations(path, relative))
    if (root / ".git").is_dir():
        history = subprocess.run(
            ["git", "log", "--format=%an <%ae> %s"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        for pattern in _BANNED:
            if pattern.search(history):
                violations.append(f"Git history contains {pattern.pattern}")
        for pattern in _LEGACY_PUBLIC_MARKERS:
            if pattern.search(history):
                violations.append(f"Git history contains legacy {pattern.pattern}")
    return sorted(set(violations))


def _module_dependencies(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    dependencies: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level == 1:
                if node.module:
                    dependencies.add(node.module.split(".", 1)[0])
                else:
                    dependencies.update(
                        alias.name.split(".", 1)[0] for alias in node.names
                    )
            elif node.level == 0 and node.module == "context_control_plane":
                dependencies.update(alias.name.split(".", 1)[0] for alias in node.names)
            elif (
                node.level == 0
                and node.module
                and node.module.startswith("context_control_plane.")
            ):
                dependencies.add(node.module.split(".", 1)[1].split(".", 1)[0])
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("context_control_plane."):
                    dependencies.add(alias.name.split(".", 2)[1])
    return dependencies


def _release_modules(source: Path) -> list[Path]:
    package = source / "context_control_plane"
    all_modules = {
        path.stem: path for path in package.glob("*.py") if path.name != "__pycache__"
    }
    available = {
        name: path
        for name, path in all_modules.items()
        if not path.name.endswith("_benchmark.py")
    }
    candidates: set[str] = set()
    pending = list(_PUBLIC_MODULE_ROOTS)
    while pending:
        name = pending.pop()
        if name in candidates:
            continue
        path = available.get(name)
        if path is None:
            raise RuntimeError(f"public module is unavailable: {name}")
        violations = _text_violations(path, path.name)
        if violations:
            raise RuntimeError(
                "public module is not neutral:\n" + "\n".join(violations)
            )
        candidates.add(name)
        pending.extend(sorted(_module_dependencies(path) - candidates))
    return sorted((available[name] for name in candidates), key=lambda path: path.name)


def _copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def _copy_public_python(source: Path, destination: Path) -> None:
    text = source.read_text(encoding="utf-8")
    text = _normalize_public_identity(text)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(text, encoding="utf-8")


def _copy_public_text(source: Path, destination: Path) -> None:
    text = _normalize_public_identity(source.read_text(encoding="utf-8"))
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(text, encoding="utf-8")


def _copy_public_plugin(source: Path, destination: Path) -> None:
    """Project the Codex adapter as a standalone public marketplace plugin."""
    if source.name == "continuity-plane":
        files = [
            source / ".codex-plugin/plugin.json",
            source / "hooks/hooks.json",
            source / "scripts/continuity-hook.py",
        ]
        hook_config = json.loads((source / "hooks/hooks.json").read_text(encoding="utf-8"))
        if any(
            "continuity-advisory-hook.py" in handler.get("command", "")
            for groups in hook_config.get("hooks", {}).values()
            for group in groups
            for handler in group.get("hooks", [])
        ):
            files.append(source / "scripts/continuity-advisory-hook.py")
    elif source.name == "continuity-plane-search":
        files = [
            source / ".codex-plugin/plugin.json",
            source / ".mcp.json",
        ]
    elif source.name == "continuity-plane-state":
        files = [
            source / ".codex-plugin/plugin.json",
            source / ".mcp.json",
            source / "scripts/continuity-mcp-server.py",
        ]
    else:
        raise RuntimeError(f"unsupported public Codex plugin: {source.name}")
    for path in files:
        if not path.is_file():
            raise RuntimeError(f"public Codex plugin file is unavailable: {path}")
        relative = path.relative_to(source)
        target = destination / relative
        if path.name == "plugin.json":
            manifest = json.loads(path.read_text(encoding="utf-8"))
            manifest["version"] = PUBLIC_RELEASE_VERSION
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        elif path.suffix.lower() in _TEXT_SUFFIXES:
            _copy_public_text(path, target)
        else:
            _copy(path, target)


def _write_public_plugin_marketplace(output: Path) -> None:
    marketplace = {
        "name": "continuity-plane",
        "interface": {"displayName": "Continuity Plane"},
        "plugins": [
            {
                "name": "continuity-plane",
                "source": {
                    "source": "local",
                    "path": "./plugins/continuity-plane",
                },
                "policy": {
                    "installation": "AVAILABLE",
                    "authentication": "ON_INSTALL",
                },
                "category": "Productivity",
            },
            {
                "name": "continuity-plane-search",
                "source": {
                    "source": "local",
                    "path": "./plugins/continuity-plane-search",
                },
                "policy": {
                    "installation": "AVAILABLE",
                    "authentication": "ON_INSTALL",
                },
                "category": "Productivity",
            },
            {
                "name": "continuity-plane-state",
                "source": {
                    "source": "local",
                    "path": "./plugins/continuity-plane-state",
                },
                "policy": {
                    "installation": "AVAILABLE",
                    "authentication": "ON_INSTALL",
                },
                "category": "Productivity",
            }
        ],
    }
    path = output / ".agents/plugins/marketplace.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(marketplace, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _normalize_public_identity(text: str) -> str:
    # Source documentation lives under public/docs; the projected mirror flattens it to docs.
    text = text.replace('version = "0.1.0a11"', 'version = "0.1.0a12"')
    text = text.replace("public/docs/", "docs/")
    text = text.replace("Context Control Plane", "Continuity Plane")
    text = text.replace(".context-control-plane", ".continuity")
    text = text.replace("context-control-plane", "continuity")
    text = text.replace("context_control_plane", "continuity_plane")
    return text


def _public_benchmark(source: Path) -> dict[str, Any]:
    evidence = source / "experiments" / "evidence"
    usage = json.loads((evidence / "m10-00-codex-usage-ab-results.json").read_text())
    code = json.loads((evidence / "m10-00-codex-code-task-results.json").read_text())
    skill = json.loads(
        (evidence / "m10-00-codex-skill-overlay-minimal-results.json").read_text()
    )
    collaboration = json.loads(
        (evidence / "m10-01-codex-collaboration-results.json").read_text()
    )
    fault_drill = json.loads(
        (evidence / "m10-00-fault-drill-results.json").read_text()
    )
    campaign = json.loads(
        (evidence / "m10-00-campaign-verdict-results.json").read_text()
    )
    notifications = json.loads(
        (evidence / "m8-10-collaboration-notification-results.json").read_text()
    )
    raw: dict[str, Any] = {
        "schema_version": "context.public-benchmark/v1",
        "release": PUBLIC_RELEASE_VERSION,
        "quality_rate": 1.0,
        "sample_sizes": {
            "context_composition_per_arm": 3,
            "code_retrieval_per_arm": 3,
            "skill_overlay_per_arm": 3,
            "collaboration_per_arm": 3,
        },
        "method": {
            "comparison": "matched task and model configuration",
            "oracle": "exact-required-facts",
            "quality_rule": "all required facts present and no forbidden assertions",
            "aggregation": "arithmetic mean per arm",
        },
        "environment": {
            "class": "single development workstation",
            "provider_disclosed": False,
            "repository_disclosed": False,
        },
        "measurements": {
            "bounded_packet_input_reduction_percent": usage["summary"]["packet"][
                "input_reduction_percent"
            ],
            "near_limit_packet_input_reduction_percent": round(
                (
                    usage["summary"]["large-history"]["input_tokens_mean"]
                    - usage["summary"]["packet"]["input_tokens_mean"]
                )
                / usage["summary"]["large-history"]["input_tokens_mean"]
                * 100,
                4,
            ),
            "bounded_retrieval_input_reduction_percent": round(
                (
                    code["summary"]["bare"]["input_tokens_mean"]
                    - code["summary"]["retrieval-packet"]["input_tokens_mean"]
                )
                / code["summary"]["bare"]["input_tokens_mean"]
                * 100,
                4,
            ),
            "bounded_retrieval_tool_reduction_percent": round(
                (
                    code["summary"]["bare"]["tool_calls_mean"]
                    - code["summary"]["retrieval-packet"]["tool_calls_mean"]
                )
                / code["summary"]["bare"]["tool_calls_mean"]
                * 100,
                4,
            ),
            "bounded_retrieval_wall_reduction_percent": round(
                (
                    code["summary"]["bare"]["wall_time_ms_mean"]
                    - code["summary"]["retrieval-packet"]["wall_time_ms_mean"]
                )
                / code["summary"]["bare"]["wall_time_ms_mean"]
                * 100,
                4,
            ),
            "skill_source_byte_reduction_percent": skill["skill_inventory"][
                "source_bytes_reduction_percent"
            ],
            "collaboration_duplicate_tool_reduction_percent": round(
                (
                    collaboration["summary"]["uncoordinated"][
                        "duplicate_tool_calls_mean"
                    ]
                    - collaboration["summary"]["coordinated"][
                        "duplicate_tool_calls_mean"
                    ]
                )
                / collaboration["summary"]["uncoordinated"]["duplicate_tool_calls_mean"]
                * 100,
                4,
            ),
            "collaboration_tool_reduction_percent": round(
                (
                    collaboration["summary"]["uncoordinated"]["tool_calls_mean"]
                    - collaboration["summary"]["coordinated"]["tool_calls_mean"]
                )
                / collaboration["summary"]["uncoordinated"]["tool_calls_mean"]
                * 100,
                4,
            ),
            "collaboration_parallel_wall_reduction_percent": round(
                (
                    collaboration["summary"]["uncoordinated"][
                        "parallel_wall_time_ms_mean"
                    ]
                    - collaboration["summary"]["coordinated"][
                        "parallel_wall_time_ms_mean"
                    ]
                )
                / collaboration["summary"]["uncoordinated"][
                    "parallel_wall_time_ms_mean"
                ]
                * 100,
                4,
            ),
            "collaboration_input_overhead_percent": round(
                (
                    collaboration["summary"]["coordinated"]["input_tokens_mean"]
                    - collaboration["summary"]["uncoordinated"]["input_tokens_mean"]
                )
                / collaboration["summary"]["uncoordinated"]["input_tokens_mean"]
                * 100,
                4,
            ),
        },
        "consistency_gates": {
            "authority_violations": notifications["authority_violations"],
            "campaign_experiments_passed": sum(
                item["passed"] is True for item in campaign["e0_e9"]
            ),
            "campaign_experiments_total": len(campaign["e0_e9"]),
            "dual_session_consistent": notifications[
                "consistent_dual_session_events"
            ],
            "dual_session_total": notifications["dual_session_delivery_attempts"],
            "duplicate_notifications_suppressed": notifications[
                "duplicate_suppressions"
            ],
            "duplicate_notifications_total": notifications["duplicate_attempts"],
            "forced_faults_passed": sum(
                item["status"] == "passed" for item in fault_drill["faults"]
            ),
            "forced_faults_total": len(fault_drill["faults"]),
            "offline_deliveries_recovered": notifications[
                "offline_catch_up_deliveries"
            ],
            "offline_delivery_attempts": notifications[
                "offline_catch_up_attempts"
            ],
        },
        "public_reproduction_command": (
            "python benchmarks/run_local_state.py --iterations 1000"
        ),
        "limitations": [
            "single-machine observational reference",
            "matched tasks with three samples per arm",
            "not a cross-model or cross-provider guarantee",
        ],
        "state_write_authority": False,
        "receipt_sha256": "",
    }
    raw["receipt_sha256"] = hashlib.sha256(
        json.dumps(
            {key: value for key, value in raw.items() if key != "receipt_sha256"},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    return raw


def build_public_release(
    source: Path, output: Path, *, initialize_git: bool = True
) -> dict[str, Any]:
    source = source.resolve()
    output = output.resolve()
    if output == source or source in output.parents:
        raise ValueError(
            "public output must not contain or replace the source repository"
        )
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)
    for relative in _ROOT_FILES:
        public_override = source / "public" / relative
        selected = public_override if public_override.is_file() else source / relative
        destination = output / relative
        if selected.suffix.lower() in _TEXT_SUFFIXES:
            _copy_public_text(selected, destination)
        else:
            _copy(selected, destination)
    _copy(source / "public/github-ci.yml", output / ".github/workflows/ci.yml")
    _copy(
        source / "public/github-publish.yml",
        output / ".github/workflows/publish.yml",
    )
    _copy_public_plugin(
        source / "integrations/codex/continuity-plane",
        output / "plugins/continuity-plane",
    )
    _copy_public_plugin(
        source / "integrations/codex/continuity-plane-search",
        output / "plugins/continuity-plane-search",
    )
    _copy_public_plugin(
        source / "integrations/codex/continuity-plane-state",
        output / "plugins/continuity-plane-state",
    )
    _write_public_plugin_marketplace(output)
    for section in ("benchmarks", "docs", "examples", "tests"):
        for path in sorted((source / "public" / section).rglob("*")):
            relative_path = path.relative_to(source / "public" / section)
            if (
                path.is_file()
                and not _PUBLIC_IGNORED_PARTS.intersection(relative_path.parts)
                and not any(part.endswith(".egg-info") for part in relative_path.parts)
                and path.suffix != ".pyc"
            ):
                _copy(
                    path,
                    output / section / relative_path,
                )
    package_output = output / "continuity_plane"
    for path in _release_modules(source):
        _copy_public_python(path, package_output / path.name)
    for path in sorted((source / "context_control_plane/templates").glob("*")):
        if path.is_file():
            _copy(path, package_output / "templates" / path.name)
    _copy(
        source / "database/migrations/001_m2_03_postgres_state.up.sql",
        package_output / "database/migrations/001_postgres_state.up.sql",
    )
    for relative in _PUBLIC_SCHEMA_FILES:
        path = source / "schemas" / relative
        if not path.is_file():
            raise RuntimeError(f"public schema is unavailable: {relative}")
        if _text_violations(path, f"schemas/{relative}"):
            raise RuntimeError(f"public schema is not neutral: {relative}")
        _copy_public_text(path, output / "schemas" / relative)
        if relative == "m4-01/spdx-license-ids-3.28.0.json":
            _copy_public_text(path, package_output / "schemas" / relative)
    schema_entries = [
        {
            "path": path.relative_to(output).as_posix(),
            "sha256": _sha(path),
        }
        for path in sorted((output / "schemas").rglob("*"))
        if path.is_file() and path.name != "registry.json"
    ]
    (output / "schemas/registry.json").write_text(
        json.dumps(
            {
                "schema_version": "context.public-schema-registry/v1",
                "schemas": schema_entries,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    templates = output / "templates"
    templates.mkdir()
    for template in sorted((source / "context_control_plane/templates").glob("*.md")):
        _copy(template, templates / template.name)
    benchmark_dir = output / "benchmarks"
    benchmark_dir.mkdir(exist_ok=True)
    (benchmark_dir / "reference-results.json").write_text(
        json.dumps(_public_benchmark(source), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    violations = scan_public_release(output)
    if violations:
        raise RuntimeError("public release scan failed:\n" + "\n".join(violations))
    files = [
        {"path": path.relative_to(output).as_posix(), "sha256": _sha(path)}
        for path in sorted(output.rglob("*"))
        if path.is_file() and ".git" not in path.relative_to(output).parts
    ]
    manifest = {
        "schema_version": "context.public-release-manifest/v1",
        "version": PUBLIC_RELEASE_VERSION,
        "file_count": len(files),
        "files": files,
    }
    (output / "RELEASE-MANIFEST.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    if initialize_git:
        subprocess.run(
            ["git", "init", "-b", "main"], cwd=output, check=True, capture_output=True
        )
        subprocess.run(
            ["git", "config", "user.name", "SkyHua"],
            cwd=output,
            check=True,
        )
        subprocess.run(
            [
                "git",
                "config",
                "user.email",
                "skyhua0224@users.noreply.github.com",
            ],
            cwd=output,
            check=True,
        )
        subprocess.run(["git", "add", "--all"], cwd=output, check=True)
        subprocess.run(
            [
                "git",
                "commit",
                "-s",
                "-m",
                "feat: publish Continuity Plane alpha",
            ],
            cwd=output,
            check=True,
            capture_output=True,
        )
        violations = scan_public_release(output)
        if violations:
            raise RuntimeError("public history scan failed:\n" + "\n".join(violations))
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="build a sanitized public mirror")
    parser.add_argument("--source", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--no-git", action="store_true")
    args = parser.parse_args(argv)
    manifest = build_public_release(
        args.source, args.output, initialize_git=not args.no_git
    )
    print(json.dumps({"status": "passed", "files": manifest["file_count"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
