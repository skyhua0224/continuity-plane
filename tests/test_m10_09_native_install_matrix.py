"""M10-09 cross-platform installation matrix receipt contract."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from context_control_plane.native_install_matrix import (
    NativeInstallMatrixError,
    build_native_install_matrix_receipt,
    validate_native_install_matrix_receipt,
)


def _steps(status: str = "passed") -> dict[str, dict[str, object]]:
    return {
        name: {
            "status": status,
            "duration_ms": 12.5,
            "output_sha256": "a" * 64,
        }
        for name in ("install", "verify", "export", "import", "rollback", "uninstall")
    }


class M1009NativeInstallMatrixTests(unittest.TestCase):
    def _receipt(self) -> dict[str, object]:
        return build_native_install_matrix_receipt(
            project_id="sample-project",
            requested_profile="local-embedded",
            platform={
                "system": "Linux",
                "release": "6.1",
                "machine": "x86_64",
                "python": "3.14.0",
            },
            package={
                "name": "continuity-plane",
                "version": "0.1.0a1",
                "artifact_sha256": "b" * 64,
            },
            steps=_steps(),
            generated_at="2026-08-18T20:00:00+08:00",
        )

    def test_receipt_is_strict_and_replayable(self) -> None:
        receipt = self._receipt()

        validate_native_install_matrix_receipt(receipt)
        self.assertEqual(receipt["schema_version"], "context.native-install-matrix/v1alpha1")
        self.assertEqual(receipt["verdict"], "passed")
        self.assertEqual(receipt["profile"]["external_services_required"], 0)
        self.assertEqual(receipt["consistency"]["state_write_authority"], False)
        self.assertEqual(receipt["consistency"]["rollback_hash_matches_export"], True)

        replay = copy.deepcopy(receipt)
        validate_native_install_matrix_receipt(replay)
        self.assertEqual(replay, receipt)

    def test_blocked_native_runner_is_explicit_and_not_a_pass(self) -> None:
        receipt = build_native_install_matrix_receipt(
            project_id="sample-project",
            requested_profile="local-embedded",
            platform={"system": "Darwin", "release": "unknown", "machine": "arm64", "python": "3.14.0"},
            package={"name": "continuity-plane", "version": "0.1.0a1", "artifact_sha256": "b" * 64},
            steps=_steps("blocked"),
            generated_at="2026-08-18T20:00:00+08:00",
        )

        validate_native_install_matrix_receipt(receipt)
        self.assertEqual(receipt["verdict"], "blocked")
        self.assertEqual(receipt["platform"]["system"], "Darwin")

    def test_profile_cannot_claim_external_services_in_local_mode(self) -> None:
        receipt = self._receipt()
        receipt["profile"]["external_services_required"] = 1

        with self.assertRaisesRegex(NativeInstallMatrixError, "external services"):
            validate_native_install_matrix_receipt(receipt)

    def test_tampered_step_and_digest_are_rejected(self) -> None:
        receipt = self._receipt()
        receipt["steps"]["verify"]["status"] = "failed"
        with self.assertRaisesRegex(NativeInstallMatrixError, "receipt digest"):
            validate_native_install_matrix_receipt(receipt)

        receipt = self._receipt()
        receipt["steps"]["rollback"]["output_sha256"] = "c" * 64
        with self.assertRaisesRegex(NativeInstallMatrixError, "rollback"):
            validate_native_install_matrix_receipt(receipt)

    def test_failed_install_cannot_be_reported_as_a_successful_uninstall(self) -> None:
        steps = _steps()
        steps["install"]["status"] = "failed"
        steps["uninstall"]["status"] = "passed"

        with self.assertRaisesRegex(NativeInstallMatrixError, "install"):
            build_native_install_matrix_receipt(
                project_id="sample-project",
                requested_profile="local-embedded",
                platform={"system": "Windows", "release": "11", "machine": "amd64", "python": "3.14.0"},
                package={"name": "continuity-plane", "version": "0.1.0a1", "artifact_sha256": "b" * 64},
                steps=steps,
                generated_at="2026-08-18T20:00:00+08:00",
            )

    def test_local_probe_marks_missing_artifact_and_migration_as_blocked(self) -> None:
        from tools.run_native_install_matrix import run_native_install_matrix

        with tempfile.TemporaryDirectory() as directory:
            receipt = run_native_install_matrix(root=Path(directory))

        validate_native_install_matrix_receipt(receipt)
        self.assertEqual(receipt["verdict"], "blocked")
        self.assertEqual(receipt["steps"]["install"]["status"], "blocked")
        self.assertEqual(receipt["steps"]["export"]["status"], "blocked")
        self.assertEqual(receipt["steps"]["rollback"]["status"], "blocked")
        self.assertEqual(json.loads(json.dumps(receipt)), receipt)


if __name__ == "__main__":
    unittest.main()
