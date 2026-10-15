"""Provider-neutral execution preferences; recommendations carry no authority."""

from __future__ import annotations

from typing import Any
import hashlib
import json
from .collaboration_registry import (
    project_data_dir,
    default_data_root,
    _locked,
    _atomic_write,
    list_tasks,
)

SCENARIOS = {
    "boundary-validation": {
        "purpose": "security",
        "description": "Validate authentication, authorization and error handling in the authorized repository and its tests.",
    },
    "independent-review": {
        "purpose": "independent-review",
        "description": "Independent verification of assumptions, correctness and failure cases within the declared scope.",
    },
    "quick-triage": {
        "urgency": "high",
        "description": "Bounded reproduction, log inspection or a small urgent fix.",
    },
    "implementation": {
        "description": "Ordinary scoped implementation with focused verification."
    },
    "security-boundary": {
        "purpose": "security",
        "description": "Authentication, authorization, unsafe inputs or security boundaries.",
    },
    "adversarial-review": {
        "purpose": "adversarial-review",
        "description": "Independent challenge of assumptions and proposed changes.",
    },
    "deep-design": {
        "complexity": "high",
        "urgency": "low",
        "description": "Difficult design or root-cause investigation with a flexible deadline.",
    },
    "independent-investigation": {
        "description": "Parallel investigation of separately verifiable domains."
    },
    "regression-verification": {
        "description": "Focused reproduction and validation of a scoped change."
    },
}


def _model_alias(value: str) -> str:
    import re

    normalized = value.casefold()
    for pattern, canonical in (
        (r"(?:^|[/_-])(?:gpt-6\.1-)?sol(?:$|[/_-])", "sol"),
        (r"(?:^|[/_-])(?:gpt-6-)?astra(?:$|[/_-])", "astra"),
        (r"(?:^|[/_-])sonnet(?:$|[/_-])", "sonnet"),
        (r"(?:^|[/_-])opus(?:$|[/_-])", "opus"),
        (r"(?:^|[/_-])glm(?:$|[/_-])", "glm"),
        (r"(?:^|[/_-])grok(?:$|[/_-])", "grok"),
    ):
        if re.search(pattern, normalized):
            return canonical
    value = value.casefold().strip()
    aliases = {
        "sonnet-high": "sonnet",
        "gpt-6.1-sol": "sol",
        "gpt-6-astra": "astra",
        "glm-5.3": "glm",
        "glm-5.3-flash": "glm",
    }
    return aliases.get(value, value)


def recommend(
    *,
    complexity: str | None = None,
    urgency: str | None = None,
    blocked_model: str | None = None,
    domain: str | None = None,
    parallel_safe: bool = False,
    purpose: str | None = None,
    thinking: str = "high",
    scenario: str | None = None,
    busy_models: tuple[str, ...] | list[str] = (),
    unavailable_models: tuple[str, ...] | list[str] = (),
    eligible_models: tuple[str, ...] | list[str] | None = None,
) -> dict[str, Any]:
    if scenario is not None and scenario not in SCENARIOS:
        raise ValueError("scenario is invalid; use collaboration scenarios")
    preset = SCENARIOS.get(scenario, {})
    complexity = (
        complexity if complexity is not None else preset.get("complexity", "normal")
    )
    urgency = urgency if urgency is not None else preset.get("urgency", "normal")
    purpose = purpose if purpose is not None else preset.get("purpose", "general")
    if complexity not in {"normal", "high"} or urgency not in {"normal", "high", "low"}:
        raise ValueError("complexity or urgency is invalid")
    if purpose not in {
        "general",
        "security",
        "adversarial-review",
        "independent-review",
    }:
        raise ValueError("purpose is invalid")
    if thinking not in {"high", "max"}:
        raise ValueError("thinking must be high or max")
    security_review = purpose == "security" or (
        purpose in {"adversarial-review", "independent-review"}
        and (domain or "").casefold()
        in {"security", "identity", "authorization", "auth"}
    )
    if purpose in {"adversarial-review", "independent-review"} and not security_review:
        preferred, fallback, reason = (
            ["astra", "opus"],
            ["sonnet", "sol", "grok", "glm"],
            "Difficult design or independent verification; reserve expensive models for this class.",
        )
    elif security_review:
        preferred, fallback, reason = (
            ["glm", "sonnet", "sol", "grok"],
            [],
            "Balance suitable general executors; GLM wins security-boundary ties.",
        )
    elif complexity == "high" and urgency != "high":
        preferred, fallback, reason = (
            ["astra", "opus"],
            ["sonnet", "sol", "grok", "glm"],
            "Difficult work with a flexible deadline; balance the specialist pool.",
        )
    elif urgency == "high":
        preferred, fallback, reason = (
            ["grok", "sonnet", "sol", "glm"],
            [],
            "Balance suitable general executors; Grok wins short-turnaround ties.",
        )
    else:
        preferred, fallback, reason = (
            ["sonnet", "sol", "grok", "glm"],
            [],
            "Balance routine work across all four suitable general executors.",
        )
    blocked = _model_alias(blocked_model) if blocked_model else None
    unavailable = {_model_alias(model) for model in unavailable_models}
    if blocked:
        unavailable.add(blocked)
    busy = {_model_alias(model) for model in busy_models} - unavailable
    if "sol" in unavailable and "sol" in preferred:
        preferred = ["glm"] + [model for model in preferred if model != "sol"]
    eligible = (
        {_model_alias(model) for model in eligible_models}
        if eligible_models is not None
        else None
    )
    if eligible is not None:
        preferred = [model for model in preferred if model in eligible]
        fallback = [model for model in fallback if model in eligible]
    preferred = [model for model in preferred if model not in unavailable]
    candidates = list(
        dict.fromkeys(preferred + [m for m in fallback if m not in unavailable])
    )
    candidates = [m for m in candidates if m not in busy] + [
        m for m in candidates if m in busy
    ]
    preferred = [model for model in preferred if model not in busy]
    if not preferred:
        preferred = [model for model in candidates if model not in busy] or candidates[
            :1
        ]
    return {
        "schema_version": "context.executor-recommendation/v1alpha1",
        "candidates": candidates,
        "preferred_pool": preferred,
        "thinking_levels": {
            model: "max"
            if model == "glm"
            else thinking
            if model in {"sonnet", "sol", "grok"}
            else "high"
            for model in candidates
        },
        "routing_inputs": {
            "complexity": complexity,
            "urgency": urgency,
            "blocked_model": blocked,
            "purpose": purpose,
            "thinking": thinking,
            "scenario": scenario,
            "busy_models": sorted(busy),
            "unavailable_models": sorted(unavailable),
            "eligible_models": sorted(eligible) if eligible is not None else None,
        },
        "balance_basis": "saved-task-recommendations; not measured usage or automatic dispatch",
        "reason": reason,
        "domain": domain,
        "parallel_allowed": parallel_safe,
        "split_mode": "independent-subtask" if unavailable or busy else "current-task",
        "adaptation": {
            "preset_is_advisory": True,
            "load_source": "caller-reported; not a live queue measurement",
            "reconsider_on": [
                "queue-delay",
                "capacity",
                "timeout",
                "failed-validation",
            ],
            "when_congested": "Consider independent scoped subtasks or handoff to another suitable candidate.",
            "split_requires": [
                "independent-verification",
                "isolated-worktree",
                "non-overlapping-writes",
            ],
            "keep_whole_when": "Shared state, coupled evidence or coordination cost exceeds parallel benefit.",
            "preserve": [
                "task-plan",
                "completed-evidence",
                "scope-and-effect-authority",
            ],
        },
        "handoff_required": [
            "task_id",
            "worktree",
            "current_head",
            "next_action",
            "verification",
        ],
        "dispatch_authority": False,
        "host_independent": True,
        "effort_requires_adapter_validation": True,
        "fallback_requires_available_executor": True,
        "ordinary_work_continues": True,
        "notes": "Do not repeat completed work, change task priority, or silently merge/deploy during a model handoff.",
    }


