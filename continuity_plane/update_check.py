"""Update detection and the explicit `continuity self-update` command.

Detection never blocks a command: a detached process asks the package index for
the release list at most once per day and stores the newest version under the
data root. Every surface (CLI notice, `continuity_start`, SessionStart, doctor)
reads that cache. Only the package name is requested; no project, user or usage
data leaves the machine. Private packages, source and editable installs never
report, and CONTINUITY_UPDATE_CHECK=0 disables all network access made here.
"""

from __future__ import annotations

import functools
import json
import os
import re
import sys
import time
from pathlib import Path

PUBLIC_DISTRIBUTION = "continuity-plane"
INDEX_URL = "https://pypi.org/pypi/continuity-plane/json"
INTERVAL_SECONDS = 24 * 60 * 60
FETCH_TIMEOUT = 3.0
LOOKUP_TIMEOUT = 10.0
MAX_METADATA_BYTES = 8 * 1024 * 1024
STATE_NAME = "update-check.json"
UPGRADE_COMMAND = "continuity self-update"
# Output that is parsed by machines or owned by a host must stay untouched.
_QUIET_COMMANDS = {"self-update", "hook", "host-hook", "mcp", "help"}
_MAX_SESSIONS = 64

_VERSION_PATTERN = (
    r"""^\s*v?(?P<release>\d+(?:\.\d+)*)
    (?P<pre>[-_.]?(?P<pre_l>alpha|a|beta|b|rc|c|preview|pre)[-_.]?(?P<pre_n>\d*))?
    (?P<post>-(?P<post_n1>\d+)|[-_.]?(?:post|rev|r)[-_.]?(?P<post_n>\d*))?
    (?P<dev>[-_.]?dev[-_.]?(?P<dev_n>\d*))?
    (?:\+[a-z0-9]+(?:[-_.][a-z0-9]+)*)?\s*$"""
)
_PRE_RANK = {"a": 0, "alpha": 0, "b": 1, "beta": 1, "rc": 2, "c": 2, "pre": 2, "preview": 2}
_INFINITY = float("inf")


@functools.lru_cache(maxsize=1)
def _version_regex():
    return re.compile(_VERSION_PATTERN, re.IGNORECASE | re.VERBOSE)


def parse_version(text):
    """A comparable PEP 440 key (local labels ignored), or None."""
    if not isinstance(text, str):
        return None
    match = _version_regex().match(text)
    if match is None:
        return None
    release = [int(part) for part in match["release"].split(".")]
    while len(release) > 1 and release[-1] == 0:
        release.pop()
    pre = (_PRE_RANK[match["pre_l"].lower()], int(match["pre_n"] or 0)) if match["pre"] else None
    post = int(match["post_n1"] or match["post_n"] or 0) if match["post"] else None
    dev = int(match["dev_n"] or 0) if match["dev"] else None
    # PEP 440 order: a bare dev release < pre-releases < the release < post-releases.
    if pre is None and post is None and dev is not None:
        pre = (-1, 0)
    elif pre is None:
        pre = (_INFINITY, 0)
    return (tuple(release), pre, -1 if post is None else post, _INFINITY if dev is None else dev)


def is_prerelease(text) -> bool:
    key = parse_version(text)
    return key is not None and (key[1][0] != _INFINITY or key[3] != _INFINITY)


def latest_from_metadata(data, current):
    """Newest installable version in the index document, or None."""
    releases = data.get("releases") if isinstance(data, dict) else None
    if not isinstance(releases, dict):
        return None
    allow_pre = is_prerelease(current)
    best, best_key = None, None
    for name, files in releases.items():
        key = parse_version(name)
        if key is None or not isinstance(files, list) or not files:
            continue
        if all(isinstance(f, dict) and f.get("yanked") for f in files):
            continue
        if not allow_pre and is_prerelease(name):
            continue
        if best_key is None or key > best_key:
            best, best_key = name, key
    return best


# --- identity -----------------------------------------------------------------


def _distribution() -> str:
    from .release_version import DISTRIBUTION

    return DISTRIBUTION


def _current() -> str:
    from .release_version import VERSION

    return VERSION


