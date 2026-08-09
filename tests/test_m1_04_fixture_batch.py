from dataclasses import asdict
import json
import re
import subprocess
import sys
import unittest
from pathlib import Path

from experiments.build_m1_04_replay_fixtures import fixture_specs
from context_control_plane.replay_fixture import validate_replay_fixture


class FixtureBatchTests(unittest.TestCase):
    def test_public_specs_define_forty_unique_real_replay_cases(self):
        specs = fixture_specs()

        self.assertEqual(len(specs), 40)
        self.assertEqual(len({spec.spec_id for spec in specs}), 40)
        self.assertEqual(
            len({(spec.source_slot, spec.line_number, spec.anchors) for spec in specs}),
            20,
        )
        self.assertTrue(all(len(spec.anchors) >= 2 for spec in specs))

    def test_batch_covers_required_alkaid_failure_classes(self):
        specs = fixture_specs()
        topics = {spec.topic for spec in specs}
        scenarios = {spec.scenario_class for spec in specs}

        self.assertTrue(
            {
                "n42-n67-blocker",
                "n68-rollback",
                "localsend-promotion",
                "single-ping-inference",
                "pacer-comparison",
                "final-srtt",
                "measurement-harness",
                "cl-queue-authorization",
            }.issubset(topics)
        )
        self.assertTrue(
            {
                "compaction-recovery",
                "task-routing",
                "stale-decision",
                "evidence-gap",
                "scope-drift",
                "experiment-promotion",
            }.issubset(scenarios)
        )

    def test_public_specs_contain_no_paths_or_provider_identifiers(self):
        encoded = json.dumps([spec.to_public_dict() for spec in fixture_specs()])

        self.assertNotIn("/home/", encoded)
        self.assertNotIn("/Users/", encoded)
        self.assertIsNone(
            re.search(
                r"[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
                encoded,
                re.IGNORECASE,
            )
        )

    def test_script_entrypoint_resolves_repository_package(self):
        root = Path(__file__).parents[1]
        result = subprocess.run(
            [
                sys.executable,
                str(root / "experiments" / "build_m1_04_replay_fixtures.py"),
                "--help",
            ],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_generated_corpus_revalidates_from_disk_against_receipts(self):
        root = Path(__file__).parents[1]
        fixture_dir = root / "replay" / "fixtures"
        manifest = json.loads(
            (fixture_dir / "validation-receipts.json").read_text(encoding="utf-8")
        )
        fixture_paths = sorted(
            path
            for path in fixture_dir.glob("fx_*.json")
            if path.name != "validation-receipts.json"
        )

        self.assertEqual(manifest["fixture_count"], 40)
        self.assertEqual(len(fixture_paths), 40)
        receipts = {item["fixture_id"]: item for item in manifest["receipts"]}
        self.assertEqual(len(receipts), 40)
        for path in fixture_paths:
            fixture = json.loads(path.read_text(encoding="utf-8"))
            recorded = receipts[fixture["fixture_id"]]
            receipt = validate_replay_fixture(
                fixture,
                current_evidence_refs=set(manifest["evidence_refs"]),
                validated_at=recorded["validated_at"],
            )
            for key, value in asdict(receipt).items():
                self.assertEqual(recorded[key], value)


if __name__ == "__main__":
    unittest.main()