def save_for_task(data_root, project_id: str, task_id: str, preference: dict) -> dict:
    if not any(
        t["task_id"] == task_id for t in list_tasks(data_root, project_id=project_id)
    ):
        raise ValueError("task_id does not exist")
    path = (
        project_data_dir(data_root or default_data_root(), project_id)
        / "executor-routing.json"
    )
    with _locked(path):
        document = json.loads(path.read_text()) if path.exists() else {}
        previous = document.get(task_id)
        # Repeated saves of an unchanged request do not consume another allocation.
        if previous and all(
            previous.get(k) == preference.get(k)
            for k in ("routing_inputs", "preferred_pool", "domain", "parallel_allowed")
        ):
            from .runtime_routing import apply

            return apply(data_root, project_id, previous)
        preference = dict(preference)
        pool = preference.get("preferred_pool", preference["candidates"][:1])
        counts = {model: 0 for model in pool}
        for other_id, other in document.items():
            if other_id != task_id and other.get("candidates"):
                model = other["candidates"][0]
                if model in counts:
                    counts[model] += 1
        ordered = sorted(pool, key=lambda model: counts[model])
        preference["candidates"] = ordered + [
            m for m in preference["candidates"] if m not in pool
        ]
        preference["allocation"] = {
            "counts_before": counts,
            "selected": ordered[0] if ordered else None,
            "policy": "least-planned-first; suitability-breaks-ties",
        }
        from .runtime_routing import apply

        preference = apply(data_root, project_id, preference)
        document[task_id] = preference
        _atomic_write(path, document)
    return preference


def for_task(data_root, project_id: str, task_id: str | None) -> dict:
    path = (
        project_data_dir(data_root or default_data_root(), project_id)
        / "executor-routing.json"
    )
    try:
        document = json.loads(path.read_text())
    except (OSError, ValueError):
        document = {}
    if task_id in document:
        from .runtime_routing import apply

        return apply(data_root, project_id, document[task_id])
    value = recommend()
    if task_id:
        # Unconfigured packet readers also avoid a universal Sonnet-first default.
        # This is a stable tie-break, not an assignment or a quota reservation.
        pool = value["preferred_pool"]
        offset = int(hashlib.sha256(task_id.encode()).hexdigest(), 16) % len(pool)
        rotated = pool[offset:] + pool[:offset]
        counts = {model: 0 for model in pool}
        for other in document.values():
            candidates = other.get("candidates", [])
            if candidates and candidates[0] in counts:
                counts[candidates[0]] += 1
        value["candidates"] = sorted(rotated, key=lambda model: counts[model]) + [
            model for model in value["candidates"] if model not in pool
        ]
    from .runtime_routing import apply

    return apply(data_root, project_id, value)
