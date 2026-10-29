"""Conservative transient classification and explicit, shell-free resume argv."""

import json
import math
import re
from datetime import UTC, datetime, timedelta, timezone
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
    r"quota.{0,25}(?:exhausted|exceeded)|usage.{0,12}limit|(?:daily|weekly|monthly)[/ \w]{0,24}limit\s+(?:exhausted|exceeded|reached)|(?:token|message|request).{0,20}(?:quota|limit).{0,20}(?:exhausted|exceeded|reached)|you(?:'|’)ve hit your limit|额度.{0,10}(?:耗尽|用完)",
    re.I,
)


_RESET_AT = re.compile(
    r"(?:reset|available|retry)[^\r\n]{0,30}?"
    r"(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})(?:\.\d+)?"
    r"[\s\[(]*(Z\b|(?:UTC|GMT)\s*[+-]\d{1,2}(?::?\d{2})?|(?:UTC|GMT)\b|[+-]\d{2}:?\d{2})?",
    re.I,
)


def _reset_seconds(text, now):
    """Seconds until a stated reset instant; a timezone-less one is host-local time."""
    match = _RESET_AT.search(text)
    if not match:
        return None
    date, clock, zone = match.groups()
    try:
        moment = datetime.fromisoformat(f"{date}T{clock}")
        if zone is None:
            moment = moment.astimezone()
        else:
            zone = re.sub(r"(?i)utc|gmt|\s", "", zone)
            if zone in {"", "Z", "z"}:
                offset = timezone.utc
            else:
                sign = -1 if zone[0] == "-" else 1
                digits = zone[1:].replace(":", "")
                hours, minutes = (
                    (int(digits[:2]), int(digits[2:] or 0))
                    if len(digits) > 2
                    else (int(digits), 0)
                )
                offset = timezone(sign * timedelta(hours=hours, minutes=minutes))
            moment = moment.replace(tzinfo=offset)
    except (ValueError, OverflowError):
        return None
    # A reset that has already passed means the limit is available again.
    return max(0.0, (moment - now).total_seconds())


def _event_error_text(row):
    """Error text of one structured provider event; empty for tool/message events."""
    kind = row.get("type")
    item = row.get("item") if isinstance(row.get("item"), dict) else {}
    if kind in {"error", "turn.failed"} or (kind == "result" and row.get("is_error")):
        parts = (row.get("error"), row.get("message"), row.get("result"))
    elif kind == "item.completed" and item.get("type") == "error":
        parts = (item.get("message"),)
    elif row.get("error") and not str(kind).startswith("item."):
        # e.g. Claude assistant/system events flagged with an API error code
        parts = (row.get("error"),)
    else:
        return ""
    return "\n".join(
        v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
        for v in parts
        if v
    )


def provider_error_text(text):
    """Keep what the provider reported, never what a tool printed.

    Structured JSONL events are reduced to their terminal error payloads, so
    source code or logs a worker just read (``command_execution`` output, file
    contents, assistant messages) cannot be mistaken for the failure cause.
    Non-JSON lines are kept: plain adapters print their diagnostics that way.
    """
    kept = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("{"):
            try:
                row = json.loads(stripped)
            except ValueError:
                row = None
            if isinstance(row, dict):
                error = _event_error_text(row)
                if error:
                    kept.append(error)
                continue
        kept.append(line)
    return "\n".join(kept)


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
        delay = _reset_seconds(text, now)
    if delay is not None and math.isfinite(delay) and delay >= 0:
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
