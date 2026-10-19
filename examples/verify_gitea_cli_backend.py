#!/usr/bin/env python3
"""Acceptance on a disposable loopback Gitea container, using real local tea.

Creates synthetic users/repository only. Never point this at a production host.
All account configs and data stay in the explicitly selected persistent fixture.
"""

from contextlib import contextmanager
import argparse
import base64
import json
import os
from pathlib import Path
import secrets
import ssl
import subprocess
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from continuity_plane.gitea_tasks import GiteaTasks


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--instance", required=True)
    parser.add_argument("--container", required=True)
    parser.add_argument("--ca", required=True, type=Path)
    parser.add_argument("--output-root", required=True, type=Path)
    args = parser.parse_args()
    if urlsplit(args.instance).hostname not in {
        "127.0.0.1",
        "localhost",
    } or not args.container.startswith("continuity-gitea-"):
        parser.error("only disposable loopback continuity-gitea-* fixtures are allowed")
    args.output_root.mkdir(parents=True, exist_ok=True)
    context = ssl.create_default_context(cafile=str(args.ca))
    passwords, tokens = {}, {}

    def run(command, env=None):
        result = subprocess.run(
            command, capture_output=True, text=True, env=env, timeout=60
        )
        if result.returncode:
            raise RuntimeError(
                "fixture setup command failed; diagnostics suppressed to protect synthetic credentials"
            )
        return result.stdout.strip()

    def api(method, endpoint, payload=None, *, user="alice"):
        headers = {"Content-Type": "application/json"}
        if user in tokens:
            headers["Authorization"] = "token " + tokens[user]
        else:
            headers["Authorization"] = (
                "Basic "
                + base64.b64encode((user + ":" + passwords[user]).encode()).decode()
            )
        request = Request(
            args.instance + "/api/v1" + endpoint,
            method=method,
            headers=headers,
            data=json.dumps(payload).encode() if payload is not None else None,
        )
        with urlopen(request, context=context, timeout=30) as response:
            raw = response.read()
            return json.loads(raw) if raw else None

    for name in ("alice", "bob"):
        passwords[name] = secrets.token_urlsafe(24)
        command = [
            "docker",
            "exec",
            args.container,
            "gitea",
            "--config",
            "/etc/gitea/app.ini",
            "admin",
            "user",
        ]
        run(
            command
            + [
                "create",
                "--username",
                name,
                "--email",
                name + "@example.test",
                "--password",
                passwords[name],
                "--must-change-password=false",
            ]
            + (["--admin"] if name == "alice" else [])
        )
        tokens[name] = run(
            command
            + [
                "generate-access-token",
                "--username",
                name,
                "--token-name",
                "fixture",
                "--scopes",
                "all",
                "--raw",
            ]
        ).splitlines()[-1]
        account_root = args.output_root / name
        account_root.mkdir(mode=0o700)
        env = {
            **os.environ,
            "XDG_CONFIG_HOME": str(account_root / "config"),
            "SSL_CERT_FILE": str(args.ca),
        }
        run(
            [
                "tea",
                "login",
                "add",
                "--name",
                "fixture",
                "--url",
                args.instance,
                "--token",
                tokens[name],
            ],
            env,
        )

    api(
        "POST",
        "/user/repos",
        {
            "name": "sample",
            "private": True,
            "auto_init": True,
            "default_branch": "main",
        },
    )
    api("PUT", "/repos/alice/sample/collaborators/bob", {"permission": "write"})
    config = {
        "backend": "gitea-issues",
        "instance": args.instance,
        "repository": "alice/sample",
        "credential_ref": "tea:fixture",
    }

    @contextmanager
    def member(name):
        old = dict(os.environ)
        os.environ.update(
            XDG_CONFIG_HOME=str(args.output_root / name / "config"),
            SSL_CERT_FILE=str(args.ca),
        )
        try:
            yield GiteaTasks(config, args.output_root / name / "data")
        finally:
            os.environ.clear()
            os.environ.update(old)

    results = {}
    with member("alice") as a:
        results["doctor"] = a.client.doctor()
        assert results["doctor"]["transport"] == "local-tea-cli"
        a.create(
            "sample",
            "Synthetic shared task",
            {
                "objective": "Verify collaboration",
                "next_action": "Test",
                "exit_criteria": ["Pass"],
            },
            "create",
        )
    with member("bob") as b:
        assert b.show("sample")["created_by"] == "alice"
        b.mutate("sample", "claim", {}, b.show("sample")["remote_revision"], "claim")
    with member("alice") as a:
        assert a.show("sample")["assignee"] == "bob"
    with member("bob") as b:
        b.mutate(
            "sample",
            "handoff-request",
            {},
            b.show("sample")["remote_revision"],
            "handoff",
            target="alice",
            note="Continue review",
        )
        assert b.show("sample")["assignee"] == "bob"
    with member("alice") as a:
        a.mutate(
            "sample",
            "handoff-accept",
            {},
            a.show("sample")["remote_revision"],
            "accept",
        )
        assert a.show("sample")["assignee"] == "alice"
    results["shared_task_claim_handoff"] = "passed"

    # API setup models human web actions; backend operations below still use tea.
    issue = api("GET", "/repos/alice/sample/issues/1")
    api(
        "PATCH",
        "/repos/alice/sample/issues/1",
        {
            "title": "Human-edited title",
            "body": issue["body"] + "\nHuman note preserved.\n",
        },
    )
    api(
        "POST",
        "/repos/alice/sample/branches",
        {"new_branch_name": "work", "old_branch_name": "main"},
    )
    api(
        "POST",
        "/repos/alice/sample/contents/test.txt",
        {
            "branch": "work",
            "content": base64.b64encode(b"test\n").decode(),
            "message": "Fixture change",
        },
        user="bob",
    )
    pr = api(
        "POST",
        "/repos/alice/sample/pulls",
        {"head": "work", "base": "main", "title": "Fixture PR"},
        user="bob",
    )
    with member("bob") as b:
        task = b.show("sample")
        assert task["title"] == "Human-edited title"
        assert "Human note preserved" in task["issue_body"]
        b.mutate(
            "sample",
            "update",
            {
                "pr": pr["number"],
                "status": "ready-for-review",
                "verification": "Fixture tests passed",
            },
            task["remote_revision"],
            "ready",
        )
        b.request_review(
            "sample", ["alice"], b.show("sample")["remote_revision"], "review"
        )
    with member("alice") as a:
        assert "sample" in a.sync()["review_requests"]
        assert not a.show("sample")["pull_request"]["delivered"]
        from continuity_plane.gitea_transport import GiteaError

        try:
            a.mutate(
                "sample",
                "update",
                {"status": "completed"},
                a.show("sample")["remote_revision"],
                "not-merged",
            )
        except GiteaError:
            results["unmerged_delivery_rejected"] = "passed"
        else:
            raise AssertionError("unmerged PR incorrectly completed")
    results["web_edit_and_real_review_request"] = "passed"
    results["credentials"] = (
        "synthetic, member-local tea configs only; never in receipt"
    )
    results["continuity_server"] = False
    (args.output_root / "acceptance.json").write_text(
        json.dumps(results, indent=2) + "\n"
    )
    print(
        json.dumps(
            {
                "status": "passed",
                "checks": list(results),
                "receipt": str(args.output_root / "acceptance.json"),
            }
        )
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        import traceback

        print(
            "".join(traceback.format_tb(exc.__traceback__)),
            file=__import__("sys").stderr,
        )
        print(type(exc).__name__, file=__import__("sys").stderr)
        raise SystemExit(
            "Gitea fixture acceptance failed; credentials and raw responses intentionally suppressed"
        ) from None
