"""Markdown ingestion: directory scanning, glob rules, hashing, snapshots, gRPC ingest.

Ported from openclaw-memory-libravdb markdown-ingest.ts and markdown-hash.ts.
All heavy processing (tokenization, chunking, embedding) is handled by the daemon
via IngestMarkdownDocument RPC — this module handles file discovery, change
detection, debounced scanning, and the chunked REPLACE/APPEND ingest queue.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set

from libravdb.ipc.v1 import rpc_pb2 as pb

logger = logging.getLogger(__name__)

# ── Constants (matching TypeScript reference) ────────────────────────────────

DEFAULT_DEBOUNCE_MS = 150
DEFAULT_POLL_INTERVAL_MS = 5000
DEFAULT_TOKENIZER_ID = "markdown-ingest:v1"
MARKDOWN_INGEST_VERSION = 3
HASH_BACKEND = "python-fnv1a64"
STREAM_CHUNK_BYTES = 64 * 1024
DEFAULT_MAX_TOKENS_PER_FILE = 128_000

_INGEST_CHUNK_TOKENS = 8192
_INGEST_RETRY_BASE_DELAY_MS = 500
_INGEST_MAX_RETRIES = 4

DEFAULT_EXCLUDES = [
    "**/node_modules/**",
    "**/.git/**",
    "**/dist/**",
    "**/build/**",
    "**/coverage/**",
    "**/.next/**",
    "**/.nuxt/**",
    "**/.svelte-kit/**",
    "**/.turbo/**",
    "**/.cache/**",
    "**/.venv/**",
    "**/venv/**",
    "**/__pycache__/**",
]

APPROX_CHARS_PER_TOKEN = 4


# ── FNV-1a 64-bit hash ──────────────────────────────────────────────────────

_FNV_OFFSET = 0xcbf29ce484222325
_FNV_PRIME = 0x100000001b3


def _fnv1a64(data: bytes) -> str:
    """FNV-1a 64-bit hash returning 16-char zero-padded hex string."""
    h = _FNV_OFFSET
    for b in data:
        h ^= b
        h = (h * _FNV_PRIME) & 0xFFFFFFFFFFFFFFFF
    return f"{h:016x}"


def _incremental_fnv1a64(seed: int, data: bytes) -> int:
    """Incremental FNV-1a 64-bit update. Returns updated hash value."""
    h = seed
    for b in data:
        h ^= b
        h = (h * _FNV_PRIME) & 0xFFFFFFFFFFFFFFFF
    return h


def _incremental_fnv_to_hex(h: int) -> str:
    return f"{h:016x}"


def hash_text(text: str) -> str:
    return _fnv1a64(text.encode("utf-8"))


# ── Glob matching ────────────────────────────────────────────────────────────


def matches_glob(value: str, pattern: str) -> bool:
    """Match a posix-style path against a glob pattern.

    Supports:
      ``**/`` — zero or more path segments ending in /
      ``**``  — match any path (including /)
      ``*``   — match within a single segment (no slashes)
      ``?``   — match a single character (no slashes)
      ``[...]`` — character classes (``[!...]`` negation → ``[^...]``)
      ``{a,b}`` — brace expansion
    """
    return bool(re.match(_glob_to_re(pattern), value))


def _glob_to_re(pattern: str) -> str:
    """Compile a glob pattern to a regular expression string."""
    i = 0
    n = len(pattern)
    parts = ["^"]
    while i < n:
        ch = pattern[i]
        if ch == "*" and i + 1 < n and pattern[i + 1] == "*":
            i += 2
            if i < n and pattern[i] == "/":
                i += 1
                parts.append(r"(?:[^/]+/)*")
            else:
                parts.append(r".*?")
        elif ch == "*":
            parts.append(r"[^/]*")
            i += 1
        elif ch == "?":
            parts.append(r"[^/]")
            i += 1
        elif ch == "[":
            end = pattern.find("]", i + 1)
            if end == -1:
                parts.append(r"\[")
                i += 1
            else:
                cls = pattern[i + 1 : end]
                if cls.startswith("!"):
                    cls = "^" + cls[1:]
                parts.append(f"[{cls}]")
                i = end + 1
        elif ch == "{":
            end = pattern.find("}", i + 1)
            if end == -1:
                parts.append(r"\{")
                i += 1
            else:
                inner = pattern[i + 1 : end]
                alts = "|".join(_glob_fragment_re(a) for a in inner.split(","))
                parts.append(f"(?:{alts})")
                i = end + 1
        else:
            parts.append(re.escape(ch))
            i += 1
    parts.append("$")
    return "".join(parts)


def _glob_fragment_re(pattern: str) -> str:
    """Minimal glob-to-regex for brace expansion fragments."""
    i = 0
    n = len(pattern)
    parts = []
    while i < n:
        ch = pattern[i]
        if ch == "*" and i + 1 < n and pattern[i + 1] == "*":
            i += 2
            if i < n and pattern[i] == "/":
                i += 1
                parts.append(r"(?:[^/]+/)*")
            else:
                parts.append(r".*?")
        elif ch == "*":
            parts.append(r"[^/]*")
            i += 1
        elif ch == "?":
            parts.append(r"[^/]")
            i += 1
        else:
            parts.append(re.escape(ch))
            i += 1
    return "".join(parts)


def _matches_excluded_dir(relative_dir: str, pattern: str) -> bool:
    """Check if a directory matches an exclude glob pattern."""
    normalized = relative_dir.rstrip("/")
    return (
        matches_glob(normalized, pattern)
        or matches_glob(f"{normalized}/", pattern)
        or matches_glob(f"{normalized}/.probe", pattern)
    )


# ── Markdown detection ──────────────────────────────────────────────────────


def _is_markdown_file(name: str) -> bool:
    lower = name.lower()
    return lower.endswith(".md") or lower.endswith(".markdown")


def _looks_like_obsidian_note(file_path: str, text: str) -> bool:
    """Heuristic: does this file look like an Obsidian note (frontmatter tags or inline #tags)."""
    frontmatter_start = _parse_frontmatter_start(text)
    if frontmatter_start is None:
        return _has_inline_obsidian_tag(text)

    parsed = _find_frontmatter_end(text, frontmatter_start)
    if parsed is None:
        return _has_inline_obsidian_tag(text)

    frontmatter = text[frontmatter_start : parsed["position"]]
    for line in frontmatter.splitlines():
        trimmed = line.lstrip()
        if any(
            trimmed.startswith(prefix)
            for prefix in ("tags:", "tag:", "openclaw:", "memory:")
        ):
            return True

    return _has_inline_obsidian_tag(text[parsed["body_offset"] :])


def _parse_frontmatter_start(text: str) -> int | None:
    if text.startswith("---\n"):
        return 4
    if text.startswith("---\r\n"):
        return 5
    return None


def _find_frontmatter_end(text: str, offset: int) -> dict | None:
    for i in range(offset, len(text) - 3):
        if (
            text[i] == "-"
            and text[i + 1] == "-"
            and text[i + 2] == "-"
        ):
            if text[i + 3] == "\n":
                return {"position": i, "body_offset": i + 4}
            if text[i + 3] == "\r" and i + 4 < len(text) and text[i + 4] == "\n":
                return {"position": i, "body_offset": i + 5}
    return None


def _has_inline_obsidian_tag(text: str) -> bool:
    in_fence = False
    tag_re = re.compile(r"(^|[^A-Za-z0-9_])#([A-Za-z][A-Za-z0-9/_-]*)\b")
    for line in text.splitlines():
        trimmed = line.lstrip()
        if trimmed.startswith("```") or trimmed.startswith("~~~"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        searchable = re.sub(r"^#{1,6}\s+", "", trimmed)
        if tag_re.search(searchable):
            return True
    return False


# ── Streamed file read with incremental FNV-1a ───────────────────────────────


def _stream_read_file_with_hash(file_path: str, max_bytes: int) -> dict | str | None:
    """Stream-read a file in 64KB chunks, incrementally computing FNV-1a hash.

    Returns:
        dict with ``text`` and ``fileHash`` on success,
        ``"too_large"`` if file exceeds max_bytes,
        ``None`` on read error.
    """
    try:
        fh = open(file_path, "rb")
    except OSError:
        return None

    try:
        chunks: list[str] = []
        h = _FNV_OFFSET
        total = 0

        while True:
            data = fh.read(STREAM_CHUNK_BYTES)
            if not data:
                break
            total += len(data)
            if total > max_bytes:
                return "too_large"
            h = _incremental_fnv1a64(h, data)
            chunks.append(data.decode("utf-8", errors="replace"))

        return {
            "text": "".join(chunks),
            "fileHash": _incremental_fnv_to_hex(h),
        }
    finally:
        fh.close()


# ── Markdown ingest queue (chunked REPLACE/APPEND) ───────────────────────────


class MarkdownIngestQueue:
    """Chunked markdown ingest queue with REPLACE/APPEND semantics.

    First chunk for a document is sent with REPLACE mode (overwrites existing),
    subsequent chunks use APPEND. Includes retry with exponential backoff + full
    jitter and burst feedback handling, matching the TypeScript IngestQueue.
    """

    def __init__(
        self,
        rpc_caller: Callable[[str, Any], Any],
        user_id: str,
        logger_override=None,
        chunk_tokens: int = _INGEST_CHUNK_TOKENS,
        retry_base_delay_ms: int = _INGEST_RETRY_BASE_DELAY_MS,
        max_retries: int = _INGEST_MAX_RETRIES,
    ):
        self._rpc_caller = rpc_caller
        self._user_id = user_id
        self._log = logger_override or logger
        self._chunk_tokens = max(1, int(chunk_tokens))
        self._retry_base_delay_ms = retry_base_delay_ms
        self._max_retries = max_retries

    def enqueue_ingest(
        self,
        source_doc: str,
        text: str,
        source_root: str,
        source_path: str,
        source_kind: str,
        file_hash: str,
        source_size: int,
        source_mtime_ms: int,
        source_ctime_ms: int,
        on_chunk_feedback: Callable[[dict], None] | None = None,
    ) -> dict | None:
        """Ingest a markdown document as one or more chunks.

        Returns the last feedback dict, or None.
        """
        if not text.strip():
            return None

        current_limit = self._chunk_tokens
        offset = 0
        is_first = True
        last_feedback: dict | None = None

        while offset < len(text):
            remaining = text[offset:]
            chunks = _split_text_chunks(remaining, current_limit)
            chunk_text = chunks[0]["text"]

            mode = 0 if is_first else 1  # REPLACE=0, APPEND=1
            try:
                resp = self._ingest_with_retry(
                    source_doc=source_doc,
                    text=chunk_text,
                    source_root=source_root,
                    source_path=source_path,
                    source_kind=source_kind,
                    file_hash=file_hash,
                    source_size=source_size,
                    source_mtime_ms=source_mtime_ms,
                    source_ctime_ms=source_ctime_ms,
                    mode=mode,
                )
                fb = _extract_feedback(resp) if resp else None
                last_feedback = fb

                if fb:
                    if on_chunk_feedback:
                        on_chunk_feedback(fb)

                    # Burst limit: daemon told us to use smaller chunks
                    if (
                        fb.get("nodesAccepted", 0) == 0
                        and fb.get("tokenBurstLimit", 0) > 0
                        and fb["tokenBurstLimit"] < current_limit
                    ):
                        current_limit = fb["tokenBurstLimit"]
                        continue

                    if fb.get("nodesAccepted", 0) == 0:
                        self._log.warning(
                            "[markdown-ingest-queue] Chunk permanently rejected for %s "
                            "at offset=%d length=%d tokenBurstLimit=%s",
                            source_doc, offset, len(chunk_text),
                            fb.get("tokenBurstLimit", "unset"),
                        )

                offset += len(chunk_text)
                is_first = False

                # Back-pressure: wait if daemon says to pause
                if fb and not fb.get("acceptMore", True) and offset < len(text):
                    delay = fb.get("retryAfterMs") or 1000
                    self._log.debug(
                        "[markdown-ingest-queue] back-pressure: acceptMore=false — waiting %dms",
                        delay,
                    )
                    time.sleep(delay / 1000)

            except Exception as exc:
                self._log.debug(
                    "[markdown-ingest-queue] ingest chunk failed for %s at offset %d: %s",
                    source_doc, offset, exc,
                )
                offset += len(chunk_text)
                is_first = False

        return last_feedback

    def enqueue_delete(self, source_doc: str) -> None:
        """Delete an authored document with retry."""
        for attempt in range(self._max_retries + 1):
            try:
                req = pb.DeleteAuthoredDocumentRequest(source_doc=source_doc)
                self._rpc_caller("DeleteAuthoredDocument", req)
                return
            except Exception as exc:
                if attempt < self._max_retries:
                    cap = self._retry_base_delay_ms * (2 ** attempt)
                    delay = (hash(source_doc + str(attempt)) & 0xFFFF) / 0xFFFF * cap
                    self._log.debug(
                        "[markdown-ingest-queue] delete retry %d for %s in %.1fms",
                        attempt, source_doc, delay,
                    )
                    time.sleep(delay / 1000)
                else:
                    raise

    def _ingest_with_retry(
        self,
        source_doc: str,
        text: str,
        source_root: str,
        source_path: str,
        source_kind: str,
        file_hash: str,
        source_size: int,
        source_mtime_ms: int,
        source_ctime_ms: int,
        mode: int,
    ):
        """Send IngestMarkdownDocument with exponential backoff retry."""
        for attempt in range(self._max_retries + 1):
            try:
                req = pb.IngestMarkdownDocumentRequest(
                    source_doc=source_doc,
                    text=text,
                    tokenizer_id=DEFAULT_TOKENIZER_ID,
                    core_doc=True,
                    user_id=self._user_id,
                    mode=mode,
                    source_meta=pb.MarkdownSourceMeta(
                        source_root=source_root,
                        source_path=source_path,
                        source_kind=source_kind,
                        file_hash=file_hash,
                        source_size=source_size,
                        source_mtime_ms=source_mtime_ms,
                        source_ctime_ms=source_ctime_ms,
                        ingest_version=MARKDOWN_INGEST_VERSION,
                        hash_backend=HASH_BACKEND,
                    ),
                )
                return self._rpc_caller("IngestMarkdownDocument", req)
            except Exception as exc:
                if attempt < self._max_retries:
                    cap = self._retry_base_delay_ms * (2 ** attempt)
                    delay = (hash(source_doc + str(attempt)) & 0xFFFF) / 0xFFFF * cap
                    self._log.debug(
                        "[markdown-ingest-queue] retry %d/%d for %s mode=%d in %.1fms: %s",
                        attempt + 1, self._max_retries, source_doc, mode, delay, exc,
                    )
                    time.sleep(delay / 1000)
                else:
                    raise


def _split_text_chunks(text: str, max_tokens: int) -> list[dict]:
    """Split text at sentence/word boundaries to fit within max_tokens."""
    if max_tokens <= 0:
        return [{"text": text, "ordinal": 0}]
    max_chars = max_tokens * 4
    if len(text) <= max_chars:
        return [{"text": text, "ordinal": 0}]

    chunks: list[dict] = []
    offset = 0
    ordinal = 0

    while offset < len(text):
        end = min(offset + max_chars, len(text))
        probe_limit = min(256, end - offset)
        hard_cut = end

        # Look for double-newline boundary
        for i in range(probe_limit):
            pos = end - i
            if text[pos - 1 : pos + 1] == "\n\n" and pos + 1 <= len(text):
                hard_cut = pos + 1
                break

        # Fall back to single newline
        if hard_cut == end:
            for i in range(probe_limit):
                pos = end - i
                if pos > 0 and text[pos - 1] == "\n":
                    hard_cut = pos
                    break

        # Fall back to space
        if hard_cut == end:
            for i in range(probe_limit):
                pos = end - i
                if pos > 0 and text[pos - 1] == " ":
                    hard_cut = pos
                    break

        chunk_text = text[offset:hard_cut]
        if chunk_text.strip():
            chunks.append({"text": chunk_text, "ordinal": ordinal})
        offset = hard_cut
        ordinal += 1

    return chunks


def _extract_feedback(resp) -> dict | None:
    """Extract IngestFeedback fields from an RPC response."""
    if not resp or not hasattr(resp, "feedback") or not resp.feedback:
        return None
    fb = resp.feedback
    return {
        "queueDepth": getattr(fb, "queue_depth", 0),
        "queueCapacity": getattr(fb, "queue_capacity", 0),
        "acceptMore": getattr(fb, "accept_more", True),
        "retryAfterMs": getattr(fb, "retry_after_ms", 0),
        "processingTimeUs": getattr(fb, "processing_time_us", 0),
        "nodesAccepted": getattr(fb, "nodes_accepted", 0),
        "nodesRejected": getattr(fb, "nodes_rejected", 0),
        "tokensIngested": getattr(fb, "tokens_ingested", 0),
        "tokenBurstLimit": getattr(fb, "token_burst_limit", 0),
        "walDepth": getattr(fb, "wal_depth", 0),
        "walCapacity": getattr(fb, "wal_capacity", 0),
    }


# ── Snapshot persistence ────────────────────────────────────────────────────


class SnapshotStore:
    """JSON file tracking file→hash→mtime mappings for change detection."""

    def __init__(self, path: str):
        self._path = path
        self._files: dict[str, dict] = {}
        self._dirty = False
        self._loaded = False

    def load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            raw = Path(self._path).read_text()
            parsed = json.loads(raw)
            if (
                parsed.get("ingestVersion") != MARKDOWN_INGEST_VERSION
                or parsed.get("hashBackend") != HASH_BACKEND
            ):
                return
            for source_doc, state in (parsed.get("files") or {}).items():
                if self._is_valid_state(source_doc, state):
                    self._files[source_doc] = state
            logger.info(
                "Markdown ingest: loaded %d file snapshots from %s",
                len(self._files),
                self._path,
            )
        except FileNotFoundError:
            pass
        except Exception as exc:
            logger.warning("Markdown ingest: failed to load snapshot %s: %s", self._path, exc)

    def save_if_dirty(self) -> None:
        if not self._dirty:
            return
        payload = {
            "version": 1,
            "ingestVersion": MARKDOWN_INGEST_VERSION,
            "hashBackend": HASH_BACKEND,
            "files": dict(sorted(self._files.items())),
        }
        try:
            target = Path(self._path)
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_suffix(f".{os.getpid()}.tmp")
            tmp.write_text(json.dumps(payload, indent=2) + "\n")
            tmp.replace(target)
            self._dirty = False
        except Exception as exc:
            logger.warning("Markdown ingest: failed to write snapshot %s: %s", self._path, exc)

    def get(self, source_doc: str) -> dict | None:
        return self._files.get(source_doc)

    def set(self, source_doc: str, state: dict) -> None:
        self._files[source_doc] = state
        self._dirty = True

    def delete(self, source_doc: str) -> None:
        self._files.pop(source_doc, None)
        self._dirty = True

    def files_for_root(self, root: str) -> set[str]:
        resolved = str(Path(root).resolve())
        return {
            doc
            for doc, st in self._files.items()
            if st.get("root") == resolved
        }

    def is_dirty(self) -> bool:
        return self._dirty

    @staticmethod
    def _is_valid_state(source_doc: str, state: Any) -> bool:
        if not isinstance(state, dict):
            return False
        return (
            state.get("sourceDoc") == source_doc
            and isinstance(state.get("root"), str)
            and isinstance(state.get("relativePath"), str)
            and isinstance(state.get("fileHash"), str)
            and isinstance(state.get("size"), (int, float))
            and isinstance(state.get("mtimeMs"), (int, float))
        )


# ── Scan statistics ─────────────────────────────────────────────────────────


class ScanStats:
    def __init__(self):
        self.directories_scanned = 0
        self.directories_pruned = 0
        self.markdown_files_seen = 0
        self.files_included = 0
        self.files_skipped = 0
        self.files_unchanged = 0
        self.files_ingested = 0
        self.files_deleted = 0
        self.sync_errors = 0
        self.files_deferred = 0


# ── Polling directory watcher ────────────────────────────────────────────────


class _PollingWatcher:
    """Polling-based directory watcher for continuous markdown monitoring.

    Periodically checks directory mtimes under watched roots and triggers
    a callback when changes are detected. Equivalent to per-directory fs.watch
    in the TypeScript plugin, but using portable polling.
    """

    def __init__(self, roots: list[str], callback: Callable[[], None], interval_ms: int = DEFAULT_POLL_INTERVAL_MS):
        self._roots = list(roots)
        self._callback = callback
        self._interval = interval_ms / 1000
        self._dir_mtimes: dict[str, float] = {}
        self._running = False
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None

    def _poll_loop(self) -> None:
        while self._running:
            try:
                self._check_all_roots()
            except Exception:
                pass
            time.sleep(self._interval)

    def _check_all_roots(self) -> None:
        any_change = False
        for root in self._roots:
            if not os.path.isdir(root):
                continue
            for dirpath, _, _ in os.walk(root):
                try:
                    current_mtime = os.path.getmtime(dirpath)
                except OSError:
                    continue
                prev = self._dir_mtimes.get(dirpath)
                if prev is not None and prev != current_mtime:
                    any_change = True
                self._dir_mtimes[dirpath] = current_mtime

        if any_change:
            try:
                self._callback()
            except Exception:
                pass


# ── Markdown ingestion handle ───────────────────────────────────────────────


class MarkdownIngestionHandle:
    """Top-level handle that owns one or more source adapters.

    Created by the provider when ``markdownIngestionEnabled`` is True and at
    least one root directory is configured.
    """

    def __init__(
        self,
        config: dict,
        rpc_caller: Callable[[str, Any], Any],
        user_id: str,
        logger_override=None,
    ):
        self._config = config
        self._rpc_caller = rpc_caller
        self._user_id = user_id
        self._log = logger_override or logger
        self._adapters: list[DirectorySourceAdapter] = []

        generic_roots = self._normalize_roots(config.get("markdownIngestionRoots"))
        if self._is_enabled(generic_roots):
            self._adapters.append(
                DirectorySourceAdapter(
                    kind="generic",
                    roots=generic_roots,
                    include_patterns=config.get("markdownIngestionInclude") or [],
                    exclude_patterns=config.get("markdownIngestionExclude") or DEFAULT_EXCLUDES,
                    debounce_ms=config.get("markdownIngestionDebounceMs", DEFAULT_DEBOUNCE_MS),
                    snapshot_path=self._resolve_snapshot_path(
                        "generic", config.get("markdownIngestionSnapshotPath")
                    ),
                    priority_mode=config.get("markdownIngestionPriorityMode", "mtime"),
                    max_tokens_per_file=config.get("markdownIngestionMaxTokensPerFile", DEFAULT_MAX_TOKENS_PER_FILE),
                    rpc_caller=rpc_caller,
                    user_id=user_id,
                    obsidian_mode=False,
                    poll_interval_ms=config.get("markdownIngestionPollIntervalMs", DEFAULT_POLL_INTERVAL_MS),
                    logger_override=self._log,
                )
            )

        obsidian_roots = self._normalize_roots(config.get("markdownIngestionObsidianRoots"))
        if config.get("markdownIngestionObsidianEnabled") is True and obsidian_roots:
            self._adapters.append(
                DirectorySourceAdapter(
                    kind="obsidian",
                    roots=obsidian_roots,
                    include_patterns=config.get("markdownIngestionObsidianInclude") or [],
                    exclude_patterns=config.get("markdownIngestionObsidianExclude") or DEFAULT_EXCLUDES,
                    debounce_ms=(
                        config.get("markdownIngestionObsidianDebounceMs")
                        or config.get("markdownIngestionDebounceMs")
                        or DEFAULT_DEBOUNCE_MS
                    ),
                    snapshot_path=self._resolve_snapshot_path(
                        "obsidian", config.get("markdownIngestionObsidianSnapshotPath")
                    ),
                    priority_mode=config.get("markdownIngestionPriorityMode", "mtime"),
                    max_tokens_per_file=config.get("markdownIngestionMaxTokensPerFile", DEFAULT_MAX_TOKENS_PER_FILE),
                    rpc_caller=rpc_caller,
                    user_id=user_id,
                    obsidian_mode=True,
                    poll_interval_ms=config.get("markdownIngestionPollIntervalMs", DEFAULT_POLL_INTERVAL_MS),
                    logger_override=self._log,
                )
            )

    @property
    def adapters(self) -> list[DirectorySourceAdapter]:
        return self._adapters

    @property
    def is_active(self) -> bool:
        return len(self._adapters) > 0

    def start(self) -> None:
        for adapter in self._adapters:
            adapter.start()

    def refresh(self) -> None:
        for adapter in self._adapters:
            adapter.refresh()

    def stop(self) -> None:
        for adapter in self._adapters:
            adapter.stop()

    def scan_status(self) -> dict:
        return {
            "active": self.is_active,
            "adapters": [
                {
                    "kind": a.kind,
                    "roots": a.roots,
                    "snapshotPath": a.snapshot_path,
                    "fileCount": len(a._snapshot._files),
                    "running": a._is_started and not a._is_stopping,
                    "lastAcceptMore": a.last_accept_more,
                    "lastQueueDepth": a.last_queue_depth,
                    "lastQueueCapacity": a.last_queue_capacity,
                }
                for a in self._adapters
            ],
        }

    @staticmethod
    def _normalize_roots(roots: list[str] | None) -> list[str]:
        if not roots:
            return []
        resolved = set()
        for r in roots:
            trimmed = r.strip()
            if trimmed:
                resolved.add(str(Path(trimmed).resolve()))
        return sorted(resolved)

    @staticmethod
    def _is_enabled(roots: list[str]) -> bool:
        return len(roots) > 0

    @staticmethod
    def _resolve_snapshot_path(kind: str, configured: str | None) -> str:
        if configured and configured.strip():
            return str(Path(configured.strip()).resolve())
        hermes_home = os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes"))
        return str(Path(hermes_home) / f"libravdb-markdown-ingest-{kind}.json")


# ── Root scan state ──────────────────────────────────────────────────────────


class _RootScanState:
    __slots__ = (
        "root", "scanning", "dirty", "timer", "resume_from_path",
        "known_files", "directory_watchers",
    )

    def __init__(self, root: str, known_files: set[str]):
        self.root = root
        self.scanning = False
        self.dirty = False
        self.timer: threading.Timer | None = None
        self.resume_from_path: str | None = None
        self.known_files = known_files


# ── Directory source adapter ────────────────────────────────────────────────


class DirectorySourceAdapter:
    """Scans a set of directory roots for markdown files and ingests them via gRPC.

    Port of the TypeScript ``DirectoryMarkdownSourceAdapter`` with full parity:
    debounce scheduling, back-pressure resume, polling-based file watching,
    streamed reads with incremental FNV-1a, and chunked REPLACE/APPEND ingest.
    """

    def __init__(
        self,
        kind: str,
        roots: list[str],
        include_patterns: list[str],
        exclude_patterns: list[str],
        debounce_ms: int,
        snapshot_path: str,
        priority_mode: str,
        max_tokens_per_file: int,
        rpc_caller: Callable[[str, Any], Any],
        user_id: str,
        obsidian_mode: bool = False,
        poll_interval_ms: int = DEFAULT_POLL_INTERVAL_MS,
        logger_override=None,
    ):
        self.kind = kind
        self.roots = roots
        self.include_patterns = include_patterns
        self.exclude_patterns = exclude_patterns
        self.debounce_ms = debounce_ms
        self.snapshot_path = snapshot_path
        self.priority_mode = priority_mode
        self.max_tokens_per_file = max(1, int(max_tokens_per_file))
        self._rpc_caller = rpc_caller
        self._user_id = user_id
        self._obsidian_mode = obsidian_mode
        self._log = logger_override or logger

        self._snapshot = SnapshotStore(snapshot_path)
        self._root_states: dict[str, _RootScanState] = {}
        self._active_scans: set[threading.Thread] = set()
        self._is_started = False
        self._is_stopping = False
        self._scan_lock = threading.Lock()

        # Ingest queue (lazy-init on first use)
        self._ingest_queue: MarkdownIngestQueue | None = None

        # Polling watcher for continuous monitoring
        self._watcher: _PollingWatcher | None = None
        self._poll_interval_ms = poll_interval_ms

        # Back-pressure / feedback state
        self.last_accept_more = True
        self.last_retry_after_ms = 0
        self.last_queue_depth = 0
        self.last_queue_capacity = 0
        self.last_processing_time_us = 0
        self.last_nodes_accepted = 0
        self.last_nodes_rejected = 0
        self.last_tokens_ingested = 0
        self.last_token_burst_limit = 512
        self.last_wal_depth = 0
        self.last_wal_capacity = 0

    # ── lifecycle ────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._is_started:
            return
        self._snapshot.load()
        self._is_started = True
        self._is_stopping = False

        # Initialize root states from snapshot
        for root in self.roots:
            resolved = str(Path(root).resolve())
            self._root_states[resolved] = _RootScanState(
                root=resolved,
                known_files=self._snapshot.files_for_root(resolved),
            )

        # Start polling watcher
        resolved_roots = [str(Path(r).resolve()) for r in self.roots]
        self._watcher = _PollingWatcher(
            roots=resolved_roots,
            callback=self._on_watcher_change,
            interval_ms=self._poll_interval_ms,
        )
        self._watcher.start()

        self.refresh()

    def refresh(self) -> None:
        if not self._is_started or self._is_stopping:
            return
        for root in self.roots:
            self._scan_root(str(Path(root).resolve()))

    def stop(self) -> None:
        self._is_stopping = True

        # Stop watcher
        if self._watcher:
            self._watcher.stop()
            self._watcher = None

        # Cancel pending timers
        for state in self._root_states.values():
            if state.timer:
                state.timer.cancel()
                state.timer = None

        # Wait for active scans
        active = list(self._active_scans)
        for t in active:
            t.join(timeout=30)

        self._snapshot.save_if_dirty()
        self._root_states.clear()
        self._is_started = False

    # ── watcher callback ──────────────────────────────────────────────────

    def _on_watcher_change(self) -> None:
        """Called by the polling watcher when directory changes are detected."""
        if self._is_stopping:
            return
        for state in self._root_states.values():
            state.resume_from_path = None
            self._schedule_root_scan(state)

    # ── scanning ─────────────────────────────────────────────────────────

    def _get_root_state(self, root: str) -> _RootScanState:
        resolved = str(Path(root).resolve())
        existing = self._root_states.get(resolved)
        if existing:
            return existing
        state = _RootScanState(
            root=resolved,
            known_files=self._snapshot.files_for_root(resolved),
        )
        self._root_states[resolved] = state
        return state

    def _scan_root(self, root: str) -> None:
        if not self._is_started or self._is_stopping:
            return

        state = self._get_root_state(root)
        if state.scanning:
            state.dirty = True
            return

        state.scanning = True
        self.last_accept_more = True
        self.last_retry_after_ms = 0

        t = threading.Thread(target=self._scan_root_impl, args=(state,), daemon=True)
        self._active_scans.add(t)
        t.start()

    def _scan_root_impl(self, state: _RootScanState) -> None:
        stats = ScanStats()
        started_at = time.monotonic()
        try:
            current_files: set[str] = set()
            candidates: list[dict] = []
            self._walk_directory(state.root, state.root, current_files, stats, candidates)
            self._sync_candidates(state, candidates, stats)
            if not self._is_stopping:
                self._prune_deleted(state.root, current_files, stats)
                state.known_files = current_files
                self._snapshot.save_if_dirty()
            elapsed = (time.monotonic() - started_at) * 1000
            self._log.info(
                "[markdown-ingest] %s scan complete root=%s dirs=%d pruned=%d "
                "markdown=%d included=%d skipped=%d unchanged=%d ingested=%d "
                "deleted=%d deferred=%d errors=%d durationMs=%d",
                self.kind, state.root,
                stats.directories_scanned, stats.directories_pruned,
                stats.markdown_files_seen, stats.files_included,
                stats.files_skipped, stats.files_unchanged,
                stats.files_ingested, stats.files_deleted,
                stats.files_deferred, stats.sync_errors,
                int(elapsed),
            )
        except Exception as exc:
            self._log.warning("[markdown-ingest] scan failed for root=%s: %s", state.root, exc)
        finally:
            state.scanning = False
            self._active_scans.discard(threading.current_thread())
            if state.dirty:
                state.dirty = False
                if not self._is_stopping:
                    self._schedule_root_scan(state)

    def _schedule_root_scan(self, state: _RootScanState, delay_ms: int | None = None) -> None:
        """Schedule a debounced rescan of a root."""
        if not self._is_started or self._is_stopping:
            return
        if state.scanning:
            state.dirty = True
            return
        if state.timer:
            return  # Already scheduled

        effective_delay = max(self.debounce_ms, delay_ms or 0) / 1000
        state.timer = threading.Timer(
            effective_delay,
            self._on_debounce_timer,
            args=(state,),
        )
        state.timer.daemon = True
        state.timer.start()

    def _on_debounce_timer(self, state: _RootScanState) -> None:
        state.timer = None
        self._scan_root(state.root)

    def _walk_directory(
        self,
        root: str,
        directory: str,
        current_files: set[str],
        stats: ScanStats,
        candidates: list[dict],
    ) -> None:
        if self._should_prune_dir(root, directory):
            stats.directories_pruned += 1
            return

        stats.directories_scanned += 1

        try:
            entries = list(Path(directory).iterdir())
        except (OSError, PermissionError):
            return

        for entry in entries:
            if self._is_stopping:
                return
            if entry.is_dir():
                self._walk_directory(root, str(entry), current_files, stats, candidates)
                continue
            if not entry.is_file() or not _is_markdown_file(entry.name):
                continue

            stats.markdown_files_seen += 1
            file_path = str(entry)

            if not self._should_include_file(root, file_path):
                stats.files_skipped += 1
                continue

            stats.files_included += 1
            current_files.add(file_path)

            try:
                stat = entry.stat()
            except OSError:
                continue

            candidates.append({
                "path": file_path,
                "size": stat.st_size,
                "mtime_ms": int(stat.st_mtime * 1000),
                "ctime_ms": int(stat.st_ctime * 1000),
                "ordinal": len(candidates),
            })

    # ── candidate sync ────────────────────────────────────────────────────

    def _sync_candidates(self, state: _RootScanState, candidates: list[dict], stats: ScanStats) -> None:
        sorted_candidates = self._sort_candidates(candidates)

        # Resume from back-pressure checkpoint
        skipping = False
        if state.resume_from_path:
            target_exists = any(c["path"] == state.resume_from_path for c in sorted_candidates)
            if target_exists:
                skipping = True
                self.last_accept_more = True
                self.last_retry_after_ms = 0
            else:
                state.resume_from_path = None

        for candidate in sorted_candidates:
            if skipping:
                if candidate["path"] == state.resume_from_path:
                    skipping = False
                else:
                    continue

            if self._is_stopping:
                return

            # Back-pressure gate: daemon says stop accepting
            if not self.last_accept_more:
                state.resume_from_path = candidate["path"]
                self._schedule_root_scan(state, self.last_retry_after_ms)
                return

            # WAL capacity check
            if self.last_wal_capacity > 0 and self.last_wal_depth > self.last_wal_capacity * 0.8:
                state.resume_from_path = candidate["path"]
                self._schedule_root_scan(state, 2000)
                return

            estimated_tokens = max(1, candidate["size"] // APPROX_CHARS_PER_TOKEN)
            if estimated_tokens > self.max_tokens_per_file:
                stats.files_deferred += 1
                continue

            try:
                result = self._sync_markdown_file(
                    state.root, candidate["path"],
                    size=candidate["size"],
                    mtime_ms=candidate["mtime_ms"],
                    ctime_ms=candidate["ctime_ms"],
                )
                if result == "ingested":
                    stats.files_ingested += 1
                elif result == "unchanged":
                    stats.files_unchanged += 1
                elif result == "deleted":
                    stats.files_deleted += 1
                else:
                    stats.files_skipped += 1
            except Exception as exc:
                stats.sync_errors += 1
                self._log.warning("[markdown-ingest] sync failed for %s: %s", candidate["path"], exc)

        state.resume_from_path = None

    def _sync_markdown_file(
        self,
        root: str,
        file_path: str,
        size: int,
        mtime_ms: int,
        ctime_ms: int,
    ) -> str:
        """Sync a single markdown file using streamed reads and chunked ingest queue.

        Returns 'ingested', 'unchanged', 'deleted', or 'skipped'.
        """
        relative_path = str(Path(file_path).relative_to(root)).replace(os.sep, "/")
        source_doc = file_path

        # Check snapshot for unchanged files (size + mtime match)
        cached = self._snapshot.get(source_doc)
        if cached and cached.get("size") == size and cached.get("mtimeMs") == mtime_ms:
            return "unchanged"

        # Streamed read with incremental FNV hash
        max_bytes = self.max_tokens_per_file * 4 + 3
        streamed = _stream_read_file_with_hash(file_path, max_bytes)

        if streamed == "too_large":
            return "skipped"

        if streamed is None:
            self._snapshot.delete(source_doc)
            return "deleted"

        text = streamed["text"]
        file_hash = streamed["fileHash"]

        # Hash-only change (mtime different but content same)
        if cached and cached.get("fileHash") == file_hash:
            self._snapshot.set(source_doc, {
                "root": root,
                "sourceDoc": source_doc,
                "relativePath": relative_path,
                "fileHash": file_hash,
                "size": size,
                "mtimeMs": mtime_ms,
            })
            return "unchanged"

        # Obsidian mode: skip files that don't look like Obsidian notes
        if self._obsidian_mode and not self.include_patterns:
            if not _looks_like_obsidian_note(file_path, text):
                self._snapshot.delete(source_doc)
                return "deleted" if cached else "skipped"

        # Ingest via chunked REPLACE/APPEND queue
        queue = self._get_ingest_queue()
        feedback = queue.enqueue_ingest(
            source_doc=source_doc,
            text=text,
            source_root=root,
            source_path=relative_path,
            source_kind=self.kind,
            file_hash=file_hash,
            source_size=size,
            source_mtime_ms=mtime_ms,
            source_ctime_ms=ctime_ms,
            on_chunk_feedback=self._apply_ingest_feedback,
        )
        if feedback:
            self._apply_ingest_feedback(feedback)

        self._snapshot.set(source_doc, {
            "root": root,
            "sourceDoc": source_doc,
            "relativePath": relative_path,
            "fileHash": file_hash,
            "size": size,
            "mtimeMs": mtime_ms,
        })
        return "ingested"

    def _apply_ingest_feedback(self, feedback: dict | None) -> None:
        """Update back-pressure state from ingest queue feedback."""
        if feedback and isinstance(feedback.get("acceptMore"), bool):
            self.last_accept_more = feedback["acceptMore"]
            self.last_queue_depth = feedback.get("queueDepth", 0)
            self.last_queue_capacity = feedback.get("queueCapacity", 0)
            self.last_processing_time_us = feedback.get("processingTimeUs", 0)
            self.last_nodes_accepted = feedback.get("nodesAccepted", 0)
            self.last_nodes_rejected = feedback.get("nodesRejected", 0)
            self.last_tokens_ingested = feedback.get("tokensIngested", 0)
            if feedback.get("tokenBurstLimit", 0) > 0:
                self.last_token_burst_limit = feedback["tokenBurstLimit"]
            self.last_wal_depth = feedback.get("walDepth", 0)
            self.last_wal_capacity = feedback.get("walCapacity", 0)
            if self.last_accept_more:
                self.last_retry_after_ms = 0
            else:
                self.last_retry_after_ms = feedback.get("retryAfterMs") or 1000
        else:
            self.last_accept_more = True
            self.last_retry_after_ms = 0
            self.last_queue_depth = 0
            self.last_queue_capacity = 0
            self.last_processing_time_us = 0
            self.last_nodes_accepted = 0
            self.last_nodes_rejected = 0
            self.last_tokens_ingested = 0

    def _get_ingest_queue(self) -> MarkdownIngestQueue:
        if self._ingest_queue is None:
            self._ingest_queue = MarkdownIngestQueue(
                rpc_caller=self._rpc_caller,
                user_id=self._user_id,
                logger_override=self._log,
            )
        return self._ingest_queue

    # ── delete handling ──────────────────────────────────────────────────

    def _delete_source_document(self, source_doc: str) -> None:
        try:
            queue = self._get_ingest_queue()
            queue.enqueue_delete(source_doc)
        except Exception as exc:
            self._log.debug("[markdown-ingest] delete failed for %s: %s", source_doc, exc)

    # ── pruning ──────────────────────────────────────────────────────────

    def _prune_deleted(self, root: str, current_files: set[str], stats: ScanStats) -> None:
        state = self._root_states.get(root)
        previous = state.known_files if state else self._snapshot.files_for_root(root)
        removed = [f for f in previous if f not in current_files]
        if not removed:
            return
        for file_path in removed:
            self._delete_source_document(file_path)
            self._snapshot.delete(file_path)
            stats.files_deleted += 1

    # ── include/exclude helpers ──────────────────────────────────────────

    def _should_prune_dir(self, root: str, directory: str) -> bool:
        try:
            relative = str(Path(directory).relative_to(root)).replace(os.sep, "/")
        except ValueError:
            return False
        if not relative or relative == ".":
            return False
        for pattern in self.exclude_patterns:
            if _matches_excluded_dir(relative, pattern):
                return True
        return False

    def _should_include_file(self, root: str, file_path: str) -> bool:
        # Always include memory.md (OpenClaw convention)
        if Path(file_path).name.lower() == "memory.md":
            return True

        try:
            relative = str(Path(file_path).relative_to(root)).replace(os.sep, "/")
        except ValueError:
            return False

        # Exclude patterns
        if self.exclude_patterns:
            for pattern in self.exclude_patterns:
                if matches_glob(relative, pattern):
                    return False

        # Include patterns (if configured, only matching files pass)
        if self.include_patterns:
            for pattern in self.include_patterns:
                if matches_glob(relative, pattern):
                    return True
            return False

        return True

    # ── candidate sorting ────────────────────────────────────────────────

    def _sort_candidates(self, candidates: list[dict]) -> list[dict]:
        mode = self.priority_mode
        result = list(candidates)
        if mode == "size":
            result.sort(key=lambda c: (-c["size"], c["ordinal"]))
        elif mode == "ctime":
            result.sort(key=lambda c: (-c["ctime_ms"], c["ordinal"]))
        elif mode == "fifo":
            result.sort(key=lambda c: c["ordinal"])
        else:  # mtime (default)
            result.sort(key=lambda c: (-c["mtime_ms"], c["ordinal"]))
        return result
