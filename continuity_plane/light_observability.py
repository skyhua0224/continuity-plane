"""Bounded Continuity policy parsing and local lightweight observations."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import sys
import threading
import time
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml


OBSERVATION_SCHEMA_VERSION = "context.light-observation/v1alpha1"
CORE_OBSERVATION_SCHEMA_VERSION = "context.codex-hook-observation/v1alpha1"
POLICY_SCHEMA_VERSION = "context.observability-policy/v1alpha1"
POLICY_FILENAME = "observability-policy.yaml"
STATE_OBSERVATION_DIRECTORY = "state-mcp-events"
CORE_OBSERVATION_DIRECTORY = "live-events"
MAX_OBSERVATION_BYTES = 2 * 1024
MAX_REPORT_FILE_BYTES = 4 * 1024 * 1024
ORPHAN_RETENTION_SECONDS = 24 * 60 * 60
LATENCY_BUCKETS_MS = (1, 5, 10, 50, 100, 500, 1000, 5000)
WRITE_TOOLS = {
    "continuity_autorun",
    "continuity_checkpoint:create",
    "continuity_work_complete",
    "continuity_work_transition",
    "continuity_work_activate",
    "continuity_claim_recover",
}
_APPEND_LOCK = threading.Lock()
_EXTRA_INTEGER_FIELDS = {
    "cached_input_tokens",
    "duplicate_resumes",
    "failed_calls",
    "input_tokens",
    "output_tokens",
    "packet_bytes",
    "peak_rss_bytes",
    "read_calls",
    "reasoning_tokens",
    "request_bytes",
    "request_bytes_total",
    "response_bytes",
    "response_bytes_total",
    "resume_calls",
    "rss_bytes",
    "session_log_bytes",
    "state_store_bytes",
    "write_calls",
}
_EXTRA_BOOLEAN_FIELDS = {"observation_degraded"}
_EXTRA_STRING_FIELDS = {"latency_buckets", "measurement_source", "tool_name"}
_TOKEN_FIELDS = {
    "cached_input_tokens",
    "input_tokens",
    "output_tokens",
    "reasoning_tokens",
}

_BALANCED = {
    "preset": "balanced",
    "resume": {"explicit_policy": "once_per_connection"},
    "checkpoint": {
        "on_pre_compact": True,
        "on_work_complete": True,
        "after_state_writes": False,
        "min_interval_seconds": 30,
    },
    "verification": {"startup_scope": "recent", "deep_verify": "manual"},
    "observability": {
        "mode": "minimal",
        "probes_enabled": True,
        "slow_call_threshold_ms": 1000,
        "resource_sampling": "boundaries_failures_and_slow",
        "retention_max_bytes": 64 * 1024 * 1024,
    },
}

_PRESETS = {
    "balanced": _BALANCED,
    "diagnostic": {
        **_BALANCED,
        "preset": "diagnostic",
        "observability": {
            **_BALANCED["observability"],
            "mode": "diagnostic",
            "resource_sampling": "every_call",
        },
    },
    "reliability-first": {
        **_BALANCED,
        "preset": "reliability-first",
        "checkpoint": {
            **_BALANCED["checkpoint"],
            "after_state_writes": True,
            "min_interval_seconds": 0,
        },
        "verification": {"startup_scope": "recent", "deep_verify": "on_error"},
    },
}

_POLICY_FIELDS = {
    "preset",
    "resume",
    "checkpoint",
    "verification",
    "observability",
}
_SECTION_FIELDS = {
    "resume": {"explicit_policy"},
    "checkpoint": {
        "on_pre_compact",
        "on_work_complete",
        "after_state_writes",
        "min_interval_seconds",
    },
    "verification": {"startup_scope", "deep_verify"},
    "observability": {
        "mode",
        "probes_enabled",
        "slow_call_threshold_ms",
        "resource_sampling",
        "retention_max_bytes",
    },
}


class PolicyConfigError(ValueError):
    """A project observability policy is unsafe or malformed."""


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _bounded_int(value: Any, field: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise PolicyConfigError(
            f"{field} must be an integer between {minimum} and {maximum}"
        )
    return value


def _require_bool(value: Any, field: str) -> bool:
    if type(value) is not bool:
        raise PolicyConfigError(f"{field} must be a boolean")
    return value


def _mapping_keys(value: Mapping[Any, Any], field: str) -> set[str]:
    if any(not isinstance(key, str) for key in value):
        raise PolicyConfigError(f"{field} field names must be strings")
    return set(value)


def resolve_policy(
    document: Mapping[str, Any] | None,
    *,
    environment: Mapping[str, str] | None = None,
    require_schema: bool = False,
) -> dict[str, Any]:
    """Resolve a strict policy and migrate the pre-alpha project field in memory."""

    if document is None:
        configured: Mapping[str, Any] = {}
    elif not isinstance(document, Mapping):
        raise PolicyConfigError("observability policy must be an object")
    elif document.get("schema_version") == "context.project/v1alpha1":
        configured = document.get("continuity_policy", {})  # type: ignore[assignment]
    else:
        schema_version = document.get("schema_version")
        if require_schema and schema_version != POLICY_SCHEMA_VERSION:
            raise PolicyConfigError("observability policy schema is unsupported")
        if schema_version is not None and schema_version != POLICY_SCHEMA_VERSION:
            raise PolicyConfigError("observability policy schema is unsupported")
        allowed_document_fields = _POLICY_FIELDS | {"schema_version"}
        unknown_document = (
            _mapping_keys(document, "observability policy") - allowed_document_fields
        )
        if unknown_document:
            raise PolicyConfigError(
                "observability policy contains unsupported fields: "
                + ", ".join(sorted(unknown_document))
            )
        configured = {
            key: value for key, value in document.items() if key != "schema_version"
        }
    if configured is None:
        configured = {}
    if not isinstance(configured, Mapping):
        raise PolicyConfigError("observability policy must be an object")
    unknown = _mapping_keys(configured, "observability policy") - _POLICY_FIELDS
    if unknown:
        raise PolicyConfigError(
            "observability policy contains unsupported fields: "
            + ", ".join(sorted(unknown))
        )
    preset = configured.get("preset", "balanced")
    if not isinstance(preset, str) or preset not in _PRESETS:
        raise PolicyConfigError("continuity_policy.preset is unsupported")
    policy = copy.deepcopy(_PRESETS[preset])
    policy["schema_version"] = POLICY_SCHEMA_VERSION
    for section, allowed_fields in _SECTION_FIELDS.items():
        override = configured.get(section)
        if override is None:
            continue
        if not isinstance(override, Mapping):
            raise PolicyConfigError(f"continuity_policy.{section} must be an object")
        unknown = _mapping_keys(
            override, f"continuity_policy.{section}"
        ) - allowed_fields
        if unknown:
            raise PolicyConfigError(
                f"continuity_policy.{section} contains unsupported fields: "
                + ", ".join(sorted(unknown))
            )
        policy[section].update(override)

    if policy["resume"]["explicit_policy"] not in {
        "once_per_connection",
        "every_call",
    }:
        raise PolicyConfigError("resume.explicit_policy is unsupported")
    for field in ("on_pre_compact", "on_work_complete", "after_state_writes"):
        _require_bool(policy["checkpoint"][field], f"checkpoint.{field}")
    if not policy["checkpoint"]["on_pre_compact"]:
        raise PolicyConfigError("checkpoint.on_pre_compact is a required safety boundary")
    if not policy["checkpoint"]["on_work_complete"]:
        raise PolicyConfigError("checkpoint.on_work_complete is a required authority boundary")
    _bounded_int(
        policy["checkpoint"]["min_interval_seconds"],
        "checkpoint.min_interval_seconds",
        0,
        86_400,
    )
    if policy["verification"]["startup_scope"] not in {"recent", "deep"}:
        raise PolicyConfigError("verification.startup_scope is unsupported")
    if policy["verification"]["deep_verify"] not in {"manual", "on_error", "startup"}:
        raise PolicyConfigError("verification.deep_verify is unsupported")
    if policy["observability"]["mode"] not in {"minimal", "diagnostic"}:
        raise PolicyConfigError("observability.mode is unsupported")
    _require_bool(policy["observability"]["probes_enabled"], "observability.probes_enabled")
    _bounded_int(
        policy["observability"]["slow_call_threshold_ms"],
        "observability.slow_call_threshold_ms",
        1,
        600_000,
    )
    if policy["observability"]["resource_sampling"] not in {
        "disabled",
        "boundaries_failures_and_slow",
        "every_call",
    }:
        raise PolicyConfigError("observability.resource_sampling is unsupported")
    _bounded_int(
        policy["observability"]["retention_max_bytes"],
        "observability.retention_max_bytes",
        1024 * 1024,
        1024 * 1024 * 1024,
    )

    environment = os.environ if environment is None else environment
    temporary_mode = environment.get("CONTINUITY_OBSERVABILITY_MODE")
    if temporary_mode is not None:
        if temporary_mode not in {"minimal", "diagnostic"}:
            raise PolicyConfigError("CONTINUITY_OBSERVABILITY_MODE is invalid")
        policy["observability"]["mode"] = temporary_mode
    return policy


def load_policy(root: Path, *, environment: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Load the independent policy, with an in-memory legacy project migration."""

    control = root / ".continuity"
    path = control / POLICY_FILENAME
    if path.is_file():
        try:
            document = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, yaml.YAMLError) as exc:
            raise PolicyConfigError("observability policy is unavailable or invalid") from exc
        if not isinstance(document, Mapping):
            raise PolicyConfigError("observability policy must be an object")
        return resolve_policy(
            document, environment=environment, require_schema=True
        )
    project_path = control / "project.yaml"
    try:
        project = yaml.safe_load(project_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise PolicyConfigError("project profile is unavailable or invalid") from exc
    if not isinstance(project, Mapping):
        raise PolicyConfigError("project profile must be an object")
    return resolve_policy(project, environment=environment)


def policy_sha256(policy: Mapping[str, Any]) -> str:
    return _sha256(_canonical(policy))


def plugin_data_root(environment: Mapping[str, str] | None = None) -> Path | None:
    environment = os.environ if environment is None else environment
    configured = environment.get("PLUGIN_DATA")
    if configured:
        return Path(configured)
    try:
        return Path.home() / ".codex/plugins/data/continuity-plane"
    except RuntimeError:
        return None


def _try_lock_file(stream: Any) -> bool:
    """Acquire the stable observation lock without waiting on another process."""

    try:
        if os.name == "nt":
            import msvcrt

            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b"\0")
                stream.flush()
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        import fcntl

        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except (ImportError, OSError):
        return False


