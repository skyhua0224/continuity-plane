"""M8-08 forge collaboration adapter behavior tests."""

from __future__ import annotations

import copy
import importlib
import unittest


class M808ForgeCollaborationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        try:
            cls.api = importlib.import_module(
                "context_control_plane.forge_collaboration"
            )
        except ModuleNotFoundError:
            cls.api = None

    def _require_api(self):
        self.assertIsNotNone(
            self.api,
            "context_control_plane.forge_collaboration is missing",
        )
        return self.api

    def test_github_and_gitea_visible_records_project_work_claim_and_evidence(self):
        api = self._require_api()
        github = {
            "provider": "github",
            "instance_id": "github.com",
            "repository": {"owner": "example", "name": "relay"},
            "source_revision": "github-delivery-17",
            "observed_at": "2026-08-16T16:00:00Z",
            "issues": [
                {
                    "number": 12,
                    "title": "Preserve claim fencing",
                    "state": "open",
                    "assignees": [{"login": "alice"}],
                }
            ],
            "pull_requests": [
                {
                    "number": 17,
                    "issue_number": 12,
                    "state": "open",
                    "head": {"ref": "feat/claim-fencing", "sha": "a" * 40},
                    "base": {"ref": "main", "sha": "b" * 40},
                    "assignees": [{"login": "alice"}],
                    "requested_reviewers": [{"login": "bob"}],
                    "reviews": [
                        {
                            "user": {"login": "bob"},
                            "state": "APPROVED",
                            "submitted_at": "2026-08-16T16:01:00Z",
                        }
                    ],
                    "checks": [
                        {
                            "name": "unit",
                            "status": "completed",
                            "conclusion": "success",
                            "details_url": "https://ci.example/runs/17",
                        }
                    ],
                }
            ],
            "refs": {"refs/heads/feat/claim-fencing": "a" * 40},
        }
        gitea = {
            "provider": "gitea",
            "instance_id": "forge.example",
            "repository": {"owner": "example", "name": "relay"},
            "source_revision": "gitea-delivery-17",
            "observed_at": "2026-08-16T16:00:00Z",
            "issues": [
                {
                    "index": 12,
                    "title": "Preserve claim fencing",
                    "state": "open",
                    "assignees": [{"login": "alice"}],
                }
            ],
            "pull_requests": [
                {
                    "index": 17,
                    "issue_index": 12,
                    "state": "open",
                    "head": {"ref": "feat/claim-fencing", "sha": "a" * 40},
                    "base": {"ref": "main", "sha": "b" * 40},
                    "assignees": [{"login": "alice"}],
                    "requested_reviewers": [{"login": "bob"}],
                    "reviews": [
                        {
                            "user": {"login": "bob"},
                            "state": "APPROVED",
                            "submitted_at": "2026-08-16T16:01:00Z",
                        }
                    ],
                    "statuses": [
                        {
                            "context": "unit",
                            "state": "success",
                            "target_url": "https://ci.example/runs/17",
                        }
                    ],
                }
            ],
            "refs": {"refs/heads/feat/claim-fencing": "a" * 40},
        }

        github_projection = api.project_forge_snapshot(github)
        gitea_projection = api.project_forge_snapshot(gitea)

        for projection, provider in (
            (github_projection, "github"),
            (gitea_projection, "gitea"),
        ):
            self.assertEqual(projection["provider"], provider)
            self.assertEqual(projection["authority"]["state_write_authority"], False)
            self.assertEqual(len(projection["works"]), 1)
            work = projection["works"][0]
            self.assertEqual(work["source_kind"], "issue-backed")
            self.assertEqual(work["readiness"], "verifying")
            instance_id = projection["instance_id"]
            self.assertEqual(
                work["candidate_claim"]["actor_refs"],
                [f"actor://{provider}/{instance_id}/alice"],
            )
            self.assertEqual(
                work["candidate_claim"]["claim_uniqueness"], "not-guaranteed"
            )
            self.assertEqual(work["branch_ref"], "refs/heads/feat/claim-fencing")
            self.assertEqual(
                work["reviewer_refs"],
                [f"actor://{provider}/{instance_id}/bob"],
            )
            self.assertEqual(len(work["evidence"]), 2)

    def test_ref_update_requires_current_remote_oid_and_unpublished_work_is_not_unique(self):
        api = self._require_api()
        projection = api.project_forge_snapshot(
            {
                "provider": "github",
                "instance_id": "github.com",
                "repository": {"owner": "example", "name": "relay"},
                "source_revision": "github-delivery-18",
                "observed_at": "2026-08-16T16:10:00Z",
                "issues": [],
                "pull_requests": [],
                "refs": {"refs/heads/main": "a" * 40},
            }
        )

        with self.assertRaisesRegex(api.ForgeConflictError, "remote_ref_mismatch"):
            api.build_ref_update_intent(
                projection,
                branch_ref="refs/heads/main",
                expected_remote_oid="b" * 40,
                desired_oid="c" * 40,
            )

        intent = api.build_ref_update_intent(
            projection,
            branch_ref="refs/heads/main",
            expected_remote_oid="a" * 40,
            desired_oid="c" * 40,
        )
        self.assertEqual(intent["expected_remote_oid"], "a" * 40)
        self.assertEqual(intent["desired_oid"], "c" * 40)
        self.assertEqual(intent["authority"]["remote_effect_authority"], False)

        offline = api.project_unpublished_work(
            {
                "work_id": "local-doc-refresh",
                "actor_ref": "actor://alice",
                "local_ref": "worktree://alice/docs-refresh",
                "observed_at": "2026-08-16T16:10:00Z",
            }
        )
        self.assertEqual(offline["visibility"], "local-only")
        self.assertEqual(offline["claim_uniqueness"], "not-guaranteed")
        self.assertEqual(offline["required_resolution"], "publish-or-state-mcp")

    def test_dual_adapter_replay_requires_matching_projection_digest(self):
        api = self._require_api()
        snapshot = {
            "provider": "gitea",
            "instance_id": "forge.example",
            "repository": {"owner": "example", "name": "relay"},
            "source_revision": "gitea-delivery-19",
            "observed_at": "2026-08-16T16:20:00Z",
            "issues": [
                {
                    "index": 13,
                    "title": "Bind remote replay",
                    "state": "open",
                    "assignees": [],
                }
            ],
            "pull_requests": [],
            "refs": {},
        }
        projection = api.project_forge_snapshot(snapshot)

        replay = api.replay_forge_snapshot(
            copy.deepcopy(snapshot),
            expected_projection_sha256=projection["projection_sha256"],
        )
        self.assertEqual(replay, projection)

        with self.assertRaisesRegex(
            api.ForgeProjectionError, "projection_replay_mismatch"
        ):
            api.replay_forge_snapshot(
                snapshot,
                expected_projection_sha256="0" * 64,
            )

    def test_gitlab_does_not_link_issue_and_merge_request_by_equal_iid(self):
        api = self._require_api()
        projection = api.project_forge_snapshot(
            {
                "provider": "gitlab",
                "instance_id": "gitlab.example",
                "repository": {"owner": "group", "name": "relay"},
                "source_revision": "gitlab-snapshot-1",
                "observed_at": "2026-08-17T00:00:00Z",
                "issues": [
                    {
                        "iid": 1,
                        "title": "Issue one",
                        "state": "open",
                        "assignees": [{"username": "alice"}],
                    }
                ],
                "pull_requests": [
                    {
                        "iid": 1,
                        "state": "open",
                        "head": {"ref": "feat/unrelated"},
                        "assignees": [{"username": "bob"}],
                        "requested_reviewers": [],
                        "reviews": [],
                        "statuses": [],
                    }
                ],
                "refs": {"refs/heads/feat/unrelated": "a" * 40},
            }
        )

        work = projection["works"][0]
        self.assertEqual(work["readiness"], "ready")
        self.assertIsNone(work["branch_ref"])
        self.assertEqual(
            work["candidate_claim"]["actor_refs"],
            ["actor://gitlab/gitlab.example/alice"],
        )

    def test_ref_update_intent_is_bound_to_a_verified_projection_digest(self):
        api = self._require_api()
        projection = api.project_forge_snapshot(
            {
                "provider": "gitea",
                "instance_id": "forge.example",
                "repository": {"owner": "example", "name": "relay"},
                "source_revision": "gitea-snapshot-2",
                "observed_at": "2026-08-17T00:00:00Z",
                "issues": [],
                "pull_requests": [],
                "refs": {"refs/heads/main": "a" * 40},
            }
        )
        intent = api.build_ref_update_intent(
            projection,
            branch_ref="refs/heads/main",
            expected_remote_oid="a" * 40,
            desired_oid="b" * 40,
        )
        self.assertEqual(
            intent["projection_sha256"], projection["projection_sha256"]
        )

        tampered = copy.deepcopy(projection)
        tampered["refs"]["refs/heads/main"] = "c" * 40
        with self.assertRaisesRegex(
            api.ForgeProjectionError, "projection_digest_mismatch"
        ):
            api.build_ref_update_intent(
                tampered,
                branch_ref="refs/heads/main",
                expected_remote_oid="c" * 40,
                desired_oid="d" * 40,
            )

    def test_projection_rejects_a_ref_oid_that_strict_schema_would_reject(self):
        api = self._require_api()
        with self.assertRaisesRegex(api.ForgeProjectionError, "ref_oid_invalid"):
            api.project_forge_snapshot(
                {
                    "provider": "github",
                    "instance_id": "github.com",
                    "repository": {"owner": "example", "name": "relay"},
                    "source_revision": "github-snapshot-invalid-oid",
                    "observed_at": "2026-08-17T00:00:00Z",
                    "issues": [],
                    "pull_requests": [],
                    "refs": {"refs/heads/main": "not-an-oid"},
                }
            )

    def test_projection_rejects_a_timestamp_that_strict_schema_would_reject(self):
        api = self._require_api()
        with self.assertRaisesRegex(api.ForgeProjectionError, "observed_at_invalid"):
            api.project_forge_snapshot(
                {
                    "provider": "github",
                    "instance_id": "github.com",
                    "repository": {"owner": "example", "name": "relay"},
                    "source_revision": "github-snapshot-invalid-time",
                    "observed_at": "yesterday",
                    "issues": [],
                    "pull_requests": [],
                    "refs": {},
                }
            )

    def test_self_hosted_forge_instance_scopes_repository_and_actor_identity(self):
        api = self._require_api()
        snapshot = {
            "provider": "gitea",
            "instance_id": "forge-a.example",
            "repository": {"owner": "group/subgroup", "name": "relay"},
            "source_revision": "snapshot-identity-1",
            "observed_at": "2026-08-17T00:00:00Z",
            "issues": [
                {
                    "index": 1,
                    "title": "Scoped identity",
                    "state": "open",
                    "assignees": [{"login": "alice"}],
                }
            ],
            "pull_requests": [],
            "refs": {},
        }
        other_instance = copy.deepcopy(snapshot)
        other_instance["instance_id"] = "forge-b.example"

        first = api.project_forge_snapshot(snapshot)
        second = api.project_forge_snapshot(other_instance)

        self.assertEqual(first["instance_id"], "forge-a.example")
        self.assertNotEqual(first["repository_ref"], second["repository_ref"])
        self.assertNotEqual(
            first["works"][0]["candidate_claim"]["actor_refs"],
            second["works"][0]["candidate_claim"]["actor_refs"],
        )

    def test_projection_enforces_strict_schema_actor_collection_bound(self):
        api = self._require_api()
        with self.assertRaisesRegex(api.ForgeProjectionError, "assignees_too_many"):
            api.project_forge_snapshot(
                {
                    "provider": "gitea",
                    "instance_id": "forge.example",
                    "repository": {"owner": "example", "name": "relay"},
                    "source_revision": "gitea-snapshot-actor-bound",
                    "observed_at": "2026-08-17T00:00:00Z",
                    "issues": [
                        {
                            "index": 1,
                            "title": "Bound actors",
                            "state": "open",
                            "assignees": [
                                {"login": f"actor-{index}"}
                                for index in range(257)
                            ],
                        }
                    ],
                    "pull_requests": [],
                    "refs": {},
                }
            )

    def test_projection_rejects_multiple_pull_requests_linked_to_one_issue(self):
        api = self._require_api()
        with self.assertRaisesRegex(
            api.ForgeProjectionError, "ambiguous_issue_pull_request"
        ):
            api.project_forge_snapshot(
                {
                    "provider": "github",
                    "instance_id": "github.com",
                    "repository": {"owner": "example", "name": "relay"},
                    "source_revision": "github-snapshot-ambiguous-pr",
                    "observed_at": "2026-08-17T00:00:00Z",
                    "issues": [
                        {
                            "number": 1,
                            "title": "One issue",
                            "state": "open",
                            "assignees": [],
                        }
                    ],
                    "pull_requests": [
                        {
                            "number": 10,
                            "issue_number": 1,
                            "state": "open",
                            "head": {"ref": "feat/a"},
                        },
                        {
                            "number": 11,
                            "issue_number": 1,
                            "state": "open",
                            "head": {"ref": "feat/b"},
                        },
                    ],
                    "refs": {
                        "refs/heads/feat/a": "a" * 40,
                        "refs/heads/feat/b": "b" * 40,
                    },
                }
            )


if __name__ == "__main__":
    unittest.main()
