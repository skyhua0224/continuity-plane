"""Owner notification channel: bounded summaries, buttons and replies.

The channel never runs shell commands and holds no controller authority. It
reads collaboration metadata, delivers messages addressed to the owner, and
stores what the owner sends as inbox messages for the configured recipient.
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import secrets
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
import re
import shlex
import subprocess
import time
import uuid
from typing import Any, Callable, Protocol
import urllib.error
import urllib.parse
import urllib.request

from .collaboration_inbox import acknowledge, list_messages, send_message
from .collaboration_registry import (
    CollaborationRegistryError,
    _atomic_write,
    _load,
    _record_change,
    add_task,
    default_data_root,
    latest_change_cursor,
    list_changes,
    list_tasks,
    update_task,
)
from . import owner_questions
from . import owner_sinks
from .project_context import _load as _load_project_context, _path as _project_context_path
from .vocabulary_memory import _SECRET_RE

CONFIG_SCHEMA_VERSION = "context.owner-channel-config/v1alpha1"
STATE_SCHEMA_VERSION = "context.owner-channel-state/v1alpha1"
OWNER_RECIPIENT = "owner"
CHANNEL_SENDER = "owner-telegram"
STATUS_LABELS = {
    "active": ("🟢", "进行中"),
    "dispatched": ("🚀", "已派发"),
    "blocked": ("⛔", "阻塞"),
    "conflict": ("⚠️", "冲突"),
    "queued": ("⏳", "排队"),
    "ready-for-review": ("👀", "待审"),
}
OPEN_STATUSES = tuple(STATUS_LABELS)
OWNER_ASSIGNEES = ("owner", "user")
OWNER_LANE = "owner-actions"
STALE_REVIEW = timedelta(days=7)
REMIND_AFTER = timedelta(hours=4)
DEFAULT_NEXT_ACTION = "Read the card body and start"
CARD_MEMORY = 500
MAX_APPROVAL_ITEMS = 6
WAKE_TIMEOUT_SECONDS = 30
ALERT_EVENTS = {
    "job.failed": "失败",
    "job.interrupted": "被中断",
    "job.lost": "进程丢失",
}
SESSION_EVENTS = ("session.quota-wait", "session.stop-failed")
SESSION_ERRORS = {
    "authentication_failed": "认证失败",
    "billing_error": "账单问题",
    "overloaded": "服务过载",
    "quota_auto_resume_disabled": "额度用完，已放弃等待",
    "quota_auto_resume_stale": "额度已恢复，等待手动继续",
    "rate_limit": "撞到限额",
    "server_error": "服务端错误",
}
FINISHED_STATUSES = ("completed", "merged", "ready-for-review")
ALERT_COOLDOWN = timedelta(hours=6)
DIGEST_EVERY = timedelta(hours=1)
QUIET_HOURS = (23, 8)
CHANGE_PAGES = 20
IMAGE_SUFFIXES = (".jpeg", ".jpg", ".png", ".webp")
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_IMAGES = 4
DEFAULT_APPROVAL_HOURS = 24
MIN_INTERVAL_SECONDS = 300
BIND_TIMEOUT_SECONDS = 600
HEALTHY_POLL_AGE = timedelta(minutes=3)
RISK_LABELS = {"low": "低", "medium": "中", "high": "高"}
STATUS_WORDS = {
    "进行中": "active",
    "阻塞": "blocked",
    "排队": "queued",
    "待审": "ready-for-review",
    "旧卡": "old",
}
_PATH_RE = re.compile(r"(?<![\w.])(?:~|/)[\w.@-]+(?:/[\w.@-]+)+/?")
MAX_TEXT_CHARS = 3900
MAX_CALLBACK_BYTES = 64
PAGE_SIZE = 8
COMMANDS = (
    ("status", "项目总览"),
    ("list", "当前任务，点标题看近况"),
    ("waiting", "等你处理的事"),
    ("report", "现在生成一次进度报告"),
    ("timers", "定时报告，例如 /every 09:00"),
    ("help", "使用说明"),
)


class OwnerChannelError(ValueError):
    """Raised for invalid channel configuration or delivery failures."""


class TelegramError(OwnerChannelError):
    def __init__(self, description: str, retry_after: int | None = None):
        super().__init__(description)
        self.retry_after = retry_after


class ChannelClient(Protocol):
    def get_updates(self, offset: int | None, timeout: int) -> list[dict[str, Any]]: ...

    def get_me(self) -> dict[str, Any]: ...

    def send_message(
        self,
        chat_id: int,
        text: str,
        keyboard: list[list[dict[str, str]]] | None = None,
        rich: bool = False,
    ) -> dict[str, Any]: ...

    def edit_message(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        keyboard: list[list[dict[str, str]]] | None = None,
        rich: bool = False,
    ) -> dict[str, Any]: ...

    def edit_buttons(
        self, chat_id: int, message_id: int, keyboard: list[list[dict[str, str]]]
    ) -> dict[str, Any]: ...

    def send_photo(self, chat_id: int, path: str, caption: str = "") -> dict[str, Any]: ...

    def answer_callback(self, callback_id: str, text: str = "") -> None: ...

    def set_commands(self, commands: tuple[tuple[str, str], ...]) -> None: ...


class TelegramClient:
    """Minimal Bot API client; errors never contain the token-bearing URL."""

    def __init__(self, token: str, *, base_url: str = "https://api.telegram.org"):
        self._url = f"{base_url.rstrip('/')}/bot{token}/"

    def _exchange(
        self, method: str, body: bytes, headers: dict[str, str], timeout: float
    ) -> Any:
        request = urllib.request.Request(self._url + method, data=body, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as reply:
                document = json.load(reply)
        except urllib.error.HTTPError as error:
            with error:
                try:
                    document = json.load(error)
                except ValueError:
                    raise TelegramError(f"{method} failed with HTTP {error.code}") from None
        except (OSError, ValueError) as error:
            raise TelegramError(f"{method} failed: {type(error).__name__}") from None
        if not document.get("ok"):
            parameters = document.get("parameters") or {}
            raise TelegramError(
                str(document.get("description") or f"{method} failed"),
                parameters.get("retry_after"),
            )
        return document.get("result")

    def _call(self, method: str, request_timeout: float = 20, **fields: Any) -> Any:
        body = urllib.parse.urlencode(
            {
                key: value if isinstance(value, (str, int)) else json.dumps(value)
                for key, value in fields.items()
                if value is not None
            }
        ).encode()
        return self._exchange(method, body, {}, request_timeout)

    def send_photo(self, chat_id, path, caption=""):
        boundary = uuid.uuid4().hex
        parts = []
        for name, value in (("chat_id", str(chat_id)), ("caption", caption)):
            parts.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'
                f"{value}\r\n".encode()
            )
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="photo"; '
            f'filename="evidence{Path(path).suffix}"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n".encode()
        )
        parts += [Path(path).read_bytes(), f"\r\n--{boundary}--\r\n".encode()]
        headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
        return self._exchange("sendPhoto", b"".join(parts), headers, 60)

    def get_me(self) -> dict[str, Any]:
        return self._call("getMe")

    def get_updates(self, offset: int | None, timeout: int) -> list[dict[str, Any]]:
        return (
            self._call(
                "getUpdates",
                request_timeout=timeout + 10,
                offset=offset,
                timeout=timeout,
                allowed_updates=["message", "callback_query"],
            )
            or []
        )

    @staticmethod
    def _format(rich: bool) -> dict[str, Any]:
        if not rich:
            return {}
        return {"parse_mode": "HTML", "link_preview_options": {"is_disabled": True}}

    def send_message(self, chat_id, text, keyboard=None, rich=False):
        return self._call(
            "sendMessage",
            chat_id=chat_id,
            text=text,
            reply_markup={"inline_keyboard": keyboard} if keyboard else None,
            **self._format(rich),
        )

    def edit_message(self, chat_id, message_id, text, keyboard=None, rich=False):
        return self._call(
            "editMessageText",
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            reply_markup={"inline_keyboard": keyboard or []},
            **self._format(rich),
        )

    def edit_buttons(self, chat_id, message_id, keyboard):
        return self._call(
            "editMessageReplyMarkup",
            chat_id=chat_id,
            message_id=message_id,
            reply_markup={"inline_keyboard": keyboard},
        )

    def answer_callback(self, callback_id, text=""):
        self._call("answerCallbackQuery", callback_query_id=callback_id, text=text or None)

    def set_commands(self, commands):
        self._call(
            "setMyCommands",
            commands=[{"command": name, "description": text} for name, text in commands],
        )


def _root(data_root: Path | str | None) -> Path:
    return Path(data_root) if data_root is not None else default_data_root()


def config_path(data_root: Path | str | None) -> Path:
    return _root(data_root) / "owner-channel" / "config.json"


def state_path(data_root: Path | str | None) -> Path:
    return _root(data_root) / "owner-channel" / "state.json"


def configure(
    data_root: Path | str | None,
    *,
    owner_id: int | None,
    token_file: str | None,
    projects: list[str],
    recipient: str,
    routes: dict[str, str] | None = None,
    evidence_roots: list[str] | None = None,
    default_timers: list[str] | None = None,
    wake_commands: dict[str, str] | None = None,
) -> dict[str, Any]:
    if not projects:
        raise OwnerChannelError("at least one project is required")
    if (owner_id is None) != (token_file is None) or (owner_id is not None and owner_id <= 0):
        raise OwnerChannelError("Telegram needs both a positive owner ID and a token file, or neither")
    previous = _load(config_path(data_root)) or {}
    if owner_id is None and previous.get("owner_id") and previous.get("token_file"):
        owner_id, token_file = int(previous["owner_id"]), previous["token_file"]
    document = {
        "schema_version": CONFIG_SCHEMA_VERSION,
        "owner_id": owner_id,
        "token_file": str(Path(token_file).expanduser()) if token_file else None,
        "sinks": list(previous.get("sinks") or []),
        "projects": projects,
        "recipient": recipient,
        "routes": routes or {},
        "evidence_roots": [str(Path(root).expanduser()) for root in evidence_roots or []],
        "default_timers": default_timers or [],
        "wake_commands": wake_commands or {},
    }
    if owner_id is not None and previous.get("bot_username") and token_file == previous.get("token_file"):
        document["bot_username"] = previous["bot_username"]
    path = config_path(data_root)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _atomic_write(path, document)
    return document


def load_config(data_root: Path | str | None) -> dict[str, Any]:
    document = _load(config_path(data_root))
    if document is None or document.get("schema_version") != CONFIG_SCHEMA_VERSION:
        raise OwnerChannelError("owner channel is not configured")
    return document


def read_token(token_file: str) -> str:
    try:
        return owner_sinks.read_secret(token_file, "token file")
    except owner_sinks.SinkError as error:
        raise OwnerChannelError(str(error)) from None


def _save_config(data_root: Path | str | None, document: dict[str, Any]) -> dict[str, Any]:
    path = config_path(data_root)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _atomic_write(path, document)
    return document


def add_sink(data_root: Path | str | None, *, name: str, url_file: str) -> dict[str, Any]:
    """Register a webhook sink; the URL stays in its private file."""
    config = load_config(data_root)
    name = owner_sinks.validate_name(name)
    url_file = str(Path(url_file).expanduser().resolve())
    owner_sinks.Sinks([{"name": name, "url_file": url_file}])  # fails on bad URL or missing extra
    sinks = [sink for sink in config.get("sinks") or [] if sink["name"] != name]
    if len(sinks) >= owner_sinks.MAX_SINKS:
        raise OwnerChannelError(f"at most {owner_sinks.MAX_SINKS} webhook sinks")
    config["sinks"] = [*sinks, {"name": name, "url_file": url_file}]
    _save_config(data_root, config)
    return {"sinks": [sink["name"] for sink in config["sinks"]]}


def remove_sink(data_root: Path | str | None, *, name: str) -> dict[str, Any]:
    config = load_config(data_root)
    sinks = [sink for sink in config.get("sinks") or [] if sink["name"] != name]
    if len(sinks) == len(config.get("sinks") or []):
        raise OwnerChannelError("no webhook sink has that name")
    config["sinks"] = sinks
    _save_config(data_root, config)
    return {"sinks": [sink["name"] for sink in sinks]}


def test_sink(data_root: Path | str | None, *, name: str | None = None) -> dict[str, bool]:
    config = load_config(data_root)
    entries = [sink for sink in config.get("sinks") or [] if name in (None, sink["name"])]
    if not entries:
        raise OwnerChannelError("no webhook sink to test")
    return owner_sinks.Sinks(entries).send("Continuity 通知测试：这条消息说明 webhook 已接通。")


def unbind(data_root: Path | str | None) -> dict[str, Any]:
    """Stop using Telegram; webhook sinks and other settings stay."""
    config = load_config(data_root)
    config["owner_id"] = None
    config["token_file"] = None
    config.pop("bot_username", None)
    _save_config(data_root, config)
    _save_offset(data_root, None, None)
    return {"telegram": False, "sinks": [sink["name"] for sink in config.get("sinks") or []]}


def bind(
    data_root: Path | str | None,
    client: ChannelClient,
    *,
    token_file: str,
    confirm: Callable[[dict[str, Any]], bool],
    show: Callable[[str], None],
    code: str | None = None,
    clock: Callable[[], float] = time.monotonic,
    timeout_seconds: int = BIND_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Bind the owner who sends the one-time code to the bot in a private chat.

    Only a private-chat /start carrying the code counts. The terminal confirms
    the Telegram account before it is saved; the code is shown only there.
    """
    config = load_config(data_root)
    code = code or secrets.token_hex(4)
    me = client.get_me()
    username = me.get("username")
    link = f"https://t.me/{username}?start=bind-{code}" if username else None
    show(
        (f"打开 {link}，或" if link else "")
        + f"在与机器人的私聊里发送 /start bind-{code}（{timeout_seconds // 60} 分钟内有效）"
    )
    state = _read_state(data_root)
    # Update IDs are per bot: an offset from another bot would hide every update.
    offset = state.get("offset") if state.get("bot_id") == me.get("id") else None
    deadline = clock() + timeout_seconds
    while clock() < deadline:
        for update in client.get_updates(offset, 20):
            offset = update["update_id"] + 1
            message = update.get("message") or {}
            chat = message.get("chat") or {}
            sender = message.get("from") or {}
            if (
                chat.get("type") != "private"
                or chat.get("id") != sender.get("id")
                or (message.get("text") or "").strip() != f"/start bind-{code}"
            ):
                continue
            account = {
                "id": sender["id"],
                "username": sender.get("username"),
                "name": " ".join(filter(None, (sender.get("first_name"), sender.get("last_name")))),
            }
            _save_offset(data_root, offset, me.get("id"))
            if not confirm(account):
                client.send_message(sender["id"], "绑定已在终端取消。")
                return {"bound": False}
            config["owner_id"] = int(sender["id"])
            config["token_file"] = str(Path(token_file).expanduser().resolve())
            if username:
                config["bot_username"] = username
            _save_config(data_root, config)
            client.send_message(sender["id"], "已绑定。之后的通知、提问和审批会发到这里。")
            return {"bound": True, "owner_id": config["owner_id"], "bot": username}
    _save_offset(data_root, offset, me.get("id"))
    raise OwnerChannelError("no matching /start arrived before the code expired")


