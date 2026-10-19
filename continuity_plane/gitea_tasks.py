"""Issue-authoritative tasks using readable, append-only Gitea comments.

Gitea v1 has no documented issue CAS. Events carry their observed parents and
field values; races are conflicts, never silently overwritten or strong leases.
Local files are explicitly caches and a durable, non-retrying operation journal.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import re

from .collaboration_registry import _atomic_write, _locked, _now
from .gitea_transport import GiteaClient, GiteaError, GiteaUnavailable

TASK_MARKER = "<!-- continuity-task:v1 -->"
EVENT_MARKER = "<!-- continuity-event:v1 -->"
FIELDS = {
    "title",
    "objective",
    "next_action",
    "exit_criteria",
    "priority",
    "dependencies",
    "allowed_files",
    "branch",
    "pr",
    "commit",
    "verification",
    "blocked_reason",
    "handoff",
    "assignee",
    "status",
    "body",
    "lane_id",
    "mode",
    "result",
}
STATES = {
    "queued",
    "active",
    "blocked",
    "ready-for-review",
    "completed",
    "merged",
    "superseded",
    "rejected",
}
CLOSED = {"completed", "merged", "superseded", "rejected"}


def digest(value):
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        ).encode()
    ).hexdigest()


def encode(marker, value, summary):
    return (
        summary
        + "\n\n"
        + marker
        + "\n```json\n"
        + json.dumps(value, ensure_ascii=False, indent=2)
        + "\n```\n"
    )


def decode(text, marker):
    """Keep human prose outside the block intact; malformed protocol is an error."""
    if not isinstance(text, str) or text.count(marker) != 1:
        raise GiteaError(
            "missing or duplicate Continuity block; original Issue preserved"
        )
    block = re.search(re.escape(marker) + r"\s*```json\s*\n(.*?)\n```", text, re.S)
    if block is None:
        raise GiteaError("malformed Continuity block; original Issue preserved")
    try:
        value = json.loads(block[1])
    except ValueError:
        raise GiteaError("invalid Continuity JSON; original Issue preserved") from None
    if not isinstance(value, dict):
        raise GiteaError("Continuity block must be an object; original Issue preserved")
    return value


def validate_fields(fields):
    if set(fields) - FIELDS:
        raise GiteaError("unsupported shared task fields")
    for key, value in fields.items():
        if key in {"dependencies", "allowed_files", "exit_criteria"}:
            if (
                not isinstance(value, list)
                or len(value) > 256
                or any(not isinstance(v, str) or len(v) > 4096 for v in value)
            ):
                raise GiteaError(f"invalid {key}")
        elif key == "pr":
            if value is not None and (type(value) is not int or value < 1):
                raise GiteaError(
                    "pr must be a positive number in the configured repository"
                )
        elif key == "handoff":
            if value is not None and (
                not isinstance(value, dict)
                or set(value) != {"from", "to", "note"}
                or not all(isinstance(v, str) for v in value.values())
            ):
                raise GiteaError("invalid handoff")
        elif value is not None and (not isinstance(value, str) or len(value) > 32768):
            raise GiteaError(f"invalid {key}")
    if "status" in fields and fields["status"] not in STATES:
        raise GiteaError("invalid shared task status")
    if fields.get("assignee") and not re.fullmatch(
        r"[A-Za-z0-9_.-]{1,128}", fields["assignee"]
    ):
        raise GiteaError("driver must be a human Forge account, not an agent identity")


def project(issue, comments):
    """Rebuild entirely from the remote Issue and its comments, never local cards."""
    initial = decode(issue.get("body"), TASK_MARKER)
    task_id = initial.pop("task_id", None)
    initial.pop("operation_id", None)
    if not isinstance(task_id, str) or not re.fullmatch(
        r"[a-z0-9][a-z0-9._-]{0,127}", task_id
    ):
        raise GiteaError("invalid stable task ID; original Issue preserved")
    validate_fields(initial)
    task = {
        "assignee": None,
        "status": "queued",
        "dependencies": [],
        "allowed_files": [],
        "exit_criteria": [],
        "handoff": None,
        "pr": None,
        **initial,
        "title": issue["title"],
    }
    parents, conflicts, operations = [], [], set()
    events = []
    for comment in sorted(comments, key=lambda r: r["id"]):
        if EVENT_MARKER not in (comment.get("body") or ""):
            continue
        try:
            event = decode(comment["body"], EVENT_MARKER)
            op = event["operation_id"]
            author = comment.get("user", {}).get("login")
            if not isinstance(op, str) or not author:
                raise GiteaError("invalid event identity")
            if op in operations:
                raise GiteaError("duplicate remote operation ID")
            operations.add(op)
            patch = event["patch"]
            validate_fields(patch)
            if event.get("parents") != parents:
                raise GiteaError("concurrent remote change: event parents differ")
            if event.get("before") != {key: task.get(key) for key in patch}:
                raise GiteaError("web or concurrent edit conflicts with event fields")
            kind = event["kind"]
            owner = task.get("assignee")
            if "assignee" in patch:
                if kind == "claim" and (
                    owner not in {None, author} or patch["assignee"] != author
                ):
                    raise GiteaError(
                        "claim conflicts with current driver or authenticated author"
                    )
                elif kind == "release" and (
                    owner != author or patch["assignee"] is not None
                ):
                    raise GiteaError("only current driver may release")
                elif kind == "handoff-accept":
                    handoff = task.get("handoff") or {}
                    if (
                        handoff.get("to") != author
                        or handoff.get("from") != owner
                        or patch["assignee"] != author
                    ):
                        raise GiteaError(
                            "handoff acceptance requires the requested member"
                        )
                elif kind not in {"claim", "release", "handoff-accept", "resolve"}:
                    raise GiteaError(
                        "driver changes require claim/release/accepted handoff"
                    )
            if kind == "handoff-request":
                if owner != author or patch.get("handoff", {}).get("from") != author:
                    raise GiteaError("only current driver may propose handoff")
            if conflicts and kind != "resolve":
                raise GiteaError("unresolved earlier conflict")
            if kind == "resolve":
                # Explicitly acknowledged conflicting comment IDs, not --force.
                if event.get("resolves") != [c["comment_id"] for c in conflicts]:
                    raise GiteaError(
                        "resolution must acknowledge all observed conflicts"
                    )
                if "assignee" in patch and patch["assignee"] not in {None, author}:
                    raise GiteaError(
                        "resolution cannot accept on another member's behalf"
                    )
                conflicts = []
            task.update(copy.deepcopy(patch))
            events.append({"comment_id": comment["id"], "kind": kind, "author": author})
        except (GiteaError, KeyError, TypeError) as exc:
            conflicts.append({"comment_id": comment["id"], "reason": str(exc)[:200]})
        parents.append(comment["id"])
    # Native sidebar assignments are not overwritten by the comment protocol.
    # Unexpected native assignments are exposed, never silently treated as accepted.
    native = sorted(
        u["login"] for u in (issue.get("assignees") or []) if u.get("login")
    )
    if native and native != [task.get("assignee")]:
        conflicts.append(
            {
                "comment_id": 0,
                "reason": "native Issue assignees differ from accepted driver",
                "native_assignees": native,
            }
        )
    if issue.get("state") == "closed" and task["status"] not in CLOSED:
        task["issue_closed_without_delivery"] = True
    task.update(
        {
            "task_id": task_id,
            "issue_number": issue["number"],
            "issue_url": issue["html_url"],
            "created_by": issue.get("user", {}).get("login"),
            "updated_at": max(
                [issue.get("updated_at", "")]
                + [c.get("updated_at", "") for c in comments]
            ),
            "remote_revision": digest({"issue": issue, "comments": comments}),
            "parents": parents,
            "conflicts": conflicts,
            "recent_events": events[-10:],
            "issue_body": issue["body"],
            "native_assignees": native,
            "authority": "gitea-issue",
            "claim_guarantee": "coordination-only",
            "exclusive": False,
        }
    )
    task["recorded_status"] = task["status"]
    if conflicts:
        task["status"] = "conflict"
    return task


class GiteaTasks:
    def __init__(self, config, directory, *, client=None):
        self.config = config
        self.directory = Path(directory) / "gitea"
        self.client = client if client is not None else GiteaClient(config)
        self.repo = self.client.repo
        self.scope = digest(
            {k: config[k] for k in ("instance", "repository", "credential_ref")}
        )
        self.directory = self.directory / self.scope

    def _cache(self, task):
        _atomic_write(
            self.directory / "cache" / f"{task['issue_number']}.json",
            {
                "authority": "cache-only",
                "fetched_at": _now(),
                "task": task,
            },
        )
        return task

    def show_number(self, number, *, issue=None):
        if issue is None:
            issue = self.client.request("GET", self.repo + f"/issues/{number}")
        comments = self.client.all(self.repo + f"/issues/{number}/comments")
        task = project(issue, comments)
        if task.get("pr"):
            task["pull_request"] = self.review(task["pr"])
            if (
                task["status"] in {"merged", "completed"}
                and not task["pull_request"]["delivered"]
            ):
                task["status"] = "conflict"
                task["conflicts"].append(
                    {"comment_id": 0, "reason": "PR is not verified merged"}
                )
        elif task["status"] in {"merged", "completed"}:
            task["status"] = "conflict"
            task["conflicts"].append(
                {"comment_id": 0, "reason": "delivery status has no linked PR"}
            )
        return self._cache(task)

    def list(self):
        tasks, malformed = [], []
        for issue in self.client.all(
            self.repo + "/issues", {"state": "all", "type": "issues"}
        ):
            if issue.get("pull_request") or TASK_MARKER not in (
                issue.get("body") or ""
            ):
                continue
            try:
                tasks.append(self.show_number(issue["number"], issue=issue))
            except GiteaUnavailable:
                raise
            except GiteaError as exc:
                malformed.append(
                    {
                        "issue_number": issue["number"],
                        "issue_url": issue["html_url"],
                        "reason": str(exc),
                    }
                )
        ids = {}
        for task in tasks:
            ids.setdefault(task["task_id"], []).append(task)
        for duplicates in ids.values():
            if len(duplicates) > 1:
                for task in duplicates:
                    task["status"] = "conflict"
                    task["conflicts"].append(
                        {
                            "comment_id": 0,
                            "reason": "duplicate stable task ID on remote",
                        }
                    )
        return tasks, malformed

    def show(self, task_id):
        if task_id.startswith("#") and task_id[1:].isdigit():
            return self.show_number(int(task_id[1:]))
        matches, malformed = [], 0
        # Select one task before loading comments/reviews, not every history.
        for issue in self.client.all(
            self.repo + "/issues", {"state": "all", "type": "issues"}
        ):
            if issue.get("pull_request") or TASK_MARKER not in (
                issue.get("body") or ""
            ):
                continue
            try:
                if decode(issue["body"], TASK_MARKER).get("task_id") == task_id:
                    matches.append(issue)
            except GiteaError:
                malformed += 1
        if not matches and task_id.isdigit():
            return self.show_number(int(task_id))
        if len(matches) != 1:
            raise GiteaError(
                "task not found or ambiguous on remote; malformed issues="
                + str(malformed)
            )
        return self.show_number(matches[0]["number"], issue=matches[0])

    def review(self, number):
        pr = self.client.request("GET", self.repo + f"/pulls/{number}")
        reviews = self.client.all(self.repo + f"/pulls/{number}/reviews")
        requested = sorted(
            u["login"] for u in (pr.get("requested_reviewers") or []) if u.get("login")
        )
        return {
            "number": number,
            "url": pr.get("html_url"),
            "state": pr.get("state"),
            "head_commit": (pr.get("head") or {}).get("sha"),
            "target_branch": (pr.get("base") or {}).get("ref"),
            "merge_commit": pr.get("merge_commit_sha"),
            "delivered": pr.get("merged") is True
            and bool(
                re.fullmatch(
                    r"[0-9a-f]{40}(?:[0-9a-f]{24})?", pr.get("merge_commit_sha") or ""
                )
            ),
            "requested_reviewers": requested,
            "reviews": [
                {
                    "id": r["id"],
                    "account": (r.get("user") or {}).get("login"),
                    "state": "DISMISSED" if r.get("dismissed") else r.get("state"),
                    "stale": r.get("stale", False),
                    "commit": r.get("commit_id"),
                }
                for r in reviews
            ],
        }

    def _journal(self, operation_id):
        if not isinstance(operation_id, str) or not re.fullmatch(
            r"[A-Za-z0-9_.-]{1,128}", operation_id
        ):
            raise GiteaError(
                "a stable --operation-id is required for every remote mutation"
            )
        return self.directory / "operations" / (operation_id + ".json")

    def publish(self, operation_id, intent, prepare, locate):
        """At-most-one send per local operation, with reconciliation after ambiguity.

        No GET absence can prove a timed-out write was not committed later.
        Unknown sends are therefore never automatically resent.
        """
        path = self._journal(operation_id)
        with _locked(path):
            saved = json.loads(path.read_text()) if path.exists() else None
            if saved and saved["intent"] != intent:
                raise GiteaError("operation-id already used for a different intent")
            if saved and saved["state"] in {"sent", "unknown", "published"}:
                if saved.get("account") != self.client.identity():
                    raise GiteaError(
                        "operation belongs to another authenticated account"
                    )
                found = locate()
                if found:
                    saved["state"] = "published"
                    _atomic_write(path, saved)
                    return {
                        "status": "published",
                        "operation_id": operation_id,
                        "remote": found,
                        "reconciled": True,
                    }
                return {
                    "status": "outcome-unknown",
                    "operation_id": operation_id,
                    "message": "Not resent: inspect remote and reconcile; absence is not proof of non-delivery",
                }
            try:
                account = self.client.identity()
                if saved and saved.get("account") not in {None, account}:
                    raise GiteaError(
                        "pending operation belongs to another authenticated account"
                    )
                prepared = prepare(account)
            except GiteaUnavailable:
                _atomic_write(
                    path,
                    {
                        "intent": intent,
                        "state": "pending",
                        "account": (saved or {}).get("account"),
                        "created_at": _now(),
                    },
                )
                return {
                    "status": "local-pending",
                    "operation_id": operation_id,
                    "received_by_remote": False,
                }
            saved = {
                "intent": intent,
                "state": "sent",
                "account": account,
                "created_at": _now(),
            }
            _atomic_write(path, saved)  # Persist before crossing the network boundary.
            try:
                if prepared is None:
                    response = {}
                else:
                    method, endpoint, payload = prepared
                    response = self.client.request(method, endpoint, payload)
            except GiteaUnavailable:
                saved["state"] = "unknown"
                _atomic_write(path, saved)
                return {
                    "status": "outcome-unknown",
                    "operation_id": operation_id,
                    "received_by_remote": "unknown",
                }
            except GiteaError:
                saved["state"] = "rejected"
                _atomic_write(path, saved)
                raise
            saved["state"] = "published"
            _atomic_write(path, saved)
            return {
                "status": "published",
                "operation_id": operation_id,
                "remote": {
                    k: response[k]
                    for k in ("id", "number", "html_url")
                    if isinstance(response, dict) and k in response
                },
                "exclusive": False,
            }

    def create(self, task_id, title, fields, operation_id):
        validate_fields(fields)
        if fields.get("assignee"):
            raise GiteaError(
                "create unclaimed tasks, then let the member claim; AI assignment is not ownership"
            )
        if fields.get("status", "queued") != "queued":
            raise GiteaError("new shared tasks must be queued")
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,127}", task_id):
            raise GiteaError("invalid task ID")
        intent = {
            "kind": "create",
            "task_id": task_id,
            "title": title,
            "fields": fields,
        }

        def locate():
            for issue in self.client.all(
                self.repo + "/issues", {"state": "all", "type": "issues"}
            ):
                try:
                    data = decode(issue.get("body"), TASK_MARKER)
                    if data.get("operation_id") == operation_id:
                        return {
                            "number": issue["number"],
                            "html_url": issue["html_url"],
                        }
                except GiteaError:
                    pass
            return None

        def prepare(account):
            tasks, malformed = self.list()
            if malformed:
                raise GiteaError(
                    "repair malformed task Issues before creating another stable ID"
                )
            if any(t["task_id"] == task_id for t in tasks):
                raise GiteaError("task ID already exists on remote")
            body = encode(
                TASK_MARKER,
                {"task_id": task_id, "operation_id": operation_id, **fields},
                "Continuity shared task. Edit this Issue's JSON fields or append progress below.\n"
                "Driver/status changes are recorded in readable comments; claims are coordination-only.",
            )
            return "POST", self.repo + "/issues", {"title": title, "body": body}

        return self.publish(operation_id, intent, prepare, locate)

    def mutate(
        self,
        task_id,
        kind,
        fields,
        revision,
        operation_id,
        *,
        target=None,
        note=None,
        resolves=None,
    ):
        validate_fields(fields)
        intent = {
            "kind": kind,
            "task_id": task_id,
            "fields": fields,
            "revision": revision,
            "target": target,
            "note": note,
            "resolves": resolves,
        }

        def locate():
            task = self.show(task_id)
            comments = self.client.all(
                self.repo + f"/issues/{task['issue_number']}/comments"
            )
            for row in comments:
                try:
                    if (
                        decode(row.get("body"), EVENT_MARKER).get("operation_id")
                        == operation_id
                    ):
                        return {
                            "comment_id": row["id"],
                            "task_id": task_id,
                            "conflicts": task["conflicts"],
                            "exclusive": False,
                        }
                except GiteaError:
                    pass
            return None

        def prepare(account):
            task = self.show(task_id)
            if task["remote_revision"] != revision:
                raise GiteaError(
                    "remote revision changed; sync and review differences before retrying"
                )
            if task["conflicts"] and kind != "resolve":
                raise GiteaError(
                    "remote conflict requires explicit resolve against current revision"
                )
            patch = dict(fields)
            if kind == "claim":
                if task["assignee"] not in {None, account}:
                    raise GiteaError(
                        "task already driven by another member; request a handoff"
                    )
                if task["status"] not in {"queued", "active"}:
                    raise GiteaError("only queued/active tasks may be claimed")
                patch.update(assignee=account, status="active")
            elif kind == "release":
                if task["assignee"] != account:
                    raise GiteaError("only authenticated current driver may release")
                patch.update(assignee=None, status="queued", handoff=None)
            elif kind == "handoff-request":
                if task["assignee"] != account or not target or target == account:
                    raise GiteaError("current driver must request a different member")
                patch["handoff"] = {"from": account, "to": target, "note": note or ""}
            elif kind == "handoff-accept":
                if (task.get("handoff") or {}).get("to") != account:
                    raise GiteaError("only requested authenticated member may accept")
                patch.update(assignee=account, handoff=None, status="active")
            elif kind == "resolve":
                if resolves != [c["comment_id"] for c in task["conflicts"]] or 0 in (
                    resolves or []
                ):
                    raise GiteaError(
                        "acknowledge every conflicting comment ID; native assignments must be reconciled on Forge"
                    )
                if patch.get("assignee") not in {None, account}:
                    raise GiteaError(
                        "cannot resolve ownership on another member's behalf"
                    )
            elif "assignee" in patch or "handoff" in patch:
                raise GiteaError(
                    "use claim/release or handoff; changing AI does not transfer ownership"
                )
            validate_fields(patch)
            pr = patch.get("pr", task.get("pr"))
            if pr:
                self.review(pr)  # Check it exists in this repository before linking.
            if patch.get("status") in {"completed", "merged"}:
                if not pr or not self.review(pr)["delivered"]:
                    raise GiteaError(
                        "completion requires a verified merged PR and merge commit; closed is insufficient"
                    )
            before = {key: task.get(key) for key in patch}
            if kind == "resolve" and "status" in before:
                before["status"] = task["recorded_status"]
            event = {
                "operation_id": operation_id,
                "kind": kind,
                "parents": task["parents"],
                "before": before,
                "patch": patch,
                "resolves": resolves or [],
            }
            summary = f"Continuity {kind} by @{account} for {task_id}. Coordination only; no exclusive lease."
            return (
                "POST",
                self.repo + f"/issues/{task['issue_number']}/comments",
                {"body": encode(EVENT_MARKER, event, summary)},
            )

        result = self.publish(operation_id, intent, prepare, locate)
        if result["status"] == "published":
            try:
                task = self.show(task_id)
                result["observed"] = {
                    key: task.get(key)
                    for key in (
                        "task_id",
                        "status",
                        "assignee",
                        "conflicts",
                        "remote_revision",
                    )
                }
                if task["conflicts"]:
                    result["status"] = "conflict"
            except GiteaUnavailable:
                result["observation"] = (
                    "published but read-back unavailable; sync before acting"
                )
        return result

    def request_review(self, task_id, reviewers, revision, operation_id):
        if not reviewers or any(
            not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", r) for r in reviewers
        ):
            raise GiteaError("reviewers must be Forge account names")
        reviewers = sorted(set(reviewers))
        intent = {
            "kind": "review-request",
            "task_id": task_id,
            "reviewers": reviewers,
            "revision": revision,
        }

        def locate():
            task = self.show(task_id)
            if not task.get("pr"):
                return None
            pr = self.review(task["pr"])
            return (
                {"pr": task["pr"], "requested_reviewers": reviewers}
                if set(reviewers) <= set(pr["requested_reviewers"])
                else None
            )

        def prepare(account):
            task = self.show(task_id)
            if task["remote_revision"] != revision or task["conflicts"]:
                raise GiteaError("sync remote changes before requesting review")
            if not task.get("pr") or task["status"] != "ready-for-review":
                raise GiteaError("link PR and mark ready-for-review first")
            if account in reviewers:
                raise GiteaError("request another member's review")
            pr = self.review(task["pr"])
            if pr["state"] != "open":
                raise GiteaError("review requires an open PR")
            if set(reviewers) <= set(pr["requested_reviewers"]):
                return None
            # Durable pre-send journal prevents ambiguous retries; no task comment
            # is substituted for the Forge's real reviewer request endpoint.
            return (
                "POST",
                self.repo + f"/pulls/{task['pr']}/requested_reviewers",
                {"reviewers": reviewers},
            )

        return self.publish(operation_id, intent, prepare, locate)

    def sync(
        self,
        consumer="default",
        *,
        acknowledge=None,
        cursor=None,
        max_results=50,
        budget=8192,
    ):
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", consumer):
            raise GiteaError("invalid consumer")
        if not 1024 <= budget <= 131072 or not 1 <= max_results <= 1000:
            raise GiteaError("invalid sync output budget/page size")
        path = self.directory / "consumers" / (consumer + ".json")
        with _locked(path):
            account = self.client.identity()
            previous = (
                json.loads(path.read_text())
                if path.exists()
                else {"seen": {}, "offered": {}}
            )
            if previous.get("account", account) != account:
                raise GiteaError(
                    "consumer belongs to another authenticated member; choose another name"
                )
            if acknowledge:
                if (
                    acknowledge != previous.get("offered_revision")
                    or not previous.get("can_ack")
                    or cursor
                ):
                    raise GiteaError(
                        "ack must match a completely read offered revision"
                    )
                previous["seen"] = previous["offered"]
            start = 0
            if cursor:
                match = re.fullmatch(r"([0-9a-f]{64}):(\d+)", cursor)
                if not match or match[1] != previous.get("offered_revision"):
                    raise GiteaError(
                        "sync cursor expired or belongs to another snapshot"
                    )
                start = int(match[2])
                if start > previous.get("read_until", 0):
                    raise GiteaError("read preceding sync pages before advancing")
            else:
                tasks, malformed = self.list()
                task_map = {t["task_id"]: t for t in tasks}
                offered = {
                    t["task_id"]: digest(
                        {
                            "task": t["remote_revision"],
                            "pr": t.get("pull_request"),
                            "dependencies": [
                                task_map.get(d, {}).get("status")
                                for d in t.get("dependencies", [])
                            ],
                        }
                    )
                    for t in tasks
                }
                changes = []
                for task in tasks:
                    if (
                        previous["seen"].get(task["task_id"])
                        == offered[task["task_id"]]
                    ):
                        continue
                    change = {
                        k: task.get(k)
                        for k in ("task_id", "issue_url", "status", "assignee")
                    }
                    change.update(
                        next_action=(task.get("next_action") or "")[:240],
                        conflict_count=len(task["conflicts"]),
                        assigned_to_me=task.get("assignee") == account,
                        handoff_requested=(task.get("handoff") or {}).get("to")
                        == account,
                        review_requested=account
                        in task.get("pull_request", {}).get("requested_reviewers", []),
                        dependencies_ready=bool(task.get("dependencies"))
                        and all(
                            task_map.get(d, {}).get("status") in {"completed", "merged"}
                            for d in task["dependencies"]
                        ),
                    )
                    changes.append(change)
                for item in malformed:
                    key = "malformed:" + str(item["issue_number"])
                    offered[key] = digest(item)
                    if previous["seen"].get(key) != offered[key]:
                        changes.append(
                            {
                                "task_id": key,
                                "status": "malformed",
                                "issue_url": item["issue_url"],
                                "next_action": "Repair original Issue on Forge; no overwrite performed",
                            }
                        )
                for missing in sorted(set(previous["seen"]) - set(offered)):
                    changes.append(
                        {"task_id": missing, "status": "remote-missing-or-malformed"}
                    )
                previous.update(
                    account=account,
                    offered=offered,
                    offered_revision=digest({"account": account, "offered": offered}),
                    changes=changes,
                    read_until=0,
                    can_ack=False,
                )
            changes = previous["changes"]
            selected = copy.deepcopy(changes[start : start + max_results])
            while True:
                end = start + len(selected)
                more = end < len(changes)
                result = {
                    "status": "synced",
                    "authority": "gitea-issue",
                    "consumer": consumer,
                    "changes": selected,
                    "total_changes": len(changes),
                    "has_more": more,
                    "next_cursor": previous["offered_revision"] + ":" + str(end)
                    if more
                    else None,
                    "ack_revision": previous["offered_revision"] if not more else None,
                    "review_requests": [
                        c["task_id"] for c in selected if c.get("review_requested")
                    ],
                    "handoff_requests": [
                        c["task_id"] for c in selected if c.get("handoff_requested")
                    ],
                    "delivery": "observed on sync; no remote AI wakeup",
                }
                if (
                    len(json.dumps(result, ensure_ascii=False, sort_keys=True).encode())
                    + 1
                    <= budget
                ):
                    break
                if len(selected) > 1:
                    selected.pop()
                elif selected and not selected[0].get("details_omitted"):
                    selected = [
                        {
                            k: selected[0][k]
                            for k in ("task_id", "status")
                            if k in selected[0]
                        }
                    ]
                    selected[0]["details_omitted"] = True
                else:
                    raise GiteaError(
                        "sync summary exceeds output budget; increase max-output-bytes"
                    )
            previous["read_until"] = max(previous["read_until"], end)
            previous["can_ack"] = previous["read_until"] >= len(changes)
            _atomic_write(path, previous)
            return result