def _dist():
    from importlib.metadata import PackageNotFoundError, distribution

    try:
        return distribution(_distribution())
    except PackageNotFoundError:
        return None


def _install_origin() -> str:
    """`index`, `editable` or `source` (directory, archive, VCS or no metadata)."""
    dist = _dist()
    if dist is None:
        return "source"
    try:
        text = dist.read_text("direct_url.json")
    except OSError:
        text = None
    if not text:
        return "index"
    try:
        document = json.loads(text)
    except ValueError:
        return "source"
    info = document.get("dir_info") if isinstance(document, dict) else None
    if isinstance(info, dict) and info.get("editable"):
        return "editable"
    return "source"


def reporting_enabled(deep=True) -> bool:
    """Whether this install may check and report.

    The shallow form (environment switch and package name) costs nothing and runs
    on every command; the deep form also reads the install metadata and is only
    needed once there is something to spawn or to say.
    """
    if os.environ.get("CONTINUITY_UPDATE_CHECK", "").strip().lower() in {"0", "false", "off", "no"}:
        return False
    if _distribution() != PUBLIC_DISTRIBUTION:
        return False
    return not deep or _install_origin() == "index"


def install_method(prefix=None, base_prefix=None) -> str:
    """`pipx`, `uv`, `venv`, `system`, `editable` or `source`."""
    origin = _install_origin()
    if origin != "index":
        return origin
    prefix = str(prefix if prefix is not None else sys.prefix)
    base = str(base_prefix if base_prefix is not None else sys.base_prefix)
    normalized = prefix.replace("\\", "/")
    if "/pipx/venvs/" in normalized:
        return "pipx"
    if "/uv/tools/" in normalized:
        return "uv"
    return "venv" if prefix != base else "system"


def upgrade_command(method, target, python=None, current=None):
    pre = is_prerelease(target or current or _current())
    if method == "pipx":
        return ["pipx", "upgrade", *(["--pip-args=--pre"] if pre else []), PUBLIC_DISTRIBUTION]
    if method == "uv":
        return ["uv", "tool", "upgrade", *(["--prerelease=allow"] if pre else []), PUBLIC_DISTRIBUTION]
    if method == "venv":
        spec = f"{PUBLIC_DISTRIBUTION}=={target}" if target else PUBLIC_DISTRIBUTION
        return [python or sys.executable, "-m", "pip", "install", "--upgrade", *(["--pre"] if pre else []), spec]
    return None


# --- cache --------------------------------------------------------------------


def _data(data_root=None) -> Path:
    if data_root is not None:
        return Path(data_root)
    from .collaboration_registry import default_data_root

    return default_data_root()


