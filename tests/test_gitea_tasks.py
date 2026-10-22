"""Two-account, two-local-root Gitea contracts; all data and credentials synthetic."""

from __future__ import annotations

import copy
from contextlib import redirect_stdout
from datetime import UTC, datetime, timedelta
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from continuity_plane.cli import main
from continuity_plane.collaboration_inbox import (
    acknowledge,
    archive,
    list_messages,
    send_message,
)
from continuity_plane.collaboration_registry import add_task, register_project
from continuity_plane.gitea_cli import backend_for
from continuity_plane.gitea_tasks import (
    EVENT_MARKER,
    TASK_MARKER,
    GiteaTasks,
    decode,
    encode,
)
from continuity_plane.gitea_transport import (
    GiteaClient,
    GiteaError,
    GiteaUnavailable,
    validate_config,
)
from continuity_plane.planning_api import (
    execute,
    handle_tool,
    help_for,
    tool_definitions,
)
from continuity_plane.retry_policy import classify


CONFIG = {
    "backend": "gitea-issues",
    "instance": "https://forge.example.test:3443/team",
    "repository": "team/sample",
    "credential_ref": "tea:member",
}


class Forge:
    """Minimal instance contract, not a deployed Gitea acceptance claim."""

    def __init__(self):
        self.issues, self.comments, self.prs, self.reviews, self.writes = (
            {},
            {},
            {},
            {},
            [],
        )
        self.lock = threading.RLock()
        self.barrier = None
        self.offline = False
        self.fail_after_write = False

    def client(self, account):
        forge = self

        class Client:
            repo = "/repos/team/sample"

            def identity(self):
                return self.request("GET", "/user")["login"]

            def doctor(self):
                return {"status": "connected", "account": self.identity()}

            def all(self, path, query=None):
                return self.request("GET", path, query=query)

            def request(self, method, path, payload=None, query=None):
                if forge.offline:
                    raise GiteaUnavailable("fixture disconnected")
                if (
                    forge.barrier is not None
                    and method == "POST"
                    and path.endswith("/comments")
                ):
                    forge.barrier.wait(timeout=10)
                with forge.lock:
                    if method != "GET":
                        forge.writes.append(
                            (account, method, path, copy.deepcopy(payload))
                        )
                    tail = path.removeprefix(self.repo)
                    if path == "/user":
                        result = {"login": account}
                    elif tail == "/issues":
                        if method == "GET":
                            result = list(forge.issues.values())
                        else:
                            n = len(forge.issues) + 1
                            result = {
                                "number": n,
                                "id": n,
                                "title": payload["title"],
                                "body": payload["body"],
                                "html_url": f"https://forge.example.test/team/sample/issues/{n}",
                                "state": "open",
                                "user": {"login": account},
                                "assignees": [],
                                "updated_at": "2026-10-19T00:00:00Z",
                            }
                            forge.issues[n] = result
                            forge.comments[n] = []
                    elif tail.startswith("/issues/"):
                        n = int(tail.split("/")[2])
                        if tail.endswith("/comments"):
                            if method == "GET":
                                result = forge.comments[n]
                            else:
                                result = {
                                    "id": 1
                                    + sum(len(v) for v in forge.comments.values()),
                                    "body": payload["body"],
                                    "user": {"login": account},
                                    "updated_at": "2026-10-19T00:00:01Z",
                                }
                                forge.comments[n].append(result)
                        else:
                            result = forge.issues[n]
                    elif tail.startswith("/pulls/"):
                        n = int(tail.split("/")[2])
                        if tail.endswith("/reviews"):
                            result = forge.reviews.get(n, [])
                        elif tail.endswith("/requested_reviewers"):
                            forge.prs[n]["requested_reviewers"] = [
                                {"login": r} for r in payload["reviewers"]
                            ]
                            result = None
                        else:
                            result = forge.prs[n]
                    else:
                        raise AssertionError((method, path))
                    if method == "POST" and forge.fail_after_write:
                        forge.fail_after_write = False
                        raise GiteaUnavailable("synthetic response lost after commit")
                    return copy.deepcopy(result)

        return Client()


class GiteaTaskTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.forge = Forge()
        self.a = GiteaTasks(
            CONFIG, self.folder / "member-a", client=self.forge.client("alice")
        )
        self.b = GiteaTasks(
            CONFIG, self.folder / "member-b", client=self.forge.client("bob")
        )
        self.fields = {
            "objective": "Implement sample",
            "next_action": "Test",
            "exit_criteria": ["Tests pass"],
            "priority": "p1",
            "body": "Repository instructions",
            "lane_id": "scope",
            "mode": "patch",
        }

    def create(self):
        return self.a.create("sample", "Shared work", self.fields, "create-1")

    def revision(self, backend=None):
        return (backend or self.a).show("sample")["remote_revision"]

    def change(self, backend, kind, fields=None, op=None, **options):
        return backend.mutate(
            "sample", kind, fields or {}, self.revision(backend), op or kind, **options
        )

    def pr(self):
        self.forge.prs[10] = {
            "number": 10,
            "state": "open",
            "merged": False,
            "merge_commit_sha": None,
            "head": {"sha": "a" * 40},
            "base": {"ref": "main"},
            "requested_reviewers": [],
        }

    def test_shared_creation_and_actual_claim_identity(self):
        self.create()
        self.assertEqual(self.b.show("sample")["created_by"], "alice")
        result = self.change(self.b, "claim")
        self.assertFalse(result["exclusive"])
        self.assertEqual(self.a.show("sample")["assignee"], "bob")
        self.assertEqual(self.a.show("sample")["status"], "active")
        self.assertFalse((self.folder / "member-a" / "dispatch-ledger.json").exists())

    def test_handoff_request_is_not_acceptance(self):
        self.create()
        self.change(self.a, "claim")
        self.change(self.a, "handoff-request", target="bob", note="Please continue")
        self.assertEqual(self.b.show("sample")["assignee"], "alice")
        with self.assertRaisesRegex(GiteaError, "only requested"):
            self.change(self.a, "handoff-accept")
        self.change(self.b, "handoff-accept", op="accept-b")
        self.assertEqual(self.a.show("sample")["assignee"], "bob")

    def test_cannot_impersonate_or_release_other_member(self):
        self.create()
        self.change(self.a, "claim")
        for kind, fields in (
            ("claim", {}),
            ("release", {}),
            ("update", {"assignee": "bob"}),
        ):
            with self.assertRaises(GiteaError):
                self.change(self.b, kind, fields)

    def test_web_body_and_unrelated_changes_are_preserved(self):
        self.create()
        issue = self.forge.issues[1]
        block = decode(issue["body"], TASK_MARKER)
        block["next_action"] = "Human's next action"
        issue["body"] = encode(TASK_MARKER, block, "Human-added text must survive")
        issue["title"] = "Human title"
        issue["labels"] = [{"id": 2, "name": "human-label"}]
        self.change(self.b, "update", {"branch": "feat/sample"})
        current = self.b.show("sample")
        self.assertEqual(current["next_action"], "Human's next action")
        self.assertEqual(current["title"], "Human title")
        self.assertIn("Human-added text", self.forge.issues[1]["body"])
        self.assertTrue(all(method == "POST" for _, method, _, _ in self.forge.writes))

    def test_stale_revision_rejected_before_write(self):
        self.create()
        revision = self.revision()
        self.forge.issues[1]["title"] = "changed on web"
        with self.assertRaisesRegex(GiteaError, "revision changed"):
            self.b.mutate("sample", "claim", {}, revision, "stale")
        self.assertEqual(len(self.forge.comments[1]), 0)

    def test_concurrent_claims_are_conflicts_not_exclusive_successes(self):
        self.create()
        revision = self.revision()
        self.forge.barrier = threading.Barrier(2)
        results, errors = [], []

        def run(backend, op):
            try:
                results.append(backend.mutate("sample", "claim", {}, revision, op))
            except Exception as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=run, args=(self.a, "claim-a")),
            threading.Thread(target=run, args=(self.b, "claim-b")),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)
        self.forge.barrier = None
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertTrue(all(not r["exclusive"] for r in results))
        self.assertEqual(self.a.show("sample")["status"], "conflict")
        self.assertIn("concurrent", self.b.show("sample")["conflicts"][0]["reason"])

    def test_explicit_conflict_resolution(self):
        self.create()
        revision = self.revision()
        self.change(self.a, "claim")
        event = decode(self.forge.comments[1][0]["body"], EVENT_MARKER)
        event["operation_id"] = "racing-b"
        event["patch"]["assignee"] = "bob"
        self.forge.comments[1].append(
            {
                "id": 2,
                "body": encode(EVENT_MARKER, event, "Race"),
                "user": {"login": "bob"},
            }
        )
        current = self.a.show("sample")
        self.assertNotEqual(current["remote_revision"], revision)
        self.a.mutate(
            "sample",
            "resolve",
            {"status": "active"},
            current["remote_revision"],
            "resolve",
            resolves=[2],
        )
        self.assertEqual(self.b.show("sample")["status"], "active")

    def test_postcommit_timeout_create_not_duplicated(self):
        self.forge.fail_after_write = True
        self.assertEqual(self.create()["status"], "outcome-unknown")
        self.assertEqual(self.create()["status"], "published")
        self.assertEqual(len(self.forge.issues), 1)

    def test_postcommit_comment_timeout_not_duplicated(self):
        self.create()
        revision = self.revision()
        self.forge.fail_after_write = True
        self.assertEqual(
            self.a.mutate("sample", "claim", {}, revision, "claim")["status"],
            "outcome-unknown",
        )
        self.assertEqual(
            self.a.mutate("sample", "claim", {}, revision, "claim")["status"],
            "published",
        )
        self.assertEqual(len(self.forge.comments[1]), 1)

    def test_unknown_absent_write_is_never_resent(self):
        self.create()
        revision = self.revision()
        self.forge.fail_after_write = True
        self.a.mutate("sample", "claim", {}, revision, "claim")
        self.forge.comments[1] = []  # Simulates a write still not visible / delayed.
        count = len(self.forge.writes)
        result = self.a.mutate("sample", "claim", {}, revision, "claim")
        self.assertEqual(result["status"], "outcome-unknown")
        self.assertEqual(count, len(self.forge.writes))

    def test_pending_offline_reconciles_before_publication(self):
        self.forge.offline = True
        self.assertEqual(self.create()["status"], "local-pending")
        self.assertEqual(self.forge.writes, [])
        self.forge.offline = False
        self.create()
        self.assertEqual(len(self.forge.issues), 1)

    def test_pending_claim_does_not_override_remote_change(self):
        self.create()
        revision = self.revision()
        self.forge.offline = True
        self.b.mutate("sample", "claim", {}, revision, "claim-b")
        self.forge.offline = False
        self.change(self.a, "claim", op="claim-a")
        with self.assertRaisesRegex(GiteaError, "revision changed"):
            self.b.mutate("sample", "claim", {}, revision, "claim-b")
        self.assertEqual(self.a.show("sample")["assignee"], "alice")

    def test_same_operation_id_different_intent_is_error(self):
        self.create()
        with self.assertRaisesRegex(GiteaError, "different intent"):
            self.a.create("other", "Shared work", self.fields, "create-1")

    def test_remote_format_corruption_preserved(self):
        self.create()
        self.forge.issues[1]["body"] += "\n" + TASK_MARKER
        raw = self.forge.issues[1]["body"]
        tasks, malformed = self.b.list()
        self.assertEqual(tasks, [])
        self.assertEqual(malformed[0]["issue_number"], 1)
        self.assertEqual(raw, self.forge.issues[1]["body"])

    def test_native_sidebar_assignment_is_visible_conflict(self):
        self.create()
        self.forge.issues[1]["assignees"] = [{"login": "bob"}]
        self.assertEqual(self.b.show("sample")["status"], "conflict")
        self.assertEqual(self.b.show("sample")["native_assignees"], ["bob"])

    def test_gitea_null_optional_arrays(self):
        self.create()
        self.forge.issues[1]["assignees"] = None
        self.assertEqual(self.b.show("sample")["status"], "queued")
        self.pr()
        self.forge.prs[10]["requested_reviewers"] = None
        self.assertEqual(self.a.review(10)["requested_reviewers"], [])

    def test_review_request_uses_forge_api_not_claimed_acceptance(self):
        self.create()
        self.pr()
        self.change(
            self.b,
            "ready-for-review",
            {"status": "ready-for-review", "pr": 10, "verification": "tests passed"},
        )
        result = self.b.request_review("sample", ["alice"], self.revision(), "review")
        self.assertEqual(result["status"], "published")
        self.assertEqual(
            self.forge.writes[-1][2], "/repos/team/sample/pulls/10/requested_reviewers"
        )
        self.assertEqual(self.a.sync()["review_requests"], ["sample"])
        self.assertEqual(self.a.show("sample")["status"], "ready-for-review")

    def test_review_request_timeout_no_duplicate_notification(self):
        self.create()
        self.pr()
        self.change(self.b, "update", {"status": "ready-for-review", "pr": 10})
        revision = self.revision()
        self.forge.fail_after_write = True
        self.assertEqual(
            self.b.request_review("sample", ["alice"], revision, "review")["status"],
            "outcome-unknown",
        )
        self.b.request_review("sample", ["alice"], revision, "review")
        self.assertEqual(
            sum(
                path.endswith("requested_reviewers")
                for _, _, path, _ in self.forge.writes
            ),
            1,
        )

    def test_merged_required_closed_is_not_delivery(self):
        self.create()
        self.pr()
        self.change(self.b, "update", {"pr": 10})
        self.forge.prs[10]["state"] = "closed"
        with self.assertRaisesRegex(GiteaError, "verified merged PR"):
            self.change(self.b, "update", {"status": "completed"}, op="finish")
        self.forge.prs[10].update(merged=True, merge_commit_sha="b" * 40)
        self.change(self.b, "update", {"status": "completed"}, op="finish-merged")
        self.assertEqual(self.a.show("sample")["status"], "completed")
        self.assertFalse(any("/merge" in path for _, _, path, _ in self.forge.writes))

    def test_review_states_and_dismissals_remain_distinct(self):
        self.create()
        self.pr()
        self.forge.reviews[10] = [
            {
                "id": 1,
                "user": {"login": "alice"},
                "state": "APPROVED",
                "dismissed": True,
            },
            {"id": 2, "user": {"login": "alice"}, "state": "REQUEST_CHANGES"},
        ]
        reviews = self.a.review(10)["reviews"]
        self.assertEqual(
            [r["state"] for r in reviews], ["DISMISSED", "REQUEST_CHANGES"]
        )

    def test_sync_acknowledgement_replays_until_explicit_ack(self):
        self.create()
        first = self.b.sync("reader")
        self.assertEqual(len(self.b.sync("reader")["changes"]), 1)
        self.assertEqual(
            self.b.sync("reader", acknowledge=first["ack_revision"])["changes"], []
        )
        with self.assertRaises(GiteaError):
            self.b.sync("reader", acknowledge="wrong")

    def test_sync_pagination_prevents_early_ack_and_keeps_snapshot(self):
        self.create()
        for n in range(4):
            self.a.create("extra-" + str(n), "Extra", self.fields, "extra-" + str(n))
        first = self.b.sync("paged", max_results=1, budget=1024)
        self.assertTrue(first["has_more"])
        self.assertIsNone(first["ack_revision"])
        with self.assertRaises(GiteaError):
            self.b.sync("paged", acknowledge=first["next_cursor"].split(":")[0])
        seen = first["changes"]
        page = first
        while page["next_cursor"]:
            page = self.b.sync(
                "paged", cursor=page["next_cursor"], max_results=1, budget=1024
            )
            seen += page["changes"]
            self.assertLessEqual(
                len(json.dumps(page, sort_keys=True, ensure_ascii=False).encode()) + 1,
                1024,
            )
        self.assertEqual(len(seen), 5)
        self.assertEqual(
            self.b.sync("paged", acknowledge=page["ack_revision"])["changes"], []
        )