def _save_offset(data_root: Path | str | None, offset: int | None, bot_id: int | None) -> None:
    """Read-modify-write so bind never replaces the rest of the channel state."""
    path = state_path(data_root)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    state = _read_state(data_root)
    state["offset"] = offset
    state["bot_id"] = bot_id
    _atomic_write(path, state)


def _split_command(command: str) -> list[str]:
    if os.name != "nt":
        return shlex.split(command)
    words = shlex.split(command, posix=False)
    return [word[1:-1] if len(word) > 1 and word[0] == word[-1] == '"' else word for word in words]


def _clip(text: str) -> str:
    return text if len(text) <= MAX_TEXT_CHARS else text[: MAX_TEXT_CHARS - 1] + "…"


def _button(label: str, data: str) -> dict[str, str]:
    if len(data.encode("utf-8")) > MAX_CALLBACK_BYTES:
        raise OwnerChannelError("button value is too long")
    return {"text": label, "callback_data": data}


def _safe(text: Any, limit: int = 400) -> str:
    """Prepare card text for HTML: no secrets, no local paths, bounded length."""
    value = " ".join(str(text or "").split())
    if _SECRET_RE.search(value):
        return "（内容含敏感字段，已隐藏）"
    value = _PATH_RE.sub("‹路径›", value)
    if len(value) > limit:
        value = value[: limit - 1] + "…"
    return html.escape(value, quote=False)


def _safe_lines(text: Any, limit: int = 1400) -> str:
    lines = [_safe(line, limit) for line in str(text or "").splitlines()]
    return "\n".join(line for line in lines if line)[:limit]


def _declared_decisions(data_root: Path | str, project: str) -> list[str]:
    context = _load_project_context(_project_context_path(data_root, project))
    return list((context.get("handoff") or {}).get("pending_decisions") or [])


def _parse_card(body: str) -> dict[str, Any] | None:
    try:
        document = json.loads(body)
    except ValueError:
        return None
    if isinstance(document, dict) and document.get("owner_card") == 1:
        return document
    return None


def _parse_schedule(word: str) -> tuple[str, str | int] | None:
    """Return ("daily", "HH:MM") or ("interval", seconds) for one schedule word."""
    clock = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", word)
    if clock:
        return "daily", f"{int(clock[1]):02d}:{clock[2]}"
    span = re.fullmatch(r"(\d{1,4})(m|h)", word)
    if span:
        seconds = int(span[1]) * (60 if span[2] == "m" else 3600)
        if seconds >= MIN_INTERVAL_SECONDS:
            return "interval", seconds
    return None


def _next_daily(clock: str, now: datetime) -> datetime:
    hour, minute = (int(part) for part in clock.split(":"))
    local = now.astimezone()
    moment = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if moment <= local:
        moment += timedelta(days=1)
    return moment.astimezone(UTC)


def _schedule_label(timer: dict[str, Any]) -> str:
    if timer["kind"] == "daily":
        when = f"每天 {timer['value']}"
    elif timer["kind"] == "once":
        when = "一次性"
    else:
        seconds = int(timer["value"])
        when = f"每 {seconds // 3600} 小时" if seconds % 3600 == 0 else f"每 {seconds // 60} 分钟"
    return f"{when} · {timer.get('project') or '全部项目'}"


def _metric_line(metric: dict[str, Any]) -> str:
    label = _safe(metric["label"], 60)
    before = _safe(metric.get("before"), 40)
    after = _safe(metric.get("after"), 40)
    if not before:
        return f"• {label}：{after}"
    numbers = [re.match(r"-?\d+(?:\.\d+)?", str(metric.get(k) or "")) for k in ("before", "after")]
    change = ""
    if all(numbers) and float(numbers[0][0]):
        ratio = (float(numbers[1][0]) - float(numbers[0][0])) / float(numbers[0][0]) * 100
        change = f"（{ratio:+.1f}%）"
    return f"• {label}：{before} → {after}{change}"


def _parse_metric(text: str) -> dict[str, str]:
    label, _, values = text.partition("=")
    before, separator, after = values.partition("->")
    if not label or not values:
        raise OwnerChannelError("metric must look like label=before->after")
    if not separator:
        return {"label": label.strip(), "after": before.strip()}
    return {"label": label.strip(), "before": before.strip(), "after": after.strip()}


def _is_quiet(now: datetime) -> bool:
    hour = now.astimezone().hour
    return hour >= QUIET_HOURS[0] or hour < QUIET_HOURS[1]


def _ago(stamp: str | None, now: datetime | None = None) -> str:
    try:
        moment = datetime.fromisoformat(stamp or "")
    except ValueError:
        return "时间未知"
    seconds = ((now or datetime.now(UTC)) - moment).total_seconds()
    if seconds < 60:
        return "刚刚"
    if seconds < 3600:
        return f"{int(seconds // 60)} 分钟前"
    if seconds < 86400:
        return f"{int(seconds // 3600)} 小时前"
    return f"{int(seconds // 86400)} 天前"