def _read(data) -> dict:
    try:
        state = json.loads((Path(data) / STATE_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return state if isinstance(state, dict) else {}


def _write(data, state) -> None:
    path = Path(data) / STATE_NAME
    temporary = path.with_name(f"{STATE_NAME}.{os.getpid()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
        os.replace(temporary, path)
    except OSError:
        try:
            temporary.unlink()
        except OSError:
            pass


def _number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def available(data_root=None, current=None):
    """The pending update from the cache, or None. Never touches the network."""
    latest = _read(_data(data_root)).get("latest")
    current = current or _current()
    latest_key, current_key = parse_version(latest), parse_version(current)
    if latest_key is None or current_key is None or latest_key <= current_key:
        return None
    return {"current": current, "latest": latest, "command": UPGRADE_COMMAND}


# --- detached refresh -----------------------------------------------------------


def _default_fetch(url, timeout):
    from urllib.request import Request, urlopen

    request = Request(url, headers={"Accept": "application/json", "User-Agent": "continuity-update-check"})
    with urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed https URL
        body = response.read(MAX_METADATA_BYTES + 1)
    if len(body) > MAX_METADATA_BYTES:
        raise ValueError("index metadata is too large")
    return json.loads(body)


def _spawn_detached(*words):
    import subprocess

    subprocess.Popen(
        [sys.executable, "-I", "-m", __spec__.name if __spec__ else __name__, *words],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
        env={**os.environ, "CONTINUITY_DATA_ROOT": str(_data())},
    )


def _default_spawn(data_root):
    _spawn_detached("refresh")


def refresh(data_root=None, fetcher=None, current=None, now=None):
    """Ask the index once and record the answer. Every failure is silent."""
    if not reporting_enabled():
        return
    data = _data(data_root)
    now = time.time() if now is None else now
    current = current or _current()
    state = _read(data)
    state["attempted_at"] = now
    try:
        latest = latest_from_metadata((fetcher or _default_fetch)(INDEX_URL, FETCH_TIMEOUT), current)
    except Exception:  # noqa: BLE001 - detection must never surface a failure
        latest = None
    if latest:
        state["latest"], state["checked_at"] = latest, now
    _write(data, state)


def maybe_refresh(data_root=None, now=None, spawner=None):
    """Start the detached check when a day has passed since the last attempt."""
    try:
        if not reporting_enabled(deep=False):
            return
        data = _data(data_root)
        now = time.time() if now is None else now
        state = _read(data)
        stamps = [s for s in (_number(state.get("checked_at")), _number(state.get("attempted_at"))) if s is not None]
        if stamps and 0 <= now - max(stamps) < INTERVAL_SECONDS:
            return
        if not reporting_enabled():
            return
        # Recorded first so concurrent commands do not all spawn a check.
        state["attempted_at"] = now
        _write(data, state)
        (spawner or _default_spawn)(data)
    except Exception:  # noqa: BLE001
        return


# --- surfaces -------------------------------------------------------------------


def cli_notice(words, stream, data_root=None, now=None):
    """At most one stderr line per day, only on a terminal, never on stdout."""
    try:
        if not words or words[0].startswith("-") or words[0] in _QUIET_COMMANDS:
            return
        if not reporting_enabled(deep=False):
            return
        data = _data(data_root)
        now = time.time() if now is None else now
        maybe_refresh(data, now=now)
        if not stream.isatty():
            return
        pending = available(data)
        if pending is None:
            return
        state = _read(data)
        shown = _number(state.get("cli_notified_at"))
        if (shown is not None and 0 <= now - shown < INTERVAL_SECONDS) or not reporting_enabled():
            return
        stream.write(
            f"continuity {pending['latest']} is available (installed {pending['current']}); "
            f"upgrade with: {pending['command']}\n"
        )
        state["cli_notified_at"] = now
        _write(data, state)
    except Exception:  # noqa: BLE001
        return


def start_field(data_root=None):
    """The `update_available` value for continuity_start, or None."""
    try:
        if not reporting_enabled(deep=False):
            return None
        data = _data(data_root)
        maybe_refresh(data)
        pending = available(data)
        return pending if pending is not None and reporting_enabled() else None
    except Exception:  # noqa: BLE001
        return None


def spawn_apply(data_root=None):
    _spawn_detached("apply")


def session_note(payload, data_root=None, max_bytes=1024):
    """One line per version per session; None when nothing fits or is due."""
    try:
        if not isinstance(payload, dict) or payload.get("hook_event_name") != "SessionStart":
            return None
        session = str(payload.get("session_id") or "")
        if not session or not reporting_enabled(deep=False):
            return None
        data = _data(data_root)
        maybe_refresh(data)
        pending = available(data)
        if pending is None or not reporting_enabled():
            return None
        state = _read(data)
        sessions = state.get("sessions") if isinstance(state.get("sessions"), dict) else {}
        if sessions.get(session) == pending["latest"]:
            return None
        auto = os.environ.get("CONTINUITY_AUTO_UPDATE") == "1" and not running_workers(data)
        if auto:
            text = (
                f"Continuity {pending['latest']} is installing in the background "
                f"(CONTINUITY_AUTO_UPDATE=1; installed {pending['current']}); MCP servers reconnect on their next call"
            )
        else:
            text = (
                f"Continuity {pending['latest']} is available (installed {pending['current']}); "
                f"run `{pending['command']}` only if the user allows"
            )
        if len(text.encode()) > max_bytes:
            return None
        if auto:
            spawn_apply(data)
        sessions.pop(session, None)
        sessions[session] = pending["latest"]
        state["sessions"] = dict(list(sessions.items())[-_MAX_SESSIONS:])
        _write(data, state)
        return text
    except Exception:  # noqa: BLE001
        return None


def merge_session_notice(output, payload, limit, data_root=None):
    """Append the SessionStart line to a hook output within its byte budget."""
    try:
        context = output.get("hookSpecificOutput", {}).get("additionalContext", "")
        note = session_note(payload, data_root, max_bytes=limit - len(context.encode()) - 1)
        if note:
            output["hookSpecificOutput"] = {
                "hookEventName": payload.get("hook_event_name", "SessionStart"),
                "additionalContext": (context + "\n" + note).strip(),
            }
    except Exception:  # noqa: BLE001
        pass
    return output


# --- workers and versions -------------------------------------------------------


def running_workers(data_root=None):
    """Worker jobs whose runner process is alive on this host."""
    from .supervisor import process_state

    projects = _data(data_root) / "projects"
    found = []
    if not projects.is_dir():
        return found
    for index in sorted(projects.glob("*/worker-jobs.json")):
        try:
            rows = json.loads(index.read_text(encoding="utf-8"))
        except OSError as exc:
            raise ValueError("worker job index is unreadable") from exc
        except ValueError as exc:
            raise ValueError("worker job index is unreadable") from exc
        for task_id, item in (rows.items() if isinstance(rows, dict) else ()):
            if not isinstance(item, dict) or (item.get("job_status") or item.get("status")) != "running":
                continue
            job_id = item.get("job_id")
            try:
                job = json.loads((index.parent / "worker-jobs" / str(job_id) / "job.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            state = process_state(job.get("runner_pid"), job.get("runner_identity"), job.get("runner_host"))
            if state == "alive":
                found.append({"project_id": index.parent.name, "task_id": task_id, "job_id": job_id})
    return found


def _mcp_version():
    """Version of the package behind the `continuity-mcp` launcher, if it differs."""
    import shutil

    launcher = shutil.which("continuity-mcp")
    if not launcher:
        return None
    try:
        with open(launcher, "rb") as stream:
            first = stream.readline().decode("utf-8", "replace").strip()
    except OSError:
        return None
    if not first.startswith("#!"):
        return None
    interpreter = first[2:].split()[0] if first[2:].split() else ""
    if not interpreter:
        return None
    if os.path.realpath(interpreter) == os.path.realpath(sys.executable):
        return _current()
    import subprocess

    try:
        done = subprocess.run(
            [interpreter, "-I", "-c", "import importlib.metadata as m,sys;print(m.version(sys.argv[1]))", _distribution()],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() or None if done.returncode == 0 else None


def _plugin_versions():
    versions = {}
    codex = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    for manifest in sorted(codex.glob("plugins/cache/continuity-plane/*/*/.codex-plugin/plugin.json")):
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        name, version = data.get("name"), data.get("version")
        key = f"codex:{name}"
        if isinstance(name, str) and isinstance(version, str):
            previous = versions.get(key)
            if previous is None or (parse_version(version) or ()) > (parse_version(previous) or ()):
                versions[key] = version
    claude = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    try:
        installed = json.loads((claude / "plugins" / "installed_plugins.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        installed = {}
    plugins = installed.get("plugins") if isinstance(installed, dict) else None
    for name, entries in (plugins.items() if isinstance(plugins, dict) else ()):
        if not str(name).startswith("continuity-plane") or not isinstance(entries, list):
            continue
        version = next((e.get("version") for e in reversed(entries) if isinstance(e, dict) and e.get("version")), None)
        if isinstance(version, str):
            versions[f"claude:{str(name).split('@')[0]}"] = version
    return versions


def version_report():
    """CLI, MCP and plugin versions with any skew; unknown components are not skew."""
    cli = _current()
    mcp = _mcp_version()
    plugins = _plugin_versions()
    skew = []
    cli_key = parse_version(cli)
    if mcp is not None and parse_version(mcp) != cli_key:
        skew.append(f"mcp launcher runs {mcp} but the CLI is {cli}; reinstall one of them so both use the same package")
    for name, version in sorted(plugins.items()):
        if parse_version(version) != cli_key:
            skew.append(f"plugin {name} is {version} but the CLI is {cli}; update the plugin from the host and reconnect")
    report = {"cli": cli, "mcp": mcp, "plugins": plugins, "skew": skew}
    pending = available()
    if pending:
        report["update_available"] = pending
    return report


# --- self-update ----------------------------------------------------------------

_REFUSAL_HINTS = {
    "editable": "This is an editable install; update the checkout (git pull) and reinstall it, or install the released package with pipx.",
    "source": "This install does not come from the package index; update its source checkout, or install the released package with pipx.",
    "system": "This interpreter is not an isolated environment; install with pipx or in a virtual environment, then run self-update there.",
    "private": "This is a private build, not the published package; update it from its own source.",
}
_NEXT_STEPS = [
    "MCP servers started by Claude Code or Codex exit on upgrade; the host reconnects on the next tool call (Claude Code: /mcp reconnect, otherwise start a new session)",
    "Plugins are updated from the host's plugin manager and then reconnected; `continuity doctor` lists any plugin/CLI version skew",
    "Sessions already running keep the old hooks until restarted",
]


def _default_runner(command):
    import subprocess

    done = subprocess.run(command, capture_output=True, text=True, timeout=900, check=False)
    if done.returncode != 0:
        sys.stderr.write((done.stderr or done.stdout)[-2000:])
    return done.returncode


def self_update(
    data_root=None,
    fetcher=None,
    runner=None,
    workers=None,
    method=None,
    force=False,
    dry_run=False,
    out=None,
):
    """Explicit upgrade with the install's own tool. Never runs automatically."""
    out = out or sys.stdout
    data = _data(data_root)
    current = _current()

    def emit(code, **report):
        print(json.dumps({"current": current, **report}, ensure_ascii=False, sort_keys=True), file=out)
        return code

    if _distribution() != PUBLIC_DISTRIBUTION:
        return emit(2, status="refused", reason="private", hint=_REFUSAL_HINTS["private"])
    method = method or install_method()
    command_for = upgrade_command(method, None, current=current)
    if command_for is None:
        return emit(2, status="refused", reason=method, hint=_REFUSAL_HINTS.get(method, _REFUSAL_HINTS["source"]))
    target = None
    if os.environ.get("CONTINUITY_UPDATE_CHECK", "").strip().lower() not in {"0", "false", "off", "no"}:
        try:
            target = latest_from_metadata((fetcher or _default_fetch)(INDEX_URL, LOOKUP_TIMEOUT), current)
        except Exception as exc:  # noqa: BLE001
            return emit(1, status="lookup-failed", message=str(exc)[:200])
        latest_key, current_key = parse_version(target), parse_version(current)
        if target is None or current_key is None or (latest_key is not None and latest_key <= current_key):
            return emit(0, status="up-to-date", latest=target)
    command = upgrade_command(method, target, current=current)
    try:
        busy = (workers or running_workers)(data)
        reason = None
    except Exception as exc:  # noqa: BLE001
        busy, reason = None, f"worker state unreadable: {str(exc)[:120]}"
    if (busy or busy is None) and not force:
        report = {"status": "deferred", "command": command, "hint": "retry when workers have finished, or pass --force to upgrade now"}
        if busy:
            report["running_workers"] = busy
        if reason:
            report["reason"] = reason
        return emit(3, **report)
    if dry_run:
        return emit(0, status="dry-run", command=command, method=method, to=target)
    code = (runner or _default_runner)(command)
    if code != 0:
        return emit(1, status="failed", command=command, method=method, exit_code=code)
    state = _read(data)
    for key in ("latest", "sessions", "cli_notified_at"):
        state.pop(key, None)
    _write(data, state)
    report = {"status": "updated", "command": command, "method": method, "from": current, "next": _NEXT_STEPS}
    if target:
        report["to"] = target
    return emit(0, **report)


def main(argv=None):
    words = list(sys.argv[1:] if argv is None else argv)
    action = words[0] if words else ""
    if action == "refresh":
        refresh()
    elif action == "apply":
        self_update(out=open(os.devnull, "w"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
