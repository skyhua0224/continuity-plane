"""Explicit installer safeguards; never registered as an Agent tool hook."""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import os
import re
import sqlite3
import tomllib
from pathlib import Path

PLUGIN_IDS = tuple(f"{name}@continuity-plane" for name in (
    "continuity-plane", "continuity-plane-search", "continuity-plane-state",
))
HOOK_KEYS = tuple(
    f"continuity-plane@continuity-plane:hooks/hooks.json:{event}:0:0"
    for event in ("session_start", "pre_compact", "post_compact", "pre_tool_use", "post_tool_use", "user_prompt_submit")
)


class GuardError(RuntimeError):
    """A non-integration field changed, or an edit exceeded installer ownership."""


def validate_codex_home(codex_home, settings):
    override = settings.get("codexConfigDir")
    if override and (not isinstance(override, str) or Path(override).expanduser().resolve() != Path(codex_home).resolve()):
        raise GuardError("codex_home_binding_mismatch")


def _merge(target, source):
    result = copy.deepcopy(target)
    for key, value in source.items():
        result[key] = _merge(result[key], value) if isinstance(value, dict) and isinstance(result.get(key), dict) else copy.deepcopy(value)
    return result


def merge_integrations(config, patch):
    if not isinstance(patch, dict) or set(patch) - {"plugins", "hooks"}:
        raise GuardError("edit_outside_integration_scope")
    for plugin_id, settings in patch.get("plugins", {}).items():
        if plugin_id not in PLUGIN_IDS or not isinstance(settings, dict) or set(settings) - {"enabled", "mcp_servers"}:
            raise GuardError("plugin_outside_integration_scope")
        if "enabled" in settings and type(settings["enabled"]) is not bool:
            raise GuardError("invalid_plugin_enabled")
        servers = settings.get("mcp_servers", {})
        expected = {"continuity-plane-search@continuity-plane": "continuity-search", "continuity-plane-state@continuity-plane": "continuity"}
        for server_id, server in servers.items():
            if server_id != expected.get(plugin_id) or not isinstance(server, dict) or set(server) - {"enabled", "default_tools_approval_mode"}:
                raise GuardError("mcp_outside_integration_scope")
            if "enabled" in server and type(server["enabled"]) is not bool:
                raise GuardError("invalid_mcp_enabled")
            if "default_tools_approval_mode" in server and server["default_tools_approval_mode"] not in {"approve", "prompt"}:
                raise GuardError("invalid_mcp_approval")
    hooks = patch.get("hooks", {})
    if not isinstance(hooks, dict) or set(hooks) - {"state"}:
        raise GuardError("hook_definitions_not_allowed")
    for key, value in hooks.get("state", {}).items():
        if key not in HOOK_KEYS or not isinstance(value, dict) or set(value) - {"trusted_hash", "enabled"}:
            raise GuardError("hook_outside_integration_scope")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", value.get("trusted_hash", "")):
            raise GuardError("invalid_hook_hash")
        if "enabled" in value and type(value["enabled"]) is not bool:
            raise GuardError("invalid_hook_enabled")
    return _merge(config, patch)


def protected_config(config):
    result = copy.deepcopy(config)
    plugins = result.get("plugins", {})
    for key in PLUGIN_IDS:
        plugins.pop(key, None)
    if not plugins:
        result.pop("plugins", None)
    hooks = result.get("hooks", {})
    state = hooks.get("state", {})
    for key in HOOK_KEYS:
        state.pop(key, None)
    if not state:
        hooks.pop("state", None)
    if not hooks:
        result.pop("hooks", None)
    return result


def safe_common(text):
    try:
        result = tomllib.loads(text)
    except (TypeError, ValueError):
        raise GuardError("invalid_common_snippet") from None
    if set(result) & {"model_provider", "model_providers", "profile", "base_url", "wire_api", "auth"}:
        raise GuardError("routing_in_common_snippet")
    sensitive = {"apikey", "authorization", "experimentalbearertoken", "bearertoken", "password",
                 "secret", "credentials", "auth", "tokens", "httpheaders", "envhttpheaders", "envkey"}
    def visit(value):
        if isinstance(value, dict):
            for key, child in value.items():
                normalized = re.sub(r"[^a-z0-9]", "", key.lower())
                if normalized in sensitive or normalized.endswith("apikey"):
                    raise GuardError("credential_in_common_snippet")
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)
    visit(result)
    return result


class CredentialSnapshot:
    """Read-only, process-keyed fingerprints; no raw credentials leave this object."""

    def __init__(self, codex_home, switch_home):
        self.codex_home = Path(codex_home)
        self.switch_home = Path(switch_home)
        self._salt = os.urandom(32)
        self.provider_count = 0

    def digest(self, value):
        data = value if isinstance(value, bytes) else json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
        return hmac.new(self._salt, data, hashlib.sha256).hexdigest()

    def __call__(self):
        result = {}
        try:
            config = tomllib.loads((self.codex_home / "config.toml").read_text())
            result["live_config"] = self.digest(protected_config(config))
            auth = self.codex_home / "auth.json"
            result["auth_file"] = self.digest(auth.read_bytes() if auth.exists() else b"absent")
            result["profiles"] = self.digest({p.name: self.digest(p.read_bytes()) for p in self.codex_home.glob("*.config.toml")})
            settings = self.switch_home / "settings.json"
            settings_bytes = settings.read_bytes() if settings.exists() else b"{}"
            validate_codex_home(self.codex_home, json.loads(settings_bytes))
            result["switch_settings"] = self.digest(settings_bytes)
            database = self.switch_home / "cc-switch.db"
            connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
            try:
                connection.execute("PRAGMA query_only=ON")
                connection.execute("BEGIN")
                row = connection.execute("SELECT value FROM settings WHERE key='common_config_codex'").fetchone()
                common = safe_common(row[0] if row and row[0] else "")
                result["common_nonintegration"] = self.digest(protected_config(common))
                providers = []
                for identity, app, raw, current, category, meta in connection.execute(
                    "SELECT id,app_type,settings_config,is_current,category,meta FROM providers ORDER BY app_type,id"
                ):
                    document = json.loads(raw)
                    metadata = json.loads(meta)
                    if app == "codex":
                        own_config = tomllib.loads(document.get("config") or "")
                        if metadata.get("commonConfigEnabled") is True:
                            own_config = _merge(own_config, common)
                        document["config"] = protected_config(own_config)
                    providers.append((identity, app, document, current, category, metadata))
                self.provider_count = len(providers)
                result["all_providers"] = self.digest(providers)
                # The proxy may update usage counters; its routing configuration must stay fixed.
                for table in ("proxy_config",):
                    result[table] = self.digest(connection.execute(f"SELECT * FROM {table} ORDER BY app_type").fetchall())
            finally:
                connection.close()
        except GuardError:
            raise
        except Exception:
            raise GuardError("credential_snapshot_unavailable") from None
        return result


class IntegrationGuard:
    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.baseline = snapshot()
        self.completed_operations = 0

    def check(self):
        if self.snapshot() != self.baseline:
            raise GuardError("protected_configuration_changed")

    def run(self, operation):
        self.check()
        try:
            result = operation()
        finally:
            self.check()
        self.completed_operations += 1
        return result
