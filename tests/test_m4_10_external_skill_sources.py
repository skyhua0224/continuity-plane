import copy
import importlib
import json
import tempfile
import time
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

from context_control_plane.artifact_store import ArtifactRef, LocalArtifactStore

SCHEMA_VERSION = "context.external-skill-source-snapshot/v1alpha1"
GENERATED_AT = "2026-08-12T00:00:02Z"
RETRIEVED_AT = "2026-08-12T00:00:01Z"


class RecordingRetriever:
    def __init__(self, observations):
        self.observations = observations
        self.calls = []

    def __call__(self, request, *, max_bytes):
        self.calls.append((request["source_id"], max_bytes))
        return copy.deepcopy(self.observations[request["source_id"]])


class RecordingTreeRetriever:
    def __init__(self, observations):
        self.observations = observations
        self.calls = []

    def __call__(self, request, *, max_bytes):
        self.calls.append((request["source_id"], request["tree_url"], max_bytes))
        return copy.deepcopy(self.observations[request["source_id"]])


class M410ExternalSkillSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(__file__).parents[1]
        cls.schema_path = (
            cls.root
            / "schemas"
            / "m4-10"
            / "external-skill-source-snapshot.schema.json"
        )
        try:
            cls.api = importlib.import_module(
                "context_control_plane.external_skill_sources"
            )
        except ModuleNotFoundError:
            cls.api = None

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = LocalArtifactStore(Path(self.temporary.name) / "artifacts")
        self.store.initialize()

    def tearDown(self):
        self.temporary.cleanup()

    def _require_api(self):
        self.assertIsNotNone(self.api, "external_skill_sources module is missing")
        return self.api

    @staticmethod
    def _request(
        source_id="openai.node-link-and-diagram-layout",
        adapter_id="adapter.openai-plugins-git",
        resource_kind="skill",
        *,
        revision="1" * 40,
        source_path="plugins/build-web-data-visualization/skills/node-link-and-diagram-layout/SKILL.md",
        retrieval_url=None,
    ):
        roots = {
            "adapter.openai-plugins-git": (
                "https://raw.githubusercontent.com/openai/plugins",
                "plugins/build-web-data-visualization/skills/node-link-and-diagram-layout/SKILL.md",
            ),
            "adapter.agent-skills-standard-git": (
                "https://raw.githubusercontent.com/agentskills/agentskills",
                "docs/specification.mdx",
            ),
            "adapter.github-awesome-copilot-git": (
                "https://raw.githubusercontent.com/github/awesome-copilot",
                "skills/agent-supply-chain/SKILL.md",
            ),
            "adapter.skills-sh-index": ("https://skills.sh", "index.json"),
        }
        root, default_path = roots[adapter_id]
        if source_path is None:
            source_path = default_path
        if retrieval_url is None:
            retrieval_url = (
                "https://skills.sh/api/search?q=skills&limit=20"
                if adapter_id == "adapter.skills-sh-index"
                else f"{root}/{revision}/{source_path}"
            )
        return {
            "source_id": source_id,
            "adapter_id": adapter_id,
            "adapter_version": "1.0.0",
            "resource_kind": resource_kind,
            "source_revision": revision,
            "source_path": source_path,
            "retrieval_url": retrieval_url,
        }

    @staticmethod
    def _observation(
        request,
        *,
        license_ref="adapter-default",
        files=None,
        expected_paths=None,
        final_url=None,
        publisher_evidence=b"publisher ownership evidence",
        license_evidence=b"license evidence",
        capabilities=None,
    ):
        if license_ref == "adapter-default":
            license_ref = {
                "adapter.openai-plugins-git": "MIT",
                "adapter.agent-skills-standard-git": "CC-BY-4.0",
                "adapter.github-awesome-copilot-git": "MIT",
                "adapter.skills-sh-index": None,
            }[request["adapter_id"]]
        if files is None:
            files = [
                {
                    "path": request["source_path"],
                    "kind": "file",
                    "content": f"# Snapshot: {request['source_id']}\n".encode(),
                }
            ]
        if expected_paths is None:
            expected_paths = [item["path"] for item in files]
        return {
            "final_url": final_url or request["retrieval_url"],
            "retrieved_at": RETRIEVED_AT,
            "acquisition": "https-get",
            "files": files,
            "expected_paths": expected_paths,
            "license_ref": license_ref,
            "license_evidence": license_evidence,
            "publisher_evidence": publisher_evidence,
            "declared_capabilities": capabilities or ["filesystem:read"],
        }

    def _snapshot(self, requests=None, observations=None, tree_observations=None):
        api = self._require_api()
        if requests is None:
            requests = [self._request()]
        if observations is None:
            observations = {
                request["source_id"]: self._observation(request) for request in requests
            }
        retriever = RecordingRetriever(observations)
        if tree_observations is None:
            tree_observations = {}
            for request in requests:
                if request["resource_kind"] != "skill":
                    continue
                tree_observations[request["source_id"]] = {
                    "final_url": api.external_skill_tree_url(request),
                    "source_revision": request["source_revision"],
                    "truncated": False,
                    "entries": [
                        {"path": path, "kind": "file"}
                        for path in observations[request["source_id"]]["expected_paths"]
                    ],
                }
        tree_retriever = RecordingTreeRetriever(tree_observations)
        retriever.tree_retriever = tree_retriever
        snapshot = api.create_external_skill_source_snapshot(
            requests,
            retriever=retriever,
            tree_retriever=tree_retriever,
            artifact_store=self.store,
            generator_version="1.0.0",
            generated_at=GENERATED_AT,
        )
        return snapshot, retriever

    def _schema_validator(self):
        schema = json.loads(self.schema_path.read_text(encoding="utf-8"))
        return Draft202012Validator(schema, format_checker=FormatChecker())

    @staticmethod
    def _artifact_ref(uri, size_bytes):
        return ArtifactRef.from_uri(uri, size_bytes)

    def _manifest(self, source):
        return {
            "skill_id": "external.openai-node-link-and-diagram-layout",
            "version": "1.0.0",
            "content_sha256": source["content_sha256"],
            "source_kind": "external",
            "license_ref": source["license_ref"],
            "applicability": [
                {"kind": "operation", "ref": "operation://data-visualization"}
            ],
            "rule_ids": ["rule.external.openai-node-link-and-diagram-layout.main"],
            "dependencies": [],
            "conflicts": [],
            "expires_at": None,
            "compatibility": {
                "schema_refs": ["context.compiled-skill-packet/v1alpha1"],
                "provider_contract_refs": ["provider://codex/v1"],
            },
            "provenance_refs": source["provenance_refs"],
            "status": "proposed",
        }

    def test_schema_runtime_and_fixed_adapter_policy_are_present(self):
        api = self._require_api()
        self.assertTrue(self.schema_path.is_file())
        self.assertTrue(hasattr(api, "create_external_skill_source_snapshot"))
        self.assertRegex(api.adapter_policy_sha256(), r"^[0-9a-f]{64}$")

    def test_four_source_classes_snapshot_and_round_trip_deterministically(self):
        api = self._require_api()
        requests = [
            self._request(),
            self._request(
                "agent-skills.specification",
                "adapter.agent-skills-standard-git",
                "standard",
                revision="2" * 40,
                source_path="docs/specification.mdx",
            ),
            self._request(
                "github.agent-supply-chain",
                "adapter.github-awesome-copilot-git",
                revision="3" * 40,
                source_path="skills/agent-supply-chain/SKILL.md",
            ),
            self._request(
                "skills-sh.index",
                "adapter.skills-sh-index",
                "catalog-index",
                revision="dynamic-index",
                source_path="index.json",
            ),
        ]
        observations = {
            request["source_id"]: self._observation(
                request,
                license_ref=(
                    None
                    if request["source_id"] == "skills-sh.index"
                    else "CC-BY-4.0"
                    if request["source_id"] == "agent-skills.specification"
                    else "MIT"
                ),
                license_evidence=(
                    None if request["source_id"] == "skills-sh.index" else b"license"
                ),
            )
            for request in requests
        }
        snapshot, retriever = self._snapshot(list(reversed(requests)), observations)
        api.validate_external_skill_source_snapshot(snapshot, artifact_store=self.store)
        self.assertEqual(len(retriever.calls), 4)
        self.assertEqual(len(retriever.tree_retriever.calls), 2)
        self.assertEqual(snapshot["schema_version"], SCHEMA_VERSION)
        self.assertEqual(snapshot["adapter_policy_sha256"], api.adapter_policy_sha256())
        self.assertEqual(
            {item["source_class"] for item in snapshot["sources"]},
            {"official", "standard", "verified-organization", "marketplace-community"},
        )
        self.assertEqual(list(self._schema_validator().iter_errors(snapshot)), [])
        before_verify_calls = len(retriever.calls)
        api.verify_external_skill_source_snapshot_inputs(
            snapshot,
            requests,
            artifact_store=self.store,
            generator_version="1.0.0",
            expected_generated_at=GENERATED_AT,
        )
        self.assertEqual(len(retriever.calls), before_verify_calls)
        self.assertEqual(len(retriever.tree_retriever.calls), 2)
        self.assertEqual(
            api.canonical_external_skill_source_snapshot_bytes(snapshot),
            api.canonical_external_skill_source_snapshot_bytes(
                api.replay_external_skill_source_snapshot(
                    snapshot,
                    requests,
                    artifact_store=self.store,
                    generator_version="1.0.0",
                    expected_generated_at=GENERATED_AT,
                )
            ),
        )

    def test_source_identity_and_trust_are_derived_from_adapter_policy(self):
        api = self._require_api()
        snapshot, _ = self._snapshot()
        source = snapshot["sources"][0]
        self.assertEqual(source["source_class"], "official")
        self.assertEqual(source["trust_tier"], "official")
        self.assertEqual(source["publisher_id"], "openai")
        self.assertEqual(source["revision_kind"], "git-commit")
        self.assertEqual(source["canonical_url"], "https://github.com/openai/plugins")
        self.assertEqual(source["license_ref"], "MIT")
        for field, value in (
            ("source_class", "marketplace-community"),
            ("trust_tier", "marketplace-community"),
            ("publisher_id", "attacker"),
            ("revision_kind", "dynamic-index"),
            ("adapter_policy_sha256", "f" * 64),
        ):
            changed = copy.deepcopy(snapshot)
            target = (
                changed if field == "adapter_policy_sha256" else changed["sources"][0]
            )
            target[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                api.validate_external_skill_source_snapshot_metadata(changed)

    def test_unknown_adapter_and_caller_declared_identity_are_rejected(self):
        api = self._require_api()
        unknown = self._request()
        unknown_observation = self._observation(unknown)
        unknown["adapter_id"] = "adapter.attacker"
        with self.assertRaises(ValueError):
            self._snapshot([unknown], {unknown["source_id"]: unknown_observation})
        declared = self._request()
        declared["source_class"] = "official"
        with self.assertRaises(ValueError):
            api.create_external_skill_source_snapshot(
                [declared],
                retriever=RecordingRetriever(
                    {declared["source_id"]: self._observation(declared)}
                ),
                artifact_store=self.store,
                generator_version="1.0.0",
                generated_at=GENERATED_AT,
            )

        request = self._request()
        observation = self._observation(request, license_ref="Apache-2.0")
        with self.assertRaises(ValueError):
            self._snapshot([request], {request["source_id"]: observation})

    def test_adapter_version_is_fixed_and_rejected_before_any_retrieval(self):
        api = self._require_api()
        request = self._request()
        request["adapter_version"] = "999.0.0"
        retriever = RecordingRetriever(
            {request["source_id"]: self._observation(request)}
        )
        tree_retriever = RecordingTreeRetriever({})
        with self.assertRaisesRegex(ValueError, "adapter_version"):
            api.create_external_skill_source_snapshot(
                [request],
                retriever=retriever,
                tree_retriever=tree_retriever,
                artifact_store=self.store,
                generator_version="1.0.0",
                generated_at=GENERATED_AT,
            )
        self.assertEqual(retriever.calls, [])
        self.assertEqual(tree_retriever.calls, [])

    def test_policy_is_frozen_before_external_retrieval_callback(self):
        api = self._require_api()
        request = self._request()
        observation = self._observation(request)
        original_publisher = api._POLICY[request["adapter_id"]]["publisher_id"]

        def mutating_retriever(received, *, max_bytes):
            del max_bytes
            api._POLICY[request["adapter_id"]]["publisher_id"] = "attacker"
            return copy.deepcopy(observation)

        try:
            tree_observation = {
                request["source_id"]: {
                    "final_url": api.external_skill_tree_url(request),
                    "source_revision": request["source_revision"],
                    "truncated": False,
                    "entries": [{"path": request["source_path"], "kind": "file"}],
                }
            }
            snapshot = api.create_external_skill_source_snapshot(
                [request],
                retriever=mutating_retriever,
                tree_retriever=RecordingTreeRetriever(tree_observation),
                artifact_store=self.store,
                generator_version="1.0.0",
                generated_at=GENERATED_AT,
            )
        finally:
            api._POLICY[request["adapter_id"]]["publisher_id"] = original_publisher
        self.assertEqual(snapshot["sources"][0]["publisher_id"], "openai")
        api.validate_external_skill_source_snapshot(snapshot, artifact_store=self.store)

    def test_retrieved_bytes_and_evidence_are_written_to_cas_once(self):
        api = self._require_api()
        request = self._request()
        content = b"# exact retrieved bytes\n"
        observation = self._observation(
            request,
            files=[
                {"path": request["source_path"], "kind": "file", "content": content}
            ],
            publisher_evidence=b"publisher-proof",
            license_evidence=b"license-proof",
        )
        snapshot, retriever = self._snapshot(
            [request], {request["source_id"]: observation}
        )
        source = snapshot["sources"][0]
        self.assertEqual(
            retriever.calls, [(request["source_id"], api.MAX_RETRIEVAL_BYTES)]
        )
        self.assertEqual(source["content_size_bytes"], len(content))
        self.assertEqual(
            self.store.read(
                self._artifact_ref(source["content_artifact_ref"], len(content))
            ),
            content,
        )
        self.assertEqual(len(source["publisher_verification_refs"]), 1)
        self.assertEqual(len(source["license_evidence_refs"]), 1)
        self.assertIn(source["content_artifact_ref"], source["provenance_refs"])
        self.assertIn(source["resource_manifest_ref"], source["provenance_refs"])
        api.verify_external_skill_source_snapshot_inputs(
            snapshot,
            [request],
            artifact_store=self.store,
            generator_version="1.0.0",
            expected_generated_at=GENERATED_AT,
        )
        self.assertEqual(len(retriever.calls), 1)

    def test_multi_file_manifest_covers_every_regular_asset(self):
        api = self._require_api()
        request = self._request()
        files = [
            {"path": request["source_path"], "kind": "file", "content": b"# Skill\n"},
            {
                "path": "plugins/build-web-data-visualization/skills/node-link-and-diagram-layout/scripts/layout.py",
                "kind": "file",
                "content": b"print('layout')\n",
            },
            {
                "path": "plugins/build-web-data-visualization/skills/node-link-and-diagram-layout/references/guide.md",
                "kind": "file",
                "content": b"# Guide\n",
            },
        ]
        snapshot, _ = self._snapshot(
            [request],
            {request["source_id"]: self._observation(request, files=files)},
        )
        source = snapshot["sources"][0]
        self.assertEqual(source["resource_count"], 3)
        self.assertRegex(source["resource_tree_sha256"], r"^[0-9a-f]{64}$")
        manifest_ref = self._artifact_ref(
            source["resource_manifest_ref"], source["resource_manifest_size_bytes"]
        )
        manifest = json.loads(self.store.read(manifest_ref))
        self.assertEqual(
            manifest["expected_paths"], sorted(item["path"] for item in files)
        )
        self.assertEqual(manifest["final_url"], request["retrieval_url"])
        self.assertEqual(manifest["retrieved_at"], RETRIEVED_AT)
        self.assertEqual(manifest["license_ref"], "MIT")
        self.assertEqual(
            [item["path"] for item in manifest["resources"]],
            sorted(item["path"] for item in files),
        )
        api.validate_external_skill_source_snapshot(snapshot, artifact_store=self.store)

    def test_independent_pinned_tree_prevents_incomplete_skill_projection(self):
        api = self._require_api()
        request = self._request()
        observation = self._observation(
            request,
            files=[
                {
                    "path": request["source_path"],
                    "kind": "file",
                    "content": b"# Skill\nRun scripts/layout.py\n",
                }
            ],
        )
        tree_observations = {
            request["source_id"]: {
                "final_url": api.external_skill_tree_url(request),
                "source_revision": request["source_revision"],
                "truncated": False,
                "entries": [
                    {"path": request["source_path"], "kind": "file"},
                    {
                        "path": request["source_path"].replace(
                            "SKILL.md", "scripts/layout.py"
                        ),
                        "kind": "file",
                    },
                ],
            }
        }
        snapshot, _ = self._snapshot(
            [request],
            {request["source_id"]: observation},
            tree_observations=tree_observations,
        )
        source = snapshot["sources"][0]
        self.assertEqual(source["status"], "quarantined")
        self.assertIn("resource-missing", source["quarantine_reasons"])
        with self.assertRaisesRegex(ValueError, "eligible"):
            api.project_snapshot_source_to_catalog_entry(
                snapshot,
                source_id=source["source_id"],
                manifest=self._manifest(source),
                artifact_store=self.store,
            )

    def test_conflicting_tree_kinds_fail_before_cas_publication(self):
        api = self._require_api()
        request = self._request()
        observation = self._observation(request)
        tree_observations = {
            request["source_id"]: {
                "final_url": api.external_skill_tree_url(request),
                "source_revision": request["source_revision"],
                "truncated": False,
                "entries": [
                    {"path": request["source_path"], "kind": "file"},
                    {"path": request["source_path"], "kind": "symlink"},
                ],
            }
        }
        before = sorted(path for path in self.store.root.rglob("*") if path.is_file())
        with self.assertRaisesRegex(ValueError, "unique"):
            self._snapshot(
                [request],
                {request["source_id"]: observation},
                tree_observations=tree_observations,
            )
        after = sorted(path for path in self.store.root.rglob("*") if path.is_file())
        self.assertEqual(after, before)

    def test_incomplete_unsafe_or_unlisted_asset_tree_is_quarantined(self):
        cases = {
            "missing-resource": (
                [
                    {
                        "path": self._request()["source_path"],
                        "kind": "file",
                        "content": b"x",
                    }
                ],
                [
                    self._request()["source_path"],
                    self._request()["source_path"].replace(
                        "SKILL.md", "scripts/missing.py"
                    ),
                ],
                "resource-missing",
            ),
            "unlisted-resource": (
                [
                    {
                        "path": self._request()["source_path"],
                        "kind": "file",
                        "content": b"x",
                    },
                    {"path": "scripts/extra.py", "kind": "file", "content": b"x"},
                ],
                [self._request()["source_path"]],
                "resource-unlisted",
            ),
            "symlink": (
                [
                    {
                        "path": self._request()["source_path"],
                        "kind": "symlink",
                        "content": b"target",
                    }
                ],
                [self._request()["source_path"]],
                "unsafe-resource-kind",
            ),
            "submodule": (
                [
                    {
                        "path": self._request()["source_path"],
                        "kind": "submodule",
                        "content": b"commit",
                    }
                ],
                [self._request()["source_path"]],
                "unsafe-resource-kind",
            ),
            "outside-skill-scope": (
                [
                    {
                        "path": self._request()["source_path"],
                        "kind": "file",
                        "content": b"x",
                    },
                    {"path": "README.md", "kind": "file", "content": b"outside"},
                ],
                [self._request()["source_path"], "README.md"],
                "resource-outside-scope",
            ),
        }
        for name, (files, expected_paths, reason) in cases.items():
            request = self._request(source_id=f"openai.{name}")
            observation = self._observation(
                request, files=files, expected_paths=expected_paths
            )
            snapshot, _ = self._snapshot([request], {request["source_id"]: observation})
            source = snapshot["sources"][0]
            with self.subTest(name=name):
                self.assertEqual(source["status"], "quarantined")
                self.assertIn(reason, source["quarantine_reasons"])

    def test_missing_corrupt_or_mismatched_artifact_fails_closed(self):
        api = self._require_api()
        request = self._request()
        snapshot, _ = self._snapshot([request])
        source = snapshot["sources"][0]
        changed = copy.deepcopy(snapshot)
        changed["sources"][0]["content_sha256"] = "f" * 64
        with self.assertRaises(ValueError):
            api.validate_external_skill_source_snapshot(
                changed, artifact_store=self.store
            )

        ref = self._artifact_ref(
            source["content_artifact_ref"], source["content_size_bytes"]
        )
        self.store.object_path(ref).write_bytes(b"corrupt")
        with self.assertRaises(ValueError):
            api.replay_external_skill_source_snapshot(
                snapshot,
                [request],
                artifact_store=self.store,
                generator_version="1.0.0",
                expected_generated_at=GENERATED_AT,
            )

        clean_request = self._request(source_id="openai.missing-manifest")
        clean_snapshot, _ = self._snapshot([clean_request])
        clean_source = clean_snapshot["sources"][0]
        clean_ref = self._artifact_ref(
            clean_source["resource_manifest_ref"],
            clean_source["resource_manifest_size_bytes"],
        )
        self.store.object_path(clean_ref).unlink()
        with self.assertRaises(ValueError):
            api.validate_external_skill_source_snapshot(
                clean_snapshot, artifact_store=self.store
            )

    def test_dynamic_marketplace_and_missing_evidence_are_quarantined(self):
        request = self._request(
            "skills-sh.index",
            "adapter.skills-sh-index",
            "catalog-index",
            revision="dynamic-index",
            source_path="index.json",
        )
        observation = self._observation(
            request,
            license_ref=None,
            license_evidence=None,
            publisher_evidence=None,
        )
        snapshot, _ = self._snapshot([request], {request["source_id"]: observation})
        source = snapshot["sources"][0]
        self.assertEqual(source["status"], "quarantined")
        self.assertEqual(
            set(source["quarantine_reasons"]),
            {"license-unverified", "mutable-source-revision", "publisher-unverified"},
        )

    def test_revision_url_redirect_and_path_traversal_fail_closed(self):
        api = self._require_api()
        preflight_cases = [
            (
                self._request(
                    retrieval_url="https://raw.githubusercontent.com/openai/plugins/main/SKILL.md"
                ),
                "revision URL",
            ),
            (
                self._request(
                    source_path="plugins/build-web-data-visualization/skills/%2e%2e/secrets"
                ),
                "path traversal",
            ),
        ]
        for request, name in preflight_cases:
            retriever = RecordingRetriever(
                {request["source_id"]: self._observation(request)}
            )
            with self.subTest(name=name), self.assertRaises(ValueError):
                self._require_api().create_external_skill_source_snapshot(
                    [request],
                    retriever=retriever,
                    artifact_store=self.store,
                    generator_version="1.0.0",
                    generated_at=GENERATED_AT,
                )
            self.assertEqual(retriever.calls, [])

        for path in (
            "plugins/build-web-data-visualization/skills/node-link-and-diagram-layout//SKILL.md",
            "plugins/build-web-data-visualization/skills/node-link-and-diagram-layout/./SKILL.md",
            "plugins/build-web-data-visualization/skills/node-link-and-diagram-layout/SKILL.md/",
        ):
            request = self._request(source_path=path)
            retriever = RecordingRetriever(
                {request["source_id"]: self._observation(request)}
            )
            with self.subTest(path=path), self.assertRaises(ValueError):
                api.create_external_skill_source_snapshot(
                    [request],
                    retriever=retriever,
                    tree_retriever=RecordingTreeRetriever({}),
                    artifact_store=self.store,
                    generator_version="1.0.0",
                    generated_at=GENERATED_AT,
                )
            self.assertEqual(retriever.calls, [])

        marketplace = self._request(
            "skills-sh.index",
            "adapter.skills-sh-index",
            "catalog-index",
            revision="dynamic-index",
            source_path="index.json",
            retrieval_url="https://skills.sh/unrelated",
        )
        retriever = RecordingRetriever(
            {marketplace["source_id"]: self._observation(marketplace)}
        )
        with self.assertRaises(ValueError):
            self._require_api().create_external_skill_source_snapshot(
                [marketplace],
                retriever=retriever,
                artifact_store=self.store,
                generator_version="1.0.0",
                generated_at=GENERATED_AT,
            )
        self.assertEqual(retriever.calls, [])

        request = self._request()
        observation = self._observation(
            request, final_url="https://attacker.example/payload"
        )
        snapshot, retriever = self._snapshot(
            [request], {request["source_id"]: observation}
        )
        self.assertEqual(len(retriever.calls), 1)
        self.assertEqual(snapshot["sources"][0]["status"], "quarantined")
        self.assertIn(
            "redirect-outside-policy",
            snapshot["sources"][0]["quarantine_reasons"],
        )

    def test_non_marketplace_dynamic_revision_is_rejected(self):
        request = self._request(revision="dynamic-index")
        with self.assertRaises(ValueError):
            self._snapshot(
                [request], {request["source_id"]: self._observation(request)}
            )

    def test_snapshot_mutation_retimestamp_and_request_change_fail_replay(self):
        api = self._require_api()
        request = self._request()
        snapshot, _ = self._snapshot([request])
        mutations = (
            (
                "content",
                lambda item: item["sources"][0].update(content_sha256="f" * 64),
            ),
            (
                "timestamp",
                lambda item: item.update(generated_at="2026-08-12T01:00:00Z"),
            ),
            ("policy", lambda item: item.update(adapter_policy_sha256="f" * 64)),
            (
                "publisher",
                lambda item: item["sources"][0].update(publisher_id="attacker"),
            ),
            ("status", lambda item: item["sources"][0].update(status="quarantined")),
        )
        for name, mutate in mutations:
            changed = copy.deepcopy(snapshot)
            mutate(changed)
            with self.subTest(name=name), self.assertRaises(ValueError):
                api.verify_external_skill_source_snapshot_inputs(
                    changed,
                    [request],
                    artifact_store=self.store,
                    generator_version="1.0.0",
                    expected_generated_at=GENERATED_AT,
                )
        changed_request = copy.deepcopy(request)
        changed_request["source_path"] = request["source_path"].replace(
            "SKILL.md", "other.md"
        )
        with self.assertRaises(ValueError):
            api.verify_external_skill_source_snapshot_inputs(
                snapshot,
                [changed_request],
                artifact_store=self.store,
                generator_version="1.0.0",
                expected_generated_at=GENERATED_AT,
            )

    def test_retrieval_time_cannot_follow_snapshot_generation(self):
        api = self._require_api()
        request = self._request()
        retriever = RecordingRetriever(
            {request["source_id"]: self._observation(request)}
        )
        with self.assertRaises(ValueError):
            api.create_external_skill_source_snapshot(
                [request],
                retriever=retriever,
                artifact_store=self.store,
                generator_version="1.0.0",
                generated_at="2026-08-12T00:00:00Z",
            )

    def test_failed_multi_source_preflight_publishes_no_target_artifacts(self):
        first = self._request()
        second = self._request(
            "github.future",
            "adapter.github-awesome-copilot-git",
            revision="3" * 40,
            source_path="skills/agent-supply-chain/SKILL.md",
        )
        observations = {
            first["source_id"]: self._observation(first),
            second["source_id"]: self._observation(second),
        }
        observations[second["source_id"]]["retrieved_at"] = "2026-08-12T00:00:03Z"
        before = sorted(path for path in self.store.root.rglob("*") if path.is_file())
        with self.assertRaisesRegex(ValueError, "retrieved_at"):
            self._snapshot([first, second], observations)
        after = sorted(path for path in self.store.root.rglob("*") if path.is_file())
        self.assertEqual(after, before)

    def test_metadata_validation_is_distinct_from_cas_admission(self):
        api = self._require_api()
        snapshot, _ = self._snapshot()
        api.validate_external_skill_source_snapshot_metadata(snapshot)
        with self.assertRaisesRegex(ValueError, "artifact_store"):
            api.validate_external_skill_source_snapshot(snapshot)
        for field in ("resource_count", "resource_manifest_size_bytes"):
            changed = copy.deepcopy(snapshot)
            changed["sources"][0][field] = 0
            changed["snapshot_id"] = api.snapshot_identity_for_test(changed)
            with self.subTest(field=field), self.assertRaises(ValueError):
                api.validate_external_skill_source_snapshot_metadata(changed)

    def test_only_immutable_candidate_skill_projects_to_m4_07(self):
        api = self._require_api()
        snapshot, _ = self._snapshot()
        source = snapshot["sources"][0]
        entry = api.project_snapshot_source_to_catalog_entry(
            snapshot,
            source_id=source["source_id"],
            manifest=self._manifest(source),
            artifact_store=self.store,
        )
        self.assertEqual(entry["source_kind"], "external")
        self.assertEqual(entry["status"], "candidate")
        self.assertEqual(entry["approval_refs"], [])
        self.assertEqual(entry["capabilities"], source["declared_capabilities"])
        self.assertTrue(all(value is False for value in entry["permissions"].values()))

        invalid_manifest = self._manifest(source)
        invalid_manifest["license_ref"] = "Apache-2.0"
        with self.assertRaises(ValueError):
            api.project_snapshot_source_to_catalog_entry(
                snapshot,
                source_id=source["source_id"],
                manifest=invalid_manifest,
                artifact_store=self.store,
            )

        standard_request = self._request(
            "agent-skills.specification",
            "adapter.agent-skills-standard-git",
            "standard",
            revision="2" * 40,
            source_path="docs/specification.mdx",
        )
        standard_snapshot, _ = self._snapshot([standard_request])
        with self.assertRaises(ValueError):
            api.project_snapshot_source_to_catalog_entry(
                standard_snapshot,
                source_id=standard_request["source_id"],
                manifest=self._manifest(standard_snapshot["sources"][0]),
                artifact_store=self.store,
            )

        with self.assertRaises(ValueError):
            api.project_snapshot_source_to_catalog_entry(
                snapshot,
                source_id=source["source_id"],
                manifest=self._manifest(source),
            )

    def test_marketplace_metadata_never_propagates_to_child_skill(self):
        api = self._require_api()
        request = self._request(
            "skills-sh.index",
            "adapter.skills-sh-index",
            "catalog-index",
            revision="dynamic-index",
            source_path="index.json",
        )
        observation = self._observation(request)
        observation["marketplace_entries"] = [
            {
                "source": "attacker/repo",
                "official": True,
                "publisher": "openai",
                "license_ref": "MIT",
            }
        ]
        with self.assertRaises(ValueError):
            self._snapshot([request], {request["source_id"]: observation})
        clean_snapshot, _ = self._snapshot(
            [request], {request["source_id"]: self._observation(request)}
        )
        with self.assertRaises(ValueError):
            api.project_snapshot_source_to_catalog_entry(
                clean_snapshot,
                source_id=request["source_id"],
                manifest=self._manifest(clean_snapshot["sources"][0]),
                artifact_store=self.store,
            )

    def test_adapter_resource_kind_and_duplicate_source_identity_are_rejected(self):
        api = self._require_api()
        standard = self._request(
            "agent-skills.specification",
            "adapter.agent-skills-standard-git",
            "skill",
            revision="2" * 40,
            source_path="docs/specification.mdx",
        )
        retriever = RecordingRetriever(
            {standard["source_id"]: self._observation(standard)}
        )
        with self.assertRaises(ValueError):
            api.create_external_skill_source_snapshot(
                [standard],
                retriever=retriever,
                artifact_store=self.store,
                generator_version="1.0.0",
                generated_at=GENERATED_AT,
            )
        self.assertEqual(retriever.calls, [])

        third_party = self._request(
            source_id="openai.superpowers-third-party",
            source_path="plugins/superpowers/skills/test-driven-development/SKILL.md",
        )
        third_party["retrieval_url"] = (
            "https://raw.githubusercontent.com/openai/plugins/"
            + third_party["source_revision"]
            + "/"
            + third_party["source_path"]
        )
        retriever = RecordingRetriever(
            {third_party["source_id"]: self._observation(third_party)}
        )
        with self.assertRaises(ValueError):
            api.create_external_skill_source_snapshot(
                [third_party],
                retriever=retriever,
                artifact_store=self.store,
                generator_version="1.0.0",
                generated_at=GENERATED_AT,
            )
        self.assertEqual(retriever.calls, [])

        first = self._request(source_id="openai.first")
        second = self._request(source_id="openai.second")
        retriever = RecordingRetriever(
            {
                first["source_id"]: self._observation(first),
                second["source_id"]: self._observation(second),
            }
        )
        with self.assertRaises(ValueError):
            api.create_external_skill_source_snapshot(
                [first, second],
                retriever=retriever,
                artifact_store=self.store,
                generator_version="1.0.0",
                generated_at=GENERATED_AT,
            )
        self.assertEqual(retriever.calls, [])

    def test_bounds_reject_before_copy_sort_or_retrieval(self):
        api = self._require_api()

        class OversizedList(list):
            def __deepcopy__(self, memo):
                raise AssertionError("oversized requests reached deepcopy")

        requests = OversizedList([self._request()] * (api.MAX_SOURCES + 1))
        retriever = RecordingRetriever({})
        with self.assertRaises(ValueError):
            api.create_external_skill_source_snapshot(
                requests,
                retriever=retriever,
                artifact_store=self.store,
                generator_version="1.0.0",
                generated_at=GENERATED_AT,
            )
        self.assertEqual(retriever.calls, [])

        oversized = self._request(source_id="x" * 257)
        with self.assertRaises(ValueError):
            api.create_external_skill_source_snapshot(
                [oversized],
                retriever=retriever,
                artifact_store=self.store,
                generator_version="1.0.0",
                generated_at=GENERATED_AT,
            )
        self.assertEqual(retriever.calls, [])

    def test_retrieval_and_resource_list_bounds_fail_closed(self):
        api = self._require_api()
        request = self._request()
        oversized_observation = self._observation(
            request,
            files=[
                {
                    "path": request["source_path"],
                    "kind": "file",
                    "content": b"x" * (api.MAX_RETRIEVAL_BYTES + 1),
                }
            ],
        )
        with self.assertRaises(ValueError):
            self._snapshot([request], {request["source_id"]: oversized_observation})
        too_many = self._observation(
            request,
            files=[
                {"path": f"assets/{index}.txt", "kind": "file", "content": b"x"}
                for index in range(api.MAX_RESOURCES_PER_SOURCE + 1)
            ],
        )
        with self.assertRaises(ValueError):
            self._snapshot([request], {request["source_id"]: too_many})

        combined_oversized = self._observation(
            request,
            files=[
                {
                    "path": request["source_path"],
                    "kind": "file",
                    "content": b"x" * (api.MAX_RETRIEVAL_BYTES // 2 + 1),
                }
            ],
            license_evidence=b"l" * (api.MAX_RETRIEVAL_BYTES // 2),
        )
        with self.assertRaises(ValueError):
            self._snapshot([request], {request["source_id"]: combined_oversized})
        object_files = [
            path
            for path in (self.store.root / "objects" / "sha256").rglob("*")
            if path.is_file()
        ]
        self.assertEqual(object_files, [])

        for observation in (
            self._observation(
                request,
                files=[
                    {"path": request["source_path"], "kind": "file", "content": b"a"},
                    {"path": request["source_path"], "kind": "file", "content": b"b"},
                ],
            ),
            self._observation(request, license_ref="Apache-2.0"),
        ):
            with self.assertRaises(ValueError):
                self._snapshot([request], {request["source_id"]: observation})
            object_files = [
                path
                for path in (self.store.root / "objects" / "sha256").rglob("*")
                if path.is_file()
            ]
            self.assertEqual(object_files, [])

    def test_runtime_and_schema_reject_shape_and_scalar_mutations(self):
        api = self._require_api()
        snapshot, _ = self._snapshot()
        mutations = (
            lambda item: item.update(extra=True),
            lambda item: item.update(snapshot_id="INVALID ID"),
            lambda item: item.update(generated_at="2026-08-12 00:00:00Z"),
            lambda item: item["sources"][0].update(content_size_bytes=True),
            lambda item: item["sources"][0].update(quarantine_reasons=["unknown"]),
            lambda item: item["sources"][0].update(provenance_refs=[]),
            lambda item: item["sources"][0].update(declared_capabilities=["BAD VALUE"]),
        )
        validator = self._schema_validator()
        for index, mutate in enumerate(mutations):
            changed = copy.deepcopy(snapshot)
            mutate(changed)
            with self.subTest(index=index):
                self.assertTrue(list(validator.iter_errors(changed)))
                with self.assertRaises(ValueError):
                    api.validate_external_skill_source_snapshot_metadata(changed)

    def test_versioned_fixture_replays_offline_and_matches_registry_hash(self):
        import yaml

        api = self._require_api()
        fixture_path = (
            self.root
            / "experiments"
            / "skills"
            / "m4-10-external-skill-source-snapshot-v1alpha1.json"
        )
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
        requests = fixture["requests"]
        observations = {}
        for source_id, observation in fixture["observations"].items():
            observations[source_id] = {
                **observation,
                "files": [
                    {**item, "content": item["content"].encode("utf-8")}
                    for item in observation["files"]
                ],
                "license_evidence": (
                    None
                    if observation["license_evidence"] is None
                    else observation["license_evidence"].encode("utf-8")
                ),
                "publisher_evidence": (
                    None
                    if observation["publisher_evidence"] is None
                    else observation["publisher_evidence"].encode("utf-8")
                ),
            }
        snapshot, retriever = self._snapshot(requests, observations)
        self.assertEqual(snapshot, fixture["snapshot"])
        self.assertEqual(len(retriever.calls), 4)
        self.assertEqual(len(retriever.tree_retriever.calls), 2)
        for _ in range(40):
            replayed = api.replay_external_skill_source_snapshot(
                snapshot,
                requests,
                artifact_store=self.store,
                generator_version="1.0.0",
                expected_generated_at=GENERATED_AT,
            )
            self.assertEqual(replayed, snapshot)
        self.assertEqual(len(retriever.calls), 4)
        self.assertEqual(len(retriever.tree_retriever.calls), 2)
        registry = yaml.safe_load(
            (self.root / "schemas" / "registry.yaml").read_text(encoding="utf-8")
        )
        entries = [
            item
            for item in registry["schemas"]
            if item["schema_id"] == "context.external-skill-source-snapshot"
        ]
        self.assertEqual(len(entries), 1)
        self.assertEqual(
            entries[0]["content_sha256"],
            __import__("hashlib").sha256(self.schema_path.read_bytes()).hexdigest(),
        )

    def test_quantified_offline_replay_and_projection_probe(self):
        api = self._require_api()
        fixture = json.loads(
            (
                self.root
                / "experiments"
                / "skills"
                / "m4-10-external-skill-source-snapshot-v1alpha1.json"
            ).read_text(encoding="utf-8")
        )
        requests = fixture["requests"]
        observations = {
            source_id: {
                **observation,
                "files": [
                    {**item, "content": item["content"].encode("utf-8")}
                    for item in observation["files"]
                ],
                "license_evidence": observation["license_evidence"].encode("utf-8")
                if observation["license_evidence"] is not None
                else None,
                "publisher_evidence": observation["publisher_evidence"].encode("utf-8")
                if observation["publisher_evidence"] is not None
                else None,
            }
            for source_id, observation in fixture["observations"].items()
        }
        snapshot, retriever = self._snapshot(requests, observations)
        durations = []
        mismatches = 0
        for _ in range(40):
            started = time.perf_counter_ns()
            replayed = api.replay_external_skill_source_snapshot(
                snapshot,
                requests,
                artifact_store=self.store,
                generator_version="1.0.0",
                expected_generated_at=GENERATED_AT,
            )
            durations.append((time.perf_counter_ns() - started) / 1_000_000)
            mismatches += replayed != snapshot
        p95 = sorted(durations)[37]
        eligible = [
            source
            for source in snapshot["sources"]
            if source["resource_kind"] == "skill" and source["status"] == "candidate"
        ]
        entries = [
            api.project_snapshot_source_to_catalog_entry(
                snapshot,
                source_id=source["source_id"],
                manifest={
                    **self._manifest(source),
                    "skill_id": f"external.{source['source_id'].replace('.', '-')}",
                    "rule_ids": [
                        f"rule.external.{source['source_id'].replace('.', '-')}.main"
                    ],
                },
                artifact_store=self.store,
            )
            for source in eligible
        ]
        self.assertEqual(len(retriever.calls), 4)
        self.assertEqual(len(retriever.tree_retriever.calls), 2)
        self.assertEqual(mismatches, 0)
        self.assertEqual(len(eligible), 2)
        self.assertEqual(len(entries), 2)
        self.assertEqual(
            sum(value for entry in entries for value in entry["permissions"].values()),
            0,
        )
        self.assertLess(p95, 10.0)


if __name__ == "__main__":
    unittest.main()
