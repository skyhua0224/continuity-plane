"""Installation-only checks keep provider credentials out of integration edits."""

import copy
import importlib.util
import io
import json
import subprocess
import tempfile
import unittest
from unittest import mock
from pathlib import Path


class IntegrationGuardTests(unittest.TestCase):
    def load(self):
        path = Path(__file__).parents[1] / "tools/codex_integration_guard.py"
        self.assertTrue(path.is_file(), "integration guard is missing")
        spec = importlib.util.spec_from_file_location("integration_guard", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_three_auth_modes_survive_the_same_patch(self):
        g = self.load()
        patch = {"plugins": {"continuity-plane@continuity-plane": {"enabled": True}},
                 "hooks": {"state": {g.HOOK_KEYS[0]: {"trusted_hash": "sha256:" + "a" * 64}}}}
        for mode, credential in (("official", "fake-official-token"), ("third-party", "fake-provider-b-key"), ("proxy", "PROXY_MANAGED")):
            config = {"model_provider": mode, "model_providers": {mode: {"experimental_bearer_token": credential}},
                      "model": "test-model", "hooks": {"state": {"another-plugin": {"trusted_hash": "keep"}}}}
            frozen = copy.deepcopy(config)
            changed = g.merge_integrations(config, patch)
            self.assertEqual(g.protected_config(changed), g.protected_config(frozen))
            self.assertEqual(config, frozen)
            self.assertEqual(changed["hooks"]["state"]["another-plugin"], frozen["hooks"]["state"]["another-plugin"])

    def test_credentials_and_unrelated_plugins_are_rejected(self):
        g = self.load()
        for patch in ({"model_provider": "first"}, {"model_providers": {"first": {"env_key": "FIRST_KEY"}}},
                      {"auth": {"OPENAI_API_KEY": "fake-first-key"}},
                      {"plugins": {"other-plugin": {"enabled": True}}},
                      {"hooks": {"PreToolUse": []}},
                      {"hooks": {"state": {"unrelated-hook": {"trusted_hash": "sha256:" + "a" * 64}}}}):
            with self.subTest(patch=patch), self.assertRaises(g.GuardError):
                g.merge_integrations({}, patch)

    def test_common_snippet_rejects_credentials_and_routing(self):
        g = self.load()
        for snippet in ('model_provider="first"', '[model_providers.first]\nbase_url="https://invalid"',
                        'experimental_bearer_token="fake-first-key"', '[env]\nOPENAI_API_KEY="fake-first-key"'):
            with self.subTest(snippet=snippet), self.assertRaises(g.GuardError):
                g.safe_common(snippet)
        self.assertEqual(g.safe_common('model_context_window=1000000'), {"model_context_window": 1000000})

    def test_late_credential_drift_aborts_without_running_operation(self):
        g = self.load()
        calls = []
        guard = g.IntegrationGuard(lambda: {"credentials": "before"})
        guard.snapshot = lambda: {"credentials": "changed"}
        with self.assertRaises(g.GuardError):
            guard.run(lambda: calls.append("must-not-run"))
        self.assertEqual(calls, [])

    def test_post_write_drift_does_not_restore_old_user_settings(self):
        g = self.load()
        state = {"credentials": "before"}
        guard = g.IntegrationGuard(lambda: copy.deepcopy(state))
        with self.assertRaises(g.GuardError):
            guard.run(lambda: state.update(credentials="external-change"))
        self.assertEqual(state["credentials"], "external-change")

    def test_mismatched_codex_home_override_is_rejected(self):
        g = self.load()
        with self.assertRaises(g.GuardError):
            g.validate_codex_home(Path("/expected"), {"codexConfigDir": "/other-provider-home"})
        g.validate_codex_home(Path("/expected"), {"codexConfigDir": "/expected"})
        g.validate_codex_home(Path("/expected"), {})

    def test_uninstalled_cc_switch_and_opencodex_surface_are_protected(self):
        g = self.load()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            codex_home = root / "codex"
            switch_home = root / "cc-switch"
            codex_home.mkdir()
            (codex_home / "config.toml").write_text(
                'model_provider="opencodex"\nmodel="test-model"\n',
                encoding="utf-8",
            )
            (codex_home / "opencodex.config.toml").write_text(
                'model_provider="opencodex"\n', encoding="utf-8"
            )
            snapshot = g.CredentialSnapshot(codex_home, switch_home)
            baseline = snapshot()
            self.assertEqual(snapshot.provider_count, 0)
            self.assertEqual(baseline["switch_settings"], snapshot.digest(b"uninstalled"))
            self.assertIn("opencodex_nonintegration", baseline)
            guard = g.IntegrationGuard(snapshot)
            with self.assertRaises(g.GuardError):
                guard.run(lambda: (codex_home / "opencodex.config.toml").write_text(
                    'model_provider="changed"\n', encoding="utf-8"
                ))

    def test_partial_cc_switch_installation_remains_fail_closed(self):
        g = self.load()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            codex_home = root / "codex"
            switch_home = root / "cc-switch"
            codex_home.mkdir()
            switch_home.mkdir()
            (codex_home / "config.toml").write_text('model="test-model"\n', encoding="utf-8")
            (switch_home / "settings.json").write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(g.GuardError, "credential_snapshot_unavailable"):
                g.CredentialSnapshot(codex_home, switch_home)()

    def test_cc_switch_takeover_is_detected_without_reading_provider_data(self):
        path = Path(__file__).parents[1] / "tools/configure_codex_continuity.py"
        spec = importlib.util.spec_from_file_location("configure_codex_continuity", path)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        with (
            mock.patch.object(module.shutil, "which", return_value="cc-switch"),
            mock.patch.object(
                module.subprocess,
                "run",
                return_value=subprocess.CompletedProcess(
                    ["cc-switch"], 0, "takeovers: claude=false, codex=true\n", ""
                ),
            ),
        ):
            self.assertTrue(module._cc_switch_codex_takeover())

    def test_deferred_install_has_a_receipt_without_reading_or_writing_configs(self):
        from tools import configure_codex_continuity as installer

        for takeover in (True, None):
            with self.subTest(takeover=takeover), tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / "receipt.json"
                with (
                    mock.patch.object(installer.sys, "argv", ["installer", "--apply", "--output", str(output)]),
                    mock.patch.object(installer, "_cc_switch_codex_takeover", return_value=takeover),
                    mock.patch.object(installer, "CredentialSnapshot", side_effect=AssertionError("provider access")) as snapshot,
                    mock.patch.object(installer, "_run", side_effect=AssertionError("installation command")) as command,
                    mock.patch.object(installer, "_connect", side_effect=AssertionError("host access")) as connect,
                    mock.patch.object(installer.sys, "stdout", io.StringIO()),
                ):
                    self.assertEqual(installer.main(), 0)
                receipt = json.loads(output.read_text())
                self.assertEqual(receipt["status"], "deferred")
                self.assertTrue(receipt["apply_requested"])
                self.assertFalse(receipt["applied"])
                self.assertEqual(receipt["guarded_operations"], 0)
                self.assertEqual(receipt["provider_rows_checked"], 0)
                snapshot.assert_not_called()
                command.assert_not_called()
                connect.assert_not_called()

    def test_explicit_unmanaged_override_allows_only_unknown_owner(self):
        from tools import configure_codex_continuity as installer

        self.assertFalse(installer._ownership_allows_apply(True, allow_unmanaged=True))
        self.assertTrue(installer._ownership_allows_apply(False, allow_unmanaged=False))
        self.assertFalse(installer._ownership_allows_apply(None, allow_unmanaged=False))
        self.assertTrue(installer._ownership_allows_apply(None, allow_unmanaged=True))


if __name__ == "__main__":
    unittest.main()
