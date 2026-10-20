"""Scoped controller knowledge and preferences; no native account discovery.

Member names are planning identities, not authentication. OS permissions/sandbox
remain the security boundary for agents running as the same operating-system user.
Imported prose is candidate data, never executable instructions or approval.
"""

from __future__ import annotations

from datetime import UTC, datetime
import getpass
import hashlib
import json
import os
from pathlib import Path
import re
import stat

from .collaboration_registry import _atomic_write, _locked, _now, default_data_root
from .project_context import _text
from .runtime_origin import current, require_local

SCHEMA = "context.controller-memory/v1"
PROFILE_FIELDS = {
    "language",
    "report_format",
    "report_triggers",
    "question_policy",
    "tool_preferences",
    "model_preferences",
    "integration_base",
}
KINDS = {"preference", "decision", "lesson", "constraint", "reference"}
SCOPES = {"private-global", "private-project"}
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}")


def sha(value):
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()


def _project(project_id):
    if not isinstance(project_id, str) or not _ID.fullmatch(project_id):
        raise ValueError("invalid project identity")
    return project_id


def member(value=None):
    identity = value or os.environ.get("CONTINUITY_MEMBER") or getpass.getuser()
    if not isinstance(identity, str) or not _ID.fullmatch(identity):
        raise ValueError("member must be a stable local planning name")
    return identity


def directory(data_root, identity):
    # Avoid accidental reuse of another OS user's private store on shared disks.
    user = str(os.getuid()) if hasattr(os, "getuid") else getpass.getuser()
    return (
        Path(data_root or default_data_root())
        / "private-memory"
        / sha({"os_user": user, "member": member(identity)})
    )


def _read(path, default=None, limit=2 * 1024 * 1024):
    if not path.exists():
        return default
    if path.is_symlink() or path.stat().st_size > limit:
        raise ValueError("memory document is a symlink or exceeds its budget")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        raise ValueError("invalid memory document; original preserved") from None


def _save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _atomic_write(path, value)  # mkstemp creates owner-only files; fsync + replace.


def store(data_root, identity):
    path = directory(data_root, identity) / "records.json"
    value = _read(path, {"schema_version": SCHEMA, "revision": 0, "records": {}})
    if value.get("schema_version") != SCHEMA:
        raise ValueError("unsupported memory schema")
    return path, value


def remember(
    data_root,
    project_id,
    *,
    identity,
    record_id,
    kind,
    text,
    source,
    scope="private-project",
    expected_revision=None,
    expires_at=None,
):
    _project(project_id)
    if not _ID.fullmatch(record_id) or kind not in KINDS or scope not in SCOPES:
        raise ValueError("invalid memory ID, kind or scope")
    _text(text, "memory text", 8192)
    _text(source, "memory source", 2048)
    if expires_at:
        parsed = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("expiry needs a timezone")
    path, _ = store(data_root, identity)
    with _locked(path):
        _, document = store(data_root, identity)
        old = document["records"].get(record_id)
        if old and (expected_revision is None or old["revision"] != expected_revision):
            raise ValueError("memory revision changed; inspect before replacing")
        value = {
            "record_id": record_id,
            "kind": kind,
            "text": text,
            "source": source,
            "scope": scope,
            "project_id": project_id if scope == "private-project" else None,
            "owner": member(identity),
            "status": "candidate",
            "revision": (old or {}).get("revision", 0) + 1,
            "updated_at": _now(),
            "expires_at": expires_at,
        }
        document["records"][record_id] = value
        document["revision"] += 1
        if len(json.dumps(document, ensure_ascii=False).encode()) > 2 * 1024 * 1024:
            raise ValueError(
                "memory exceeds bounded store; revoke/export old entries explicitly"
            )
        _save(
            directory(data_root, identity)
            / "history"
            / record_id
            / (str(value["revision"]) + ".json"),
            value,
        )
        _save(path, document)
    return value


