"""Install the local core candidate and trust it without touching provider credentials."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.build_public_release import build_public_release  # noqa: E402
from tools.codex_integration_guard import (  # noqa: E402
    CredentialSnapshot, GuardError, HOOK_KEYS, IntegrationGuard, PLUGIN_IDS,
    merge_integrations,
)
from tools.run_codex_native_hook_probe import Host  # noqa: E402


def _run(command):
    result = subprocess.run(command, capture_output=True, text=True, timeout=60)
    if result.returncode:
        # Never echo arguments or stderr: a management command may include
        # credentials in either surface. The receipt's stage locates the call.
        raise GuardError(f"installer_command_failed:{Path(command[0]).name}:exit_{result.returncode}")
    return result.stdout


def _cc_switch_codex_takeover() -> bool | None:
    """Detect cc-switch ownership before touching Codex's live config."""
    executable = shutil.which("cc-switch")
    if executable is None:
        return False
    try:
        result = subprocess.run(
            [executable, "daemon", "status"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    match = re.search(r"(?m)^\s*takeovers:[^\n]*\bcodex=(true|false)\b", result.stdout)
    return match.group(1) == "true" if match else None


def _ownership_allows_apply(takeover: bool | None, *, allow_unmanaged: bool) -> bool:
    """Allow writes only when cc-switch is absent or explicitly unmanaged.

    ``None`` means the daemon status could not be established.  The default is
    fail-closed; the explicit installer flag is a one-shot operator decision for
    machines where the daemon is intentionally stopped.  A positive takeover is
    never overridable, so an active cc-switch Codex owner cannot be raced.
    """
    if takeover is True:
        return False
    if takeover is False:
        return True
    return allow_unmanaged


def _connect():
    host = Host(os.environ.copy())
    host.call("initialize", {"clientInfo": {"name": "continuity-safe-installer", "version": "1"},
                             "capabilities": {"experimentalApi": True}})
    host.send({"method": "initialized", "params": {}})
    return host


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--allow-unmanaged-codex-home",
        action="store_true",
        help="continue only when cc-switch status is unknown, never when it reports codex=true",
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    takeover = _cc_switch_codex_takeover() if args.apply else False
    ownership_override = takeover is None and args.allow_unmanaged_codex_home
    if not _ownership_allows_apply(takeover, allow_unmanaged=args.allow_unmanaged_codex_home):
        receipt = {
            "observed_at": datetime.now(UTC).isoformat(),
            "status": "deferred", "apply_requested": True, "applied": False,
            "failed_gate": "cc_switch_codex_takeover" if takeover else "configuration_ownership_unknown",
            "ordinary_work_allowed": True, "guarded_operations": 0,
            "provider_rows_checked": 0, "protected_surfaces_unchanged": None,
            "plaintext_credentials_emitted": False,
            "next_action": "continue ordinary project work; defer installation without changing provider configuration",
            "stages": ["deferred-before-write"],
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
        print(json.dumps(receipt, sort_keys=True))
        return 0
    codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).resolve()
    switch_home = Path(os.environ.get("CC_SWITCH_CONFIG_DIR", Path.home() / ".cc-switch")).resolve()
    snapshot = CredentialSnapshot(codex_home, switch_home)
    guard = IntegrationGuard(snapshot)
    receipt = {"observed_at": datetime.now(UTC).isoformat(), "status": "planned", "applied": args.apply,
               "provider_rows_checked": snapshot.provider_count, "protected_surfaces_unchanged": True,
               "plaintext_credentials_emitted": False, "stages": [],
               "ownership_override": ownership_override}
    host = None
    try:
        root = codex_home / "dev-marketplaces/continuity-plane-current"
        marketplace = root / ".agents/plugins/marketplace.json"
        skill = codex_home / "skills/.system/plugin-creator"
        name = _run([sys.executable, str(skill / "scripts/read_marketplace_name.py"), "--marketplace-path", str(marketplace)]).strip()
        if name != "continuity-plane":
            raise GuardError("unexpected_marketplace")
        entry = json.loads(marketplace.read_text())
        core_entry = next(item for item in entry["plugins"] if item["name"] == "continuity-plane")
        if core_entry["source"] != {"source": "local", "path": "./plugins/continuity-plane"}:
            raise GuardError("unexpected_plugin_source")
        guard.check()
        if args.apply:
            with tempfile.TemporaryDirectory(prefix="continuity-safe-install-") as directory:
                temp = Path(directory)
                build_public_release(ROOT, temp / "public", initialize_git=False)
                source = temp / "public/plugins/continuity-plane"
                target = root / "plugins/continuity-plane"
                def prepare_candidate():
                    for relative in (
                        ".codex-plugin/plugin.json",
                        "scripts/continuity-hook.py",
                        "scripts/continuity-advisory-hook.py",
                        "hooks/hooks.json",
                    ):
                        destination = target / relative
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(source / relative, destination)
                    _run([sys.executable, str(skill / "scripts/update_plugin_cachebuster.py"), str(target)])
                    _run([sys.executable, str(skill / "scripts/validate_plugin.py"), str(target)])
                guard.run(prepare_candidate)
                receipt["candidate_version"] = json.loads((target / ".codex-plugin/plugin.json").read_text())["version"]
                receipt["stages"].append("candidate-prepared")
                # Replacement of an existing registered plugin needs no
                # installer CLI invocation. Never call cc-switch common set:
                # an empty current-provider config can make that command
                # replace the live config with only the common snippet. The
                # app-server CAS below is the only live-config mutation.
                if not (root / ".agents/plugins/marketplace.json").is_file():
                    raise GuardError("plugin_marketplace_missing")
                receipt["stages"].append("marketplace-verified")
                host = _connect()
                listed = host.call("hooks/list", {"cwds": [str(ROOT)]})
                found = [h for e in listed.get("data", []) for h in e.get("hooks", []) if h.get("pluginId") == "continuity-plane@continuity-plane"]
                if {h["key"] for h in found} != set(HOOK_KEYS):
                    raise GuardError("installed_hook_set_mismatch")
                patch = {
                    "plugins": {plugin: {"enabled": True} for plugin in PLUGIN_IDS},
                    "hooks": {"state": {h["key"]: {"trusted_hash": h["currentHash"], "enabled": True} for h in found}},
                }
                patch["plugins"]["continuity-plane-search@continuity-plane"]["mcp_servers"] = {
                    "continuity-search": {"enabled": True, "default_tools_approval_mode": "approve"}}
                patch["plugins"]["continuity-plane-state@continuity-plane"]["mcp_servers"] = {
                    "continuity": {"enabled": True, "default_tools_approval_mode": "approve"}}
                merge_integrations({}, patch)
                host.close()
                host = _connect()
                current = host.call("config/read", {"includeLayers": True})
                layer = next(x for x in current["layers"] if x["name"]["type"] == "user" and x["name"].get("profile") is None)
                # The API uses CAS against the existing user file and merges only these two tables.
                guard.run(lambda: host.call("config/batchWrite", {
                    "filePath": str(codex_home / "config.toml"), "expectedVersion": layer["version"],
                    "edits": [{"keyPath": key, "value": value, "mergeStrategy": "upsert"} for key, value in patch.items()],
                    "reloadUserConfig": True,
                }))
                receipt["stages"].append("live-integration-merged")
            host.close()
            host = _connect()
            listed = host.call("hooks/list", {"cwds": [str(ROOT)]})
            found = [h for e in listed.get("data", []) for h in e.get("hooks", []) if h.get("pluginId") == "continuity-plane@continuity-plane"]
            receipt["trusted_hooks"] = sum(h["trustStatus"] == "trusted" and h["enabled"] for h in found)
            if receipt["trusted_hooks"] != 6:
                raise GuardError("hook_trust_not_applied")
            guard.check()
            receipt["status"] = "passed"
            receipt["stages"].append("trust-verified")
    except Exception as exc:
        receipt["status"] = "failed"
        receipt["failed_gate"] = str(exc) if isinstance(exc, GuardError) else type(exc).__name__
        try:
            guard.check()
        except GuardError:
            receipt["protected_surfaces_unchanged"] = False
    finally:
        if host is not None:
            host.close()
    receipt["guarded_operations"] = guard.completed_operations
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    print(json.dumps(receipt, sort_keys=True))
    return 0 if receipt["status"] in {"passed", "planned", "deferred"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