def _unlock_file(stream: Any) -> None:
    if os.name == "nt":
        import msvcrt

        stream.seek(0)
        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _safe_extra(extra: Mapping[str, Any] | None) -> dict[str, Any]:
    """Admit only schema-declared, bounded, non-sensitive observation fields."""

    if extra is None:
        return {}
    safe: dict[str, Any] = {}
    for key, value in extra.items():
        if key in _EXTRA_INTEGER_FIELDS and type(value) is int and value >= 0:
            safe[key] = value
        elif key in _EXTRA_BOOLEAN_FIELDS and type(value) is bool:
            safe[key] = value
        elif (
            key in _EXTRA_STRING_FIELDS
            and isinstance(value, str)
            and 1 <= len(value) <= 128
        ):
            if key == "tool_name" and not all(
                character.isascii()
                and (character.isalnum() or character in "_.:/-")
                for character in value
            ):
                continue
            if key == "latency_buckets" and not all(
                character.isdigit() or character == "," for character in value
            ):
                continue
            if key != "measurement_source" or value in {
                "host",
                "plugin",
                "unavailable",
            }:
                safe[key] = value
    if safe.get("measurement_source") != "host":
        for field in _TOKEN_FIELDS:
            safe.pop(field, None)
    return safe


def _write_all(descriptor: int, payload: bytes) -> None:
    """Write one complete JSONL record or raise instead of accepting a short write."""

    remaining = memoryview(payload)
    while remaining:
        written = os.write(descriptor, remaining)
        if written <= 0:
            raise OSError("observation write was incomplete")
        remaining = remaining[written:]


