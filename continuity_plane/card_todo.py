"""Todo as a live card projection, retaining legacy queues for single-agent use."""

from .collaboration_registry import resolve_project, list_tasks, update_task, _TASK_STATUS

OPEN_STATES = ("queued", "active", "dispatched", "blocked", "ready-for-review")

MAP = {
    "queued": "queued",
    "active": "active",
    "dispatched": "active",
    "blocked": "blocked",
    "ready-for-review": "completed",
    "completed": "completed",
    "merged": "completed",
    "rejected": "completed",
    "superseded": "completed",
}


def project(document, root, data_root):
    config = document["card_projection"]
    tasks = list_tasks(data_root, project_id=config["project_id"])
    ids = config.get("task_ids")
    tasks = [
        t
        for t in tasks
        if (ids is None or t["task_id"] in ids)
        and (not config.get("assignee") or t["assignee"] == config["assignee"])
        and (not config.get("lane_id") or t["lane_id"] == config["lane_id"])
        and (config.get("statuses") is None or t["status"] in config["statuses"])
    ]
    if ids is not None:
        tasks.sort(key=lambda t: ids.index(t["task_id"]))
    view = dict(document)
    # Migration history remains on disk, not in every bounded planning read.
    view.pop("legacy_items", None)
    view["items"] = [
        dict(
            item_id="card:" + t["task_id"],
            task_id=t["task_id"],
            title=t["title"],
            status=MAP[t["status"]],
            task_status=t["status"],
            result=t.get("result"),
            order=i + 1,
        )
        for i, t in enumerate(tasks)
    ]
    view["status"] = (
        "active"
        if any(t["status"] in {"active", "queued"} for t in view["items"])
        else (
            "blocked"
            if any(t["status"] == "blocked" for t in view["items"])
            else "completed"
        )
    )
    view["source"] = "task-cards"
    return view


def enable(root, data_root=None, *, assignee=None, lane_id=None, linked_only=False, statuses=None, include_closed=False):
    from .intent_todo import load_queue, _write, queue_path, SCHEMA_VERSION, _now

    if statuses is not None and (
        not isinstance(statuses, (list, tuple)) or not statuses
        or any(s not in _TASK_STATUS for s in statuses)
    ):
        raise ValueError("statuses must be a nonempty list of task states")
    if include_closed and statuses is not None:
        raise ValueError("choose --all or --status, not both")
    selected = None if include_closed else list(dict.fromkeys(statuses or OPEN_STATES))
    registration = resolve_project(root, data_root)
    if registration is None:
        raise ValueError("project must be registered before projecting cards")
    root = registration["control_root"]
    previous = load_queue(root, data_root) or {}
    ids = (
        [i["task_id"] for i in previous.get("items", []) if i.get("task_id")]
        if linked_only
        else None
    )
    if linked_only and (
        not ids or any(not i.get("task_id") for i in previous.get("items", []))
    ):
        raise ValueError(
            "link every existing Todo item to a card before using --linked-only"
        )
    document = {
        "schema_version": SCHEMA_VERSION,
        "queue_id": "cards:" + registration["project_id"],
        "root": str(root),
        "items": [],
        "created_at": previous.get("created_at", _now()),
        "updated_at": _now(),
        "owner_session_id": previous.get("owner_session_id"),
        "plan_binding": previous.get("plan_binding", {}),
        "card_projection": dict(
            project_id=registration["project_id"],
            assignee=assignee,
            lane_id=lane_id,
            task_ids=ids,
            statuses=selected,
        ),
        "legacy_items": previous.get("legacy_items", previous.get("items", [])),
    }
    view = project(document, root, data_root)
    _write(queue_path(root, data_root), document)
    return view


def transition(
    document,
    root,
    item_id,
    *,
    data_root=None,
    status,
    result=None,
    reason=None,
    assignee=None,
):
    from .intent_todo import load_queue

    item = next((i for i in document["items"] if i["item_id"] == item_id), None)
    if item is None:
        raise ValueError("Todo item does not exist in the card projection")
    update_task(
        data_root,
        project_id=document["card_projection"]["project_id"],
        task_id=item["task_id"],
        status=status,
        result=result,
        blocked_reason=reason,
        assignee=assignee,
    )
    return load_queue(root, data_root)


def append_actions(root, document, values, data_root=None):
    import hashlib
    import json
    import re
    from .collaboration_registry import add_task
    from .intent_todo import queue_path, _write, load_queue

    project_id = document["card_projection"]["project_id"]
    existing = list_tasks(data_root, project_id=project_id)
    normalize = lambda text: re.sub(r"\s+", " ", text).strip().casefold()
    by_title = {normalize(t["title"]): t for t in existing}
    ids = document["card_projection"].get("task_ids")
    for title in values:
        card = by_title.get(normalize(title))
        if card is None:
            task = (
                "intake-" + hashlib.sha256(normalize(title).encode()).hexdigest()[:20]
            )
            card = add_task(
                data_root,
                project_id=project_id,
                task_id=task,
                lane_id="intake",
                mode="patch",
                priority="p1",
                title=title,
                objective=title,
                next_action="Reconcile with the latest user instruction, implement this scoped action, and verify it",
                exit_criteria=["Verify the requested outcome and attach the evidence"],
                body=title,
            )
            by_title[normalize(title)] = card
        if ids is not None and card["task_id"] not in ids:
            ids.append(card["task_id"])
    path = queue_path(root, data_root)
    raw = json.loads(path.read_text())
    raw["card_projection"] = document["card_projection"]
    _write(path, raw)
    return load_queue(root, data_root)
