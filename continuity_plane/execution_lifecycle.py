"""Project-local run bindings and replayable card projections, never State claims."""

from pathlib import Path

from .collaboration_registry import (
    CollaborationRegistryError,
    _ASSIGNEE_RE,
    _atomic_write,
    _ledger_path,
    _load_ledger,
    _locked,
    _now,
    _record_change,
    default_data_root,
)


class ExecutionBindingError(CollaborationRegistryError):
    """An execution was rejected before its child command could start."""


def bind_execution(
    data_root,
    *,
    project_id,
    task_id,
    assignee,
    attempt_id,
    expected_previous=None,
    require_idle=False,
    expected_scope=None,
    expected_updated_at=None,
    model=None,
):
    if not isinstance(attempt_id, str) or not attempt_id or len(attempt_id) > 128:
        raise CollaborationRegistryError("attempt_id must contain 1–128 characters")
    if not isinstance(assignee, str) or _ASSIGNEE_RE.fullmatch(assignee) is None:
        raise CollaborationRegistryError("assignee is invalid")
    root = Path(data_root or default_data_root())
    path = _ledger_path(root, project_id)
    with _locked(path):
        ledger = _load_ledger(root, project_id)
        card = next((row for row in ledger["tasks"] if row["task_id"] == task_id), None)
        if card is None:
            raise CollaborationRegistryError(
                "task_id does not exist; create the card before starting a worker"
            )
        if card["assignee"] not in (None, assignee):
            raise ExecutionBindingError("task is assigned to another executor")
        if card["status"] in {
            "merged",
            "completed",
            "accepted",
            "rejected",
            "superseded",
        }:
            raise ExecutionBindingError(
                "task is closed; create a successor card for another run"
            )
        bindings = ledger.setdefault("execution_bindings", {})
        previous = bindings.get(task_id, {})
        from .delivery_records import scope_hash

        scope_sha = scope_hash(
            card, ledger.get("task_details", {}).get(task_id, {}).get("body")
        )
        if (expected_scope is not None and expected_scope != scope_sha) or (
            expected_updated_at is not None and expected_updated_at != card["updated_at"]
        ):
            raise ExecutionBindingError(
                "task changed after prompt preparation; rebuild the dispatch from the current card"
            )
        if (
            expected_previous is not None
            and previous.get("attempt_id") != expected_previous
        ):
            raise ExecutionBindingError(
                "attempt was superseded; the older supervisor must not retry or reclaim it"
            )
        if (
            expected_previous is not None
            and previous.get("scope_sha256")
            and previous["scope_sha256"] != scope_sha
        ):
            raise ExecutionBindingError(
                "task scope changed; rebuild the assigned prompt before retrying"
            )
        if require_idle and previous.get("status") == "running":
            raise ExecutionBindingError(
                "task already has running attempt "
                + str(previous.get("attempt_id"))
                + "; inspect the actual worker or use another isolated task/worktree"
            )
        used = list(previous.get("used_attempt_ids", []))
        if previous.get("attempt_id") and previous["attempt_id"] not in used:
            used.append(previous["attempt_id"])
        if attempt_id in used:
            raise CollaborationRegistryError(
                "attempt_id was already used; use a new attempt id"
            )
        card["assignee"] = assignee
        if card["status"] != "active":
            card["status"] = "dispatched"
        card["blocked_reason"] = None
        card["updated_at"] = _now()
        bindings[task_id] = {
            "attempt_id": attempt_id,
            "assignee": assignee,
            "status": "running",
            "used_attempt_ids": used + [attempt_id],
            "scope_sha256": scope_sha,
            "model": model,
        }
        ledger["updated_at"] = _now()
        _atomic_write(path, ledger)


def finish_execution(
    data_root,
    *,
    project_id,
    task_id,
    assignee,
    attempt_id,
    status,
    return_code,
    reason=None,
    event_prefix="attempt",
    replay=False,
    report=None,
    **details,
):
    if status not in {"completed", "failed", "interrupted"}:
        raise CollaborationRegistryError("execution finish status is invalid")
    root = Path(data_root or default_data_root())
    path = _ledger_path(root, project_id)
    with _locked(path):
        ledger = _load_ledger(root, project_id)
        card = next((row for row in ledger["tasks"] if row["task_id"] == task_id), None)
        binding = ledger.get("execution_bindings", {}).get(task_id, {})
        current = (
            binding.get("attempt_id") == attempt_id
            and binding.get("assignee") == assignee
        )
        if replay and not (current and binding.get("status") == "running"):
            return {
                "card_status": card["status"] if card else None,
                "card_projection": "already-applied",
            }
        projection = "ignored-stale"
        if current:
            if binding.get("status") != "running":
                return {
                    "card_status": card["status"] if card else None,
                    "card_projection": "already-applied",
                }
            projection = "preserved"
            if (
                card
                and card["assignee"] == assignee
                and card["status"] in {"active", "dispatched"}
            ):
                card["status"] = (
                    "ready-for-review" if status == "completed" else "blocked"
                )
                card["blocked_reason"] = (
                    None if status == "completed" else (reason or status)[:1024]
                )
                if status == "completed" and card["result"] is None:
                    card["result"] = (
                        "Worker exited successfully; verification and review remain pending."
                    )
                card["updated_at"] = _now()
                projection = "updated"
            # Evidence delivery is independent of status projection. A worker
            # may already have submitted/completed its card before exiting.
            # Only the current attempt and unchanged executor may attach it.
            if report and card and card["assignee"] == assignee:
                import hashlib

                text = report.get("text", "")
                if isinstance(text, str) and 0 < len(text.encode()) <= 65536:
                    ledger.setdefault("task_details", {}).setdefault(task_id, {})[
                        "report"
                    ] = {
                        **report,
                        "bytes": len(text.encode()),
                        "sha256": hashlib.sha256(text.encode()).hexdigest(),
                        "attempt_id": attempt_id,
                        "commit": card.get("commit"),
                        "recorded_at": _now(),
                    }
        result = {
            "card_status": card["status"] if card else None,
            "card_projection": projection,
        }
        # Journal first. If the ledger write fails, daemon replay reapplies this
        # projection. Stable event IDs make that replay idempotent.
        _record_change(
            root,
            project_id,
            event_prefix + ".finished",
            task_id,
            event_id=f"{event_prefix}:{task_id}:{attempt_id}:finished",
            attempt_id=attempt_id,
            assignee=assignee,
            status=status,
            return_code=return_code,
            reason=reason,
            finished_at=details.pop("finished_at", _now()),
            card_status=card["status"] if card else None,
            card_projection="pending"
            if current and projection == "updated"
            else projection,
            projection_target=result["card_status"]
            if projection == "updated"
            else None,
            **details,
            **({"report": report} if report else {}),
        )
        if current:
            binding["status"] = status
            binding["reason"] = reason
            ledger["updated_at"] = _now()
            _atomic_write(path, ledger)
            _record_change(
                root,
                project_id,
                "task.run-projected",
                task_id,
                event_id=f"{event_prefix}:{task_id}:{attempt_id}:projected",
                attempt_id=attempt_id,
                assignee=assignee,
                **result,
            )
        return result