def _posix_process_memory(
    *, statm_path: Path = Path("/proc/self/statm")
) -> dict[str, int]:
    """Return current Linux RSS, or an explicitly named POSIX peak fallback."""

    if sys.platform.startswith("linux"):
        try:
            fields = statm_path.read_text(encoding="ascii").split()
            resident_pages = int(fields[1])
            page_size = int(os.sysconf("SC_PAGE_SIZE"))
            if resident_pages >= 0 and page_size > 0:
                return {"rss_bytes": resident_pages * page_size}
        except (IndexError, OSError, UnicodeError, ValueError):
            pass
    try:
        import resource

        maximum_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        multiplier = 1 if sys.platform == "darwin" else 1024
        return {"peak_rss_bytes": int(maximum_rss * multiplier)}
    except (AttributeError, ImportError, OSError, ValueError):
        return {}


def _resource_snapshot(
    project_root: Path, data_root: Path | None, session_id: str
) -> dict[str, int]:
    snapshot: dict[str, int] = {}
    try:
        snapshot["state_store_bytes"] = (
            project_root / ".continuity/state.sqlite3"
        ).stat().st_size
    except OSError:
        snapshot["state_store_bytes"] = 0
    try:
        snapshot["session_log_bytes"] = (
            data_root
            / STATE_OBSERVATION_DIRECTORY
            / f"{_sha256(session_id)}.jsonl"
        ).stat().st_size if data_root is not None else 0
    except OSError:
        snapshot["session_log_bytes"] = 0
    try:
        if os.name == "nt":
            import ctypes

            class ProcessMemoryCounters(ctypes.Structure):
                _fields_ = [
                    ("cb", ctypes.c_ulong),
                    ("page_fault_count", ctypes.c_ulong),
                    ("peak_working_set_size", ctypes.c_size_t),
                    ("working_set_size", ctypes.c_size_t),
                    ("quota_peak_paged_pool_usage", ctypes.c_size_t),
                    ("quota_paged_pool_usage", ctypes.c_size_t),
                    ("quota_peak_non_paged_pool_usage", ctypes.c_size_t),
                    ("quota_non_paged_pool_usage", ctypes.c_size_t),
                    ("pagefile_usage", ctypes.c_size_t),
                    ("peak_pagefile_usage", ctypes.c_size_t),
                ]

            counters = ProcessMemoryCounters()
            counters.cb = ctypes.sizeof(counters)
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            psapi = ctypes.WinDLL("psapi", use_last_error=True)
            kernel32.GetCurrentProcess.restype = ctypes.c_void_p
            psapi.GetProcessMemoryInfo.argtypes = [
                ctypes.c_void_p,
                ctypes.POINTER(ProcessMemoryCounters),
                ctypes.c_ulong,
            ]
            psapi.GetProcessMemoryInfo.restype = ctypes.c_int
            process = kernel32.GetCurrentProcess()
            if psapi.GetProcessMemoryInfo(
                process, ctypes.byref(counters), counters.cb
            ):
                snapshot["rss_bytes"] = int(counters.working_set_size)
        else:
            snapshot.update(_posix_process_memory())
    except (AttributeError, ImportError, OSError, ValueError):
        pass
    return snapshot


