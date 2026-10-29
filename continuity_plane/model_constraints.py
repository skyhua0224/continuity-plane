"""Per-card model constraints: a minimum capability tier and denied models.

Tiers are an operator label on a configured adapter (``worker configure
--tier``). Adapters without one use a small built-in mapping by model family;
unknown families are ``standard``. Dispatch, the queue and fallback all refuse a
model that the card does not allow, and say which models would be accepted.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .executor_routing import _model_alias

TIERS = ("light", "standard", "strong")
DEFAULT_TIER = "standard"
_DEFAULT_TIERS = {
    "opus": "strong",
    "sol": "strong",
    "grok": "strong",
    "sonnet": "standard",
    "astra": "standard",
    "glm": "standard",
    "luna": "light",
    "haiku": "light",
}
_MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}")


def validate(min_tier, deny_models):
    """Return a normalized constraint dict, or None when nothing is constrained."""
    if min_tier is not None and min_tier not in TIERS:
        raise ValueError(
            f"min_tier {min_tier!r} is invalid; valid tiers: {', '.join(reversed(TIERS))}"
        )
    deny = list(dict.fromkeys(deny_models or []))
    if len(deny) > 16 or any(
        not isinstance(m, str) or _MODEL_RE.fullmatch(m) is None for m in deny
    ):
        raise ValueError("deny_models needs up to 16 adapter or model names")
    if min_tier is None and not deny:
        return None
    return {"min_tier": min_tier, "deny_models": deny}


def tier_of(profiles, model):
    configured = (profiles.get(model) or {}).get("tier")
    if configured in TIERS:
        return configured
    return _DEFAULT_TIERS.get(_model_alias(model), DEFAULT_TIER)


def _denied(constraints, model):
    alias = _model_alias(model)
    return any(d == model or _model_alias(d) == alias for d in constraints["deny_models"])


def _below(constraints, profiles, model):
    minimum = constraints.get("min_tier")
    return bool(minimum) and TIERS.index(tier_of(profiles, model)) < TIERS.index(minimum)


def allowed(constraints, profiles, model):
    if not constraints:
        return True
    return not _denied(constraints, model) and not _below(constraints, profiles, model)


def load_profiles(data_root):
    path = Path(data_root) / "executor-adapters.json"
    try:
        value = json.loads(path.read_text()) if path.exists() else {}
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def require(constraints, profiles, model, task_id="this card"):
    """Raise a ValueError naming the violated constraint and the allowed models."""
    if allowed(constraints, profiles, model):
        return
    if _denied(constraints, model):
        why = f"model {model} is denied for {task_id} (deny_models: {', '.join(constraints['deny_models'])})"
    else:
        why = (
            f"model {model} ({tier_of(profiles, model)}) is below the min_tier "
            f"{constraints['min_tier']} required by {task_id}"
        )
    permitted = [
        f"{name} ({tier_of(profiles, name)})"
        for name in sorted(profiles)
        if allowed(constraints, profiles, name)
    ]
    raise ValueError(
        why + "; allowed models: " + (", ".join(permitted) or "none configured")
    )
