"""Webhook sinks for the owner channel: one-way copies of proactive messages.

Telegram stays the interactive channel. A sink receives the same bounded
summaries as plain text through Apprise (BSD-2-Clause), which covers Feishu,
Lark, DingTalk, WeCom, Slack, Discord, Teams, ntfy, Bark, e-mail and generic
JSON endpoints. Sink URLs carry credentials, so the config stores only the path
of a private file holding each URL.
"""

from __future__ import annotations

import html
import os
import re
import stat
import threading
import urllib.parse
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

INSTALL_HINT = "webhook sinks need the optional extra: pip install 'continuity-plane[webhooks]'"
MAX_SINKS = 8
_NAME_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")
_TAG_RE = re.compile(r"<[^>]+>")
_LINK_RE = re.compile(r"<a\s[^>]*>(.*?)</a>", re.S)
_WINDOWS = os.name == "nt"


class SinkError(ValueError):
    pass


def read_secret(path: str | Path, label: str = "secret file") -> str:
    """Read a credential file that only the current user may read.

    Windows has no POSIX mode bits (stat reports 0o666 for any writable file);
    there the file must live in the user profile, whose ACL is private by
    default, and only emptiness is checked.
    """
    path = Path(path).expanduser()
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
        value = path.read_text(encoding="utf-8").strip()
    except OSError:
        raise SinkError(f"{label} cannot be read") from None
    if not _WINDOWS and mode & 0o077:
        raise SinkError(f"{label} must not be readable by group or others")
    if not value:
        raise SinkError(f"{label} is empty")
    return value


def normalize_url(url: str) -> str:
    """Accept the webhook address as copied from the service's admin page.

    Apprise 2.2.0 parses native Lark, WeCom, Slack and Discord addresses but not
    Feishu or DingTalk ones, so those two are rewritten to Apprise schemes.
    """
    url = url.strip()
    parts = urllib.parse.urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme == "https" and host == "open.feishu.cn":
        match = re.fullmatch(r"/open-apis/bot/v2/hook/([\w-]+)/?", parts.path)
        if match:
            return f"feishu://{match.group(1)}"
    if parts.scheme == "https" and host == "oapi.dingtalk.com" and parts.path == "/robot/send":
        query = urllib.parse.parse_qs(parts.query)
        token = (query.get("access_token") or [""])[0]
        secret = (query.get("secret") or [""])[0]
        if re.fullmatch(r"\w+", token):
            prefix = f"{urllib.parse.quote(secret, safe='')}@" if secret else ""
            return f"dingtalk://{prefix}{token}"
    return url


def to_plain_text(text: str) -> str:
    """Telegram HTML to plain text; deep links are Telegram-only, so keep labels."""
    text = _LINK_RE.sub(r"\1", text)
    return html.unescape(_TAG_RE.sub("", text)).strip()


def keyboard_hint(keyboard: list[list[dict[str, str]]] | None, interactive: bool) -> str:
    labels = [button.get("text", "") for row in keyboard or [] for button in row]
    labels = [label for label in labels if label]
    if not labels:
        return ""
    where = "在 Telegram 里点选" if interactive else "可选"
    return f"\n\n（{where}：{' · '.join(labels[:8])}）"


def _apprise_notifier(url: str) -> Callable[[str, str | None], bool]:
    try:
        import apprise
    except ImportError:
        raise SinkError(INSTALL_HINT) from None
    target = apprise.Apprise()
    if not target.add(normalize_url(url)):
        raise SinkError("sink URL is not a supported webhook address")

    def send(body: str, image: str | None = None) -> bool:
        attach = apprise.AppriseAttachment(image) if image else None
        return bool(target.notify(body=body, body_format=apprise.NotifyFormat.TEXT, attach=attach))

    return send