def append_observation(
    *,
    data_root: Path | None,
    session_id: str,
    project_root: Path,
    policy: Mapping[str, Any],
    event_type: str,
    success: bool,
    duration_ms: float | None = None,
    extra: Mapping[str, Any] | None = None,
) -> bool:
    """Append one bounded observation; telemetry failure never escapes."""

    if data_root is None or not isinstance(session_id, str) or not session_id:
        return False
    try:
        record: dict[str, Any] = {
            "schema_version": OBSERVATION_SCHEMA_VERSION,
            "event_type": str(event_type)[:64],
            "observed_at_utc": datetime.now(UTC).isoformat(),
            "session_sha256": _sha256(session_id),
            "project_sha256": _sha256(str(project_root.resolve())),
            "policy_sha256": policy_sha256(policy),
            "preset": policy["preset"],
            "success": bool(success),
        }
        if duration_ms is not None:
            record["duration_ms"] = round(max(0.0, float(duration_ms)), 3)
        record.update(_safe_extra(extra))
        serialized = (_canonical(record) + "\n").encode("utf-8")
        if len(serialized) > MAX_OBSERVATION_BYTES:
            required = {
                key: record[key]
                for key in (
                    "schema_version",
                    "event_type",
                    "observed_at_utc",
                    "session_sha256",
                    "project_sha256",
                    "policy_sha256",
                    "preset",
                    "success",
                )
            }
            required["observation_truncated"] = True
            serialized = (_canonical(required) + "\n").encode("utf-8")
    except (KeyError, OSError, TypeError, ValueError):
        return False
    try:
        directory = data_root / STATE_OBSERVATION_DIRECTORY
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{_sha256(session_id)}.jsonl"
        lock_path = directory / ".retention.lock"
        if not _APPEND_LOCK.acquire(blocking=False):
            return False
        try:
            lock_stream = lock_path.open("a+b")
        except OSError:
            _APPEND_LOCK.release()
            return False
        try:
            with lock_stream:
                if not _try_lock_file(lock_stream):
                    return False
                descriptor = -1
                original_size: int | None = None
                try:
                    descriptor = os.open(
                        path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600
                    )
                    original_size = os.lseek(descriptor, 0, os.SEEK_END)
                    _write_all(descriptor, serialized)
                except OSError:
                    if descriptor >= 0 and original_size is not None:
                        try:
                            os.ftruncate(descriptor, original_size)
                        except OSError:
                            pass
                    raise
                finally:
                    if descriptor >= 0:
                        os.close(descriptor)
                    _unlock_file(lock_stream)
        finally:
            _APPEND_LOCK.release()
    except OSError:
        return False
    return True


