"""Private provisioning boundary for source namespace HMAC secrets."""

from __future__ import annotations

import base64
import json
import os
import secrets
from pathlib import Path

from .source_registry import _OPAQUE_KEY_ID_RE


SECRET_SCHEMA_VERSION = "context.source-namespace-secret/v1alpha1"


def provision_namespace_secret(path: Path | str, *, opaque_key_id: str) -> str:
    """Create one private 256-bit namespace secret without exposing its value."""
    if not isinstance(opaque_key_id, str) or not _OPAQUE_KEY_ID_RE.fullmatch(opaque_key_id):
        raise ValueError("opaque_key_id must be a public lowercase identifier")
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    payload = {
        "schema_version": SECRET_SCHEMA_VERSION,
        "opaque_key_id": opaque_key_id,
        "encoded_namespace_key": base64.urlsafe_b64encode(
            secrets.token_bytes(32)
        ).decode("ascii").rstrip("="),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return opaque_key_id
