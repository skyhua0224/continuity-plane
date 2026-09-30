"""M9-06 Obsidian vault measured receipt tests."""

from __future__ import annotations

import copy
import hashlib
import importlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


def _reseal(receipt: dict) -> None:
    receipt["receipt_sha256"] = hashlib.sha256(
        json.dumps(
            {key: value for key, value in receipt.items() if key != "receipt_sha256"},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


class M906ObsidianVaultBenchmarkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(__file__).resolve().parents[1]

    def module(self):
        spec = importlib.util.find_spec(
            "context_control_plane.obsidian_vault_benchmark"
        )
        self.assertIsNotNone(spec, "M9-06 benchmark module is absent")
        return importlib.import_module("context_control_plane.obsidian_vault_benchmark")

    def test_local_build_write_validate_and_tamper_gates_are_measured(self) -> None:
        module = self.module()
        receipt = module.benchmark_obsidian_vault(
            root=self.root,
            iterations=5,
            generated_at="2026-08-17T23:59:00+08:00",
        )

        module.validate_obsidian_vault_benchmark(receipt, root=self.root)
        self.assertEqual(receipt["gate"], {"status": "passed", "failed_gates": []})
        self.assertEqual(receipt["results"]["build_validate_successes"], 5)
        for field in (
            "source_binding_rejections",
            "generated_file_tamper_rejections",
            "manifest_tamper_rejections",
            "unmanaged_file_rejections",
            "overwrite_rejections",
        ):
            self.assertEqual(receipt["results"][field], 5)
        self.assertEqual(receipt["results"]["authority_violations"], 0)
        self.assertEqual(receipt["results"]["provider_invocations"], 0)
        self.assertEqual(receipt["results"]["external_services"], 0)
        self.assertEqual(len(receipt["latency_samples_ms"]), 5)
        self.assertLess(receipt["latency_ms"]["p95"], 50.0)

    def test_resealed_gate_or_provenance_tampering_is_rejected(self) -> None:
        module = self.module()
        receipt = module.benchmark_obsidian_vault(
            root=self.root,
            iterations=2,
            generated_at="2026-08-17T23:59:00+08:00",
        )
        for mutate in (
            lambda item: item["results"].__setitem__("manifest_tamper_rejections", 1),
            lambda item: item["results"].__setitem__("authority_violations", 1),
            lambda item: item["latency_ms"].__setitem__("p95", 99.0),
            lambda item: item["provenance"].__setitem__(
                "implementation_sha256", "0" * 64
            ),
        ):
            forged = copy.deepcopy(receipt)
            mutate(forged)
            _reseal(forged)
            with (
                self.subTest(mutate=mutate),
                self.assertRaises(module.ObsidianVaultBenchmarkError),
            ):
                module.validate_obsidian_vault_benchmark(forged, root=self.root)

    def test_invalid_iteration_count_is_rejected(self) -> None:
        module = self.module()
        for iterations in (True, 0, 1001):
            with self.subTest(iterations=iterations), self.assertRaises(ValueError):
                module.benchmark_obsidian_vault(
                    root=self.root,
                    iterations=iterations,
                    generated_at="2026-08-17T23:59:00+08:00",
                )

    def test_runner_writes_a_valid_receipt(self) -> None:
        runner_path = self.root / "tools/run_obsidian_vault_benchmark.py"
        self.assertTrue(runner_path.is_file())
        if not runner_path.is_file():
            return
        from tools import run_obsidian_vault_benchmark

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "receipt.json"
            result = run_obsidian_vault_benchmark.main(
                [
                    "--root",
                    str(self.root),
                    "--iterations",
                    "3",
                    "--generated-at",
                    "2026-08-17T23:59:00+08:00",
                    "--output",
                    str(output),
                ]
            )
            receipt = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(result, 0)
        self.module().validate_obsidian_vault_benchmark(receipt, root=self.root)

    def test_committed_receipt_is_independently_valid(self) -> None:
        path = self.root / "experiments/evidence/m9-06-obsidian-vault-results.json"
        self.assertTrue(path.is_file())
        if not path.is_file():
            return
        receipt = json.loads(path.read_text(encoding="utf-8"))
        self.module().validate_obsidian_vault_benchmark(receipt, root=self.root)
        self.assertEqual(receipt["parameters"]["iterations"], 1000)
        self.assertEqual(receipt["gate"], {"status": "passed", "failed_gates": []})


if __name__ == "__main__":
    unittest.main()