def _closed_observation(path: Path) -> bool:
    try:
        with path.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            stream.seek(max(0, size - 4096))
            tail = stream.read(4096)
    except OSError:
        return False
    for line in reversed(tail.splitlines()):
        try:
            record = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        return isinstance(record, dict) and record.get("event_type") == "session_end"
    return False


def prune_closed_observations(
    data_root: Path | None,
    *,
    retention_max_bytes: int,
    current_session_id: str | None = None,
    orphan_after_seconds: int | None = None,
) -> int:
    """Prune completed or conservatively stale Session files above the local cap."""

    if data_root is None or retention_max_bytes < 0:
        return 0
    directory = data_root / STATE_OBSERVATION_DIRECTORY
    if not directory.is_dir():
        return 0
    lock_path = directory / ".retention.lock"
    try:
        if not _APPEND_LOCK.acquire(blocking=False):
            return 0
        try:
            lock_stream = lock_path.open("a+b")
        except OSError:
            _APPEND_LOCK.release()
            return 0
        try:
            with lock_stream:
                if not _try_lock_file(lock_stream):
                    return 0
                try:
                    return _prune_locked_observations(
                        directory,
                        retention_max_bytes=retention_max_bytes,
                        current_session_id=current_session_id,
                        orphan_after_seconds=orphan_after_seconds,
                    )
                finally:
                    _unlock_file(lock_stream)
        finally:
            _APPEND_LOCK.release()
    except OSError:
        return 0


def _prune_locked_observations(
    directory: Path,
    *,
    retention_max_bytes: int,
    current_session_id: str | None,
    orphan_after_seconds: int | None,
) -> int:
    """Prune State MCP observations while the stable directory lock is held."""

    paths = list(directory.glob("*.jsonl"))
    entries: list[tuple[int, int, Path]] = []
    total = 0
    for path in paths:
        try:
            stat = path.stat()
        except OSError:
            continue
        total += stat.st_size
        entries.append((stat.st_mtime_ns, stat.st_size, path))
    if total <= retention_max_bytes:
        return 0
    current_name = (
        f"{_sha256(current_session_id)}.jsonl" if current_session_id else None
    )
    removed = 0
    now_ns = time.time_ns()
    orphan_age_ns = (
        None
        if orphan_after_seconds is None
        else max(0, orphan_after_seconds) * 1_000_000_000
    )
    for modified_ns, size, path in sorted(entries):
        if total <= retention_max_bytes:
            break
        stale_orphan = (
            orphan_age_ns is not None and now_ns - modified_ns >= orphan_age_ns
        )
        if path.name == current_name or not (
            _closed_observation(path) or stale_orphan
        ):
            continue
        try:
            path.unlink()
        except OSError:
            continue
        total -= size
        removed += 1
    return removed


