import copy
import hashlib
import json
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker, ValidationError

from context_control_plane.codegraph_verification import (
    CodeGraphVerificationError,
    canonical_codegraph_receipt_bytes,
    codegraph_clue_evidence_sha256,
    probe_codegraph_relation,
    validate_codegraph_receipt,
    verify_codegraph_clues,
)


class M602CodeGraphVerificationTests(unittest.TestCase):
    root = Path(__file__).parents[1]

    @staticmethod
    def reseal(receipt: dict) -> None:
        by_clue: dict[str, list[dict]] = {}
        for evidence in receipt["verifier_evidence"]:
            evidence["output_sha256"] = hashlib.sha256(
                json.dumps(
                    evidence["output"],
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
            by_clue.setdefault(evidence["clue_id"], []).append(evidence)
        for clue in receipt["clues"]:
            clue["index_sha256"] = codegraph_clue_evidence_sha256(
                by_clue[clue["clue_id"]]
            )
        unsigned = copy.deepcopy(receipt)
        unsigned.pop("receipt_sha256")
        receipt["receipt_sha256"] = hashlib.sha256(
            json.dumps(
                unsigned,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()

    def live_receipt(self) -> dict:
        return probe_codegraph_relation(
            root=self.root,
            clue_id="clue/m6-02/retrieval-to-benchmark",
            source_repository="context-control-plane",
            target_repository="context-control-plane",
            source_symbol="context_control_plane.retrieval_benchmark._plan",
            target_symbol="context_control_plane.retrieval_routing.plan_retrieval",
            relation="references",
            source_path="context_control_plane/retrieval_benchmark.py",
            symbol_name="plan_retrieval",
            index_revision="worktree:m6-codegraph-probe",
            verified_at="2026-08-15T07:15:00Z",
        )

    def clue(self) -> dict:
        clue = {
            "clue_id": "clue/execution-packet-to-artifact",
            "source_repository": "context-control-plane",
            "target_repository": "context-control-plane",
            "source_symbol": "context_control_plane.execution_packet.compose_execution_packet",
            "target_symbol": "context_control_plane.artifact_store.ArtifactRef",
            "relation": "references",
            "index_revision": "git:cfcf543",
            "index_sha256": "",
        }
        clue["index_sha256"] = codegraph_clue_evidence_sha256(self.evidence())
        return clue

    def evidence(self) -> list[dict]:
        common = {
            "clue_id": "clue/execution-packet-to-artifact",
            "source_symbol": "context_control_plane.execution_packet.compose_execution_packet",
            "target_symbol": "context_control_plane.artifact_store.ArtifactRef",
            "revision": "git:cfcf543",
            "source_sha256": "2" * 64,
            "target_sha256": "3" * 64,
        }
        items = []
        for verifier, target_path, target_line in (
            ("rg", "context_control_plane/execution_packet.py", 20),
            ("lsp", "context_control_plane/artifact_store.py", 10),
        ):
            command = [verifier, "definition", "ArtifactRef"]
            output = {
                "source_path": "context_control_plane/execution_packet.py",
                "source_line": 1,
                "query_line": 20,
                "query_column": 4,
                "target_path": target_path,
                "target_line": target_line,
                "target_column": 4,
            }
            items.append(
                {
                    **common,
                    "verifier": verifier,
                    "tool_version": f"{verifier}/fixture-v1",
                    "command": command,
                    "command_sha256": hashlib.sha256(
                        json.dumps(command, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
                    ).hexdigest(),
                    "output": output,
                    "output_sha256": hashlib.sha256(
                        json.dumps(output, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
                    ).hexdigest(),
                }
            )
        return items

    def test_requires_rg_and_lsp_for_every_codegraph_clue(self) -> None:
        receipt = verify_codegraph_clues(
            clues=[self.clue()],
            verifier_evidence=self.evidence(),
            verified_at="2026-08-15T05:00:00Z",
        )
        self.assertEqual(receipt["verified_clues"], 1)
        self.assertEqual(receipt["verifier_counts"], {"rg": 1, "lsp": 1})
        self.assertFalse(receipt["codegraph_authority"])
        self.assertFalse(receipt["state_write_authority"])
        canonical_codegraph_receipt_bytes(receipt)

    def test_rejects_missing_independent_verifier(self) -> None:
        with self.assertRaisesRegex(CodeGraphVerificationError, "rg and lsp"):
            verify_codegraph_clues(
                clues=[self.clue()],
                verifier_evidence=self.evidence()[:1],
                verified_at="2026-08-15T05:00:00Z",
            )

    def test_rejects_same_name_pollution(self) -> None:
        polluted = self.evidence()
        polluted[1]["target_symbol"] = "other_package.artifact_store.ArtifactRef"
        with self.assertRaisesRegex(CodeGraphVerificationError, "symbol mismatch"):
            verify_codegraph_clues(
                clues=[self.clue()],
                verifier_evidence=polluted,
                verified_at="2026-08-15T05:00:00Z",
            )

    def test_rejects_duplicate_clues_and_unknown_fields(self) -> None:
        with self.assertRaises(CodeGraphVerificationError):
            verify_codegraph_clues(
                clues=[self.clue(), self.clue()],
                verifier_evidence=self.evidence(),
                verified_at="2026-08-15T05:00:00Z",
            )

        unknown = copy.deepcopy(self.clue())
        unknown["complete"] = True
        with self.assertRaises(CodeGraphVerificationError):
            verify_codegraph_clues(
                clues=[unknown],
                verifier_evidence=self.evidence(),
                verified_at="2026-08-15T05:00:00Z",
            )

    def test_receipt_validator_rechecks_verifier_pairing(self) -> None:
        receipt = verify_codegraph_clues(
            clues=[self.clue()],
            verifier_evidence=self.evidence(),
            verified_at="2026-08-15T05:00:00Z",
        )
        receipt["verifier_evidence"][1]["verifier"] = "rg"
        unsigned = copy.deepcopy(receipt)
        unsigned.pop("receipt_sha256")
        receipt["receipt_sha256"] = hashlib.sha256(
            json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        with self.assertRaisesRegex(CodeGraphVerificationError, "rg and lsp"):
            validate_codegraph_receipt(receipt)

    def test_live_rg_and_lsp_probe_binds_current_repository_evidence(self) -> None:
        receipt = probe_codegraph_relation(
            root=self.root,
            clue_id="clue/m6-02/retrieval-to-benchmark",
            source_repository="context-control-plane",
            target_repository="context-control-plane",
            source_symbol="context_control_plane.retrieval_benchmark._plan",
            target_symbol="context_control_plane.retrieval_routing.plan_retrieval",
            relation="references",
            source_path="context_control_plane/retrieval_benchmark.py",
            symbol_name="plan_retrieval",
            index_revision="worktree:m6-codegraph-probe",
            verified_at="2026-08-15T07:15:00Z",
        )
        validate_codegraph_receipt(receipt, root=self.root)
        evidence = {item["verifier"]: item for item in receipt["verifier_evidence"]}
        self.assertEqual(evidence["rg"]["tool_version"].split()[0], "ripgrep")
        self.assertEqual(evidence["lsp"]["tool_version"], "pylsp/1.15.0")
        self.assertEqual(
            evidence["lsp"]["output"]["target_path"],
            "context_control_plane/retrieval_routing.py",
        )

    def test_live_probe_digest_is_stable_for_unchanged_repository(self) -> None:
        arguments = {
            "root": self.root,
            "clue_id": "clue/m6-02/retrieval-to-benchmark",
            "source_repository": "context-control-plane",
            "target_repository": "context-control-plane",
            "source_symbol": "context_control_plane.retrieval_benchmark._plan",
            "target_symbol": "context_control_plane.retrieval_routing.plan_retrieval",
            "relation": "references",
            "source_path": "context_control_plane/retrieval_benchmark.py",
            "symbol_name": "plan_retrieval",
            "index_revision": "worktree:m6-codegraph-probe",
            "verified_at": "2026-08-15T07:15:00Z",
        }
        first = probe_codegraph_relation(**arguments)
        second = probe_codegraph_relation(**arguments)
        self.assertEqual(first["clues"][0]["index_sha256"], second["clues"][0]["index_sha256"])
        self.assertEqual(first["receipt_sha256"], second["receipt_sha256"])

    def test_current_repository_validation_rejects_resealed_fake_tool_evidence(self) -> None:
        receipt = probe_codegraph_relation(
            root=self.root,
            clue_id="clue/m6-02/retrieval-to-benchmark",
            source_repository="context-control-plane",
            target_repository="context-control-plane",
            source_symbol="context_control_plane.retrieval_benchmark._plan",
            target_symbol="context_control_plane.retrieval_routing.plan_retrieval",
            relation="references",
            source_path="context_control_plane/retrieval_benchmark.py",
            symbol_name="plan_retrieval",
            index_revision="worktree:m6-codegraph-probe",
            verified_at="2026-08-15T07:15:00Z",
        )
        receipt["verifier_evidence"][0]["output"]["source_path"] = (
            "context_control_plane/recall_provider.py"
        )
        output = receipt["verifier_evidence"][0]["output"]
        receipt["verifier_evidence"][0]["output_sha256"] = hashlib.sha256(
            json.dumps(output, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        receipt["clues"][0]["index_sha256"] = codegraph_clue_evidence_sha256(
            receipt["verifier_evidence"]
        )
        unsigned = copy.deepcopy(receipt)
        unsigned.pop("receipt_sha256")
        receipt["receipt_sha256"] = hashlib.sha256(
            json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        with self.assertRaisesRegex(CodeGraphVerificationError, "current repository"):
            validate_codegraph_receipt(receipt, root=self.root)

    def test_receipt_recomputes_clue_digest_from_verifier_evidence(self) -> None:
        receipt = probe_codegraph_relation(
            root=self.root,
            clue_id="clue/m6-02/retrieval-to-benchmark",
            source_repository="context-control-plane",
            target_repository="context-control-plane",
            source_symbol="context_control_plane.retrieval_benchmark._plan",
            target_symbol="context_control_plane.retrieval_routing.plan_retrieval",
            relation="references",
            source_path="context_control_plane/retrieval_benchmark.py",
            symbol_name="plan_retrieval",
            index_revision="worktree:m6-codegraph-probe",
            verified_at="2026-08-15T07:15:00Z",
        )
        receipt["clues"][0]["index_sha256"] = "f" * 64
        unsigned = copy.deepcopy(receipt)
        unsigned.pop("receipt_sha256")
        receipt["receipt_sha256"] = hashlib.sha256(
            json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        with self.assertRaisesRegex(CodeGraphVerificationError, "index digest"):
            validate_codegraph_receipt(receipt, root=self.root)

    def test_current_probe_rejects_unproved_relation_kind(self) -> None:
        with self.assertRaisesRegex(CodeGraphVerificationError, "relation"):
            probe_codegraph_relation(
                root=self.root,
                clue_id="clue/m6-02/retrieval-to-benchmark",
                source_repository="context-control-plane",
                target_repository="context-control-plane",
                source_symbol="context_control_plane.retrieval_benchmark._plan",
                target_symbol="context_control_plane.retrieval_routing.plan_retrieval",
                relation="calls",
                source_path="context_control_plane/retrieval_benchmark.py",
                symbol_name="plan_retrieval",
                index_revision="worktree:m6-codegraph-probe",
                verified_at="2026-08-15T07:15:00Z",
            )

    def test_root_validation_rejects_resealed_fake_module_prefixes(self) -> None:
        receipt = self.live_receipt()
        for field in ("source_symbol", "target_symbol"):
            with self.subTest(field=field):
                invalid = copy.deepcopy(receipt)
                original = invalid["clues"][0][field]
                replacement = "fake.module." + original.rsplit(".", 1)[-1]
                invalid["clues"][0][field] = replacement
                for evidence in invalid["verifier_evidence"]:
                    evidence[field] = replacement
                self.reseal(invalid)
                with self.assertRaisesRegex(
                    CodeGraphVerificationError, "module/path"
                ):
                    validate_codegraph_receipt(invalid, root=self.root)

    def test_root_validation_rejects_resealed_repository_path_binding(self) -> None:
        receipt = self.live_receipt()
        for field in ("source_repository", "target_repository"):
            with self.subTest(field=field):
                invalid = copy.deepcopy(receipt)
                invalid["clues"][0][field] = "forged-repository"
                self.reseal(invalid)
                with self.assertRaisesRegex(
                    CodeGraphVerificationError, "repository identity"
                ):
                    validate_codegraph_receipt(invalid, root=self.root)

    def test_schema_rejects_noncanonical_repository_and_symbol_tokens(self) -> None:
        schema = json.loads(
            (
                self.root
                / "schemas/m6-02/codegraph-verification-receipt.schema.json"
            ).read_text()
        )
        validator = Draft202012Validator(schema, format_checker=FormatChecker())
        receipt = self.live_receipt()
        validator.validate(receipt)

        mutations = (
            lambda value: value["clues"][0].update(
                source_repository="forged/repository"
            ),
            lambda value: value["clues"][0].update(
                source_symbol="fake-module._plan"
            ),
            lambda value: value["verifier_evidence"][0].update(
                target_symbol="fake-module.plan_retrieval"
            ),
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                invalid = copy.deepcopy(receipt)
                mutation(invalid)
                with self.assertRaises(ValidationError):
                    validator.validate(invalid)


if __name__ == "__main__":
    unittest.main()
