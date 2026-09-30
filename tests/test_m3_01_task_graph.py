import copy
import importlib
import json
import statistics
import tempfile
import time
import unittest
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator, ValidationError

from context_control_plane.task_graph import (
    TaskGraphError,
    canonical_task_graph_bytes,
    validate_task_graph_against_state,
    validate_task_graph,
)
from context_control_plane.state_events import (
    StateEventError,
    build_state_event,
    replay_state_events,
)
from context_control_plane.sqlite_state_store import (
    SQLiteStateIntegrityError,
    SQLiteStateStore,
)
from context_control_plane.state_mcp import RequestContext, StateMCPService
from context_control_plane.typed_state import TypedStateError, validate_typed_state


class _AllowAuthorizer:
    def authorize(self, context, action, project_id):
        return True


class M301TaskGraphTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(__file__).parents[1]
        fixture_path = cls.root / "experiments" / "routing" / "m3-01-task-graph.yaml"
        cls.fixture = yaml.safe_load(fixture_path.read_text(encoding="utf-8"))

    def test_schema_is_strict_registered_and_matches_fixture(self):
        registry = yaml.safe_load(
            (self.root / "schemas" / "registry.yaml").read_text(encoding="utf-8")
        )
        entry = next(
            item for item in registry["schemas"] if item["schema_id"] == "context.task-graph"
        )
        schema = json.loads((self.root / entry["artifact_path"]).read_text(encoding="utf-8"))

        self.assertEqual(entry["current_wire_version"], "context.task-graph/v1alpha1")
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(set(schema["required"]), set(schema["properties"]))
        Draft202012Validator(schema).validate(self.fixture)

        empty_roots = copy.deepcopy(self.fixture)
        empty_roots["root_ids"] = []
        with self.assertRaises(ValidationError):
            Draft202012Validator(schema).validate(empty_roots)

        zero_budget = copy.deepcopy(self.fixture)
        next(
            node
            for node in zero_budget["nodes"]
            if node["node_id"] == "experiment-classifier"
        )["attempt_budget"] = 0
        with self.assertRaises(ValidationError):
            Draft202012Validator(schema).validate(zero_budget)

    def test_fault_matrix_and_validator_latency_are_reproducible(self):
        cases = [
            ("parent-cycle", "campaign-m3", "parent_id", "work-router"),
            ("orphan", "work-router", "parent_id", None),
            ("invalid-kind-edge", "experiment-classifier", "parent_id", "campaign-m3"),
            ("missing-return", "experiment-classifier", "return_point_id", None),
            ("missing-promotion", "experiment-classifier", "promotion_target_id", None),
        ]
        rejected = 0
        for _, node_id, field, value in cases:
            broken = copy.deepcopy(self.fixture)
            next(node for node in broken["nodes"] if node["node_id"] == node_id)[field] = value
            with self.assertRaises(TaskGraphError):
                validate_task_graph(broken)
            rejected += 1
        self.assertEqual(rejected, len(cases))

        timings = []
        for _ in range(200):
            started = time.perf_counter_ns()
            validate_task_graph(self.fixture)
            timings.append((time.perf_counter_ns() - started) / 1_000_000)
        ordered = sorted(timings)
        p95 = ordered[int(len(ordered) * 0.95) - 1]
        self.assertLess(p95, 10.0)
        self.assertLess(statistics.median(timings), 10.0)

    def test_canonical_graph_is_deterministic_for_set_like_arrays(self):
        validate_task_graph(self.fixture)
        reordered = copy.deepcopy(self.fixture)
        reordered["nodes"] = list(reversed(reordered["nodes"]))
        reordered["nodes"][0]["dependency_ids"] = list(
            reversed(reordered["nodes"][0]["dependency_ids"])
        )

        self.assertEqual(
            canonical_task_graph_bytes(self.fixture),
            canonical_task_graph_bytes(reordered),
        )

    def test_parent_graph_must_be_acyclic(self):
        broken = copy.deepcopy(self.fixture)
        campaign = next(node for node in broken["nodes"] if node["node_id"] == "campaign-m3")
        campaign["parent_id"] = "work-router"

        with self.assertRaisesRegex(TaskGraphError, "parent cycle"):
            validate_task_graph(broken)

    def test_dependency_graph_must_be_acyclic(self):
        broken = copy.deepcopy(self.fixture)
        work = next(node for node in broken["nodes"] if node["node_id"] == "work-router")
        experiment = next(
            node for node in broken["nodes"] if node["node_id"] == "experiment-classifier"
        )
        work["dependency_ids"] = ["experiment-classifier"]
        experiment["dependency_ids"] = ["work-router"]

        with self.assertRaisesRegex(TaskGraphError, "dependency cycle"):
            validate_task_graph(broken)

    def test_combined_parent_and_dependency_graph_must_be_acyclic(self):
        broken = copy.deepcopy(self.fixture)
        router = next(
            node for node in broken["nodes"] if node["node_id"] == "work-router"
        )
        evidence = next(
            node for node in broken["nodes"] if node["node_id"] == "work-evidence"
        )
        experiment = next(
            node
            for node in broken["nodes"]
            if node["node_id"] == "experiment-classifier"
        )
        router["dependency_ids"] = ["work-evidence"]
        evidence["dependency_ids"] = ["experiment-classifier"]
        experiment["parent_id"] = "work-router"

        with self.assertRaisesRegex(TaskGraphError, "combined.*cycle"):
            validate_task_graph(broken)

    def test_every_node_must_be_reachable_from_a_campaign(self):
        broken = copy.deepcopy(self.fixture)
        work = next(node for node in broken["nodes"] if node["node_id"] == "work-router")
        work["parent_id"] = None

        with self.assertRaisesRegex(TaskGraphError, "orphan"):
            validate_task_graph(broken)

    def test_parent_kind_must_follow_campaign_goal_work_experiment_contract(self):
        broken = copy.deepcopy(self.fixture)
        experiment = next(
            node for node in broken["nodes"] if node["node_id"] == "experiment-classifier"
        )
        experiment["parent_id"] = "campaign-m3"

        with self.assertRaisesRegex(TaskGraphError, "parent kind"):
            validate_task_graph(broken)

    def test_experiment_requires_bounded_return_and_promotion_contract(self):
        for field, value in (
            ("return_point_id", None),
            ("attempt_budget", None),
            ("expiry", None),
            ("exit_criteria", []),
            ("promotion_target_id", None),
        ):
            with self.subTest(field=field):
                broken = copy.deepcopy(self.fixture)
                experiment = next(
                    node
                    for node in broken["nodes"]
                    if node["node_id"] == "experiment-classifier"
                )
                experiment[field] = value
                with self.assertRaisesRegex(TaskGraphError, "experiment"):
                    validate_task_graph(broken)

    def test_experiment_promotion_target_requires_mainline_authority(self):
        broken = copy.deepcopy(self.fixture)
        parent_experiment = next(
            node
            for node in broken["nodes"]
            if node["node_id"] == "experiment-classifier"
        )
        parent_experiment["node_id"] = "experiment-parent"
        parent_experiment["parent_id"] = "work-router"
        parent_experiment["return_point_id"] = "work-router"
        parent_experiment["promotion_target_id"] = "work-router"
        child = copy.deepcopy(parent_experiment)
        child["node_id"] = "experiment-child"
        child["parent_id"] = "experiment-parent"
        child["return_point_id"] = "experiment-parent"
        child["promotion_target_id"] = "experiment-parent"
        broken["nodes"] = [
            node
            for node in broken["nodes"]
            if node["node_id"] != "experiment-parent"
        ] + [parent_experiment, child]

        with self.assertRaisesRegex(TaskGraphError, "promotion target.*mainline"):
            validate_task_graph(broken)

    def test_non_experiment_cannot_claim_experiment_authority(self):
        broken = copy.deepcopy(self.fixture)
        work = next(node for node in broken["nodes"] if node["node_id"] == "work-router")
        work["mainline_authority"] = False

        with self.assertRaisesRegex(TaskGraphError, "non-experiment"):
            validate_task_graph(broken)

    def test_dependency_must_not_point_to_a_descendant(self):
        broken = copy.deepcopy(self.fixture)
        goal = next(node for node in broken["nodes"] if node["node_id"] == "goal-routing")
        goal["dependency_ids"] = ["experiment-classifier"]

        with self.assertRaisesRegex(TaskGraphError, "descendant|combined-edge"):
            validate_task_graph(broken)

    def test_root_ids_must_exactly_project_campaign_roots(self):
        broken = copy.deepcopy(self.fixture)
        broken["root_ids"] = []

        with self.assertRaisesRegex(TaskGraphError, "root_ids"):
            validate_task_graph(broken)

    def test_graph_projection_must_match_authoritative_typed_state(self):
        state = self._typed_state_for_graph_v2()
        validate_typed_state(state)
        validate_task_graph_against_state(self.fixture, state)

        for field, value in (
            ("title", "Tampered graph title"),
            ("kind", "goal"),
            ("parent_id", "work-evidence"),
            ("dependency_ids", ["work-evidence"]),
            ("work_revision", 99),
        ):
            with self.subTest(field=field):
                broken = copy.deepcopy(self.fixture)
                work = next(
                    node for node in broken["nodes"] if node["node_id"] == "work-router"
                )
                work[field] = value
                with self.assertRaisesRegex(TaskGraphError, "typed state"):
                    validate_task_graph_against_state(broken, state)

        for state_field, value in (
            ("return_point_work_id", "goal-routing"),
            ("exit_criteria", ["different gate"]),
            ("attempt_budget", 9),
            ("expires_at", "2026-10-01T00:00:00Z"),
            ("promotion_target_work_id", "goal-routing"),
            ("mainline_authority", True),
        ):
            with self.subTest(state_field=state_field):
                broken_state = copy.deepcopy(state)
                next(
                    work
                    for work in broken_state["works"]
                    if work["work_id"] == "experiment-classifier"
                )[state_field] = value
                with self.assertRaisesRegex(TaskGraphError, "typed state"):
                    validate_task_graph_against_state(self.fixture, broken_state)

    def test_graph_projection_runs_the_full_typed_state_validator(self):
        state = self._typed_state_for_graph_v2()
        state["project"]["primary_work_id"] = "work-evidence"

        with self.assertRaisesRegex(TaskGraphError, "typed state"):
            validate_task_graph_against_state(self.fixture, state)

    def test_v1alpha1_legacy_hierarchy_semantics_remain_readable(self):
        legacy = self._typed_state_for_graph()
        legacy["works"] = [
            work for work in legacy["works"] if work["kind"] in {"campaign", "work"}
        ]
        for work in legacy["works"]:
            work["parent_work_id"] = None
            work["status"] = "ready"
        legacy["project"]["active_work_ids"] = ["campaign-m3"]
        legacy["project"]["primary_work_id"] = "campaign-m3"
        next(work for work in legacy["works"] if work["work_id"] == "campaign-m3")[
            "status"
        ] = "active"
        legacy["project"]["active_work_ids"] = ["campaign-m3"]
        legacy["claims"][0]["work_id"] = "campaign-m3"
        legacy["claims"][0]["scope_owners"] = copy.deepcopy(
            next(work for work in legacy["works"] if work["work_id"] == "campaign-m3")[
                "scope_refs"
            ]
        )
        legacy["ideas"][0]["parent_work_id"] = "campaign-m3"
        legacy["ideas"][0]["return_work_id"] = "campaign-m3"
        legacy["decisions"][0]["work_id"] = "campaign-m3"
        legacy["constraints"][0]["scope_work_ids"] = ["campaign-m3"]
        legacy["effects"][0]["work_id"] = "campaign-m3"
        legacy["effects"][0]["scope_ref"] = copy.deepcopy(
            legacy["claims"][0]["scope_owners"][0]
        )

        validate_typed_state(legacy)

    def test_v2alpha1_migration_is_replayable_idempotent_and_reversible(self):
        module = importlib.import_module("context_control_plane.typed_state_migration")
        migrate = getattr(module, "migrate_v1alpha1_to_v2alpha1")
        rollback = getattr(module, "rollback_v2alpha1_to_v1alpha1")
        legacy = self._typed_state_for_graph()
        contracts = {
            "experiment-classifier": {
                "return_point_work_id": "work-router",
                "exit_criteria": ["classification matrix passes"],
                "attempt_budget": 3,
                "expires_at": "2026-09-01T00:00:00Z",
                "promotion_target_work_id": "work-router",
                "mainline_authority": False,
            }
        }

        migrated = migrate(legacy, experiment_contracts=contracts)
        validate_typed_state(migrated)
        self.assertEqual(migrate(migrated, experiment_contracts=contracts), migrated)
        self.assertEqual(rollback(migrated), legacy)

    def test_v2alpha1_experiment_contract_is_authoritative(self):
        state = self._typed_state_for_graph_v2()
        experiment = next(
            work
            for work in state["works"]
            if work["work_id"] == "experiment-classifier"
        )
        for field, value in (
            ("return_point_work_id", None),
            ("exit_criteria", []),
            ("attempt_budget", 0),
            ("expires_at", None),
            ("promotion_target_work_id", None),
            ("mainline_authority", True),
        ):
            with self.subTest(field=field):
                broken = copy.deepcopy(state)
                next(
                    work
                    for work in broken["works"]
                    if work["work_id"] == "experiment-classifier"
                )[field] = value
                with self.assertRaisesRegex(TypedStateError, "experiment"):
                    validate_typed_state(broken)

        self.assertFalse(experiment["mainline_authority"])

    def test_v2alpha1_experiment_return_and_promotion_are_ancestor_bound(self):
        state = self._typed_state_for_graph_v2()
        experiment = next(
            work
            for work in state["works"]
            if work["work_id"] == "experiment-classifier"
        )
        for field in ("return_point_work_id", "promotion_target_work_id"):
            with self.subTest(field=field):
                broken = copy.deepcopy(state)
                next(
                    work
                    for work in broken["works"]
                    if work["work_id"] == experiment["work_id"]
                )[field] = "work-evidence"
                with self.assertRaisesRegex(TypedStateError, "experiment.*ancestor"):
                    validate_typed_state(broken)

    def test_combined_parent_and_dependency_cycle_is_rejected_by_typed_state(self):
        state = self._typed_state_for_graph_v2()
        router = next(item for item in state["works"] if item["work_id"] == "work-router")
        evidence = next(item for item in state["works"] if item["work_id"] == "work-evidence")
        router["dependency_ids"] = ["work-evidence"]
        evidence["dependency_ids"] = ["experiment-classifier"]

        with self.assertRaisesRegex(TypedStateError, "combined.*cycle"):
            validate_typed_state(state)

    def test_container_nodes_cannot_be_active_claimed_leaves(self):
        state = self._typed_state_for_graph_v2()
        state["project"]["active_work_ids"] = ["goal-routing"]
        state["project"]["primary_work_id"] = "goal-routing"
        next(item for item in state["works"] if item["work_id"] == "work-router")[
            "status"
        ] = "ready"
        next(item for item in state["works"] if item["work_id"] == "goal-routing")[
            "status"
        ] = "active"
        state["claims"][0]["work_id"] = "goal-routing"
        state["claims"][0]["scope_owners"] = copy.deepcopy(
            state["works"][1]["scope_refs"]
        )

        with self.assertRaisesRegex(TypedStateError, "executable leaf"):
            validate_typed_state(state)

    def test_one_thousand_node_graph_is_iterative_and_bounded(self):
        graph = {
            "schema_version": "context.task-graph/v1alpha1",
            "project_id": "project-large",
            "state_revision": 1,
            "root_ids": ["campaign-0"],
            "nodes": [],
        }
        for index in range(1_000):
            graph["nodes"].append(
                {
                    "node_id": "campaign-0" if index == 0 else f"goal-{index}",
                    "work_revision": 1,
                    "kind": "campaign" if index == 0 else "goal",
                    "title": f"Node {index}",
                    "parent_id": None if index == 0 else (
                        "campaign-0" if index == 1 else f"goal-{index - 1}"
                    ),
                    "dependency_ids": [],
                    "return_point_id": None,
                    "exit_criteria": [],
                    "attempt_budget": None,
                    "expiry": None,
                    "promotion_target_id": None,
                    "mainline_authority": True,
                }
            )

        started = time.perf_counter()
        validate_task_graph(graph)
        elapsed_ms = (time.perf_counter() - started) * 1_000

        self.assertLess(elapsed_ms, 100.0)

    def test_graph_change_requires_monotonic_work_revision(self):
        initial = self._typed_state_for_graph()
        self._make_graph_inactive(initial)
        after = copy.deepcopy(initial)
        after["project"]["revision"] += 1
        after["project"]["updated_at"] = "2026-08-13T13:00:00+08:00"
        changed_work = next(
            item for item in after["works"] if item["work_id"] == "work-evidence"
        )
        changed_work["parent_work_id"] = "work-router"
        event = build_state_event(
            event_id="event-reparent-without-work-revision",
            event_type="state-transition",
            project_id=after["project"]["project_id"],
            sequence_no=1,
            revision_before=initial["project"]["revision"],
            occurred_at=after["project"]["updated_at"],
            actor_ref="actor-owner",
            causation_ref="work:M3-01",
            correlation_ref="campaign:M3",
            previous_event_sha256=None,
            supersedes_event_id=None,
            changes=[
                {
                    "collection": "works",
                    "object_id": changed_work["work_id"],
                    "value": changed_work,
                }
            ],
            project_after=after["project"],
        )

        with self.assertRaisesRegex(StateEventError, "Work revision"):
            replay_state_events(initial, [event])

    def test_invalid_graph_event_is_atomic_for_the_sqlite_store(self):
        initial = self._typed_state_for_graph_v2()
        self._make_graph_inactive(initial)
        after = copy.deepcopy(initial)
        after["project"]["revision"] += 1
        after["project"]["updated_at"] = "2026-08-13T13:01:00+08:00"
        campaign = next(
            item for item in after["works"] if item["work_id"] == "campaign-m3"
        )
        campaign["parent_work_id"] = "work-router"
        campaign["revision"] += 1
        event = build_state_event(
            event_id="event-illegal-parent-cycle",
            event_type="state-transition",
            project_id=after["project"]["project_id"],
            sequence_no=1,
            revision_before=initial["project"]["revision"],
            occurred_at=after["project"]["updated_at"],
            actor_ref="actor-owner",
            causation_ref="work:M3-01",
            correlation_ref="campaign:M3",
            previous_event_sha256=None,
            supersedes_event_id=None,
            changes=[
                {
                    "collection": "works",
                    "object_id": campaign["work_id"],
                    "value": campaign,
                }
            ],
            project_after=after["project"],
        )

        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStateStore(Path(directory) / "m3-01.sqlite3")
            store.initialize()
            store.create_project(initial)
            before = store.read_project(initial["project"]["project_id"])

            with self.assertRaisesRegex(
                SQLiteStateIntegrityError, "typed state validation|replay"
            ):
                store.commit_event(
                    project_id=initial["project"]["project_id"],
                    expected_revision=initial["project"]["revision"],
                    event=event,
                    expected_snapshot=after,
                )

            self.assertEqual(store.read_project(initial["project"]["project_id"]), before)
            self.assertEqual(store.read_events(initial["project"]["project_id"]), [])

    def test_invalid_graph_commit_is_atomic_through_state_mcp(self):
        initial = self._typed_state_for_graph_v2()
        self._make_graph_inactive(initial)
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStateStore(Path(directory) / "m3-01-mcp.sqlite3")
            store.initialize()
            store.create_project(initial)
            service = StateMCPService(
                store,
                authorizer=_AllowAuthorizer(),
                registry_digest="a" * 64,
                clock=lambda: "2026-08-13T13:02:00+08:00",
                event_id_factory=lambda request_id: f"event-{request_id}",
            )
            campaign = copy.deepcopy(
                next(
                    item for item in initial["works"] if item["work_id"] == "campaign-m3"
                )
            )
            campaign["parent_work_id"] = "work-router"
            campaign["revision"] += 1
            request = {
                "schema_version": "context.state-mcp-request/v1alpha1",
                "request_id": "m3-01-illegal-graph",
                "project_id": initial["project"]["project_id"],
                "expected_revision": initial["project"]["revision"],
                "causation_ref": "work:M3-01",
                "correlation_ref": "campaign:M3",
                "supersedes_event_id": None,
                "changes": [
                    {
                        "collection": "works",
                        "object_id": campaign["work_id"],
                        "value": campaign,
                    }
                ],
            }

            response = service.call_tool(
                "context.state.commit",
                request,
                context=RequestContext("actor-owner", "m3-01-test"),
            )

            self.assertFalse(response["ok"])
            self.assertEqual(store.read_project(initial["project"]["project_id"]), initial)
            self.assertEqual(store.read_events(initial["project"]["project_id"]), [])

    def test_v2alpha1_state_mcp_commit_persists_and_replays_in_sqlite(self):
        initial = self._typed_state_for_graph_v2()
        self._make_graph_inactive(initial)
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStateStore(Path(directory) / "m3-01-v2.sqlite3")
            store.initialize()
            store.create_project(initial)
            service = StateMCPService(
                store,
                authorizer=_AllowAuthorizer(),
                registry_digest="b" * 64,
                clock=lambda: "2026-08-13T13:03:00+08:00",
                event_id_factory=lambda request_id: f"event-{request_id}",
            )
            evidence = {
                "evidence_id": "evidence-v2-commit",
                "kind": "test",
                "artifact_ref": "artifact://sha256/" + "c" * 64,
                "content_sha256": "c" * 64,
                "validity": "verified",
                "observed_at": "2026-08-13T13:03:00+08:00",
                "verified_at": "2026-08-13T13:03:00+08:00",
            }
            request = {
                "schema_version": "context.state-mcp-request/v1alpha1",
                "request_id": "m3-01-v2-commit",
                "project_id": initial["project"]["project_id"],
                "expected_revision": initial["project"]["revision"],
                "causation_ref": "work:M3-01",
                "correlation_ref": "campaign:M3",
                "supersedes_event_id": None,
                "changes": [
                    {
                        "collection": "evidence",
                        "object_id": evidence["evidence_id"],
                        "value": evidence,
                    }
                ],
            }

            response = service.call_tool(
                "context.state.commit",
                request,
                context=RequestContext("actor-owner", "m3-01-test"),
            )

            self.assertTrue(response["ok"], response)
            stored = store.read_project(initial["project"]["project_id"])
            events = store.read_events(initial["project"]["project_id"])
            self.assertEqual(stored, response["result"]["snapshot"])
            self.assertEqual(events[0]["schema_version"], "context.state-event/v2alpha1")
            self.assertEqual(replay_state_events(initial, events), stored)

    @staticmethod
    def _make_graph_inactive(state):
        state["project"]["active_work_ids"] = []
        state["project"]["primary_work_id"] = None
        next(item for item in state["works"] if item["work_id"] == "work-router")[
            "status"
        ] = "ready"
        state["claims"] = []
        state["effects"] = []

    @classmethod
    def _typed_state_for_graph(cls):
        fixture_set = yaml.safe_load(
            (cls.root / "experiments" / "state" / "m2-01-core-fixtures.yaml").read_text(
                encoding="utf-8"
            )
        )
        state = copy.deepcopy(fixture_set["cases"][0]["document"])
        state["project"].update(
            {
                "project_id": cls.fixture["project_id"],
                "revision": cls.fixture["state_revision"],
                "active_work_ids": ["work-router"],
                "primary_work_id": "work-router",
            }
        )
        state["works"] = []
        for graph_node in cls.fixture["nodes"]:
            state["works"].append(
                {
                    "work_id": graph_node["node_id"],
                    "kind": graph_node["kind"],
                    "title": graph_node["title"],
                    "status": "active" if graph_node["node_id"] == "work-router" else "ready",
                    "parent_work_id": graph_node["parent_id"],
                    "dependency_ids": copy.deepcopy(graph_node["dependency_ids"]),
                    "owner_refs": ["actor-owner"],
                    "scope_refs": [
                        {
                            "scope_kind": "capability",
                            "scope_ref": f"routing/{graph_node['node_id']}",
                        }
                    ],
                    "overlap_candidate_ids": [],
                    "dedupe_status": "clear",
                    "supersedes_work_id": None,
                    "evidence_ids": [],
                    "blocker_ids": [],
                    "revision": graph_node["work_revision"],
                }
            )
        state["claims"][0].update(
            {
                "work_id": "work-router",
                "expected_project_revision": cls.fixture["state_revision"],
                "scope_owners": [
                    {"scope_kind": "capability", "scope_ref": "routing/work-router"}
                ],
            }
        )
        state["ideas"][0]["parent_work_id"] = "work-router"
        state["ideas"][0]["return_work_id"] = "work-router"
        state["decisions"][0]["work_id"] = "work-router"
        state["constraints"][0]["scope_work_ids"] = ["work-router"]
        state["effects"][0]["work_id"] = "work-router"
        state["effects"][0]["expected_project_revision"] = cls.fixture["state_revision"]
        state["effects"][0]["scope_ref"] = {
            "scope_kind": "capability",
            "scope_ref": "routing/work-router",
        }
        return state

    @classmethod
    def _typed_state_for_graph_v2(cls):
        state = cls._typed_state_for_graph()
        state["schema_version"] = "context.typed-state/v2alpha1"
        graph_by_id = {node["node_id"]: node for node in cls.fixture["nodes"]}
        for work in state["works"]:
            node = graph_by_id[work["work_id"]]
            work.update(
                {
                    "return_point_work_id": node["return_point_id"],
                    "exit_criteria": copy.deepcopy(node["exit_criteria"]),
                    "attempt_budget": node["attempt_budget"],
                    "expires_at": node["expiry"],
                    "promotion_target_work_id": node["promotion_target_id"],
                    "mainline_authority": node["mainline_authority"],
                }
            )
        return state


if __name__ == "__main__":
    unittest.main()
