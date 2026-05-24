from __future__ import annotations

import hashlib
import json
import logging
import os
import platform
import socket
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .scopes import validate_collection_name

logger = logging.getLogger(__name__)

IdentitySource = Literal["config", "file", "auto", "session-key", "default"]


@dataclass(frozen=True)
class ResolvedIdentity:
    user_id: str
    source: IdentitySource


def _derive_parts() -> dict[str, str]:
    """Derive identity parts from OS: username, hostname, home hash."""
    username = os.environ.get("USER") or os.environ.get("USERNAME") or os.environ.get("LOGNAME") or "anon"
    home = os.environ.get("HOME") or os.environ.get("USERPROFILE") or os.path.expanduser("~")
    host = socket.gethostname()
    home_hash = hashlib.sha256(
        home.replace("\\", "/").lower().encode()
    ).hexdigest()[:8]
    return {"username": username, "host": host, "home": home, "home_hash": home_hash}


def _derive_auto_id(parts: dict[str, str]) -> str:
    username = _sanitize_identity_part(parts["username"], fallback="user")
    host = _sanitize_identity_part(parts["host"], fallback="host")
    candidate = f"{username}@{host}#{parts['home_hash']}"
    if not candidate[0].isalpha():
        candidate = f"u-{candidate}"
    return validate_collection_name(candidate[:128])


def _sanitize_identity_part(value: str, *, fallback: str) -> str:
    # Trim punctuation at the edges so auto-derived IDs pass collection-name validation.
    # The home hash suffix in the final ID keeps rare sanitized-name collisions isolated.
    sanitized = "".join(
        ch if ch.isalnum() or ch in "_.:@#-" else "-"
        for ch in str(value or "").strip()
    ).strip(".:@#-")
    return sanitized or fallback


def _write_identity_file(path: Path, user_id: str, parts: dict[str, str]) -> None:
    identity = {
        "userId": user_id,
        "derivedFrom": {
            "username": parts["username"],
            "hostname": parts["host"],
            "homeHash": parts["home_hash"],
            "platform": platform.system(),
        },
        "createdAt": _now_iso(),
    }

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        suffix=".tmp",
        prefix=f".{os.getpid()}.",
        dir=str(path.parent),
    )
    try:
        os.write(fd, (json.dumps(identity, indent=2) + "\n").encode())
    finally:
        os.close(fd)
    os.chmod(tmp, 0o600)
    os.replace(tmp, str(path))


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def resolve_identity(
    config: dict | None = None,
    hermes_home: Path | None = None,
    session_key: str | None = None,
    *,
    no_auto_persist: bool = False,
) -> ResolvedIdentity:
    """
    Resolve a stable user identity for LibraVDB collection naming.

    Resolution order:
      1. Explicit ``userId`` in plugin config (highest priority).
      2. Persisted ``libravdb-identity.json`` in the Hermes home directory.
      3. Auto-derived ``username@hostname#homehash`` (persisted unless
         *no_auto_persist* is True).
      4. Session-key fallback (``session-key:{session_key}``).
      5. ``"default"`` as final fallback.
    """
    config = config or {}

    # 1. Plugin config override
    config_user_id = str(config.get("userId", "")).strip()
    if config_user_id:
        return ResolvedIdentity(user_id=validate_collection_name(config_user_id), source="config")

    # Resolve identity file path
    identity_path = config.get("identityPath")
    if identity_path:
        file_path = Path(identity_path)
    elif hermes_home:
        file_path = hermes_home / "libravdb-identity.json"
    else:
        file_path = Path(os.path.expanduser("~/.hermes/libravdb-identity.json"))

    # 2. Existing identity file
    if file_path.exists():
        try:
            raw = file_path.read_text()
            parsed = json.loads(raw)
        except Exception:
            pass
        else:
            uid = str(parsed.get("userId", "")).strip()
            if uid:
                return ResolvedIdentity(user_id=validate_collection_name(uid), source="file")

    # 3. Auto-derive
    try:
        parts = _derive_parts()
    except Exception:
        fallback = (session_key or "").strip()
        if fallback:
            return ResolvedIdentity(user_id=f"session-key:{fallback}", source="session-key")
        return ResolvedIdentity(user_id="default", source="default")

    auto_id = _derive_auto_id(parts)

    if not no_auto_persist:
        try:
            _write_identity_file(file_path, auto_id, parts)
        except Exception:
            pass

    return ResolvedIdentity(user_id=auto_id, source="auto")
