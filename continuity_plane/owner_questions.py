"""Durable questions from agents to the owner; the first answer wins."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
from pathlib import Path
from typing import Any

from .collaboration_inbox import send_message
from .collaboration_registry import (
    CollaborationRegistryError,
    _ASSIGNEE_RE,
    _atomic_write,
    _load,
    _locked,
    _record_change,
    default_data_root,
    project_data_dir,
)
from .vocabulary_memory import _SECRET_RE

SCHEMA_VERSION = "context.owner-questions/v1alpha1"
DEFAULT_EXPIRY_HOURS = 24
URGENCIES = ("normal", "urgent")
MAX_OPTIONS = 6
MAX_QUESTION_CHARS = 1000
MAX_OPTION_CHARS = 60
MAX_CLOSED = 200


def _root(data_root: Path | str | None) -> Path:
    return Path(data_root) if data_root is not None else default_data_root()


def _path(data_root: Path | str | None, project_id: str) -> Path:
    directory = project_data_dir(_root(data_root), project_id)
    if not (directory / "project.json").is_file():
        raise CollaborationRegistryError("project is not registered")
    return directory / "owner-questions.json"


def _questions(path: Path) -> list[dict[str, Any]]:
    document = _load(path)
    if document is None:
        return []
    if document.get("schema_version") != SCHEMA_VERSION:
        raise CollaborationRegistryError("owner questions file is invalid")
    return document["questions"]


def _save(path: Path, questions: list[dict[str, Any]]) -> None:
    closed = [q for q in questions if q["status"] != "open"]
    dropped = {q["question_id"] for q in closed[:-MAX_CLOSED]}
    _atomic_write(
        path,
        {
            "schema_version": SCHEMA_VERSION,
            "questions": [q for q in questions if q["question_id"] not in dropped],
        },
    )


def ask(
    data_root: Path | str | None,
    *,
    project_id: str,
    asker: str,
    question: str,
    options: list[str] | None = None,
    urgency: str = "normal",
    expires_hours: float = DEFAULT_EXPIRY_HOURS,
    resume_on_answer: bool = False,
    task_id: str | None = None,
) -> dict[str, Any]:
    """File a question. Repeating an open question returns the existing one."""
    text = question.strip() if isinstance(question, str) else ""
    choices = [str(option).strip() for option in options or []]
    if not _ASSIGNEE_RE.fullmatch(asker or ""):
        raise CollaborationRegistryError("asker is invalid")
    if not text or len(text) > MAX_QUESTION_CHARS:
        raise CollaborationRegistryError(f"question must be 1..{MAX_QUESTION_CHARS} characters")
    if len(choices) > MAX_OPTIONS or any(
        not choice or len(choice) > MAX_OPTION_CHARS for choice in choices
    ):
        raise CollaborationRegistryError(
            f"use up to {MAX_OPTIONS} options of 1..{MAX_OPTION_CHARS} characters"
        )
    if urgency not in URGENCIES or expires_hours <= 0:
        raise CollaborationRegistryError("urgency or expiry is invalid")
    if _SECRET_RE.search(" ".join([text, *choices])):
        raise CollaborationRegistryError("credential assignments must not be stored in questions")
    question_id = "q-" + hashlib.sha256(f"{asker}\n{task_id}\n{text}".encode()).hexdigest()[:10]
    now = datetime.now(UTC)
    path = _path(data_root, project_id)
    with _locked(path):
        questions = _questions(path)
        existing = next(
            (q for q in questions if q["question_id"] == question_id and q["status"] == "open"),
            None,
        )
        if existing:
            return existing
        questions = [q for q in questions if q["question_id"] != question_id]
        item = {
            "question_id": question_id,
            "asker": asker,
            "question": text,
            "options": choices,
            "urgency": urgency,
            "task_id": task_id,
            "resume_on_answer": bool(resume_on_answer),
            "status": "open",
            "created_at": now.isoformat(),
            "expires_at": (now + timedelta(hours=expires_hours)).isoformat(),
            "delivered_at": None,
            "answer": None,
            "answered_at": None,
            "answered_via": None,
        }
        questions.append(item)
        _save(path, questions)
    _record_change(
        _root(data_root),
        project_id,
        "owner.question.asked",
        task_id,
        question_id=question_id,
        asker=asker,
        urgency=urgency,
    )
    return item


def _tell_asker(data_root, project_id: str, item: dict[str, Any], body: str) -> None:
    send_message(
        data_root,
        project_id=project_id,
        message_id=f"answer-{item['question_id']}-{item['status']}",
        sender="owner",
        recipient=item["asker"],
        body=body[:1900],
        task_id=item["task_id"],
    )


def list_questions(
    data_root: Path | str | None, *, project_id: str, status: str | None = "open"
) -> list[dict[str, Any]]:
    """List questions; questions past their expiry close here and tell the asker."""
    path = _path(data_root, project_id)
    now = datetime.now(UTC).isoformat()
    expired: list[dict[str, Any]] = []
    with _locked(path):
        questions = _questions(path)
        for item in questions:
            if item["status"] == "open" and item["expires_at"] <= now:
                item["status"] = "expired"
                expired.append(item)
        if expired:
            _save(path, questions)
    for item in expired:
        _tell_asker(
            data_root,
            project_id,
            item,
            f"问题已过期，owner 没有回答：{item['question']}\n编号：{item['question_id']}",
        )
    return [q for q in questions if status is None or q["status"] == status]


def mark_delivered(
    data_root: Path | str | None,
    *,
    project_id: str,
    question_id: str,
    delivered: bool = True,
) -> None:
    path = _path(data_root, project_id)
    with _locked(path):
        questions = _questions(path)
        for item in questions:
            if item["question_id"] == question_id:
                item["delivered_at"] = datetime.now(UTC).isoformat() if delivered else None
        _save(path, questions)


def answer(
    data_root: Path | str | None,
    *,
    project_id: str,
    question_id: str,
    text: str,
    via: str,
) -> dict[str, Any]:
    """Record the first answer and return it to the asker; later answers change nothing."""
    value = text.strip() if isinstance(text, str) else ""
    if not value:
        raise CollaborationRegistryError("answer must not be empty")
    list_questions(data_root, project_id=project_id, status=None)
    path = _path(data_root, project_id)
    with _locked(path):
        questions = _questions(path)
        item = next((q for q in questions if q["question_id"] == question_id), None)
        if item is None:
            raise CollaborationRegistryError("question not found")
        if item["status"] != "open":
            return {**item, "accepted": False}
        item.update(
            status="answered",
            answer=value,
            answered_at=datetime.now(UTC).isoformat(),
            answered_via=via,
        )
        _save(path, questions)
    _tell_asker(
        data_root,
        project_id,
        item,
        f"owner 的回答：{value}\n问题：{item['question']}\n编号：{question_id}",
    )
    _record_change(
        _root(data_root),
        project_id,
        "owner.question.answered",
        item["task_id"],
        question_id=question_id,
        asker=item["asker"],
        via=via,
        resume_requested=item["resume_on_answer"],
    )
    return {**item, "accepted": True}


__all__ = ["answer", "ask", "list_questions", "mark_delivered"]
