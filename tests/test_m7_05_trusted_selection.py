from __future__ import annotations

import copy
import hashlib
import importlib
import json
import unittest

from context_control_plane import affected_test_selection as selection
from context_control_plane.verification_profile import build_verification_profile


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _ref(payload: bytes) -> tuple[str, str]:
    digest = hashlib.sha256(payload).hexdigest()
    return f"artifact://sha256/{digest}", digest


def _reseal(document: dict, field: str) -> None:
    body = {key: value for key, value in document.items() if key != field}
    document[field] = hashlib.sha256(_canonical(body)).hexdigest()


class M705TrustedSelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.base_revision = "1" * 40
        self.repository_revision = "2" * 40
        self.now = "2026-08-16T12:00:00Z"
        self.profile = build_verification_profile(
            profile_id="verification/m7-05-trusted/default",
            project_id="m7-05-trusted",
            profile_version="1.0.0-alpha.1",
            revision=1,
            valid_from="2026-08-16T00:00:00Z",
            valid_until=None,
            gates=[
                self._gate("static", "static"),
                self._gate("tdd", "tdd", ["static"]),
                self._gate("build", "build", ["tdd"]),
            ],
        )
        self.graph_source = _canonical(
            {
                "nodes": [
                    self._node("core", "src/core"),
                    self._node("api", "src/api", ["core"]),
                    self._node("cli", "src/cli", ["core"]),
                    self._node("docs", "docs"),
                ]
            }
        )
        self.inventory_source = _canonical(
            {
                "tests": [
                    self._test("static-all", "static", [], always_run=True),
                    self._test("build-all", "build", [], always_run=True),
                    self._test("test-core", "tdd", ["core"]),
                    self._test("test-api", "tdd", ["api"]),
                    self._test("test-cli", "tdd", ["cli"]),
                    self._test("test-docs", "tdd", ["docs"]),
                ]
            }
        )
        self.dynamic_source = _canonical(
            {
                "dynamic_edge_status": "none",
                "unresolved_edge_count": 0,
                "repository_revision": self.repository_revision,
            }
        )
        self.graph_adapter = b"trusted graph adapter v1"
        self.inventory_adapter = b"trusted inventory adapter v1"
        self.graph_config = b"graph config v1"
        self.inventory_config = b"inventory config v1"
        self.diff_source = _canonical(
            {
                "base_repository_revision": self.base_revision,
                "repository_revision": self.repository_revision,
                "changed_paths": ["src/core/runtime.py"],
            }
        )
        payloads = (
            self.graph_source,
            self.inventory_source,
            self.dynamic_source,
            self.graph_adapter,
            self.inventory_adapter,
            self.graph_config,
            self.inventory_config,
            self.diff_source,
        )
        self.artifacts = {_ref(payload)[0]: payload for payload in payloads}
        self.graph_anchor = self._anchor(
            subject_kind="affected-graph",
            adapter_id="adapter/m7-05/graph",
            adapter_version="1.0.0",
            adapter_payload=self.graph_adapter,
            config_payload=self.graph_config,
            source_payload=self.graph_source,
            dynamic_payload=self.dynamic_source,
        )
        self.inventory_anchor = self._anchor(
            subject_kind="required-test-inventory",
            adapter_id="adapter/m7-05/inventory",
            adapter_version="1.0.0",
            adapter_payload=self.inventory_adapter,
            config_payload=self.inventory_config,
            source_payload=self.inventory_source,
            dynamic_payload=None,
        )

    @staticmethod
    def _gate(gate_id: str, kind: str, dependencies: list[str] | None = None) -> dict:
        return {
            "gate_id": gate_id,
            "gate_kind": kind,
            "mode": "required",
            "condition_ref": None,
            "capability_refs": ["capability/local-python"],
            "depends_on_gate_ids": dependencies or [],
            "evidence_requirements": ["artifact-digest"],
            "thresholds": [],
        }

    @staticmethod
    def _node(node_id: str, prefix: str, dependencies: list[str] | None = None) -> dict:
        return {
            "node_id": node_id,
            "path_prefixes": [prefix],
            "depends_on_node_ids": dependencies or [],
        }

    @staticmethod
    def _test(
        test_id: str,
        gate_id: str,
        node_ids: list[str],
        *,
        always_run: bool = False,
    ) -> dict:
        return {
            "test_id": test_id,
            "gate_id": gate_id,
            "node_ids": node_ids,
            "always_run": always_run,
            "estimated_wall_time_ms": 10_000,
            "estimated_input_bytes": 1_000_000,
        }

    @staticmethod
    def _anchor(
        *,
        subject_kind: str,
        adapter_id: str,
        adapter_version: str,
        adapter_payload: bytes,
        config_payload: bytes,
        source_payload: bytes,
        dynamic_payload: bytes | None,
    ) -> dict:
        anchor = {
            "subject_kind": subject_kind,
            "adapter_id": adapter_id,
            "adapter_version": adapter_version,
            "adapter_sha256": _ref(adapter_payload)[1],
            "config_sha256": _ref(config_payload)[1],
            "source_sha256": _ref(source_payload)[1],
            "dynamic_edge_sha256": None,
        }
        if dynamic_payload is not None:
            anchor["dynamic_edge_sha256"] = _ref(dynamic_payload)[1]
        return anchor

    def current_context(self, project_id: str) -> dict:
        self.assertEqual(project_id, "m7-05-trusted")
        return {
            "project_id": project_id,
            "repository_revision": self.repository_revision,
            "profile_id": self.profile["profile_id"],
            "profile_sha256": self.profile["profile_sha256"],
            "derivation_anchors": {
                "affected-graph": self.graph_anchor,
                "required-test-inventory": self.inventory_anchor,
            },
        }

    def change_set_resolver(self, project_id: str, base: str, head: str) -> bytes:
        self.assertEqual(
            (project_id, base, head),
            ("m7-05-trusted", self.base_revision, self.repository_revision),
        )
        return self.diff_source

    @staticmethod
    def derivation_resolver(
        receipt: dict,
        source: bytes,
        config: bytes,
        dynamic: bytes | None,
    ) -> dict:
        del config
        payload = json.loads(source)
        if receipt["subject_kind"] == "affected-graph":
            dynamic_payload = json.loads(dynamic or b"{}")
            return {
                "nodes": payload["nodes"],
                "dynamic_edge_status": dynamic_payload["dynamic_edge_status"],
            }
        return {"tests": payload["tests"]}

    def trusted_inputs(self) -> tuple[dict, dict, dict]:
        change_set = selection.build_affected_change_set(
            change_set_id="change-set/m7-05/trusted",
            project_id="m7-05-trusted",
            base_repository_revision=self.base_revision,
            repository_revision=self.repository_revision,
            generated_at="2026-08-16T11:30:00Z",
            diff_artifact_ref=_ref(self.diff_source)[0],
            diff_artifact_sha256=_ref(self.diff_source)[1],
            artifact_resolver=self.artifacts.get,
            change_set_resolver=self.change_set_resolver,
            current_context_resolver=self.current_context,
        )
        graph_derivation = self.build_derivation(self.graph_anchor)
        inventory_derivation = self.build_derivation(self.inventory_anchor)
        graph = selection.build_affected_graph(
            graph_id="affected-graph/m7-05/trusted",
            profile=self.profile,
            derivation=graph_derivation,
            generated_at="2026-08-16T11:00:00Z",
            valid_until="2026-08-16T13:00:00Z",
            completeness="complete",
            artifact_resolver=self.artifacts.get,
            derivation_resolver=self.derivation_resolver,
            current_context_resolver=self.current_context,
        )
        inventory = selection.build_test_inventory(
            inventory_id="test-inventory/m7-05/trusted",
            profile=self.profile,
            derivation=inventory_derivation,
            generated_at="2026-08-16T11:00:00Z",
            valid_until="2026-08-16T13:00:00Z",
            artifact_resolver=self.artifacts.get,
            derivation_resolver=self.derivation_resolver,
            current_context_resolver=self.current_context,
        )
        return change_set, graph, inventory

    def build_derivation(self, anchor: dict) -> dict:
        adapter_payload = (
            self.graph_adapter
            if anchor["subject_kind"] == "affected-graph"
            else self.inventory_adapter
        )
        config_payload = (
            self.graph_config
            if anchor["subject_kind"] == "affected-graph"
            else self.inventory_config
        )
        source_payload = (
            self.graph_source
            if anchor["subject_kind"] == "affected-graph"
            else self.inventory_source
        )
        return selection.build_derivation_receipt(
            derivation_id=f"derivation/m7-05/{anchor['subject_kind']}",
            subject_kind=anchor["subject_kind"],
            project_id="m7-05-trusted",
            repository_revision=self.repository_revision,
            profile=self.profile,
            generated_at="2026-08-16T11:00:00Z",
            adapter_id=anchor["adapter_id"],
            adapter_version=anchor["adapter_version"],
            adapter_artifact_ref=_ref(adapter_payload)[0],
            adapter_artifact_sha256=_ref(adapter_payload)[1],
            config_artifact_ref=_ref(config_payload)[0],
            config_artifact_sha256=_ref(config_payload)[1],
            source_artifact_ref=_ref(source_payload)[0],
            source_artifact_sha256=_ref(source_payload)[1],
            dynamic_edge_artifact_ref=(
                _ref(self.dynamic_source)[0]
                if anchor["subject_kind"] == "affected-graph"
                else None
            ),
            dynamic_edge_artifact_sha256=(
                _ref(self.dynamic_source)[1]
                if anchor["subject_kind"] == "affected-graph"
                else None
            ),
            artifact_resolver=self.artifacts.get,
            derivation_resolver=self.derivation_resolver,
            current_context_resolver=self.current_context,
        )

    def test_trusted_selection_contract_apis_exist(self) -> None:
        module = importlib.import_module(
            "context_control_plane.affected_test_selection"
        )

        for name in (
            "build_affected_change_set",
            "build_derivation_receipt",
            "validate_affected_change_set",
            "validate_derivation_receipt",
        ):
            with self.subTest(name=name):
                self.assertTrue(callable(getattr(module, name, None)))

    def test_change_set_is_recomputed_from_current_diff(self) -> None:
        try:
            change_set, _, _ = self.trusted_inputs()
        except NotImplementedError:
            self.fail("trusted change-set reconstruction is not implemented")

        self.assertEqual(change_set["changed_paths"], ["src/core/runtime.py"])
        forged = copy.deepcopy(change_set)
        forged["changed_paths"] = ["src/cli/main.py"]
        _reseal(forged, "change_set_sha256")
        with self.assertRaises(selection.AffectedTestSelectionError):
            selection.validate_affected_change_set(
                forged,
                artifact_resolver=self.artifacts.get,
                change_set_resolver=self.change_set_resolver,
                current_context_resolver=self.current_context,
            )

    def test_graph_and_inventory_are_recomputed_from_derivation(self) -> None:
        try:
            change_set, graph, inventory = self.trusted_inputs()
        except NotImplementedError:
            self.fail("trusted graph and inventory derivation is not implemented")
        forged_graph = copy.deepcopy(graph)
        for node in forged_graph["nodes"]:
            node["depends_on_node_ids"] = []
        _reseal(forged_graph, "graph_sha256")

        with self.assertRaises(selection.AffectedTestSelectionError):
            self.select(change_set, forged_graph, inventory)

        forged_inventory = copy.deepcopy(inventory)
        forged_inventory["tests"] = [
            test for test in forged_inventory["tests"] if test["test_id"] != "test-api"
        ]
        _reseal(forged_inventory, "inventory_sha256")
        with self.assertRaises(selection.AffectedTestSelectionError):
            self.select(change_set, graph, forged_inventory)

    def test_receipt_validator_replays_all_trusted_inputs(self) -> None:
        try:
            change_set, graph, inventory = self.trusted_inputs()
        except NotImplementedError:
            self.fail("trusted selection receipt replay is not implemented")
        receipt = self.select(change_set, graph, inventory)
        self.assertEqual(
            receipt["selected_test_ids"],
            ["build-all", "static-all", "test-api", "test-cli", "test-core"],
        )

        forged = copy.deepcopy(receipt)
        forged["selected_test_ids"] = ["build-all", "static-all", "test-core"]
        forged["selected_estimated_wall_time_ms"] = 30_000
        forged["selected_input_bytes"] = 3_000_000
        forged["wall_time_reduction_basis_points"] = 5000
        forged["input_bytes_reduction_basis_points"] = 5000
        _reseal(forged, "receipt_sha256")
        with self.assertRaises(selection.AffectedTestSelectionError):
            selection.validate_affected_test_selection_receipt(
                forged,
                profile=self.profile,
                inventory=inventory,
                graph=graph,
                change_set=change_set,
                artifact_resolver=self.artifacts.get,
                change_set_resolver=self.change_set_resolver,
                derivation_resolver=self.derivation_resolver,
                current_context_resolver=self.current_context,
            )

    def select(self, change_set: dict, graph: dict, inventory: dict) -> dict:
        return selection.select_affected_tests(
            selection_id="selection/m7-05/trusted",
            work_id="M7-05",
            project_revision=58,
            profile=self.profile,
            inventory=inventory,
            graph=graph,
            change_set=change_set,
            evaluated_at=self.now,
            artifact_resolver=self.artifacts.get,
            change_set_resolver=self.change_set_resolver,
            derivation_resolver=self.derivation_resolver,
            current_context_resolver=self.current_context,
        )


if __name__ == "__main__":
    unittest.main()