def decide(data_root, identity, record_id, *, status, expected_revision, reason):
    if status not in {"approved", "revoked"}:
        raise ValueError("status must be approved or revoked")
    _text(reason, "adoption/revocation reason", 2048)
    path, _ = store(data_root, identity)
    with _locked(path):
        _, document = store(data_root, identity)
        value = document["records"].get(record_id)
        if not value or value["revision"] != expected_revision:
            raise ValueError("memory revision changed or missing")
        value.update(
            status=status,
            reason=reason,
            revision=value["revision"] + 1,
            updated_at=_now(),
            adoption_basis="operator-declared; not external-action authorization",
        )
        document["revision"] += 1
        _save(
            directory(data_root, identity)
            / "history"
            / record_id
            / (str(value["revision"]) + ".json"),
            value,
        )
        _save(path, document)
    return value


def records(data_root, project_id, identity, *, query=None, include_candidates=False):
    _project(project_id)
    _, value = store(data_root, identity)
    now = datetime.now(UTC)
    rows = []
    for record in value["records"].values():
        if record["owner"] != member(identity) or (
            record["project_id"] not in {None, project_id}
        ):
            continue
        if (
            record["status"] == "revoked"
            or not include_candidates
            and record["status"] != "approved"
        ):
            continue
        if (
            record.get("expires_at")
            and datetime.fromisoformat(record["expires_at"].replace("Z", "+00:00"))
            <= now
        ):
            continue
        consent = record.get("consent_id")
        if consent:
            policy = _read(
                directory(data_root, identity) / "consents.json", {"items": {}}
            )["items"].get(consent)
            if not policy or policy["status"] != "active":
                continue
        if (
            query
            and query.casefold()
            not in json.dumps(record, ensure_ascii=False).casefold()
        ):
            continue
        rows.append(record)
    return sorted(rows, key=lambda r: (r["updated_at"], r["record_id"]), reverse=True)


