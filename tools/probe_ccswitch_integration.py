"""Verify common integration updates with fake keys in an isolated CC Switch home."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
from pathlib import Path

import tomli_w

from codex_integration_guard import CredentialSnapshot, HOOK_KEYS, IntegrationGuard, merge_integrations


def main():
    real = CredentialSnapshot(Path.home() / ".codex", Path.home() / ".cc-switch")
    guard = IntegrationGuard(real)
    with tempfile.TemporaryDirectory(prefix="continuity-ccswitch-fixture-") as directory:
        root = Path(directory)
        switch = root / "switch"
        switch.mkdir()
        settings = {}
        for app in ("claude", "codex", "gemini", "opencode", "hermes", "openclaw", "pi"):
            target = root / app
            target.mkdir()
            settings[f"{app}ConfigDir"] = str(target)
        (switch / "settings.json").write_text(json.dumps(settings))
        environment = {**os.environ, "CC_SWITCH_CONFIG_DIR": str(switch)}

        def run(*args):
            result = guard.run(lambda: subprocess.run(
                [shutil.which("cc-switch"), "--app", "codex", *args],
                env=environment, capture_output=True, text=True, timeout=30,
            ))
            if result.returncode:
                raise RuntimeError("isolated_ccswitch_command_failed")

        for index in (1, 2):
            document = {"auth": {"OPENAI_API_KEY": f"fake-distinct-key-{index}"},
                        "config": f'model_provider="provider{index}"\nmodel="test-model"\n[model_providers.provider{index}]\nname="Fixture"\nbase_url="https://example.invalid/{index}"\nwire_api="responses"\nrequires_openai_auth=true\n'}
            path = root / f"provider-{index}.json"
            path.write_text(json.dumps(document))
            run("provider", "add", "--name", f"Fixture {index}", "--id", f"fixture-{index}", "--config-file", str(path), "--common-config")
        run("provider", "switch", "fixture-1")
        if not (root / "codex/config.toml").exists():
            raise RuntimeError("fixture_codex_override_not_used")

        def credentials():
            connection = sqlite3.connect((switch / "cc-switch.db").as_uri() + "?mode=ro", uri=True)
            try:
                return {identity: json.loads(raw)["auth"] for identity, raw in connection.execute(
                    "SELECT id,settings_config FROM providers WHERE app_type='codex' ORDER BY id"
                )}
            finally:
                connection.close()

        before = credentials()
        patch = {"plugins": {"continuity-plane@continuity-plane": {"enabled": True}},
                 "hooks": {"state": {HOOK_KEYS[0]: {"trusted_hash": "sha256:" + "a" * 64}}}}
        snippet = root / "common.toml"
        snippet.write_text(tomli_w.dumps(merge_integrations({}, patch)))
        run("config", "common", "set", "--file", str(snippet))
        run("provider", "switch", "fixture-2")
        run("provider", "switch", "fixture-1")
        if before != credentials():
            raise RuntimeError("isolated_provider_credentials_changed")
        guard.check()
        print(json.dumps({"status": "passed", "fake_providers": 2, "provider_credentials_unchanged": True,
                          "real_providers_checked": real.provider_count, "real_protected_surfaces_unchanged": True,
                          "plaintext_credentials_emitted": False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