class SessionProbe:
    """In-memory MCP counters with bounded failure/slow/boundary persistence."""

    def __init__(
        self,
        project_root: Path,
        policy: Mapping[str, Any],
        *,
        data_root: Path | None = None,
        session_id: str | None = None,
    ) -> None:
        self.project_root = project_root.resolve()
        self.policy = copy.deepcopy(dict(policy))
        self.data_root = plugin_data_root() if data_root is None else data_root
        self.session_id = session_id or f"mcp-{uuid.uuid4()}"
        self.started = time.perf_counter()
        self.read_calls = 0
        self.write_calls = 0
        self.failed_calls = 0
        self.resume_calls = 0
        self.duplicate_resumes = 0
        self.request_bytes = 0
        self.response_bytes = 0
        self.latency_buckets = [0] * (len(LATENCY_BUCKETS_MS) + 1)
        self.closed = False
        self.degraded = False
        self.persisted_records = 0

    @property
    def enabled(self) -> bool:
        return bool(self.policy["observability"]["probes_enabled"])

    def _bucket(self, duration_ms: float) -> None:
        for index, ceiling in enumerate(LATENCY_BUCKETS_MS):
            if duration_ms <= ceiling:
                self.latency_buckets[index] += 1
                return
        self.latency_buckets[-1] += 1

    def boundary(
        self,
        event_type: str,
        *,
        success: bool,
        duration_ms: float | None = None,
        extra: Mapping[str, Any] | None = None,
    ) -> None:
        merged_extra = dict(extra or {})
        if (
            self.enabled
            and self.policy["observability"]["resource_sampling"] != "disabled"
        ):
            for key, value in _resource_snapshot(
                self.project_root, self.data_root, self.session_id
            ).items():
                merged_extra.setdefault(key, value)
        written = append_observation(
            data_root=self.data_root,
            session_id=self.session_id,
            project_root=self.project_root,
            policy=self.policy,
            event_type=event_type,
            success=success,
            duration_ms=duration_ms,
            extra=merged_extra,
        )
        self.degraded = self.degraded or not written
        self.persisted_records += int(written)

    def record_call(
        self,
        tool_name: str,
        *,
        duration_ms: float,
        success: bool,
        request_bytes: int = 0,
        response_bytes: int = 0,
    ) -> None:
        if self.closed:
            return
        is_write = tool_name in WRITE_TOOLS
        if self.enabled:
            self.write_calls += int(is_write)
            self.read_calls += int(not is_write)
            self.failed_calls += int(not success)
            if tool_name == "continuity_resume":
                self.resume_calls += 1
                if self.policy["resume"]["explicit_policy"] == "once_per_connection":
                    self.duplicate_resumes += int(self.resume_calls > 1)
            self.request_bytes += max(0, request_bytes)
            self.response_bytes += max(0, response_bytes)
            self._bucket(duration_ms)
        slow = self.enabled and (
            duration_ms >= self.policy["observability"]["slow_call_threshold_ms"]
        )
        diagnostic = self.enabled and (
            self.policy["observability"]["mode"] == "diagnostic"
        )
        if not (is_write or not success or slow or diagnostic):
            return
        event_type = (
            "state_write_completed"
            if is_write and success
            else "state_write_failed"
            if is_write
            else "tool_call_failed"
            if not success
            else "slow_call"
            if slow
            else "tool_call"
        )
        self.boundary(
            event_type,
            success=success,
            duration_ms=duration_ms,
            extra={
                "tool_name": tool_name,
                "request_bytes": request_bytes if diagnostic or slow else None,
                "response_bytes": response_bytes if diagnostic or slow else None,
            },
        )

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        if not self.enabled and not self.degraded and self.persisted_records == 0:
            prune_closed_observations(
                self.data_root,
                retention_max_bytes=self.policy["observability"][
                    "retention_max_bytes"
                ],
                current_session_id=self.session_id,
            )
            return
        self.boundary(
            "session_end",
            success=not self.degraded,
            duration_ms=(time.perf_counter() - self.started) * 1000,
            extra={
                "read_calls": self.read_calls,
                "write_calls": self.write_calls,
                "failed_calls": self.failed_calls,
                "resume_calls": self.resume_calls,
                "duplicate_resumes": self.duplicate_resumes,
                "request_bytes_total": self.request_bytes,
                "response_bytes_total": self.response_bytes,
                "latency_buckets": ",".join(str(value) for value in self.latency_buckets),
                "observation_degraded": self.degraded,
            },
        )
        prune_closed_observations(
            self.data_root,
            retention_max_bytes=self.policy["observability"]["retention_max_bytes"],
            current_session_id=self.session_id,
        )