class GiteaIntegrationTests(unittest.TestCase):
    create = GiteaTaskTests.create
    revision = GiteaTaskTests.revision
    change = GiteaTaskTests.change

    def setUp(self):
        GiteaTaskTests.setUp(self)
        self.root = self.folder / "repository"
        self.root.mkdir()
        self.data = self.folder / "local-registry"
        register_project(self.data, project_id="sample", control_root=self.root)
        (self.root / ".continuity").mkdir()
        (self.root / ".continuity" / "collaboration.toml").write_text(
            "[collaboration]\n"
            + "".join(f"{k} = {json.dumps(v)}\n" for k, v in CONFIG.items())
        )
        self.patch_client = patch(
            "continuity_plane.gitea_cli.GiteaTasks",
            side_effect=lambda config, directory: GiteaTasks(
                config, directory, client=self.forge.client("alice")
            ),
        )
        self.patch_client.start()
        self.addCleanup(self.patch_client.stop)

    def call(self, operation, **parameters):
        return execute(
            operation,
            parameters,
            root=str(self.root),
            project_id="sample",
            data_root=str(self.data),
        )

    def test_cli_and_mcp_share_remote_tasks_not_local_ledger(self):
        created = self.call(
            "task.add",
            task_id="sample",
            title="Shared",
            operation_id="cli-create",
            **{
                "lane_id": "scope",
                "mode": "patch",
                "priority": "p1",
                "objective": "Check",
                "next_action": "Test",
                "exit_criteria": ["PASS"],
            },
        )
        self.assertEqual(created["status"], "published")
        result = handle_tool(
            "continuity_plan",
            {
                "action": "task.claim",
                "executor_id": "worker-sol",
                "root": str(self.root),
                "project_id": "sample",
                "data_root": str(self.data),
                "format": "json",
                "parameters": {
                    "task_id": "sample",
                    "expected_remote_revision": self.revision(),
                    "operation_id": "cli-claim",
                },
            },
        )
        self.assertEqual(result["structuredContent"]["observed"]["assignee"], "alice")
        self.assertFalse(list(self.data.rglob("dispatch-ledger.json")))

    def test_task_alias_and_body_only_update(self):
        self.create()
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(
                main(
                    [
                        "task",
                        "update",
                        "--project-id",
                        "sample",
                        "--data-root",
                        str(self.data),
                        "--task-id",
                        "sample",
                        "--body",
                        "Updated instructions",
                        "--operation-id",
                        "body",
                        "--expected-remote-revision",
                        self.revision(),
                    ]
                ),
                0,
            )
        self.assertEqual(self.a.show("sample")["body"], "Updated instructions")

    def test_shared_list_personal_filters(self):
        self.create()
        self.change(self.a, "claim")
        value = self.call("task.list", mine=True, created_by_me=True)
        self.assertEqual(value["items"][0]["task_id"], "sample")

    def test_backend_sync_cli_contract(self):
        self.create()
        value = self.call("backend.sync", consumer="cli-reader", max_output_bytes=1024)
        self.assertEqual(value["changes"][0]["task_id"], "sample")
        self.assertIsNotNone(value["ack_revision"])

    def test_update_task_definition_fields(self):
        self.create()
        self.call(
            "task.update",
            task_id="sample",
            title="Revised task",
            objective="Revised objective",
            priority="p0",
            exit_criteria=["New gate"],
            operation_id="definition-update",
            expected_remote_revision=self.revision(),
        )
        task = self.a.show("sample")
        self.assertEqual(task["title"], "Revised task")
        self.assertEqual(task["objective"], "Revised objective")
        self.assertEqual(task["priority"], "p0")
        self.assertEqual(task["exit_criteria"], ["New gate"])

    def test_bounded_remote_task_pages_keep_every_item(self):
        self.create()
        for n in range(5):
            self.a.create(
                "extra-" + str(n), "Long title " * 10, self.fields, "extra-" + str(n)
            )
        page = self.call("task.list", max_output_bytes=1024)
        items = page["items"][:]
        while page["next_cursor"]:
            page = self.call(
                "task.list", max_output_bytes=1024, cursor=page["next_cursor"]
            )
            items.extend(page["items"])
        self.assertEqual(len({t["task_id"] for t in items}), 6)

    def test_help_and_mcp_discover_shared_commands(self):
        self.assertIn("--expected-remote-revision", help_for("task.update")["help"])
        tool = next(t for t in tool_definitions() if t["name"] == "continuity_plan")
        self.assertTrue(tool["annotations"]["openWorldHint"])
        self.assertIn(
            "task.handoff-accept", tool["inputSchema"]["properties"]["action"]["enum"]
        )

    def test_corrupt_config_has_no_silent_local_fallback(self):
        (self.root / ".continuity/collaboration.toml").write_text("broken = [")
        with self.assertRaisesRegex(GiteaError, "refusing local fallback"):
            backend_for(self.data, "sample")

    def test_configure_creates_safe_repository_ignore_without_overwriting(self):
        declaration = self.root / ".continuity/collaboration.toml"
        declaration.unlink()
        with patch("continuity_plane.gitea_cli.GiteaClient") as client:
            client.return_value.doctor.return_value = {
                "status": "connected",
                "account": "alice",
            }
            self.call(
                "backend.configure",
                instance=CONFIG["instance"],
                repository=CONFIG["repository"],
                credential_ref=CONFIG["credential_ref"],
            )
        ignore = self.root / ".continuity/.gitignore"
        self.assertEqual(
            ignore.read_text().splitlines()[1:],
            [
                "*",
                "!.gitignore",
                "!collaboration.toml",
                "!controller-profile.json",
                "!knowledge/",
                "!knowledge/*.json",
            ],
        )
        declaration.unlink()
        ignore.write_text("existing ignore\n")
        with patch("continuity_plane.gitea_cli.GiteaClient") as client:
            client.return_value.doctor.return_value = {"status": "connected"}
            self.call(
                "backend.configure",
                instance=CONFIG["instance"],
                repository=CONFIG["repository"],
                credential_ref=CONFIG["credential_ref"],
            )
        self.assertEqual(ignore.read_text(), "existing ignore\n")