def _card_key(project: str, task_id: str) -> str:
    return hashlib.sha256(f"{project}/{task_id}".encode()).hexdigest()[:12]


def _is_stale_review(task: dict[str, Any]) -> bool:
    if task["status"] != "ready-for-review":
        return False
    try:
        moment = datetime.fromisoformat(task["updated_at"])
    except ValueError:
        return True
    return datetime.now(UTC) - moment > STALE_REVIEW


class OwnerChannel:
    def __init__(
        self,
        client: ChannelClient,
        config: dict[str, Any],
        data_root: Path | str | None = None,
    ):
        self.client = client
        self.owner_id = int(config.get("owner_id") or 0)
        # Webhook-only channels cannot receive buttons or replies.
        self.interactive = bool(getattr(client, "interactive", True))
        self.projects = list(config["projects"])
        self.recipient = config["recipient"]
        self.data_root = data_root
        self.bot_username: str | None = config.get("bot_username")
        self.routes: dict[str, str] = dict(config.get("routes") or {})
        self.reminders: dict[str, str] = {}
        self.cards: dict[str, dict[str, Any]] = {}
        # Inbox message id -> what of its card already reached Telegram, so a retry after a transient error resumes
        # instead of resending the header and the images that already went out.
        self.delivery_progress: dict[str, dict[str, int]] = {}
        self.timers: list[dict[str, Any]] = []
        self.wake_commands = {
            project: _split_command(command)
            for project, command in (config.get("wake_commands") or {}).items()
        }
        self.evidence_roots = [Path(root).resolve() for root in config.get("evidence_roots") or []]
        self.cursors: dict[str, int] = {}
        self.alerted: dict[str, str] = {}
        self.finished: dict[str, list[str]] = {}
        self.digest_at: dict[str, str] = {}

    def resolve_username(self) -> None:
        """Links are optional: a failed lookup leaves plain titles."""
        try:
            self.bot_username = self.client.get_me().get("username")
        except TelegramError:
            self.bot_username = None

    def notify(
        self,
        text: str,
        *,
        project: str | None = None,
        options: list[str] | None = None,
    ) -> dict[str, Any]:
        if _SECRET_RE.search(text):
            raise OwnerChannelError("credential assignments must not leave this host")
        project = project or self.projects[0]
        if project not in self.projects:
            raise OwnerChannelError("project is outside the channel scope")
        keyboard = [
            [_button(option, f"a|{project}|{index}")]
            for index, option in enumerate(options or [])
        ]
        return self.client.send_message(
            self.owner_id, _clip(f"[{project}] {text}"), keyboard or None
        )

    def _remember(
        self,
        sent: dict[str, Any] | None,
        project: str,
        task_id: str | None = None,
        sender: str | None = None,
        **extra: Any,
    ) -> None:
        """Tie a delivered message to its origin so replies and buttons route back."""
        if not sent or "message_id" not in sent:
            return
        self.cards[str(sent["message_id"])] = {
            "project": project,
            "task_id": task_id,
            "sender": sender,
            **extra,
        }
        while len(self.cards) > CARD_MEMORY:
            self.cards.pop(next(iter(self.cards)))

    def wake(self, project: str, text: str) -> bool:
        """Start the project's agent session; the message arrives on stdin."""
        command = self.wake_commands.get(project)
        if not command:
            return False
        try:
            subprocess.run(
                command,
                input=text.encode(),
                capture_output=True,
                timeout=WAKE_TIMEOUT_SECONDS,
                check=True,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return True

    def _context(self, message: dict[str, Any] | None) -> dict[str, Any]:
        return self.cards.get(str((message or {}).get("message_id")), {})

    def deliver_pending(self) -> int:
        """Forward inbox messages addressed to the owner, then acknowledge them."""
        delivered = 0
        for project in self.projects:
            for item in list_messages(
                self.data_root, project_id=project, recipient=OWNER_RECIPIENT, limit=20
            ):
                try:
                    card = _parse_card(item["body"])
                    if card:
                        self._deliver_card(project, item, card)
                    else:
                        sent = self.notify(f"{item['sender']}：\n{item['body']}", project=project)
                        self._remember(sent, project, item.get("task_id"), item["sender"])
                except TelegramError:
                    raise
                except (CollaborationRegistryError, OwnerChannelError, KeyError, TypeError, ValueError):
                    self.client.send_message(
                        self.owner_id,
                        f"[{project}] {item['sender']} 发来一张无法显示的卡片，已跳过。",
                    )
                acknowledge(
                    self.data_root,
                    project_id=project,
                    recipient=OWNER_RECIPIENT,
                    message_id=item["message_id"],
                )
                self.delivery_progress.pop(str(item["message_id"]), None)
                delivered += 1
        return delivered

    def _custom_keyboard(
        self, buttons: list[dict[str, Any]] | None
    ) -> tuple[list[list[dict[str, str]]], list[dict[str, str]]]:
        rows: list[list[dict[str, str]]] = []
        stored: list[dict[str, str]] = []
        for button in (buttons or [])[:8]:
            label = str(button["label"])[:40]
            if button.get("url"):
                url = str(button["url"])
                if not url.startswith("https://"):
                    raise OwnerChannelError("button links must use https")
                rows.append([{"text": label, "url": url}])
            else:
                stored.append({"label": label, "value": str(button.get("value") or label)[:200]})
                rows.append([_button(label, f"b|{len(stored) - 1}")])
        return rows, stored

    def _open_questions(self, project: str) -> list[dict[str, Any]]:
        return owner_questions.list_questions(self.data_root, project_id=project)

    def deliver_questions(self) -> int:
        delivered = 0
        for project in self.projects:
            for item in self._open_questions(project):
                if item["delivered_at"] is None:
                    self._send_question(project, item)
                    delivered += 1
        return delivered

    def _send_question(self, project: str, item: dict[str, Any]) -> None:
        icon = "🔴" if item["urgency"] == "urgent" else "❓"
        expires = datetime.fromisoformat(item["expires_at"]).astimezone()
        lines = [f"<b>{icon} 需要你回答</b> · {_safe(project)}", ""]
        lines += [f"<b>{_safe_lines(item['question'])}</b>", ""]
        lines.append(f"<i>来自 {_safe(item['asker'], 60)} · 有效至 {expires:%m-%d %H:%M}</i>")
        if self.interactive:
            lines.append("<i>点选项，或回复这条消息写你的答案</i>")
        else:
            lines.append(
                f"<i>回答：continuity notify answer --project {_safe(project)} "
                f"--question-id {item['question_id']} --text 你的答案</i>"
            )
        keyboard = [
            [_button(option[:40], f"q|{index}")]
            for index, option in enumerate(item["options"])
        ]
        sent = self.client.send_message(
            self.owner_id, _clip("\n".join(lines)), keyboard or None, rich=True
        )
        self._remember(
            sent,
            project,
            item["task_id"],
            item["asker"],
            question_id=item["question_id"],
            options=item["options"],
        )
        owner_questions.mark_delivered(
            self.data_root, project_id=project, question_id=item["question_id"]
        )

    def _answer(self, context: dict[str, Any], text: str) -> dict[str, Any]:
        return owner_questions.answer(
            self.data_root,
            project_id=context["project"],
            question_id=context["question_id"],
            text=text,
            via=CHANNEL_SENDER,
        )

    def _usable_image(self, path: str) -> Path | None:
        """Only images under a configured evidence root may leave this host."""
        try:
            resolved = Path(path).resolve()
            size = resolved.stat().st_size
        except OSError:
            return None
        allowed = any(resolved.is_relative_to(root) for root in self.evidence_roots)
        if not allowed or resolved.suffix.lower() not in IMAGE_SUFFIXES:
            return None
        return resolved if 0 < size <= MAX_IMAGE_BYTES else None

    def send_evidence(self, project: str, images: list[str] | None, title: str, progress: dict[str, int] | None = None) -> int:
        requested = (images or [])[:MAX_IMAGES]
        sent = 0
        done = (progress or {}).get("images", 0)
        for index, path in enumerate(requested, 1):
            if index <= done:
                sent += 1
                continue
            usable = self._usable_image(str(path))
            if usable is None:
                continue
            caption = f"[{project}] {title} · 验收截图 {index}/{len(requested)}"[:200]
            try:
                result = self.client.send_photo(self.owner_id, str(usable), caption)
            except TelegramError as error:
                # A permanent rejection of one photo (e.g. PHOTO_INVALID_DIMENSIONS) must not make the whole
                # card retry from its first image: skip this one and keep going. Transient errors still raise.
                if "Bad Request" not in str(error):
                    raise
                continue
            self._remember(result, project)
            sent += 1
            if progress is not None:
                progress["images"] = index
        if sent < len(requested):
            self.client.send_message(
                self.owner_id,
                f"[{project}] {len(requested) - sent} 张截图没有发送：不在允许的目录，或不是可用的图片。",
            )
        return sent

    def _deliver_card(self, project: str, item: dict[str, Any], card: dict[str, Any]) -> None:
        kind = card.get("kind", "message")
        title = str(card["title"])
        text = str(card.get("text") or "")
        task_id = item.get("task_id") or card.get("task_id")
        sender = item["sender"]
        if _SECRET_RE.search(json.dumps(card, ensure_ascii=False)):
            raise OwnerChannelError("credential assignments must not leave this host")
        if kind == "question":
            owner_questions.ask(
                self.data_root,
                project_id=project,
                asker=sender,
                question=title,
                options=[str(option) for option in card.get("options") or []],
                urgency=str(card.get("urgency") or "normal"),
                expires_hours=float(card.get("expires_hours") or 24),
                resume_on_answer=bool(card.get("resume_on_answer")),
                task_id=task_id,
            )
        elif kind == "action":
            self.request_action(
                title,
                detail=text,
                steps=[str(step) for step in card.get("steps") or []],
                project=project,
                priority=str(card.get("priority") or "p1"),
            )
        elif kind == "stage":
            progress = self.delivery_progress.setdefault(str(item.get("message_id")), {})
            if not progress.get("head"):
                sent = self.send_stage_report(
                    title,
                    summary=text,
                    recommendations=[str(entry) for entry in card.get("next") or []],
                    project=project,
                    metrics=card.get("metrics"),
                )
                self._remember(sent, project, task_id, sender)
                progress["head"] = 1
            self.send_evidence(project, card.get("images"), title, progress)
        elif kind == "approval":
            self._send_approval(project, title, text, task_id, sender, card)
        else:
            keyboard, stored = self._custom_keyboard(card.get("buttons"))
            lines = [f"<b>💬 {_safe(sender, 60)}</b> · {_safe(project)}", ""]
            lines += [f"<b>{_safe(title, 120)}</b>", _safe_lines(text)]
            if card.get("metrics"):
                lines += ["", "<b>📈 实测数据</b>", *map(_metric_line, card["metrics"][:10])]
            lines += ["", "<i>回复这条消息可以追问</i>"]
            sent = self.client.send_message(
                self.owner_id, _clip("\n".join(lines)), keyboard or None, rich=True
            )
            self._remember(
                sent,
                project,
                task_id,
                sender,
                title=title,
                buttons=stored,
                resume=bool(card.get("resume_on_answer")),
            )
            self.send_evidence(project, card.get("images"), title)

    def _approval_buttons(self, approval: dict[str, Any]) -> list[list[dict[str, str]]]:
        rows = [
            [
                _button(
                    f"{'☑' if index in approval['selected'] else '☐'} {item}"[:40],
                    f"p|t|{index}",
                )
            ]
            for index, item in enumerate(approval["items"])
        ]
        label = "✅ 批准所选" if approval["items"] else "✅ 批准"
        rows.append(
            [
                _button(label, "p|yes"),
                _button("❌ 全部拒绝" if approval["items"] else "❌ 拒绝", "p|no"),
            ]
        )
        return rows

    def _send_approval(
        self,
        project: str,
        title: str,
        text: str,
        task_id: str | None,
        sender: str,
        card: dict[str, Any],
    ) -> None:
        risk = str(card.get("risk") or "medium")
        if risk not in RISK_LABELS:
            raise OwnerChannelError("approval risk is invalid")
        subject = str(card.get("subject") or "")
        items = [str(item) for item in card.get("items") or []]
        if len(items) > MAX_APPROVAL_ITEMS:
            raise OwnerChannelError("an approval has too many items")
        digest = hashlib.sha256(
            json.dumps([project, task_id, title, text, subject, items], ensure_ascii=False).encode()
        ).hexdigest()[:16]
        hours = float(card.get("expires_hours") or DEFAULT_APPROVAL_HOURS)
        expires = datetime.now(UTC) + timedelta(hours=hours)
        local = expires.astimezone().strftime("%m-%d %H:%M")
        lines = [f"<b>🔏 审批申请</b> · {_safe(project)}", ""]
        lines += [f"<b>{_safe(title, 120)}</b>", _safe_lines(text)]
        if subject:
            lines += ["", f"对象：{_safe(subject, 200)}"]
        if items:
            lines += ["", "<b>申请项</b>"]
            lines += [f"{index}. {_safe(item, 120)}" for index, item in enumerate(items, 1)]
        lines += ["", f"风险：{RISK_LABELS[risk]} · 有效至 {local}"]
        lines += [f"申请人：{_safe(sender, 60)} · 编号 {digest}"]
        if not self.interactive:
            lines += ["", "<i>这个通道只能通知，不能在这里批准；申请人已收到说明。</i>"]
            send_message(
                self.data_root,
                project_id=project,
                message_id=f"approval-{digest}-not-interactive",
                sender=CHANNEL_SENDER,
                recipient=sender,
                body=f"审批 {digest} 已通知所有者，但通知通道没有 Telegram，无法在通道内批准；"
                "请改用 continuity_ask_owner 提问，所有者用 notify answer 回答。",
                task_id=task_id,
            )
        elif risk == "high":
            lines += ["", "<i>高风险审批要动态码，现在还不能在这里批准，请回电脑处理。</i>"]
        else:
            hint = "点申请项取消勾选，可以只批准其中几项。" if items else ""
            lines += ["", f"<i>{hint}批准只记录决定，由申请人执行。回复这条消息可以追问。</i>"]
        approval = {
            "id": digest,
            "risk": risk,
            "expires_at": expires.isoformat(),
            "state": "pending",
            "items": items,
            "selected": list(range(len(items))),
        }
        sent = self.client.send_message(
            self.owner_id,
            _clip("\n".join(lines)),
            None if risk == "high" or not self.interactive else self._approval_buttons(approval),
            rich=True,
        )
        self._remember(
            sent,
            project,
            task_id,
            sender,
            title=title,
            approval=approval,
            resume=bool(card.get("resume_on_answer")),
        )

    def _open_tasks(self, project: str) -> list[dict[str, Any]]:
        return [
            task
            for task in list_tasks(self.data_root, project_id=project)
            if task["status"] in OPEN_STATUSES
        ]

    def _rows(self) -> list[tuple[str, dict[str, Any]]]:
        rows = [(p, t) for p in self.projects for t in self._open_tasks(p)]
        order = {status: index for index, status in enumerate(OPEN_STATUSES)}
        rows.sort(key=lambda row: row[1]["updated_at"], reverse=True)
        rows.sort(key=lambda row: order[row[1]["status"]])
        return rows

    def _owner_rows(self) -> list[tuple[str, dict[str, Any]]]:
        return [
            row
            for row in self._rows()
            if row[1]["assignee"] in OWNER_ASSIGNEES
            and row[1]["status"] in ("active", "dispatched", "queued")
        ]

    def _owner_decisions(self, project: str) -> list[str]:
        """Decisions a controller declared for the owner in its handoff."""
        try:
            pending = _declared_decisions(self.data_root or default_data_root(), project)
        except (CollaborationRegistryError, OSError, ValueError):
            return []
        return [
            entry.split(":", 1)[-1].strip()
            for entry in pending
            if entry.casefold().startswith("owner")
        ]

    def _resolve_project(self, word: str) -> str | None:
        matches = [project for project in self.projects if word.casefold() in project]
        return matches[0] if len(matches) == 1 else None

    def _link(self, project: str, task: dict[str, Any]) -> str:
        title = _safe(task["title"], 70)
        if not self.bot_username:
            return title
        key = _card_key(project, task["task_id"])
        return f'<a href="https://t.me/{self.bot_username}?start=c_{key}">{title}</a>'

    def _find(self, key: str) -> tuple[str, dict[str, Any]] | None:
        for project in self.projects:
            for task in list_tasks(self.data_root, project_id=project):
                if _card_key(project, task["task_id"]) == key:
                    return project, task
        return None

    def _section(self, rows: list[tuple[str, dict[str, Any]]], limit: int = 5) -> list[str]:
        lines = [f"• {self._link(project, task)}" for project, task in rows[:limit]]
        if len(rows) > limit:
            lines.append(f"  还有 {len(rows) - limit} 项，发 /list 查看")
        return lines

    def render_status(self) -> tuple[str, list[list[dict[str, str]]]]:
        now = datetime.now().astimezone().strftime("%H:%M")
        lines = [f"<b>📊 Continuity 总览</b> · {now}", ""]
        for project in self.projects:
            tasks = self._open_tasks(project)
            counts = Counter(task["status"] for task in tasks)
            stale = sum(_is_stale_review(task) for task in tasks)
            lines.append(f"<b>{_safe(project)}</b>")
            lines.append(
                f"🟢 进行中 {counts['active'] + counts['dispatched']}　"
                f"⏳ 排队 {counts['queued']}　⛔ 阻塞 {counts['blocked']}"
            )
            lines.append(f"👀 待审 {counts['ready-for-review'] - stale}（另有 {stale} 张旧卡）")
            lines.append("")
        waiting = len(self._owner_rows()) + sum(
            len(self._owner_decisions(project)) + len(self._open_questions(project))
            for project in self.projects
        )
        lines.append(f"🧑‍💻 等你处理：<b>{waiting}</b> 件" if waiting else "🧑‍💻 没有等你处理的事")
        return "\n".join(lines), [
            [_button("🧑‍💻 等你处理", "v|waiting"), _button("📋 任务", "v|list|recent|0")],
            [_button("🔄 刷新", "v|status")],
        ]

    def render_list(
        self, status: str = "recent", page: int = 0, project: str | None = None
    ) -> tuple[str, list[list[dict[str, str]]]]:
        rows = [row for row in self._rows() if project in (None, row[0])]
        stale = [row for row in rows if _is_stale_review(row[1])]
        if status == "recent":
            rows = [row for row in rows if not _is_stale_review(row[1])]
            heading = "当前任务"
        elif status == "old":
            rows = stale
            heading = "超过 7 天没动的待审卡"
        else:
            rows = [row for row in rows if row[1]["status"] == status]
            heading = STATUS_LABELS.get(status, ("", status))[1]
        pages = max(1, -(-len(rows) // PAGE_SIZE))
        page = min(max(page, 0), pages - 1)
        lines = [f"<b>📋 {_safe(heading)}</b> · {len(rows)} 项"]
        if pages > 1:
            lines[0] += f"（第 {page + 1}/{pages} 页）"
        current = None
        for project, task in rows[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]:
            if task["status"] != current:
                current = task["status"]
                icon, label = STATUS_LABELS[current]
                lines += ["", f"<b>{icon} {label}</b>"]
            where = f"{_safe(project)} · " if len(self.projects) > 1 else ""
            who = _safe(task["assignee"] or "未分配", 40)
            lines.append(f"• {self._link(project, task)}")
            lines.append(f"   <i>{where}{who} · {_ago(task['updated_at'])}</i>")
        if not rows:
            lines += ["", "这里没有卡片。"]
        lines += ["", "<i>点标题查看近况</i>"] if rows and self.bot_username else []
        navigation = []
        scope = project or ""
        if page > 0:
            navigation.append(_button("‹ 上一页", f"v|list|{status}|{page - 1}|{scope}"))
        if page + 1 < pages:
            navigation.append(_button("下一页 ›", f"v|list|{status}|{page + 1}|{scope}"))
        keyboard = [navigation] if navigation else []
        if status == "recent" and stale:
            keyboard.append([_button(f"🗂 旧卡（{len(stale)}）", f"v|list|old|0|{scope}")])
        keyboard.append([_button("‹ 返回总览", "v|status")])
        return "\n".join(lines), keyboard

    def render_waiting(self) -> tuple[str, list[list[dict[str, str]]]]:
        rows = self._owner_rows()
        decisions = [
            (project, entry)
            for project in self.projects
            for entry in self._owner_decisions(project)
        ]
        questions = [
            (project, item) for project in self.projects for item in self._open_questions(project)
        ]
        back = [[_button("‹ 返回总览", "v|status")]]
        if not rows and not decisions and not questions:
            return "🧑‍💻 现在没有等你处理的事。", back
        total = len(rows) + len(decisions) + len(questions)
        lines = [f"<b>🧑‍💻 等你处理</b> · {total} 件", ""]
        for project, item in questions[:PAGE_SIZE]:
            lines.append(f"• ❓ {_safe(item['question'], 90)}")
            lines.append(f"   <i>{_safe(project)} · {_safe(item['asker'], 40)}</i>")
            back.insert(
                0,
                [
                    _button(
                        f"回答：{item['question']}"[:40],
                        f"w|{project}|{item['question_id']}",
                    )
                ],
            )
        for project, task in rows[:PAGE_SIZE]:
            lines.append(f"• {self._link(project, task)}")
            lines.append(f"   <i>{_safe(project)} · {_ago(task['created_at'])}</i>")
        if decisions:
            lines += ["", "<b>主控交接里待你决定</b>"]
            for project, entry in decisions:
                lines.append(f"• {_safe(entry, 120)}")
                lines.append(f"   <i>{_safe(project)} · 可能已过时，回复主控确认</i>")
        return "\n".join(lines), back

    def render_report(
        self, project: str | None = None, since: str | None = None
    ) -> tuple[str, list[list[dict[str, str]]]]:
        now = datetime.now(UTC)
        start = since or (now - timedelta(hours=24)).isoformat()
        hours = max(1, round((now - datetime.fromisoformat(start)).total_seconds() / 3600))
        local = now.astimezone().strftime("%H:%M")
        lines = [f"<b>🕘 进度报告</b> · {local}（过去 {hours} 小时）"]
        for name in [project] if project else self.projects:
            tasks = list_tasks(self.data_root, project_id=name)
            changed = Counter(t["status"] for t in tasks if t["updated_at"] >= start)
            open_rows = [row for row in self._rows() if row[0] == name]
            active = [r for r in open_rows if r[1]["status"] in ("active", "dispatched")]
            blocked = [
                r
                for r in open_rows
                if r[1]["status"] in ("blocked", "conflict") and r[1]["updated_at"] >= start
            ]
            finished = changed["completed"] + changed["merged"]
            lines += ["", f"<b>{_safe(name)}</b>"]
            lines.append(
                f"✅ 完成 {finished}　👀 提交待审 {changed['ready-for-review']}　"
                f"⛔ 新阻塞 {len(blocked)}"
            )
            if active:
                lines += [f"🟢 进行中（{len(active)}）", *self._section(active, 4)]
            if blocked:
                lines.append("⛔ 新阻塞")
                for row_project, task in blocked[:3]:
                    reason = _safe(task.get("blocked_reason") or "未写原因", 60)
                    lines.append(f"• {self._link(row_project, task)}\n   <i>{reason}</i>")
            waiting = sum(row[0] == name for row in self._owner_rows())
            decisions = len(self._owner_decisions(name))
            questions = self._open_questions(name)
            if waiting + decisions + len(questions):
                lines.append(f"🧑‍💻 等你处理 {waiting + decisions + len(questions)} 件")
            for item in questions[:3]:
                lines.append(f"❓ {_safe(item['question'], 80)}")
        keyboard = [[_button("🧑‍💻 等你处理", "v|waiting"), _button("📋 任务", "v|list|recent|0")]]
        return _clip("\n".join(lines)), keyboard

    def _read_new_changes(self, project: str) -> tuple[list[dict[str, Any]], int]:
        cursor = self.cursors.get(project)
        latest = latest_change_cursor(self.data_root, project_id=project)
        if cursor is None:
            return [], latest  # Start from now: history is not news.
        changes: list[dict[str, Any]] = []
        for _ in range(CHANGE_PAGES):
            if cursor >= latest:
                break
            page = list_changes(
                self.data_root, project_id=project, since=cursor, max_results=500
            )["changes"]
            if not page:
                break
            changes += page
            cursor = int(page[-1]["cursor"])
        return changes, cursor

    def _alert_rows(
        self, project: str, changes: list[dict[str, Any]], now: datetime
    ) -> dict[str, str]:
        rows: dict[str, str] = {}
        for change in changes:
            task_id = change.get("task_id")
            reason = ALERT_EVENTS.get(change["event"])
            if change["event"] == "task.updated" and change.get("status") in ("blocked", "conflict"):
                reason = "阻塞"
            if not task_id or reason is None:
                continue
            key = f"{project}/{task_id}"
            previous = self.alerted.get(key)
            if previous and now - datetime.fromisoformat(previous) < ALERT_COOLDOWN:
                continue
            rows[task_id] = reason
        return rows

    def _send_session_alerts(
        self, project: str, changes: list[dict[str, Any]], now: datetime
    ) -> int:
        """A stopped session has no card, so it gets its own notice."""
        seen: dict[str, dict[str, Any]] = {}
        for change in changes:
            if change["event"] not in SESSION_EVENTS:
                continue
            key = f"{project}/session/{change.get('session_id')}/{change.get('error')}"
            previous = self.alerted.get(key)
            if previous and now - datetime.fromisoformat(previous) < ALERT_COOLDOWN:
                continue
            seen[key] = change
        if not seen:
            return 0
        lines = [f"<b>🛑 会话中断</b> · {_safe(project)}（{len(seen)} 个）", ""]
        for change in list(seen.values())[:6]:
            error = str(change.get("error") or "unknown")
            lines.append(f"• {SESSION_ERRORS.get(error, _safe(error, 40))}")
            if change.get("detail"):
                lines.append(f"   <i>{_safe(change['detail'], 100)}</i>")
        sent = self.client.send_message(self.owner_id, _clip("\n".join(lines)), None, rich=True)
        self._remember(sent, project)
        for key in seen:
            self.alerted[key] = now.isoformat()
        return 1

    def _send_alerts(self, project: str, rows: dict[str, str], now: datetime) -> None:
        tasks = {t["task_id"]: t for t in list_tasks(self.data_root, project_id=project)}
        lines = [f"<b>🚨 任务出问题</b> · {_safe(project)}（{len(rows)} 项）", ""]
        for task_id, reason in list(rows.items())[:8]:
            task = tasks.get(task_id)
            name = self._link(project, task) if task else _safe(task_id, 70)
            who = _safe((task or {}).get("assignee") or "未分配", 40)
            detail = _safe((task or {}).get("blocked_reason"), 80)
            lines.append(f"• {name}\n   <i>{reason} · {who}</i>")
            if detail and detail != reason:
                lines.append(f"   <i>{detail}</i>")
        if len(rows) > 8:
            lines.append(f"还有 {len(rows) - 8} 项")
        sent = self.client.send_message(
            self.owner_id,
            _clip("\n".join(lines)),
            [[_button("📋 任务", "v|list|blocked|0"), _button("📊 总览", "v|status")]],
            rich=True,
        )
        self._remember(sent, project)
        for task_id in rows:
            self.alerted[f"{project}/{task_id}"] = now.isoformat()

    def _send_digest(self, project: str, task_ids: list[str]) -> None:
        tasks = {t["task_id"]: t for t in list_tasks(self.data_root, project_id=project)}
        rows = [tasks[task_id] for task_id in task_ids if task_id in tasks]
        counts = Counter(task["status"] for task in rows)
        lines = [f"<b>✅ 任务完成总结</b> · {_safe(project)}（{len(rows)} 项）"]
        lines.append(
            f"完成 {counts['completed'] + counts['merged']}　待审 {counts['ready-for-review']}"
        )
        lines.append("")
        for task in rows[:PAGE_SIZE]:
            lines.append(f"• {self._link(project, task)}")
            outcome = _safe(task.get("result") or task.get("verification"), 110)
            if outcome:
                lines.append(f"   <i>{outcome}</i>")
        if len(rows) > PAGE_SIZE:
            lines.append(f"还有 {len(rows) - PAGE_SIZE} 项，发 /list {project} 查看")
        sent = self.client.send_message(
            self.owner_id,
            _clip("\n".join(lines)),
            [[_button("📋 任务", "v|list|recent|0"), _button("📊 总览", "v|status")]],
            rich=True,
        )
        self._remember(sent, project)

    def watch_events(self, now: datetime | None = None) -> int:
        """Push failures at once; fold finished cards into an hourly digest."""
        now = now or datetime.now(UTC)
        pushed = 0
        for project in self.projects:
            changes, cursor = self._read_new_changes(project)
            rows = self._alert_rows(project, changes, now)
            pushed += self._send_session_alerts(project, changes, now)
            if rows:
                self._send_alerts(project, rows, now)
                pushed += 1
            waiting = self.finished.setdefault(project, [])
            for change in changes:
                if change["event"] == "owner.question.answered" and change.get("resume_requested"):
                    self.wake(
                        project,
                        f"Owner 回答了你的问题 {change.get('question_id')}，答案在你的 inbox 里，请继续。",
                    )
                if (
                    change["event"] == "task.updated"
                    and change.get("status") in FINISHED_STATUSES
                    and change.get("assignee") not in OWNER_ASSIGNEES
                    and change.get("task_id") not in waiting
                ):
                    waiting.append(change["task_id"])
            self.cursors[project] = cursor
            last = self.digest_at.setdefault(project, now.isoformat())
            if (
                waiting
                and not _is_quiet(now)
                and now - datetime.fromisoformat(last) >= DIGEST_EVERY
            ):
                self._send_digest(project, waiting)
                self.finished[project] = []
                self.digest_at[project] = now.isoformat()
                pushed += 1
        return pushed

    def render_timers(self) -> tuple[str, list[list[dict[str, str]]]]:
        lines = ["<b>⏰ 定时报告</b>", ""]
        keyboard = []
        for timer in self.timers:
            lines.append(f"• {_schedule_label(timer)}")
            keyboard.append([_button(f"取消：{_schedule_label(timer)}"[:40], f"t|del|{timer['id']}")])
        if not self.timers:
            lines.append("还没有定时报告。")
        lines += ["", "<i>也可以发 /every 09:00、/every 2h platform、/in 30m</i>"]
        keyboard.append(
            [
                _button("+ 早 9 点", "t|add|09:00"),
                _button("+ 晚 10 点", "t|add|22:00"),
                _button("+ 每 2 小时", "t|add|2h"),
            ]
        )
        return "\n".join(lines), keyboard

    def add_timer(self, word: str, project: str | None = None, *, once: bool = False) -> dict[str, Any]:
        schedule = _parse_schedule(word)
        if schedule is None or (once and schedule[0] != "interval"):
            raise OwnerChannelError("时间格式不对：用 09:00、30m 或 2h，最短 5 分钟")
        kind, value = schedule
        now = datetime.now(UTC)
        moment = _next_daily(value, now) if kind == "daily" else now + timedelta(seconds=int(value))
        timer = {
            "id": hashlib.sha256(f"{kind}{value}{project}{once}".encode()).hexdigest()[:8],
            "kind": "once" if once else kind,
            "value": value,
            "project": project,
            "next_at": moment.isoformat(),
            "last_run": None,
        }
        self.timers[:] = [item for item in self.timers if item["id"] != timer["id"]]
        self.timers.append(timer)
        return timer

    def run_timers(self, now: datetime | None = None) -> int:
        """Send due reports; time missed while stopped collapses into one report."""
        now = now or datetime.now(UTC)
        due = [timer for timer in self.timers if timer["next_at"] <= now.isoformat()]
        for timer in due:
            text, keyboard = self.render_report(timer.get("project"), timer.get("last_run"))
            self.client.send_message(self.owner_id, text, keyboard, rich=True)
            timer["last_run"] = now.isoformat()
            if timer["kind"] == "once":
                self.timers.remove(timer)
            elif timer["kind"] == "daily":
                timer["next_at"] = _next_daily(timer["value"], now).isoformat()
            else:
                timer["next_at"] = (now + timedelta(seconds=int(timer["value"]))).isoformat()
        return len(due)

    def _recent_events(self, project: str, task_id: str) -> list[str]:
        latest = latest_change_cursor(self.data_root, project_id=project)
        page = list_changes(
            self.data_root,
            project_id=project,
            since=max(0, latest - 300),
            max_results=300,
        )
        lines: list[str] = []
        previous = None
        for change in page["changes"]:
            if change.get("task_id") != task_id:
                continue
            if change["event"] == "task.added":
                text = "创建"
            elif change["event"] == "task.claimed":
                text = f"{_safe(change.get('assignee'), 40)} 接手"
            elif change["event"] == "message.sent":
                text = "收到消息"
            elif change["event"] == "task.updated" and change.get("status"):
                text = "状态变为" + STATUS_LABELS.get(change["status"], ("", change["status"]))[1]
            else:
                continue
            if text != previous:
                lines.append(f"• {_ago(change.get('observed_at'))}：{_safe(text, 60)}")
            previous = text
        if not lines:
            return [] if not page["changes"] else self._card_times(project, task_id)
        return lines[-5:]

    def _card_times(self, project: str, task_id: str) -> list[str]:
        """Busy journals age out of the window; the card's own times still hold."""
        found = self._find(_card_key(project, task_id))
        if found is None:
            return []
        task = found[1]
        return [f"• {_ago(task['created_at'])}：创建", f"• {_ago(task['updated_at'])}：最近更新"]

    def _action_buttons(self, key: str) -> list[list[dict[str, str]]]:
        return [
            [_button("✅ 已完成", f"o|done|{key}"), _button("⏰ 稍后提醒", f"o|later|{key}")],
            [_button("💬 有问题", f"o|ask|{key}")],
        ]

    def render_card(self, key: str) -> tuple[str, list[list[dict[str, str]]]]:
        found = self._find(key)
        if found is None:
            return "这张卡找不到了。", [[_button("‹ 返回总览", "v|status")]]
        project, task = found
        icon, label = STATUS_LABELS.get(task["status"], ("✅", "已结束"))
        lines = [f"{icon} {label} · {_safe(task['priority']).upper()} · {_safe(project)}"]
        lines += [f"<b>{_safe(task['title'], 120)}</b>", ""]
        lines += ["<b>目标</b>", _safe(task["objective"]), ""]
        if task["next_action"] and task["next_action"] != DEFAULT_NEXT_ACTION:
            lines += ["<b>最新进展</b>", _safe(task["next_action"]), ""]
        if task.get("blocked_reason"):
            lines += ["<b>卡住原因</b>", _safe(task["blocked_reason"]), ""]
        if task.get("result"):
            lines += ["<b>结果</b>", _safe(task["result"]), ""]
        events = self._recent_events(project, task["task_id"])
        if events:
            lines += ["<b>近况</b>", *events, ""]
        who = _safe(task["assignee"] or "未分配", 40)
        lines.append(f"<i>{who} · 更新于 {_ago(task['updated_at'])}</i>")
        keyboard = []
        if task["assignee"] in OWNER_ASSIGNEES and task["status"] in ("active", "queued"):
            keyboard = self._action_buttons(key)
        keyboard.append([_button("🔄 刷新", f"v|card|{key}"), _button("‹ 任务", "v|list|recent|0")])
        return "\n".join(lines), keyboard

    def _view(self, data: str) -> tuple[str, list[list[dict[str, str]]]]:
        parts = data.split("|")
        if parts[1:2] == ["waiting"]:
            return self.render_waiting()
        if parts[1:2] == ["list"] and len(parts) >= 4:
            scope = parts[4] if len(parts) > 4 and parts[4] else None
            return self.render_list(parts[2], int(parts[3]), scope)
        if parts[1:2] == ["timers"]:
            return self.render_timers()
        if parts[1:2] == ["card"] and len(parts) == 3:
            return self.render_card(parts[2])
        return self.render_status()

    def request_action(
        self,
        title: str,
        *,
        detail: str,
        steps: list[str] | None = None,
        project: str | None = None,
        priority: str = "p1",
    ) -> dict[str, Any]:
        """File a task card assigned to the owner, then deliver it as a card."""
        project = project or self.projects[0]
        if project not in self.projects:
            raise OwnerChannelError("project is outside the channel scope")
        if _SECRET_RE.search(f"{title} {detail} {' '.join(steps or [])}"):
            raise OwnerChannelError("credential assignments must not leave this host")
        digest = hashlib.sha256(f"{title}\n{detail}".encode()).hexdigest()[:10]
        task_id = f"owner-{digest}"
        task = add_task(
            self.data_root,
            project_id=project,
            task_id=task_id,
            lane_id=OWNER_LANE,
            mode="decision",
            priority=priority,
            title=title,
            objective=detail,
            next_action="；".join(steps or []) or "等待 owner 处理",
            exit_criteria=["The owner confirms this is done"],
            assignee=OWNER_RECIPIENT,
        )
        sent = self._send_action(project, task, steps)
        self._remember(sent, project, task_id)
        return task

    def _send_action(
        self, project: str, task: dict[str, Any], steps: list[str] | None = None
    ) -> dict[str, Any]:
        lines = [f"<b>🧑‍💻 需要你处理</b> · {_safe(project)}", ""]
        lines += [f"<b>{_safe(task['title'], 120)}</b>", _safe(task["objective"], 800)]
        if steps:
            lines += ["", "<b>怎么做</b>"]
            lines += [
                f"{index}. {html.escape(step, quote=False)}"
                for index, step in enumerate(steps, 1)
            ]
        key = _card_key(project, task["task_id"])
        return self.client.send_message(
            self.owner_id, _clip("\n".join(lines)), self._action_buttons(key), rich=True
        )

    def send_stage_report(
        self,
        title: str,
        *,
        summary: str,
        recommendations: list[str] | None = None,
        project: str | None = None,
        metrics: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Deliver an agent-written stage summary with the live card picture."""
        project = project or self.projects[0]
        if project not in self.projects:
            raise OwnerChannelError("project is outside the channel scope")
        if _SECRET_RE.search(f"{title} {summary} {' '.join(recommendations or [])}"):
            raise OwnerChannelError("credential assignments must not leave this host")
        lines = [f"<b>🏁 阶段完成</b> · {_safe(project)}", ""]
        lines += [f"<b>{_safe(title, 120)}</b>", _safe_lines(summary)]
        if metrics:
            lines += ["", "<b>📈 实测数据</b>", *map(_metric_line, metrics[:10])]
        keyboard = []
        if recommendations:
            lines += ["", "<b>🧭 下一阶段建议</b>"]
            for index, item in enumerate(recommendations):
                lines.append(f"{index + 1}. {_safe(item, 200)}")
            keyboard.append(
                [
                    _button(f"做 {index + 1}", f"a|{project}|{index}")
                    for index in range(min(len(recommendations), 4))
                ]
            )
        rows = [row for row in self._rows() if row[0] == project]
        groups = (
            ("🧑‍💻 等你处理", [r for r in rows if r[1]["assignee"] in OWNER_ASSIGNEES]),
            ("🟢 进行中", [r for r in rows if r[1]["status"] in ("active", "dispatched") and r[1]["assignee"] not in OWNER_ASSIGNEES]),
            ("⛔ 阻塞", [r for r in rows if r[1]["status"] in ("blocked", "conflict") and r[1]["assignee"] not in OWNER_ASSIGNEES]),
            ("⏳ 排队", [r for r in rows if r[1]["status"] == "queued" and r[1]["assignee"] not in OWNER_ASSIGNEES]),
        )
        for label, group in groups:
            if group:
                lines += ["", f"<b>{label}（{len(group)}）</b>", *self._section(group)]
        keyboard.append([_button("📋 全部任务", "v|list|recent|0"), _button("📊 总览", "v|status")])
        return self.client.send_message(
            self.owner_id, _clip("\n".join(lines)), keyboard, rich=True
        )

    def _is_owner(self, sender: dict[str, Any], chat: dict[str, Any]) -> bool:
        return (
            sender.get("id") == self.owner_id
            and chat.get("id") == self.owner_id
            and chat.get("type") == "private"
        )

    def _project_of(self, message: dict[str, Any] | None) -> str:
        first_line = ((message or {}).get("text") or "").split("\n", 1)[0]
        for project in self.projects:
            if project in first_line:
                return project
        return self.projects[0]

    def _recipient(self, project: str) -> str:
        return self.routes.get(project, self.recipient)

    def _store(
        self,
        update_id: int,
        project: str,
        body: str,
        *,
        task_id: str | None = None,
        recipient: str | None = None,
    ) -> str:
        target = recipient or self._recipient(project)
        send_message(
            self.data_root,
            project_id=project,
            message_id=f"tg-{update_id}",
            sender=CHANNEL_SENDER,
            recipient=target,
            body=body,
            task_id=task_id,
        )
        return target

    def _handle_timer_command(self, command: str, words: list[str]) -> None:
        project = None
        if len(words) > 2:
            project = self._resolve_project(words[2])
            if project is None:
                self.client.send_message(self.owner_id, "没找到这个项目。")
                return
        try:
            timer = self.add_timer(words[1] if len(words) > 1 else "", project, once=command == "in")
        except OwnerChannelError as error:
            self.client.send_message(self.owner_id, str(error))
            return
        view = self.render_timers()
        self.client.send_message(
            self.owner_id, f"已添加：{_schedule_label(timer)}\n\n{view[0]}", view[1], rich=True
        )

    def _handle_command(self, text: str) -> None:
        words = text.split()
        command = words[0].split("@")[0][1:]
        if command in ("every", "in"):
            self._handle_timer_command(command, words)
            return
        argument = words[1] if len(words) > 1 else None
        project = self._resolve_project(argument) if argument else None
        if command == "start" and argument and argument.startswith("c_"):
            view = self.render_card(argument[2:])
            found = self._find(argument[2:])
            sent = self.client.send_message(self.owner_id, view[0], view[1], rich=True)
            if found:
                self._remember(sent, found[0], found[1]["task_id"])
            return
        if command == "status":
            view = self.render_status()
        elif command == "list":
            status = STATUS_WORDS.get(argument or "", argument)
            if project or status is None:
                view = self.render_list("recent", 0, project)
            else:
                view = self.render_list(status)
        elif command == "waiting":
            view = self.render_waiting()
        elif command == "report":
            view = self.render_report(project)
        elif command == "timers":
            view = self.render_timers()
        else:
            view = (
                "<b>Continuity 通知</b>\n\n"
                "/status 项目总览\n/list 当前任务，点标题看近况\n/waiting 等你处理的事\n"
                "/report 现在生成报告\n/timers 定时报告\n\n"
                "任务失败会马上提醒；任务完成每小时汇总一次，23 点到 8 点只推失败。\n"
                "回复任意消息就是追问，内容会带上下文转给发这条消息的 agent。",
                None,
            )
        self.client.send_message(self.owner_id, view[0], view[1], rich=True)

    def _handle_text(self, update_id: int, message: dict[str, Any], text: str) -> None:
        reply = message.get("reply_to_message")
        if reply is None and len(self.projects) > 1:
            self.client.send_message(
                self.owner_id,
                f"发给哪个项目？\n\n{text}"[:MAX_TEXT_CHARS],
                [[_button(project, f"s|{project}")] for project in self.projects],
            )
            return
        context = self._context(reply)
        if context.get("question_id"):
            result = self._answer(context, text)
            self.client.send_message(
                self.owner_id, "已回答。" if result["accepted"] else "这个问题已经结束了。"
            )
            return
        project = context.get("project") or self._project_of(reply)
        body = text
        if reply:
            quoted = " ".join((reply.get("text") or "").split())[:300]
            body = f"{text}\n\n（这是对这条消息的追问：{quoted}）"
        try:
            target = self._store(
                update_id,
                project,
                body,
                task_id=context.get("task_id"),
                recipient=context.get("sender"),
            )
        except CollaborationRegistryError as error:
            self.client.send_message(self.owner_id, f"没有写入：{error}")
            return
        keyboard = [[_button("🔔 叫醒它", "k|go")]] if project in self.wake_commands else None
        sent = self.client.send_message(self.owner_id, f"已转给 {target}。", keyboard)
        self._remember(sent, project, wake_text=text[:1500])

    def handle_update(self, update: dict[str, Any]) -> None:
        callback = update.get("callback_query")
        if callback:
            message = callback.get("message") or {}
            if not self._is_owner(callback.get("from") or {}, message.get("chat") or {}):
                return
            self._handle_callback(update["update_id"], callback, message)
            return
        message = update.get("message")
        if not message or not self._is_owner(message.get("from") or {}, message.get("chat") or {}):
            return
        text = (message.get("text") or "").strip()
        if text.startswith("/"):
            self._handle_command(text)
        elif text:
            self._handle_text(update["update_id"], message, text)

    def _handle_callback(
        self, update_id: int, callback: dict[str, Any], message: dict[str, Any]
    ) -> None:
        data = callback.get("data") or ""
        if data.startswith("v|"):
            text, keyboard = self._view(data)
            try:
                self.client.edit_message(
                    self.owner_id, message["message_id"], text, keyboard, rich=True
                )
            except TelegramError as error:
                if "not modified" not in str(error):
                    raise
            self.client.answer_callback(callback["id"])
            return
        if data.startswith("o|"):
            self._handle_action(update_id, callback, message, data)
            return
        if data.startswith("p|"):
            self._handle_approval(update_id, callback, message, data)
            return
        if data.startswith("t|"):
            self._handle_timer_button(callback, message, data)
            return
        if data.startswith("b|"):
            self._handle_custom_button(update_id, callback, message, data)
            return
        if data.startswith("s|"):
            self._handle_project_choice(update_id, callback, message, data)
            return
        if data.startswith("q|"):
            self._handle_question_button(callback, message, data)
            return
        if data.startswith("k|"):
            self._handle_wake(callback, message)
            return
        if data.startswith("w|"):
            _, project, question_id = data.split("|")
            owner_questions.mark_delivered(
                self.data_root, project_id=project, question_id=question_id, delivered=False
            )
            self.client.answer_callback(callback["id"], "问题马上发给你")
            return
        parts = data.split("|")
        if len(parts) != 3 or parts[0] != "a" or parts[1] not in self.projects:
            self.client.answer_callback(callback["id"], "已处理过")
            return
        rows = (message.get("reply_markup") or {}).get("inline_keyboard") or []
        label = next(
            (b["text"] for row in rows for b in row if b.get("callback_data") == data),
            None,
        )
        if label is None:
            self.client.answer_callback(callback["id"], "已处理过")
            return
        question = message.get("text") or ""
        context = self._context(message)
        self._store(
            update_id,
            parts[1],
            f"按钮回答：{label}\n问题：{question}"[:900],
            task_id=context.get("task_id"),
            recipient=context.get("sender"),
        )
        self._mark_handled(message, f"✅ 已回答：{label}")
        self.client.answer_callback(callback["id"], "已记录")

    def _handle_custom_button(
        self,
        update_id: int,
        callback: dict[str, Any],
        message: dict[str, Any],
        data: str,
    ) -> None:
        context = self._context(message)
        buttons = context.get("buttons") or []
        index = int(data.split("|")[1])
        if context.get("answered") or index >= len(buttons):
            self.client.answer_callback(callback["id"], "已处理过")
            return
        choice = buttons[index]
        context["answered"] = True
        self._store(
            update_id,
            context["project"],
            f"按钮回答：{choice['label']}（{choice['value']}）\n卡片：{context.get('title')}"[:900],
            task_id=context.get("task_id"),
            recipient=context.get("sender"),
        )
        if context.get("resume"):
            self.wake(context["project"], f"Owner 选了：{choice['label']}，详情在你的 inbox 里。")
        self._mark_handled(message, f"✅ 已选：{choice['label']}")
        self.client.answer_callback(callback["id"], "已记录")

    def _handle_wake(self, callback: dict[str, Any], message: dict[str, Any]) -> None:
        context = self._context(message)
        text = context.pop("wake_text", None)
        if text is None:
            self.client.answer_callback(callback["id"], "已处理过")
            return
        woke = self.wake(
            context["project"],
            f"Owner 通过 Telegram 发来消息（已在 inbox，发送方 {CHANNEL_SENDER}）：\n{text}",
        )
        self._mark_handled(message, "🔔 已叫醒" if woke else "叫醒失败，消息还在 inbox 里")
        self.client.answer_callback(callback["id"], "已叫醒" if woke else "叫醒失败")

    def _handle_question_button(
        self, callback: dict[str, Any], message: dict[str, Any], data: str
    ) -> None:
        context = self._context(message)
        options = context.get("options") or []
        index = int(data.split("|")[1])
        if not context.get("question_id") or index >= len(options):
            self.client.answer_callback(callback["id"], "已处理过")
            return
        result = self._answer(context, options[index])
        if result["accepted"]:
            self._mark_handled(message, f"✅ 已回答：{options[index]}"[:60])
            self.client.answer_callback(callback["id"], "已记录")
        else:
            self._mark_handled(message, "这个问题已经结束了")
            self.client.answer_callback(callback["id"], "已处理过")

    def _handle_project_choice(
        self,
        update_id: int,
        callback: dict[str, Any],
        message: dict[str, Any],
        data: str,
    ) -> None:
        project = data.split("|", 1)[1]
        body = (message.get("text") or "").partition("\n\n")[2]
        if project not in self.projects or not body:
            self.client.answer_callback(callback["id"], "已处理过")
            return
        target = self._store(update_id, project, body)
        self._mark_handled(message, f"✅ 已转给 {target}")
        self.client.answer_callback(callback["id"], "已转发")

    def _handle_timer_button(
        self, callback: dict[str, Any], message: dict[str, Any], data: str
    ) -> None:
        _, action, value = data.split("|", 2)
        if action == "del":
            self.timers[:] = [timer for timer in self.timers if timer["id"] != value]
        else:
            self.add_timer(value)
        text, keyboard = self.render_timers()
        self.client.edit_message(self.owner_id, message["message_id"], text, keyboard, rich=True)
        self.client.answer_callback(callback["id"], "已更新")

    def _handle_approval(
        self,
        update_id: int,
        callback: dict[str, Any],
        message: dict[str, Any],
        data: str,
    ) -> None:
        context = self._context(message)
        approval = context.get("approval")
        if not approval or approval["state"] != "pending":
            self.client.answer_callback(callback["id"], "已处理过")
            return
        parts = data.split("|")
        action = parts[1]
        if action == "t" and approval["risk"] != "high":
            index = int(parts[2])
            selected = set(approval["selected"]) ^ {index}
            approval["selected"] = sorted(selected)
            self.client.edit_buttons(
                self.owner_id, message["message_id"], self._approval_buttons(approval)
            )
            self.client.answer_callback(callback["id"])
        elif datetime.now(UTC).isoformat() >= approval["expires_at"]:
            self._decide(update_id, context, message, "expired", "⌛ 已过期")
            self.client.answer_callback(callback["id"], "这个申请已过期")
        elif action == "no":
            self._decide(update_id, context, message, "rejected", "❌ 已拒绝")
            self.client.answer_callback(callback["id"], "已拒绝")
        elif action == "cancel":
            self.client.edit_buttons(
                self.owner_id, message["message_id"], self._approval_buttons(approval)
            )
            self.client.answer_callback(callback["id"])
        elif approval["risk"] == "high":
            self.client.answer_callback(callback["id"], "高风险审批请回电脑处理")
        elif approval["items"] and not approval["selected"]:
            self.client.answer_callback(callback["id"], "至少选一项，或者点全部拒绝")
        elif action == "yes" and approval["risk"] == "medium":
            self.client.edit_buttons(
                self.owner_id,
                message["message_id"],
                [[_button("⚠️ 确认批准", "p|confirm"), _button("取消", "p|cancel")]],
            )
            self.client.answer_callback(callback["id"], "再点一次确认")
        else:
            partial = len(approval["selected"]) < len(approval["items"])
            decision, label = ("partial", "✅ 部分批准") if partial else ("approved", "✅ 已批准")
            self._decide(update_id, context, message, decision, label)
            self.client.answer_callback(callback["id"], label[2:])

    def _decide(
        self,
        update_id: int,
        context: dict[str, Any],
        message: dict[str, Any],
        decision: str,
        label: str,
    ) -> None:
        approval = context["approval"]
        approval["state"] = decision
        project = context["project"]
        body = f"审批结果：{decision}\n事项：{context.get('title')}\n编号：{approval['id']}"
        chosen = approval["selected"] if decision in ("approved", "partial") else []
        if approval["items"]:
            names = approval["items"]
            body += "\n批准：" + ("；".join(names[i] for i in chosen) or "无")
            body += "\n未批准：" + (
                "；".join(n for i, n in enumerate(names) if i not in chosen) or "无"
            )
        self._store(
            update_id,
            project,
            body[:1800],
            task_id=context.get("task_id"),
            recipient=context.get("sender"),
        )
        if context.get("resume"):
            self.wake(project, f"Owner 处理了审批 {approval['id']}：{decision}，详情在你的 inbox 里。")
        _record_change(
            _root(self.data_root),
            project,
            "owner.approval",
            context.get("task_id"),
            approval_id=approval["id"],
            decision=decision,
            approved_items=chosen,
            actor=CHANNEL_SENDER,
        )
        self._mark_handled(message, label)

    def _mark_handled(self, message: dict[str, Any], label: str) -> None:
        self.client.edit_buttons(
            self.owner_id, message["message_id"], [[_button(label, "x")]]
        )

    def _handle_action(
        self,
        update_id: int,
        callback: dict[str, Any],
        message: dict[str, Any],
        data: str,
    ) -> None:
        _, action, key = data.split("|")
        found = self._find(key)
        if found is None or found[1]["status"] not in OPEN_STATUSES:
            self.client.answer_callback(callback["id"], "已处理过")
            return
        project, task = found
        if action == "ask":
            self.client.answer_callback(callback["id"], "直接回复这张卡写下问题")
        elif action == "later":
            self.reminders[key] = (datetime.now(UTC) + REMIND_AFTER).isoformat()
            self._mark_handled(message, "⏰ 4 小时后提醒")
            self.client.answer_callback(callback["id"], "4 小时后再提醒你")
        else:
            update_task(
                self.data_root,
                project_id=project,
                task_id=task["task_id"],
                status="completed",
                result="The owner confirmed this from the channel",
                actor=CHANNEL_SENDER,
            )
            self.reminders.pop(key, None)
            self._store(update_id, project, f"已完成：{task['title']}"[:600])
            self._mark_handled(message, "✅ 已完成")
            self.client.answer_callback(callback["id"], "已记录")

    def resend_due(self) -> int:
        """Send owner action cards again once their reminder time has passed."""
        now = datetime.now(UTC).isoformat()
        due = [key for key, moment in self.reminders.items() if moment <= now]
        for key in due:
            self.reminders.pop(key)
            found = self._find(key)
            if found and found[1]["status"] in OPEN_STATUSES:
                self._send_action(*found)
        return len(due)


def _read_state(data_root: Path | str | None) -> dict[str, Any]:
    document = _load(state_path(data_root))
    if document is None or document.get("schema_version") != STATE_SCHEMA_VERSION:
        return {"schema_version": STATE_SCHEMA_VERSION, "offset": None, "last_poll_at": None}
    return document


def channel_status(data_root: Path | str | None) -> dict[str, Any]:
    config = load_config(data_root)
    state = _read_state(data_root)
    polled = state.get("last_poll_at")
    try:
        age = datetime.now(UTC) - datetime.fromisoformat(polled) if polled else None
    except ValueError:
        age = None
    return {
        "owner_configured": True,
        "telegram": bool(config.get("owner_id") and config.get("token_file")),
        "sinks": [sink["name"] for sink in config.get("sinks") or []],
        "serving": age is not None and age <= HEALTHY_POLL_AGE,
        "projects": config["projects"],
        "recipient": config["recipient"],
        "routes": config.get("routes") or {},
        "timers": [_schedule_label(timer) for timer in state.get("timers") or []],
        "last_poll_at": state.get("last_poll_at"),
    }


def serve(
    channel: OwnerChannel,
    *,
    default_timers: list[str] | None = None,
    poll_seconds: int = 10,
    should_stop: Callable[[], bool] = lambda: False,
    sleep: Callable[[float], None] = time.sleep,
    log: Callable[[str], None] = lambda line: None,
) -> None:
    """Run the single poller for this token; only this loop writes channel state."""
    state = _read_state(channel.data_root)
    state_path(channel.data_root).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    channel.reminders = state.setdefault("reminders", {})
    channel.cards = state.setdefault("cards", {})
    channel.timers = state.setdefault("timers", [])
    channel.cursors = state.setdefault("cursors", {})
    channel.alerted = state.setdefault("alerted", {})
    channel.finished = state.setdefault("finished", {})
    channel.digest_at = state.setdefault("digest_at", {})
    if not state.get("timers_seeded"):
        for word in default_timers or []:
            channel.add_timer(word)
        state["timers_seeded"] = True
    ready = False
    failures = 0
    while not should_stop():
        try:
            if not ready:
                channel.client.set_commands(COMMANDS)
                me = channel.client.get_me()
                channel.bot_username = me.get("username")
                if me.get("id") is not None and state.get("bot_id") != me["id"]:
                    state["offset"] = None if state.get("bot_id") else state.get("offset")
                    state["bot_id"] = me["id"]
                ready = True
            channel.deliver_pending()
            channel.deliver_questions()
            channel.resend_due()
            channel.run_timers()
            channel.watch_events()
            _atomic_write(state_path(channel.data_root), state)
            updates = channel.client.get_updates(state.get("offset"), poll_seconds)
            for update in updates:
                try:
                    with owner_sinks.quiet(channel.client):
                        channel.handle_update(update)
                except (OwnerChannelError, CollaborationRegistryError, KeyError, ValueError) as error:
                    log(f"update {update.get('update_id')} failed: {type(error).__name__}")
                state["offset"] = update["update_id"] + 1
                _atomic_write(state_path(channel.data_root), state)
            state["last_poll_at"] = datetime.now(UTC).isoformat()
            _atomic_write(state_path(channel.data_root), state)
            failures = 0
        except TelegramError as error:
            failures += 1
            log(f"poll failed: {error}")
            sleep(error.retry_after or min(60, 2**failures))
        except (CollaborationRegistryError, OSError, ValueError, KeyError) as error:
            failures += 1
            log(f"tick failed: {type(error).__name__}")
            sleep(min(60, 2**failures))


def build_channel(
    data_root: Path | str | None, *, log: Callable[[str], None] = lambda line: None
) -> OwnerChannel:
    """Telegram when bound, plus a one-way copy to every webhook sink."""
    config = load_config(data_root)
    telegram = bool(config.get("owner_id") and config.get("token_file"))
    entries = config.get("sinks") or []
    if not telegram and not entries:
        raise OwnerChannelError("bind Telegram or add a webhook sink first")
    client: Any = TelegramClient(read_token(config["token_file"])) if telegram else owner_sinks.NullClient()
    if entries:
        try:
            sinks = owner_sinks.Sinks(entries, log=log)
        except owner_sinks.SinkError as error:
            raise OwnerChannelError(str(error)) from None
        client = owner_sinks.MirroredClient(client, sinks, interactive=telegram)
    return OwnerChannel(client, config, data_root)


__all__ = [
    "OwnerChannel",
    "OwnerChannelError",
    "add_sink",
    "bind",
    "remove_sink",
    "test_sink",
    "unbind",
    "TelegramClient",
    "TelegramError",
    "build_channel",
    "channel_status",
    "configure",
    "load_config",
    "read_token",
    "serve",
]
