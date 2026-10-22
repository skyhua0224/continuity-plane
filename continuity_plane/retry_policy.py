"""Conservative transient classification and explicit, shell-free resume argv."""

import math
import re
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path

_STRONG_POLICY_REFUSAL = re.compile(
    r"cyber[_ -]?policy|content[_ -]?(?:filter|policy)|safety[_ -]?refusal|可能有网络安全风险",
    re.I,
)
_GENERIC_POLICY_REFUSAL = re.compile(r"policy[_ -]?(?:refusal|violation)", re.I)
_TRANSPORT_OR_AUTH = re.compile(
    r"\b(?:401|403)\b|unauthorized|invalid.api.key|permission denied|"
    r"cloudflare|cf-ray|attention.required|forbidden|"
    r"(?:https?[_ -]?)?proxy(?:[_ -](?:error|failure|blocked))?|"
    r"upstream[_ -]?(?:reset|blocked|denied)|bad gateway|gateway error|"
    r"intercept(?:ed|ion)",
    re.I,
)
_QUOTA_EXHAUSTED = re.compile(
    r"quota.{0,25}(?:exhausted|exceeded)|usage.{0,12}limit|(?:token|message|request).{0,20}(?:quota|limit).{0,20}(?:exhausted|exceeded|reached)|you(?:'|’)ve hit your limit|额度.{0,10}(?:耗尽|用完)",
    re.I,
)


def quota_exhausted(text):
    """Recognise an account/usage limit before generic policy wording."""
    return bool(_QUOTA_EXHAUSTED.search(text[-8192:]))


def policy_refusal(text):
    """Policy outcomes must not disappear when diagnostic tails are shortened."""
    text = text[-8192:]
    if _STRONG_POLICY_REFUSAL.search(text):
        return True
    # HTTP authentication failures, Cloudflare/proxy interception pages and
    # gateway diagnostics frequently contain generic words such as "policy"
    # or "violation". Only the strong, model-specific markers above are
    # sufficient to treat those responses as content refusals.
    if _TRANSPORT_OR_AUTH.search(text):
        return False
    # Provider quota errors sometimes say "usage policy" or "policy violation".
    # A clear quota signal is a temporary availability condition, not a safety
    # refusal, unless one of the strong safety patterns above is also present.
    return bool(_GENERIC_POLICY_REFUSAL.search(text)) and not quota_exhausted(text)


def classify(text, *, now=None):
    now = now or datetime.now(UTC)
    text = text[-8192:]
    lower = text.casefold()
    policy = policy_refusal(text)
    quota = quota_exhausted(text)
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
        r"\b(?:401|403)\b|unauthorized|invalid.api.key|permission denied|"
        r"cloudflare|cf-ray|attention.required|forbidden|proxy|intercept",
        lower,
    ):
        return {
            **result,
            "failure_class": "authentication",
            "resume_same_session": True,
            "requires_new_session": False,
            "guidance": "HTTP 401/403, Cloudflare/proxy interception and authentication failures are not content-policy refusals. Correct the route or credentials before explicitly resuming the recorded session; do not start from zero.",
        }
    if re.search(
        r"upstream_reset_replay_refused|replay[_ -]refused|upstream[_ -]reset", lower
    ):
        return {
            **result,
            "failure_class": "upstream-reset",
            "requires_new_session": True,
            "guidance": "Upstream reset/replay refusal is not a quota reset. Stop same-thread retries; preserve work and explicitly review a new session or an authorized model handoff.",
        }
    if quota:
        result.update(
            failure_class="quota-exhausted",
            retryable=True,
            resume_same_session=True,
            requires_new_session=False,
            guidance="Pause this origin/model until the provider reset, then resume the recorded session; do not invent a reset time or start from zero.",
        )
    for pattern, kind in (
        ()
        if quota
        else (
            (r"\b429\b|rate.limit|too many requests", "rate-limit"),
            (r"\b(?:502|503|504)\b|bad gateway|temporarily unavailable", "gateway"),
            (r"model.{0,40}(?:capacity|overloaded)|at capacity|满载", "capacity"),
        )
    ):
        if re.search(pattern, lower):
            result.update(failure_class=kind, retryable=True)
            break
    if not result["retryable"] and not quota:
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