class Sinks:
    """Named webhook targets; a failing sink never blocks the others."""

    def __init__(
        self,
        entries: list[dict[str, Any]],
        *,
        factory: Callable[[str], Callable[[str, str | None], bool]] | None = None,
        log: Callable[[str], None] = lambda line: None,
    ):
        self.log = log
        factory = factory or _apprise_notifier
        self.targets: list[tuple[str, Callable[[str, str | None], bool]]] = []
        for entry in entries[:MAX_SINKS]:
            url = read_secret(entry["url_file"], f"sink {entry['name']} URL file")
            self.targets.append((entry["name"], factory(url)))

    def send(self, text: str, image: str | None = None) -> dict[str, bool]:
        results = {}
        for name, send in self.targets:
            try:
                results[name] = send(text, image)
            except Exception as error:  # noqa: BLE001 - one sink must not stop delivery
                results[name] = False
                self.log(f"sink {name} failed: {type(error).__name__}")
            if not results[name]:
                self.log(f"sink {name} did not accept the message")
        return results


class NullClient:
    """Stands in for Telegram when only webhook sinks are configured."""

    def __init__(self, sleep: Callable[[float], None] | None = None):
        self._sleep = sleep or threading.Event().wait
        self._next = 0

    def _id(self) -> dict[str, Any]:
        self._next += 1
        return {"message_id": self._next}

    def get_updates(self, offset, timeout):
        self._sleep(timeout)
        return []

    def get_me(self):
        return {}

    def send_message(self, chat_id, text, keyboard=None, rich=False):
        return self._id()

    def edit_message(self, chat_id, message_id, text, keyboard=None, rich=False):
        return {}

    def edit_buttons(self, chat_id, message_id, keyboard):
        return {}

    def send_photo(self, chat_id, path, caption=""):
        return self._id()

    def answer_callback(self, callback_id, text=""):
        return None

    def set_commands(self, commands):
        return None

    def set_reaction(self, chat_id, message_id, emoji):
        return None

    def pin(self, chat_id, message_id):
        return None

    def unpin(self, chat_id, message_id):
        return None


class MirroredClient:
    """Sends through the primary client and copies proactive messages to sinks.

    Edits, callback answers and replies to the owner's own commands stay on the
    primary channel; serve() wraps update handling in quiet().
    """

    def __init__(self, primary: Any, sinks: Sinks, *, interactive: bool):
        self.primary = primary
        self.sinks = sinks
        self.interactive = interactive
        self._quiet = 0

    @contextmanager
    def quiet(self) -> Iterator[None]:
        self._quiet += 1
        try:
            yield
        finally:
            self._quiet -= 1

    def _mirror(self, text: str, keyboard=None, image: str | None = None, rich: bool = False) -> None:
        if self._quiet or not self.sinks.targets:
            return
        body = (to_plain_text(text) if rich else text.strip()) + keyboard_hint(keyboard, self.interactive)
        if body.strip() or image:
            self.sinks.send(body.strip(), image)

    def send_message(self, chat_id, text, keyboard=None, rich=False):
        sent = self.primary.send_message(chat_id, text, keyboard, rich)
        self._mirror(text, keyboard, rich=rich)
        return sent

    def send_photo(self, chat_id, path, caption=""):
        sent = self.primary.send_photo(chat_id, path, caption)
        self._mirror(caption or "截图", image=path)
        return sent

    def send_rich(self, chat_id, html_text, keyboard=None, *, fallback=""):
        """Webhooks get the classic rendering of a rich message as plain text."""
        sent = self.primary.send_rich(chat_id, html_text, keyboard, fallback=fallback)
        self._mirror(fallback or html_text, keyboard, rich=True)
        return sent

    def __getattr__(self, name: str) -> Any:
        return getattr(self.primary, name)


@contextmanager
def quiet(client: Any) -> Iterator[None]:
    if isinstance(client, MirroredClient):
        with client.quiet():
            yield
    else:
        yield


def validate_name(name: str) -> str:
    if not _NAME_RE.fullmatch(name):
        raise SinkError("sink name must be lowercase letters, digits or '-', at most 32")
    return name


__all__ = [
    "INSTALL_HINT",
    "MirroredClient",
    "NullClient",
    "SinkError",
    "Sinks",
    "keyboard_hint",
    "normalize_url",
    "quiet",
    "read_secret",
    "to_plain_text",
    "validate_name",
]