def share(
    root, data_root, project_id, identity, record_id, *, expected_revision, reason
):
    _text(reason, "sharing reason", 2048)
    value = next(
        (
            r
            for r in records(data_root, project_id, identity)
            if r["record_id"] == record_id
        ),
        None,
    )
    if (
        not value
        or value["revision"] != expected_revision
        or value["scope"] != "private-project"
    ):
        raise ValueError(
            "sharing requires current approved project memory; global/private candidates cannot be published"
        )
    # A reviewed Git artifact, not an external write or approval assertion.
    exported = {
        k: value[k]
        for k in ("record_id", "kind", "text", "revision", "project_id", "expires_at")
    }
    exported.update(
        schema_version=SCHEMA,
        authority="repository-declared-data; does not override current instructions",
        sharing_reason=reason,
    )
    target = (
        Path(root)
        / ".continuity"
        / "knowledge"
        / (record_id + "." + sha(exported)[:16] + ".json")
    )
    if (Path(root) / ".continuity").is_symlink() or target.parent.is_symlink():
        raise ValueError("shared-memory directories must not be symlinks")
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if _read(target) != exported:
            raise ValueError("shared artifact differs; not overwritten")
    else:
        with target.open("x", encoding="utf-8") as handle:
            json.dump(exported, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
    return {
        "status": "exported-for-review",
        "path": str(target),
        "sha256": sha(exported),
        "published": False,
        "warning": "Review content and explicitly commit this exact artifact; ignore rules and OS ACLs are separate.",
    }


def profile(data_root, project_id, identity, *, patch=None, expected_revision=None):
    name = "project-" + _project(project_id) if project_id is not None else "global"
    path = directory(data_root, identity) / "profiles" / (name + ".json")
    if patch is None:
        return _read(path, {"revision": 0, "values": {}})
    if not isinstance(patch, dict) or set(patch) - PROFILE_FIELDS:
        raise ValueError(
            "unknown profile field; profiles cannot grant tool/merge/deploy/account permission"
        )
    for key, value in patch.items():
        _text(value, "profile " + key, 2048)
    with _locked(path):
        previous = profile(data_root, project_id, identity)
        if expected_revision != previous["revision"]:
            raise ValueError("profile revision changed")
        value = {
            "revision": previous["revision"] + 1,
            "values": {**previous["values"], **patch},
            "updated_at": _now(),
        }
        _save(path, value)
    return value


def effective(root, data_root, project_id, identity, *, include_private=True):
    _project(project_id)
    if (Path(root) / ".continuity").is_symlink():
        raise ValueError("repository profile directory must not be a symlink")
    declared = _read(
        Path(root) / ".continuity" / "controller-profile.json",
        {"defaults": {}, "locked_fields": []},
        limit=32768,
    )
    if (
        not isinstance(declared, dict)
        or not isinstance(declared.get("defaults"), dict)
        or not isinstance(declared.get("locked_fields"), list)
    ):
        raise ValueError("invalid repository controller profile")
    if (
        set(declared) - {"defaults", "locked_fields"}
        or set(declared["defaults"]) - PROFILE_FIELDS
        or set(declared["locked_fields"]) - PROFILE_FIELDS
    ):
        raise ValueError("invalid repository controller profile")
    for key, value in declared["defaults"].items():
        _text(value, "project profile " + key, 2048)
    personal = (
        profile(data_root, project_id, identity)
        if include_private
        else {"revision": 0, "values": {}}
    )
    global_profile = (
        profile(data_root, None, identity)
        if include_private
        else {"revision": 0, "values": {}}
    )
    values = {**global_profile["values"], **declared["defaults"], **personal["values"]}
    conflicts = []
    for key in declared["locked_fields"]:
        if key not in declared["defaults"]:
            raise ValueError("locked field needs an explicit project value")
        if (
            key in personal["values"]
            and personal["values"][key] != declared["defaults"][key]
        ):
            conflicts.append(key)
        values[key] = declared["defaults"][key]
    result = {
        "values": values,
        "personal_revision": personal["revision"],
        "global_revision": global_profile["revision"],
        "project_sha256": sha(declared),
        "conflicts": conflicts,
        "authority": "workflow preferences within host/current user/project instructions; no permission grant",
    }
    result["profile_sha256"] = sha(result)
    return result


def consent(
    data_root,
    identity,
    project_id,
    *,
    consent_id,
    provider=None,
    files=None,
    revoke=False,
):
    _project(project_id)
    if not _ID.fullmatch(consent_id):
        raise ValueError("invalid consent ID")
    path = directory(data_root, identity) / "consents.json"
    with _locked(path):
        doc = _read(path, {"items": {}})
        if revoke:
            if consent_id not in doc["items"]:
                raise ValueError("consent does not exist")
            doc["items"][consent_id]["status"] = "revoked"
        else:
            if provider not in {"claude", "codex"} or not files or len(files) > 32:
                raise ValueError(
                    "explicit provider and 1..32 exact Markdown/text paths required"
                )
            origin = current(provider)
            variable = "CLAUDE_CONFIG_DIR" if provider == "claude" else "CODEX_HOME"
            base = Path(
                os.environ.get(variable)
                or Path.home() / (".claude" if provider == "claude" else ".codex")
            )
            if not base.is_absolute() or base.is_symlink():
                raise ValueError(
                    "native profile must be an absolute, non-symlink origin path"
                )
            base = base.resolve()
            selected = []
            for item in files:
                file = Path(item)
                if (
                    not file.is_absolute()
                    or ".." in file.parts
                    or not file.is_relative_to(base)
                ):
                    raise ValueError(
                        "source must be an exact path under the owning profile"
                    )
                if file.suffix.lower() not in {".md", ".txt"} or re.search(
                    r"auth|credential|secret|token|session|rollout|history|settings|config",
                    file.name,
                    re.I,
                ):
                    raise ValueError(
                        "account/config/transcript sources are forbidden; select a memory note"
                    )
                selected.append(str(file))
            candidate = {
                "project_id": project_id,
                "owner": member(identity),
                "origin": origin,
                "base": str(base),
                "files": sorted(set(selected)),
                "status": "active",
            }
            old = doc["items"].get(consent_id)
            if old and old != candidate:
                raise ValueError(
                    "consent ID already exists; revoke it and use a new ID for changed scope"
                )
            doc["items"][consent_id] = candidate
        _save(path, doc)
    return {
        "consent_id": consent_id,
        "status": doc["items"][consent_id]["status"],
        "permission": "origin-local read only; not adoption/share/writeback",
    }


def _source_text(base, file):
    # Walk relative components through directory descriptors. No symlink hop,
    # recursive discovery, raw transcripts, pipes, or follow-up Markdown links.
    if not hasattr(os, "O_NOFOLLOW"):
        raise ValueError(
            "secure native intake unavailable on this OS; use explicit reviewed memory input"
        )
    descriptor = os.open(base, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        parts = Path(file).relative_to(base).parts
        for part in parts[:-1]:
            child = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor
            )
            os.close(descriptor)
            descriptor = child
        source = os.open(
            parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor
        )
        with os.fdopen(source, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size > 65536:
                raise ValueError("native note must be a regular file within 64 KiB")
            raw = stream.read(65537)
            after = os.fstat(stream.fileno())
            if len(raw) > 65536 or (before.st_mtime_ns, before.st_size) != (
                after.st_mtime_ns,
                after.st_size,
            ):
                raise ValueError("source changed during read; retry explicitly")
        return raw.decode("utf-8")
    finally:
        os.close(descriptor)


def ingest(data_root, project_id, identity, consent_id):
    policy = _read(directory(data_root, identity) / "consents.json", {"items": {}})[
        "items"
    ].get(consent_id)
    if (
        not policy
        or policy["status"] != "active"
        or policy["owner"] != member(identity)
        or policy["project_id"] != project_id
    ):
        raise ValueError("no active owner/project consent; no source was read")
    require_local(policy["origin"], policy["origin"]["provider"])
    path, _ = store(data_root, identity)
    candidates = []
    for file in policy["files"]:
        content = _source_text(policy["base"], file)
        _text(content, "native note", 65536)  # Secret assignments rejected, not copied.
        key = "native-" + sha({"consent": consent_id, "file": file})[:24]
        candidates.append((key, content, sha(content)))
    with _locked(path):
        # Revocation is rechecked before publishing any imported candidate.
        active = _read(directory(data_root, identity) / "consents.json", {"items": {}})[
            "items"
        ].get(consent_id)
        if active != policy:
            raise ValueError("consent changed; imported data discarded")
        _, doc = store(data_root, identity)
        changed = []
        for key, content, content_hash in candidates:
            old = doc["records"].get(key)
            if old and old.get("content_sha256") == content_hash:
                continue
            doc["records"][key] = {
                "record_id": key,
                "kind": "lesson",
                "text": content,
                "source": "native-note:"
                + sha({"file": key, "origin": policy["origin"]}),
                "scope": "private-project",
                "project_id": project_id,
                "owner": member(identity),
                "status": "candidate",
                "revision": (old or {}).get("revision", 0) + 1,
                "updated_at": _now(),
                "expires_at": None,
                "consent_id": consent_id,
                "content_sha256": content_hash,
            }
            _save(
                directory(data_root, identity)
                / "history"
                / key
                / (str(doc["records"][key]["revision"]) + ".json"),
                doc["records"][key],
            )
            changed.append(key)
        if changed:
            doc["revision"] += 1
            if len(json.dumps(doc, ensure_ascii=False).encode()) > 2 * 1024 * 1024:
                raise ValueError(
                    "native candidates exceed bounded store; nothing adopted"
                )
            _save(path, doc)
    return {
        "status": "candidates-only",
        "changed_ids": changed,
        "unchanged_sources": len(candidates) - len(changed),
        "links_followed": False,
        "shared": False,
    }


def refresh_consents(data_root, project_id, identity):
    """Refresh exact notes after one grant without blocking ordinary startup."""
    _project(project_id)
    document = _read(directory(data_root, identity) / "consents.json", {"items": {}})
    refreshed, unavailable = [], []
    for consent_id, item in sorted(document.get("items", {}).items()):
        if (
            item.get("status") != "active"
            or item.get("owner") != member(identity)
            or item.get("project_id") != project_id
        ):
            continue
        try:
            result = ingest(data_root, project_id, identity, consent_id)
            refreshed.append(
                {
                    "consent_id": consent_id,
                    "changed": len(result["changed_ids"]),
                    "unchanged": result["unchanged_sources"],
                }
            )
        except (OSError, ValueError) as exc:
            unavailable.append({"consent_id": consent_id, "reason": str(exc)[:240]})
    return {"refreshed": refreshed, "unavailable": unavailable}
