import copy
import hashlib
import json
import unittest
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator, FormatChecker, ValidationError

from context_control_plane.assertion_provenance import compose_assertion_provenance
from context_control_plane.codegraph_verification import (
    codegraph_clue_evidence_sha256,
    verify_codegraph_clues,
)
from context_control_plane.mcp_admission import (
    decide_mcp_admission,
    mcp_registry_snapshot_sha256,
)
from context_control_plane.memory_ablation import benchmark_memory_ablation
from context_control_plane.recall_provider import (
    InMemoryRecallProvider,
    RecallCoordinator,
)
from context_control_plane.retrieval_benchmark import benchmark_retrieval
from context_control_plane.retrieval_routing import (
    compose_retrieval_receipt,
    plan_retrieval,
)
from context_control_plane.reviewer_adapter import (
    LocalReviewerAdapter,
    ReviewCoordinator,
)


class M6ContractSchemaTests(unittest.TestCase):
    root = Path(__file__).parents[1]

    @staticmethod
    def _json_sha256(value):
        return hashlib.sha256(
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()

    def _current_code_assertion_evidence(self):
        relative_path = "schemas/m7-01/assertion-provenance.schema.json"
        payload = (self.root / relative_path).read_bytes()
        digest = hashlib.sha256(payload).hexdigest()
        receipt = (
            self.root / "experiments/retrieval/m6-01-retrieval-receipt.json"
        ).read_bytes()
        receipt_ref = "artifact://sha256/" + hashlib.sha256(receipt).hexdigest()

        def artifact_resolver(ref):
            return receipt if ref == receipt_ref else None

        return (
            [
                {
                    "evidence_id": "evidence/schema/assertion",
                    "authority_kind": "current_code",
                    "source_ref": f"repo://context-control-plane/{relative_path}#L1",
                    "revision": f"worktree:sha256:{digest}",
                    "sha256": digest,
                    "valid_at": "2026-08-15T07:00:00Z",
                    "retrieval_receipt_ref": receipt_ref,
                }
            ],
            {"root": self.root, "artifact_resolver": artifact_resolver},
        )

    def _retrieval_plan_and_receipt(self):
        question = {
            "question_id": "question/schema/retrieval",
            "kind": "symbol_definition",
            "query": "ExecutionPacket",
            "repositories": ["context-control-plane"],
            "freshness_required": True,
        }
        plan = plan_retrieval(
            question,
            available_tools={"rg", "lsp"},
            max_queries=2,
            max_scanned_bytes=1024,
            max_returned_bytes=256,
            max_index_age_seconds=3600,
        )
        evidence = [
            {
                "evidence_id": "evidence/schema/retrieval",
                "source_kind": "current_code",
                "source_ref": "repo://context-control-plane/retrieval.py#L1",
                "revision": "git:cfcf543",
                "sha256": "1" * 64,
                "range": {"offset_bytes": 0, "length_bytes": 64},
                "retrieved_at": "2026-08-15T07:00:00Z",
                "valid_at": "2026-08-15T07:00:00Z",
            }
        ]
        receipt = compose_retrieval_receipt(
            plan=plan,
            evidence=evidence,
            step_results=[
                {
                    "tool": "rg",
                    "queries": 1,
                    "scanned_bytes": 256,
                    "returned_bytes": 64,
                    "index_revision": None,
                    "index_sha256": None,
                    "index_age_seconds": 0,
                },
                {
                    "tool": "lsp",
                    "queries": 1,
                    "scanned_bytes": 256,
                    "returned_bytes": 64,
                    "index_revision": "git:cfcf543",
                    "index_sha256": "2" * 64,
                    "index_age_seconds": 10,
                },
            ],
            executed_at="2026-08-15T07:00:01Z",
            cache_status="miss",
            prior_receipt_ref=None,
        )
        return plan, receipt

    def _codegraph(self):
        clue = {
            "clue_id": "clue/schema/graph",
            "source_repository": "context-control-plane",
            "target_repository": "context-control-plane",
            "source_symbol": "context_control_plane.retrieval_routing.plan_retrieval",
            "target_symbol": "context_control_plane.retrieval_benchmark.benchmark_retrieval",
            "relation": "references",
            "index_revision": "git:cfcf543",
            "index_sha256": "1" * 64,
        }
        common = {
            "clue_id": clue["clue_id"],
            "source_symbol": clue["source_symbol"],
            "target_symbol": clue["target_symbol"],
            "revision": "git:cfcf543",
            "source_sha256": "2" * 64,
            "target_sha256": "3" * 64,
            "tool_version": "schema-fixture/1",
        }
        evidence = []
        for verifier in ("rg", "lsp"):
            command = [verifier, "schema-fixture"]
            output = {
                "source_path": "context_control_plane/retrieval_routing.py",
                "source_line": 1,
                "query_line": 2,
                "query_column": 0,
                "target_path": "context_control_plane/retrieval_benchmark.py",
                "target_line": 1,
                "target_column": 0,
            }
            evidence.append(
                {
                    **common,
                    "verifier": verifier,
                    "command": command,
                    "command_sha256": self._json_sha256(command),
                    "output": output,
                    "output_sha256": self._json_sha256(output),
                }
            )
        clue["index_sha256"] = codegraph_clue_evidence_sha256(evidence)
        return verify_codegraph_clues(
            clues=[clue], verifier_evidence=evidence, verified_at="2026-08-15T07:00:00Z"
        )

    def _recall(self):
        content = "Current schema candidate."
        provider = InMemoryRecallProvider(
            [
                {
                    "record_id": "memory/schema",
                    "content": content,
                    "tags": ["schema"],
                    "observed_at": "2026-08-15T07:00:00Z",
                    "valid_until": "2026-08-16T07:00:00Z",
                    "source_ref": "event://schema",
                    "source_sha256": "1" * 64,
                }
            ]
        )
        return RecallCoordinator(provider).retrieve(
            {
                "request_id": "recall/schema",
                "task_id": "M6-03",
                "query": "schema",
                "max_candidates": 1,
                "max_returned_bytes": 256,
            },
            observed_at="2026-08-15T07:00:01Z",
        )

    def _review(self):
        finding = {
            "finding_id": "finding/schema",
            "severity": "low",
            "summary": "Review candidate.",
            "evidence_refs": ["artifact://sha256/" + "1" * 64],
            "candidate_only": True,
        }
        return ReviewCoordinator(LocalReviewerAdapter([finding])).review(
            {
                "request_id": "review/schema",
                "task_id": "M6-05",
                "execution_packet_ref": "artifact://sha256/" + "2" * 64,
                "artifact_refs": ["artifact://sha256/" + "1" * 64],
                "max_output_bytes": 256,
                "deadline_at": "2026-08-15T08:00:00Z",
            },
            observed_at="2026-08-15T07:00:00Z",
        )

    def _mcp(self):
        snapshot = {
            "schema_version": "context.mcp-registry-snapshot/v1alpha1",
            "snapshot_id": "mcp/schema",
            "registry_kind": "fixture",
            "registry_url": "https://registry.modelcontextprotocol.io/",
            "registry_revision": "f36b7dd4afe2d540a4ceb9b64d3627085bf5db03",
            "registry_sha256": "",
            "retrieved_at": "2026-08-15T07:00:00Z",
            "servers": [
                {
                    "server_id": "server/schema",
                    "publisher": "publisher",
                    "publisher_verified": True,
                    "license_ref": "Apache-2.0",
                    "license_verified": True,
                    "source_url": "https://example.invalid/server",
                    "source_revision": "2" * 40,
                    "source_sha256": "3" * 64,
                    "auth_kind": "none",
                    "tools": [
                        {"name": "search", "scope": "read", "requires_state_mcp": False}
                    ],
                }
            ],
        }
        snapshot["registry_sha256"] = mcp_registry_snapshot_sha256(snapshot)
        request = {
            "request_id": "mcp/schema",
            "project_id": "context-control-plane",
            "operation_id": "operation/schema",
            "requested_server_ids": ["server/schema"],
            "granted_scopes": ["read"],
            "available_auth_kinds": ["none"],
            "allow_external_effects": False,
            "state_mcp_route": None,
            "expected_registry_revision": snapshot["registry_revision"],
            "expected_registry_sha256": snapshot["registry_sha256"],
        }
        return snapshot, decide_mcp_admission(snapshot, request)

    def test_all_m6_contracts_are_registered_and_schema_valid(self):
        plan, receipt = self._retrieval_plan_and_receipt()
        snapshot, decision = self._mcp()
        assertion_evidence, assertion_resolution = self._current_code_assertion_evidence()
        assertion = compose_assertion_provenance(
            assertion_id="assertion/schema/m6",
            assertion_text="Current schema evidence.",
            bearing=True,
            evidence=assertion_evidence,
            asserted_at="2026-08-15T07:00:01Z",
            valid_until="2026-09-15T07:00:01Z",
            **assertion_resolution,
        )
        instances = {
            "context.retrieval-plan": ("schemas/m6-01/retrieval-plan.schema.json", plan),
            "context.retrieval-receipt": ("schemas/m6-01/retrieval-receipt.schema.json", receipt),
            "context.retrieval-benchmark": (
                "schemas/m6-01/retrieval-benchmark.schema.json",
                benchmark_retrieval(samples=10),
            ),
            "context.codegraph-verification-receipt": (
                "schemas/m6-02/codegraph-verification-receipt.schema.json",
                self._codegraph(),
            ),
            "context.recall-receipt": ("schemas/m6-03/recall-receipt.schema.json", self._recall()),
            "context.memory-ablation": (
                "schemas/m6-04/memory-ablation.schema.json",
                benchmark_memory_ablation(samples=100),
            ),
            "context.review-receipt": ("schemas/m6-05/review-receipt.schema.json", self._review()),
            "context.mcp-registry-snapshot": (
                "schemas/m6-06/mcp-registry-snapshot.schema.json",
                snapshot,
            ),
            "context.mcp-admission-decision": (
                "schemas/m6-06/mcp-admission-decision.schema.json",
                decision,
            ),
            "context.assertion-provenance": (
                "schemas/m7-01/assertion-provenance.schema.json",
                assertion,
            ),
        }
        registry = yaml.safe_load((self.root / "schemas/registry.yaml").read_text())
        entries = {entry["schema_id"]: entry for entry in registry["schemas"]}
        for schema_id, (relative_path, instance) in instances.items():
            with self.subTest(schema_id=schema_id):
                path = self.root / relative_path
                self.assertTrue(path.is_file())
                schema = json.loads(path.read_text())
                Draft202012Validator.check_schema(schema)
                Draft202012Validator(schema, format_checker=FormatChecker()).validate(instance)
                self.assertIn(schema_id, entries)
                self.assertEqual(entries[schema_id]["artifact_path"], relative_path)

    def test_schema_semantics_reject_runtime_invalid_recall_and_review_states(self):
        recall_schema = json.loads(
            (self.root / "schemas/m6-03/recall-receipt.schema.json").read_text()
        )
        degraded_with_candidates = copy.deepcopy(self._recall())
        degraded_with_candidates["status"] = "degraded"
        degraded_with_candidates["provider_error_code"] = "503"
        with self.assertRaises(ValidationError):
            Draft202012Validator(
                recall_schema, format_checker=FormatChecker()
            ).validate(degraded_with_candidates)

        review_schema = json.loads(
            (self.root / "schemas/m6-05/review-receipt.schema.json").read_text()
        )
        completed_but_degraded = copy.deepcopy(self._review())
        completed_but_degraded["async_degraded"] = True
        with self.assertRaises(ValidationError):
            Draft202012Validator(
                review_schema, format_checker=FormatChecker()
            ).validate(completed_but_degraded)

    def test_memory_ablation_schema_requires_bounded_receipt_artifact(self):
        schema = json.loads(
            (self.root / "schemas/m6-04/memory-ablation.schema.json").read_text()
        )
        validator = Draft202012Validator(schema, format_checker=FormatChecker())
        valid = benchmark_memory_ablation(samples=100)
        validator.validate(valid)

        mutations = {
            "missing artifact": lambda result: result.pop(
                "recall_receipt_artifact"
            ),
            "missing payload": lambda result: result[
                "recall_receipt_artifact"
            ].pop("payload_base64"),
            "wrong encoding": lambda result: result[
                "recall_receipt_artifact"
            ].__setitem__("encoding", "identity"),
            "wrong artifact ref": lambda result: result[
                "recall_receipt_artifact"
            ].__setitem__("artifact_ref", "artifact://sha256/not-a-digest"),
            "oversized sample count": lambda result: result.__setitem__(
                "samples", 10_010
            ),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                invalid = copy.deepcopy(valid)
                mutate(invalid)
                with self.assertRaises(ValidationError):
                    validator.validate(invalid)

    def test_schema_semantics_reject_runtime_invalid_routes_and_assertions(self):
        plan_schema = json.loads(
            (self.root / "schemas/m6-01/retrieval-plan.schema.json").read_text()
        )
        wrong_route = plan_retrieval(
            {
                "question_id": "question/schema/wrong-route",
                "kind": "exact_text",
                "query": "schema route",
                "repositories": ["context-control-plane"],
                "freshness_required": True,
            },
            available_tools={"rg"},
            max_queries=1,
            max_scanned_bytes=1024,
            max_returned_bytes=256,
            max_index_age_seconds=3600,
        )
        wrong_route["question_kind"] = "official_reference"
        with self.assertRaises(ValidationError):
            Draft202012Validator(plan_schema).validate(wrong_route)

        degraded_mismatch = plan_retrieval(
            {
                "question_id": "question/schema/degraded-route",
                "kind": "large_corpus_text",
                "query": "schema route",
                "repositories": ["context-control-plane"],
                "freshness_required": True,
            },
            available_tools={"rg"},
            max_queries=1,
            max_scanned_bytes=1024,
            max_returned_bytes=256,
            max_index_age_seconds=3600,
        )
        degraded_mismatch["degraded_reasons"] = []
        with self.assertRaises(ValidationError):
            Draft202012Validator(plan_schema).validate(degraded_mismatch)

        codegraph_schema = json.loads(
            (
                self.root
                / "schemas/m6-02/codegraph-verification-receipt.schema.json"
            ).read_text()
        )
        unproved_relation = self._codegraph()
        unproved_relation["clues"][0]["relation"] = "calls"
        with self.assertRaises(ValidationError):
            Draft202012Validator(codegraph_schema).validate(unproved_relation)

        assertion_schema = json.loads(
            (self.root / "schemas/m7-01/assertion-provenance.schema.json").read_text()
        )
        assertion_evidence, assertion_resolution = self._current_code_assertion_evidence()
        bearing_candidate = compose_assertion_provenance(
            assertion_id="assertion/schema/current",
            assertion_text="Current schema evidence.",
            bearing=True,
            evidence=assertion_evidence,
            asserted_at="2026-08-15T07:00:01Z",
            valid_until="2026-09-15T07:00:01Z",
            **assertion_resolution,
        )
        bearing_candidate["evidence"][0]["authority_kind"] = "memory_candidate"
        with self.assertRaises(ValidationError):
            Draft202012Validator(assertion_schema).validate(bearing_candidate)

        nonbearing_inaccurate_coverage = copy.deepcopy(bearing_candidate)
        nonbearing_inaccurate_coverage["bearing"] = False
        nonbearing_inaccurate_coverage["provenance_coverage"] = 0
        with self.assertRaises(ValidationError):
            Draft202012Validator(assertion_schema).validate(
                nonbearing_inaccurate_coverage
            )


if __name__ == "__main__":
    unittest.main()