class AuthenticationTests(unittest.TestCase):
    def setUp(self):
        resolve = patch(
            "continuity_plane.gitea_transport.shutil.which",
            return_value="tea-fixture",
        )
        resolve.start()
        self.addCleanup(resolve.stop)

    def test_https_custom_port_subpath_and_unsafe_configs(self):
        self.assertEqual(validate_config(CONFIG)["instance"], CONFIG["instance"])
        for instance in (
            "http://example.test",
            "https://user:pass@example.test",
            "https://example.test/?token=secret",
            "https://example.test:bad",
        ):
            with self.assertRaises(GiteaError):
                validate_config({**CONFIG, "instance": instance})
        with self.assertRaises(GiteaError):
            validate_config({**CONFIG, "credential_ref": "env:SOME_UNRELATED_SECRET"})

    def test_real_cli_aliases_allow_spaces_colons_and_unicode(self):
        for name in ("Company Gitea:3443", "本机 登录", "--literal-name"):
            config = {**CONFIG, "credential_ref": "tea:" + name}
            self.assertEqual(validate_config(config)["credential_ref"], "tea:" + name)
            metadata = json.dumps([{"Name": name, "URL": CONFIG["instance"]}]).encode()
            with patch(
                "continuity_plane.gitea_transport.run_tea",
                side_effect=[(0, metadata, b""), (0, b"--include --data", b"")],
            ):
                client = GiteaClient(config)
            with patch(
                "continuity_plane.gitea_transport.run_tea",
                return_value=(0, b'{"login":"alice"}', b"HTTP/2.0 200 OK\n"),
            ) as run:
                self.assertEqual(client.identity(), "alice")
                self.assertIn("--login=" + name, run.call_args.args[0])

    def test_cli_alias_controls_empty_and_surrounding_space_are_rejected(self):
        for name in (
            "",
            " name",
            "name ",
            "name\n--debug",
            "name\x00",
            "name\x7f",
            "x" * 257,
        ):
            with self.assertRaises(GiteaError):
                validate_config({**CONFIG, "credential_ref": "tea:" + name})

    def test_tea_origin_binding_and_no_secret_output(self):
        metadata = json.dumps(
            [{"Name": "member", "URL": CONFIG["instance"], "User": "alice"}]
        ).encode()
        with patch(
            "continuity_plane.gitea_transport.run_tea",
            side_effect=[(0, metadata, b""), (0, b"--include --data", b"")],
        ) as run:
            client = GiteaClient(CONFIG)
        self.assertEqual(client.login, "member")
        self.assertEqual(
            run.call_args_list[0].args[0][-4:], ["logins", "list", "--output", "json"]
        )
        with patch(
            "continuity_plane.gitea_transport.run_tea",
            return_value=(0, metadata, b""),
        ):
            with self.assertRaisesRegex(GiteaError, "does not match"):
                GiteaClient({**CONFIG, "instance": "https://different.example.test"})

    def client(self):
        metadata = json.dumps([{"Name": "member", "URL": CONFIG["instance"]}]).encode()
        with patch(
            "continuity_plane.gitea_transport.run_tea",
            side_effect=[(0, metadata, b""), (0, b"--include --data", b"")],
        ):
            return GiteaClient(CONFIG)

    def test_transport_pagination_does_not_assume_server_page_size(self):
        client = self.client()
        with patch.object(
            client, "request", side_effect=[[{"id": 1}], [{"id": 2}], []]
        ) as request:
            self.assertEqual(len(client.all("/sample")), 2)
            self.assertEqual(request.call_count, 3)

    def test_cli_stdin_payload_and_http_status_even_when_exit_zero(self):
        client = self.client()
        with patch(
            "continuity_plane.gitea_transport.run_tea",
            return_value=(0, b'{"id":1}', b"HTTP/2.0 201 Created\n"),
        ) as run:
            self.assertEqual(
                client.request("POST", "/example", {"body": "private-task"})["id"], 1
            )
            command, body = run.call_args.args
            self.assertNotIn("private-task", " ".join(command))
            self.assertIn(b"private-task", body)
            self.assertIn("@-", command)
        with patch(
            "continuity_plane.gitea_transport.run_tea",
            return_value=(0, b"private server diagnostic", b"HTTP/1.1 403 Forbidden\n"),
        ):
            with self.assertRaisesRegex(GiteaError, "HTTP 403") as error:
                client.request("GET", "/example")
            self.assertNotIn("private server diagnostic", str(error.exception))

    def test_comments_endpoint_is_not_paginated(self):
        client = self.client()
        with patch.object(client, "request", return_value=[{"id": 1}]) as request:
            self.assertEqual(client.all("/issues/1/comments"), [{"id": 1}])
            self.assertEqual(request.call_count, 1)


class RetirementAndHealthTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.data = self.root / "data"
        register_project(self.data, project_id="sample", control_root=self.root)

    def test_inbox_archive_is_acknowledged_only_recoverable_and_idempotent(self):
        send_message(
            self.data,
            project_id="sample",
            message_id="one",
            sender="a",
            recipient="alice",
            body="one",
        )
        send_message(
            self.data,
            project_id="sample",
            message_id="two",
            sender="a",
            recipient="alice",
            body="two",
        )
        self.assertEqual(
            archive(self.data, project_id="sample", recipient="alice")["archived"],
            0,
        )
        acknowledge(
            self.data,
            project_id="sample",
            recipient="alice",
            message_id="one",
        )
        self.assertEqual(
            archive(self.data, project_id="sample", recipient="alice")["archived"],
            1,
        )
        self.assertEqual(
            [
                m["message_id"]
                for m in list_messages(
                    self.data, project_id="sample", recipient="alice"
                )
            ],
            ["two"],
        )
        self.assertEqual(
            send_message(
                self.data,
                project_id="sample",
                message_id="one",
                sender="a",
                recipient="alice",
                body="one",
            )["message_id"],
            "one",
        )
        self.assertTrue(
            (self.data / "projects/sample/inbox-archive/one.json").is_file()
        )

    def test_quota_is_not_plain_permanent_failure_or_rate_limit(self):
        reset_at = (
            (datetime.now(UTC) + timedelta(hours=1))
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        )
        value = classify(f"Usage limit reached; retry at {reset_at}")
        self.assertEqual(value["failure_class"], "quota-exhausted")
        self.assertTrue(value["retryable"])
        self.assertTrue(value["resume_same_session"])
        self.assertFalse(value["requires_new_session"])
        self.assertEqual(value["available_at"], reset_at.replace("Z", "+00:00"))
        from continuity_plane.retry_policy import policy_refusal

        self.assertFalse(
            policy_refusal(
                "Usage policy violation: usage limit reached; retry after reset"
            )
        )
        self.assertEqual(
            classify(
                f"Usage policy violation: usage limit reached; retry at {reset_at}"
            )["failure_class"],
            "quota-exhausted",
        )
        self.assertEqual(
            classify("Provider content policy refusal")["failure_class"],
            "policy-review",
        )

    def test_quota_record_names_the_missing_subscription_flag(self):
        from continuity_plane.subscription_quota import record

        with self.assertRaisesRegex(ValueError, "--official-subscription"):
            record(
                self.data,
                "sample",
                "gpt-6.1-sol",
                used_percent=100,
                resets_at="2099-01-01T00:00:00+00:00",
                source="manual",
                official_subscription=False,
            )

    def test_batch_update_stops_at_failure_and_reports_no_transaction(self):
        add_task(
            self.data,
            project_id="sample",
            task_id="alpha",
            lane_id="scope",
            mode="patch",
            priority="p1",
            title="Alpha",
            objective="Check batch",
            next_action="Start",
            exit_criteria=["Pass"],
        )
        path = self.root / "batch.json"
        path.write_text(
            json.dumps(
                {
                    "updates": [
                        {"task_id": "alpha", "next_action": "Continue"},
                        {"task_id": "missing", "next_action": "Fail"},
                    ]
                }
            )
        )
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(
                [
                    "task",
                    "batch-update",
                    "--project-id",
                    "sample",
                    "--data-root",
                    str(self.data),
                    "--file",
                    str(path),
                    "--format",
                    "json",
                ]
            )
        value = json.loads(output.getvalue())
        self.assertEqual(code, 1)
        self.assertEqual(value["status"], "stopped")
        self.assertEqual(value["failed"]["index"], 1)
        self.assertFalse(value["transactional"])


if __name__ == "__main__":
    unittest.main()
