import copy
import hashlib
import json
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker, ValidationError

from context_control_plane.retrieval_routing import (
    RetrievalBudgetError,
    RetrievalContractError,
    compose_retrieval_receipt,
    plan_retrieval,
    validate_retrieval_plan,
    validate_retrieval_receipt,
)


class M601RetrievalRoutingTests(unittest.TestCase):
    root = Path(__file__).parents[1]

    @staticmethod
    def reseal(plan: dict) -> None:
        unsigned = copy.deepcopy(plan)
        unsigned.pop("plan_sha256")
        plan["plan_sha256"] = hashlib.sha256(
            json.dumps(
                unsigned,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()

    def question(self, kind: str = "symbol_definition") -> dict:
        return {
            "question_id": "question/m6-01/packet-composer",
            "kind": kind,
            "query": "ExecutionPacket composer",
            "repositories": ["context-control-plane"],
            "freshness_required": True,
        }

    def plan(self, kind: str = "symbol_definition") -> dict:
        return plan_retrieval(
            self.question(kind),
            available_tools={"rg", "zoekt", "lsp", "scip", "rtfm"},
            max_queries=4,
            max_scanned_bytes=32_768,
            max_returned_bytes=2_048,
            max_index_age_seconds=3_600,
        )

    def evidence(self) -> list[dict]:
        return [
            {
                "evidence_id": "evidence/execution-packet-definition",
                "source_kind": "current_code",
                "source_ref": "repo://context-control-plane/context_control_plane/execution_packet.py#L1",
                "revision": "git:cfcf543da0a573c4fdda560038b30bd237fd5510",
                "sha256": "1" * 64,
                "range": {"offset_bytes": 0, "length_bytes": 384},
                "retrieved_at": "2026-08-15T04:00:00Z",
                "valid_at": "2026-08-15T04:00:00Z",
            }
        ]

    @staticmethod
    def reseal_receipt(receipt: dict) -> None:
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

    def step_results(self) -> list[dict]:
        return [
            {
                "tool": "rg",
                "queries": 1,
                "scanned_bytes": 1_000,
                "returned_bytes": 128,
                "index_revision": None,
                "index_sha256": None,
                "index_age_seconds": 0,
            },
            {
                "tool": "lsp",
                "queries": 1,
                "scanned_bytes": 1_000,
                "returned_bytes": 128,
                "index_revision": "git:cfcf543",
                "index_sha256": "2" * 64,
                "index_age_seconds": 10,
            },
        ]

    def receipt(
        self,
        *,
        plan: dict | None = None,
        evidence: list[dict] | None = None,
    ) -> tuple[dict, dict]:
        trusted_plan = plan or self.plan()
        return trusted_plan, compose_retrieval_receipt(
            plan=trusted_plan,
            evidence=evidence or self.evidence(),
            step_results=self.step_results(),
            executed_at="2026-08-15T04:00:01Z",
            cache_status="miss",
            prior_receipt_ref=None,
        )

    def test_routes_question_families_to_minimum_tools(self) -> None:
        expected = {
            "exact_text": ["rg"],
            "large_corpus_text": ["rg", "zoekt"],
            "symbol_definition": ["rg", "lsp"],
            "cross_repository_impact": ["rg", "lsp", "scip"],
            "official_reference": ["rtfm"],
        }
        for kind, tools in expected.items():
            with self.subTest(kind=kind):
                plan = self.plan(kind)
                self.assertEqual([step["tool"] for step in plan["steps"]], tools)
                validate_retrieval_plan(plan)

    def test_falls_back_to_rg_when_zoekt_is_unavailable(self) -> None:
        plan = plan_retrieval(
            self.question("large_corpus_text"),
            available_tools={"rg"},
            max_queries=2,
            max_scanned_bytes=32_768,
            max_returned_bytes=2_048,
            max_index_age_seconds=3_600,
        )
        self.assertEqual([step["tool"] for step in plan["steps"]], ["rg"])
        self.assertEqual(plan["degraded_reasons"], ["zoekt_unavailable"])

    def test_standalone_validator_rejects_resealed_wrong_route(self) -> None:
        plan = self.plan("official_reference")
        plan["steps"] = copy.deepcopy(self.plan("exact_text")["steps"])
        self.reseal(plan)

        with self.assertRaisesRegex(RetrievalContractError, "route"):
            validate_retrieval_plan(plan)

    def test_standalone_validator_binds_degraded_reason_to_route(self) -> None:
        full_plan = self.plan("large_corpus_text")
        full_plan["degraded_reasons"] = ["zoekt_unavailable"]
        self.reseal(full_plan)
        with self.assertRaisesRegex(RetrievalContractError, "degraded"):
            validate_retrieval_plan(full_plan)

        fallback_plan = plan_retrieval(
            self.question("large_corpus_text"),
            available_tools={"rg"},
            max_queries=2,
            max_scanned_bytes=32_768,
            max_returned_bytes=2_048,
            max_index_age_seconds=3_600,
        )
        fallback_plan["degraded_reasons"] = []
        self.reseal(fallback_plan)
        with self.assertRaisesRegex(RetrievalContractError, "degraded"):
            validate_retrieval_plan(fallback_plan)

    def test_rejects_route_when_required_verifier_is_unavailable(self) -> None:
        with self.assertRaisesRegex(RetrievalContractError, "required tool lsp"):
            plan_retrieval(
                self.question("symbol_definition"),
                available_tools={"rg"},
                max_queries=2,
                max_scanned_bytes=32_768,
                max_returned_bytes=2_048,
                max_index_age_seconds=3_600,
            )

    def test_receipt_binds_provenance_range_and_fresh_index(self) -> None:
        plan = self.plan()
        receipt = compose_retrieval_receipt(
            plan=plan,
            evidence=self.evidence(),
            step_results=[
                {
                    "tool": "rg",
                    "queries": 1,
                    "scanned_bytes": 6_000,
                    "returned_bytes": 384,
                    "index_revision": None,
                    "index_sha256": None,
                    "index_age_seconds": 0,
                },
                {
                    "tool": "lsp",
                    "queries": 1,
                    "scanned_bytes": 4_000,
                    "returned_bytes": 256,
                    "index_revision": "git:cfcf543",
                    "index_sha256": "2" * 64,
                    "index_age_seconds": 10,
                },
            ],
            executed_at="2026-08-15T04:00:01Z",
            cache_status="miss",
            prior_receipt_ref=None,
        )
        validate_retrieval_receipt(receipt)
        self.assertEqual(receipt["totals"]["returned_bytes"], 640)
        self.assertEqual(receipt["evidence_count"], 1)
        self.assertFalse(receipt["state_write_authority"])
        self.assertFalse(receipt["memory_authority"])

    def test_rejects_budget_overrun_and_stale_index(self) -> None:
        plan = self.plan()
        over_budget = [
            {
                "tool": "rg",
                "queries": 5,
                "scanned_bytes": 33_000,
                "returned_bytes": 384,
                "index_revision": None,
                "index_sha256": None,
                "index_age_seconds": 0,
            },
            {
                "tool": "lsp",
                "queries": 1,
                "scanned_bytes": 1_000,
                "returned_bytes": 128,
                "index_revision": "git:cfcf543",
                "index_sha256": "2" * 64,
                "index_age_seconds": 10,
            },
        ]
        with self.assertRaises(RetrievalBudgetError):
            compose_retrieval_receipt(
                plan=plan,
                evidence=self.evidence(),
                step_results=over_budget,
                executed_at="2026-08-15T04:00:01Z",
                cache_status="miss",
                prior_receipt_ref=None,
            )

        stale = [
            {
                "tool": "rg",
                "queries": 1,
                "scanned_bytes": 1_000,
                "returned_bytes": 128,
                "index_revision": None,
                "index_sha256": None,
                "index_age_seconds": 0,
            },
            {
                "tool": "lsp",
                "queries": 1,
                "scanned_bytes": 1_000,
                "returned_bytes": 128,
                "index_revision": "git:stale",
                "index_sha256": "2" * 64,
                "index_age_seconds": 3_601,
            },
        ]
        with self.assertRaisesRegex(RetrievalContractError, "stale"):
            compose_retrieval_receipt(
                plan=plan,
                evidence=self.evidence(),
                step_results=stale,
                executed_at="2026-08-15T04:00:01Z",
                cache_status="miss",
                prior_receipt_ref=None,
            )

    def test_cache_hit_requires_prior_receipt_and_cannot_hide_reads(self) -> None:
        plan = self.plan("exact_text")
        with self.assertRaisesRegex(RetrievalContractError, "prior receipt"):
            compose_retrieval_receipt(
                plan=plan,
                evidence=self.evidence(),
                step_results=[],
                executed_at="2026-08-15T04:00:01Z",
                cache_status="hit",
                prior_receipt_ref=None,
            )

    def test_strict_plan_and_receipt_reject_unknown_fields(self) -> None:
        plan = self.plan()
        invalid_plan = copy.deepcopy(plan)
        invalid_plan["authority"] = "state"
        with self.assertRaises(RetrievalContractError):
            validate_retrieval_plan(invalid_plan)

        receipt = compose_retrieval_receipt(
            plan=plan,
            evidence=self.evidence(),
            step_results=[
                {
                    "tool": "rg",
                    "queries": 1,
                    "scanned_bytes": 1_000,
                    "returned_bytes": 128,
                    "index_revision": None,
                    "index_sha256": None,
                    "index_age_seconds": 0,
                },
                {
                    "tool": "lsp",
                    "queries": 1,
                    "scanned_bytes": 1_000,
                    "returned_bytes": 128,
                    "index_revision": "git:cfcf543",
                    "index_sha256": "2" * 64,
                    "index_age_seconds": 10,
                },
            ],
            executed_at="2026-08-15T04:00:01Z",
            cache_status="miss",
            prior_receipt_ref=None,
        )
        receipt["completion"] = True
        with self.assertRaises(RetrievalContractError):
            validate_retrieval_receipt(receipt)

    def test_receipt_validator_rechecks_index_freshness(self) -> None:
        plan = self.plan()
        receipt = compose_retrieval_receipt(
            plan=plan,
            evidence=self.evidence(),
            step_results=[
                {"tool": "rg", "queries": 1, "scanned_bytes": 1000, "returned_bytes": 128, "index_revision": None, "index_sha256": None, "index_age_seconds": 0},
                {"tool": "lsp", "queries": 1, "scanned_bytes": 1000, "returned_bytes": 128, "index_revision": "git:cfcf543", "index_sha256": "2" * 64, "index_age_seconds": 10},
            ],
            executed_at="2026-08-15T04:00:01Z",
            cache_status="miss",
            prior_receipt_ref=None,
        )
        receipt["step_results"][1]["index_age_seconds"] = 3601
        unsigned = copy.deepcopy(receipt)
        unsigned.pop("receipt_sha256")
        receipt["receipt_sha256"] = hashlib.sha256(
            json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        with self.assertRaisesRegex(RetrievalContractError, "stale"):
            validate_retrieval_receipt(receipt)

    def test_receipt_rejects_duplicate_evidence_ids_and_future_evidence(self) -> None:
        _, receipt = self.receipt()
        duplicate = copy.deepcopy(receipt)
        duplicate["evidence"].append(copy.deepcopy(duplicate["evidence"][0]))
        duplicate["evidence_count"] += 1
        self.reseal_receipt(duplicate)
        with self.assertRaisesRegex(RetrievalContractError, "unique"):
            validate_retrieval_receipt(duplicate)

        for field in ("retrieved_at", "valid_at"):
            with self.subTest(field=field):
                future = copy.deepcopy(receipt)
                future["evidence"][0][field] = "2026-08-15T04:00:02Z"
                self.reseal_receipt(future)
                with self.assertRaisesRegex(RetrievalContractError, "executed_at"):
                    validate_retrieval_receipt(future)

    def test_cache_hit_prior_receipt_ref_is_content_addressed(self) -> None:
        plan = self.plan("exact_text")
        prior = compose_retrieval_receipt(
            plan=plan,
            evidence=self.evidence(),
            step_results=[self.step_results()[0]],
            executed_at="2026-08-15T04:00:00Z",
            cache_status="miss",
            prior_receipt_ref=None,
        )
        valid_ref = "receipt://sha256/" + prior["receipt_sha256"]
        receipt = compose_retrieval_receipt(
            plan=plan,
            evidence=self.evidence(),
            step_results=[],
            executed_at="2026-08-15T04:00:01Z",
            cache_status="hit",
            prior_receipt_ref=valid_ref,
            prior_receipt=prior,
        )
        self.assertEqual(receipt["prior_receipt_ref"], valid_ref)
        validate_retrieval_receipt(
            receipt,
            trusted_plan=plan,
            prior_receipt_resolver=lambda ref: prior if ref == valid_ref else None,
        )

        for invalid_ref in ("receipt://missing", "receipt://sha256/" + "A" * 64):
            with self.subTest(prior_receipt_ref=invalid_ref), self.assertRaisesRegex(
                RetrievalContractError, "content-addressed"
            ):
                compose_retrieval_receipt(
                    plan=plan,
                    evidence=self.evidence(),
                    step_results=[],
                    executed_at="2026-08-15T04:00:01Z",
                    cache_status="hit",
                    prior_receipt_ref=invalid_ref,
                    prior_receipt=prior,
                )

    def test_cache_hit_requires_resolved_matching_lineage(self) -> None:
        plan = self.plan("exact_text")
        prior = compose_retrieval_receipt(
            plan=plan,
            evidence=self.evidence(),
            step_results=[self.step_results()[0]],
            executed_at="2026-08-15T04:00:00Z",
            cache_status="miss",
            prior_receipt_ref=None,
        )
        hit = copy.deepcopy(prior)
        hit["executed_at"] = "2026-08-15T04:00:01Z"
        hit["cache_status"] = "hit"
        hit["prior_receipt_ref"] = "receipt://sha256/" + prior["receipt_sha256"]
        hit["step_results"] = []
        hit["totals"] = {"queries": 0, "scanned_bytes": 0, "returned_bytes": 0}
        self.reseal_receipt(hit)

        with self.assertRaisesRegex(RetrievalContractError, "resolver"):
            validate_retrieval_receipt(hit, trusted_plan=plan)
        with self.assertRaisesRegex(RetrievalContractError, "missing"):
            validate_retrieval_receipt(
                hit,
                trusted_plan=plan,
                prior_receipt_resolver=lambda _ref: None,
            )

        wrong = copy.deepcopy(prior)
        wrong["evidence"][0]["sha256"] = "9" * 64
        self.reseal_receipt(wrong)
        with self.assertRaisesRegex(RetrievalContractError, "lineage"):
            validate_retrieval_receipt(
                hit,
                trusted_plan=plan,
                prior_receipt_resolver=lambda _ref: wrong,
            )

    def test_trusted_plan_rechecks_identity_route_budgets_and_index_limit(self) -> None:
        plan, receipt = self.receipt()
        validate_retrieval_receipt(receipt, trusted_plan=plan)
        mutations = (
            lambda value: value.update(plan_id="plan/question/m6-01/replacement"),
            lambda value: value.update(query_sha256="3" * 64),
            lambda value: value.update(max_index_age_seconds=3_599),
            lambda value: value["step_results"][1].update(tool="scip"),
            lambda value: (
                value["step_results"][0].update(queries=3),
                value["totals"].update(queries=4),
            ),
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                invalid = copy.deepcopy(receipt)
                mutation(invalid)
                self.reseal_receipt(invalid)
                with self.assertRaisesRegex(RetrievalContractError, "trusted plan"):
                    validate_retrieval_receipt(invalid, trusted_plan=plan)

    def test_root_recomputes_current_code_hash_and_byte_range(self) -> None:
        path = self.root / "context_control_plane/retrieval_routing.py"
        payload = path.read_bytes()
        needle = b"def plan_retrieval("
        offset = payload.index(needle)
        evidence = [
            {
                "evidence_id": "evidence/current-router",
                "source_kind": "current_code",
                "source_ref": (
                    "repo://context-control-plane/context_control_plane/"
                    f"retrieval_routing.py#bytes={offset}:{len(needle)}"
                ),
                "revision": "worktree:m6-01",
                "sha256": hashlib.sha256(payload).hexdigest(),
                "range": {"offset_bytes": offset, "length_bytes": len(needle)},
                "retrieved_at": "2026-08-15T04:00:00Z",
                "valid_at": "2026-08-15T04:00:00Z",
            }
        ]
        plan, receipt = self.receipt(evidence=evidence)
        validate_retrieval_receipt(receipt, trusted_plan=plan, root=self.root)

        wrong_hash = copy.deepcopy(receipt)
        wrong_hash["evidence"][0]["sha256"] = "0" * 64
        self.reseal_receipt(wrong_hash)
        with self.assertRaisesRegex(RetrievalContractError, "current file hash"):
            validate_retrieval_receipt(wrong_hash, root=self.root)

        bad_range = copy.deepcopy(receipt)
        bad_range["evidence"][0]["source_ref"] = (
            "repo://context-control-plane/context_control_plane/"
            f"retrieval_routing.py#bytes={len(payload)}:1"
        )
        bad_range["evidence"][0]["range"] = {
            "offset_bytes": len(payload),
            "length_bytes": 1,
        }
        self.reseal_receipt(bad_range)
        with self.assertRaisesRegex(RetrievalContractError, "range"):
            validate_retrieval_receipt(bad_range, root=self.root)

    def test_artifact_resolver_recomputes_content_hash_and_range(self) -> None:
        payload = b"bounded artifact evidence"
        digest = hashlib.sha256(payload).hexdigest()
        source_ref = f"artifact://sha256/{digest}"
        evidence = [
            {
                "evidence_id": "evidence/artifact-router",
                "source_kind": "current_index",
                "source_ref": source_ref,
                "revision": "artifact:m6-01",
                "sha256": digest,
                "range": {"offset_bytes": 0, "length_bytes": len(payload)},
                "retrieved_at": "2026-08-15T04:00:00Z",
                "valid_at": "2026-08-15T04:00:00Z",
            }
        ]
        _, receipt = self.receipt(evidence=evidence)
        validate_retrieval_receipt(
            receipt,
            artifact_resolver=lambda ref: payload if ref == source_ref else None,
        )
        with self.assertRaisesRegex(RetrievalContractError, "artifact"):
            validate_retrieval_receipt(
                receipt,
                artifact_resolver=lambda _ref: b"changed",
            )

    def test_receipt_schema_rejects_duplicate_evidence_and_loose_cache_ref(self) -> None:
        schema = json.loads(
            (self.root / "schemas/m6-01/retrieval-receipt.schema.json").read_text()
        )
        _, receipt = self.receipt()
        duplicate = copy.deepcopy(receipt)
        duplicate["evidence"].append(copy.deepcopy(duplicate["evidence"][0]))
        duplicate["evidence_count"] += 1
        with self.assertRaises(ValidationError):
            Draft202012Validator(
                schema, format_checker=FormatChecker()
            ).validate(duplicate)

        hit = copy.deepcopy(receipt)
        hit["cache_status"] = "hit"
        hit["prior_receipt_ref"] = "receipt://loose"
        hit["step_results"] = []
        hit["totals"] = {"queries": 0, "scanned_bytes": 0, "returned_bytes": 0}
        with self.assertRaises(ValidationError):
            Draft202012Validator(schema, format_checker=FormatChecker()).validate(hit)


if __name__ == "__main__":
    unittest.main()
