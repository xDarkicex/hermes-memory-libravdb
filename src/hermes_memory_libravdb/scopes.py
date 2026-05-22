from __future__ import annotations

import re
from typing import Literal

__all__ = [
    "validate_collection_name",
    "user_collection",
    "session_collection",
    "resolve_search_scopes",
    "CollectionScope",
    "SEARCH_SCOPES_ALL",
]

# Must start with a letter, then up to 127 chars of alphanumeric/_.:@#-
_COLLECTION_NAME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_.:@#-]{0,127}$")

USER_COLLECTION_PREFIX = "user:"
SESSION_COLLECTION_PREFIX = "session:"
SESSION_SUMMARY_PREFIX = "session_summary:"
SESSION_RECALL_PREFIX = "session_recall:"
GLOBAL_COLLECTION = "global"

CollectionScope = Literal["session", "user", "global"]


def validate_collection_name(name: str) -> str:
    """Validate and return a collection name, raising on invalid characters or length."""
    if not _COLLECTION_NAME_RE.match(name):
        raise ValueError(
            f"Invalid collection name: {name!r}. "
            f"Must match {_COLLECTION_NAME_RE.pattern}"
        )
    return name


def user_collection(user_id: str) -> str:
    """Return a validated ``user:{userId}`` collection name."""
    namespace = user_id.strip()
    if not namespace:
        raise ValueError("user_id must be non-empty")
    validate_collection_name(namespace)
    return validate_collection_name(f"{USER_COLLECTION_PREFIX}{namespace}")


def session_collection(session_id: str) -> str:
    """Return a validated ``session:{sessionId}`` collection name."""
    sid = session_id.strip()
    if not sid:
        raise ValueError("session_id must be non-empty")
    validate_collection_name(sid)
    return validate_collection_name(f"{SESSION_COLLECTION_PREFIX}{sid}")


def _session_summary_collection(session_id: str) -> str:
    sid = session_id.strip()
    validate_collection_name(sid)
    return validate_collection_name(f"{SESSION_SUMMARY_PREFIX}{sid}")


def _session_recall_collection(session_id: str) -> str:
    sid = session_id.strip()
    validate_collection_name(sid)
    return validate_collection_name(f"{SESSION_RECALL_PREFIX}{sid}")


def resolve_search_scopes(
    user_id: str,
    session_id: str | None = None,
    *,
    cross_session_recall: bool = True,
    use_session_summary_search: bool = False,
    use_session_recall_projection: bool = False,
) -> list[str]:
    """
    Return an ordered list of collection names for a memory search.

    Session-scoped collections come first (most relevant), followed by durable
    user memory and global.  When *cross_session_recall* is False only the
    session collection is returned.
    """
    collections: list[str] = []

    if session_id:
        if use_session_summary_search:
            collections.append(_session_summary_collection(session_id))
        elif use_session_recall_projection:
            collections.append(_session_recall_collection(session_id))
        else:
            collections.append(session_collection(session_id))

    if cross_session_recall:
        collections.append(user_collection(user_id))
        collections.append(GLOBAL_COLLECTION)

    return collections


def resolve_exact_recall_collections(
    user_id: str,
    *,
    cross_session_recall: bool = True,
) -> list[str]:
    """Return collections for exact recall lookup (user + global only)."""
    if not cross_session_recall:
        return []
    return [user_collection(user_id), GLOBAL_COLLECTION]


# Sentinel for "search all available scopes"
SEARCH_SCOPES_ALL: tuple[CollectionScope, ...] = ("session", "user", "global")