def build_observation_report(
    project_root: Path,
    *,
    data_root: Path | None = None,
    session_limit: int = 20,
) -> dict[str, Any]:
    """Build a bounded offline report without reading transcripts or source files."""

    data_root = plugin_data_root() if data_root is None else data_root
    project_digest = _sha256(str(project_root.resolve()))
    records: list[dict[str, Any]] = []
    corrupt_lines = 0
    truncated_files = 0
    if data_root is not None:
        directories = [
            data_root / STATE_OBSERVATION_DIRECTORY,
            data_root / CORE_OBSERVATION_DIRECTORY,
        ]
        entries: list[tuple[int, Path]] = []
        for directory in directories:
            try:
                candidates = directory.glob("*.jsonl")
                for path in candidates:
                    try:
                        entries.append((path.stat().st_mtime_ns, path))
                    except OSError:
                        continue
            except OSError:
                continue
        paths = [
            path
            for _, path in sorted(entries, key=lambda item: item[0], reverse=True)[
                : max(1, min(session_limit, 100))
            ]
        ]
        for path in paths:
            try:
                with path.open("rb") as stream:
                    stream.seek(0, os.SEEK_END)
                    size = stream.tell()
                    offset = max(0, size - MAX_REPORT_FILE_BYTES)
                    stream.seek(offset)
                    payload = stream.read(MAX_REPORT_FILE_BYTES)
            except (OSError, UnicodeDecodeError):
                corrupt_lines += 1
                continue
            if offset:
                truncated_files += 1
                newline = payload.find(b"\n")
                payload = b"" if newline < 0 else payload[newline + 1 :]
            try:
                lines = payload.decode("utf-8").splitlines()
            except UnicodeDecodeError:
                corrupt_lines += 1
                continue
            for line in lines:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    corrupt_lines += 1
                    continue
                if (
                    isinstance(record, dict)
                    and record.get("schema_version")
                    in {OBSERVATION_SCHEMA_VERSION, CORE_OBSERVATION_SCHEMA_VERSION}
                    and (
                        record.get("project_sha256") == project_digest
                        or record.get("project_root_sha256") == project_digest
                    )
                ):
                    records.append(record)
    event_counts: dict[str, int] = {}
    durations: list[float] = []
    failures = 0
    duplicate_resumes_by_session: dict[str, int] = {}
    for record in records:
        event_type = str(record.get("event_type", "unknown"))
        event_counts[event_type] = event_counts.get(event_type, 0) + 1
        failures += int(record.get("success") is False)
        duplicate = record.get("duplicate_resumes")
        if type(duplicate) is int:
            session = str(record.get("session_sha256", ""))
            duplicate_resumes_by_session[session] = max(
                duplicate_resumes_by_session.get(session, 0), duplicate
            )
        duration = record.get("duration_ms")
        if type(duration) in {int, float}:
            durations.append(float(duration))
    suggestions: list[str] = []
    duplicate_resumes = sum(duplicate_resumes_by_session.values())
    if duplicate_resumes:
        suggestions.append("check MCP connection lifecycle; duplicate resume calls were observed")
    if event_counts.get("slow_call", 0):
        suggestions.append("inspect slow-call events before lowering probe thresholds")
    if event_counts.get("postcompact", 0) and event_counts.get("autorun", 0) == 0:
        suggestions.append("verify PostCompact autorun and recovery-context admission")
    if failures:
        suggestions.append("temporarily enable diagnostic or reliability-first for failed boundaries")
    return {
        "schema_version": "context.light-observation-report/v1alpha1",
        "project_sha256": project_digest,
        "sessions_scanned": len({record.get("session_sha256") for record in records}),
        "records": len(records),
        "corrupt_lines": corrupt_lines,
        "truncated_files": truncated_files,
        "failures": failures,
        "event_counts": dict(sorted(event_counts.items())),
        "duration_ms_max": round(max(durations), 3) if durations else None,
        "duplicate_resumes": duplicate_resumes,
        "provider_usage_available": any(
            type(record.get("input_tokens")) is int for record in records
        ),
        "suggestions": suggestions,
    }


__all__ = [
    "MAX_REPORT_FILE_BYTES",
    "CORE_OBSERVATION_DIRECTORY",
    "CORE_OBSERVATION_SCHEMA_VERSION",
    "ORPHAN_RETENTION_SECONDS",
    "OBSERVATION_SCHEMA_VERSION",
    "POLICY_FILENAME",
    "POLICY_SCHEMA_VERSION",
    "STATE_OBSERVATION_DIRECTORY",
    "PolicyConfigError",
    "SessionProbe",
    "append_observation",
    "build_observation_report",
    "load_policy",
    "plugin_data_root",
    "policy_sha256",
    "prune_closed_observations",
    "resolve_policy",
]
