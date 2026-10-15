"""Conservative transient classification and explicit, shell-free resume argv."""

import math
import re
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path

_POLICY_REFUSAL = re.compile(
    r"cyber[_ -]?policy|content[_ -]?(?:filter|policy)|policy[_ -]?(?:refusal|violation)|safety[_ -]?refusal|可能有网络安全风险",
    re.I,
)


def policy_refusal(text):
    """Policy outcomes must not disappear when diagnostic tails are shortened."""
    return bool(_POLICY_REFUSAL.search(text))


def classify(text, *, now=None):
    now = now or datetime.now(UTC)
    policy = policy_refusal(text)
    text = text[-8192:]
    lower = text.casefold()
    result = {
        "failure_class": "permanent",
        "retryable": False,
        "retry_after_seconds": None,
        "available_at": None,
    }
    if policy:
        return {
            **result,
            "failure_class": "policy-review",
            "guidance": "Clarify legitimate defensive scope and authorization or request an independent review; do not evade provider safeguards.",
        }
    if re.search(
        r"\b(?:401|403)\b|unauthorized|invalid.api.key|permission denied", lower
    ):
        return {**result, "failure_class": "authentication"}
    for pattern, kind in (
        (r"\b429\b|rate.limit|too many requests", "rate-limit"),
        (r"\b(?:502|503|504)\b|bad gateway|temporarily unavailable", "gateway"),
        (r"model.{0,40}(?:capacity|overloaded)|at capacity|满载", "capacity"),
    ):
        if re.search(pattern, lower):
            result.update(failure_class=kind, retryable=True)
            break
    if not result["retryable"]:
        return result
    delay = None
    header = re.search(r"retry-after\s*[:=]\s*([^\r\n]+)", text, re.I)
    if header:
        value = header[1].strip()
        try:
            delay = float(value)
        except ValueError:
            try:
                date = parsedate_to_datetime(value)
                if date.tzinfo:
                    delay = (date - now).total_seconds()
            except (TypeError, ValueError, OverflowError):
                pass
    if delay is None:
        reset = re.search(
            r"(?:reset|available|retry)[^\r\n]{0,30}(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:Z|[+-]\d{2}:\d{2}))",
            text,
            re.I,
        )
        if reset:
            try:
                delay = (
                    datetime.fromisoformat(reset[1].replace("Z", "+00:00")) - now
                ).total_seconds()
            except ValueError:
                pass
    if delay is not None and math.isfinite(delay) and delay >= 0:
        from datetime import timedelta

        delay = min(delay, 30 * 86400)
        result.update(
            retry_after_seconds=delay,
            available_at=(now + timedelta(seconds=delay)).isoformat(),
        )
    return result


def resume_argv(template, thread_file):
    if (
        not isinstance(template, list)
        or not template
        or not all(isinstance(v, str) and v for v in template)
    ):
        raise ValueError("resume argv must be a nonempty JSON string array")
    if not any("{thread_id}" in v for v in template):
        raise ValueError("resume argv must contain {thread_id}")
    if not thread_file:
        raise ValueError("resume requires --thread-file")
    path = Path(thread_file)
    with path.open("rb") as stream:
        value = stream.read(257)
    if len(value) > 256:
        raise ValueError("thread file exceeds 256 bytes")
    thread = value.decode("utf-8").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", thread):
        raise ValueError("thread file must contain one session identifier")
    return [v.replace("{thread_id}", thread) for v in template]
