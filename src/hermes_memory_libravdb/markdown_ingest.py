"""Markdown ingestion: directory scanning, glob rules, hashing, snapshots, gRPC ingest.

Ported from openclaw-memory-libravdb markdown-ingest.ts and markdown-hash.ts.
All heavy processing (tokenization, chunking, embedding) is handled by the daemon
via IngestMarkdownDocument RPC — this module handles file discovery, change
detection, and the RPC dispatch layer.
"""

from __future__ import annotations

import hashlib
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
DEFAULT_TOKENIZER_ID = "markdown-ingest:v1"
MARKDOWN_INGEST_VERSION = 3
HASH_BACKEND = "python-fnv1a64"
STREAM_CHUNK_BYTES = 64 * 1024
DEFAULT_MAX_TOKENS_PER_FILE = 128_000

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
                    "running": a._started and not a._stopping,
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


# ── Directory source adapter ────────────────────────────────────────────────


class DirectorySourceAdapter:
    """Scans a set of directory roots for markdown files and ingests them via gRPC.

    Port of the TypeScript ``DirectoryMarkdownSourceAdapter``.
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
        self._started = False
        self._stopping = False
        self._scan_lock = threading.Lock()
        self._active_scans: set[threading.Thread] = set()
        self._known_files: dict[str, set[str]] = {}  # root → set of sourceDoc paths

    # ── lifecycle ────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._started:
            return
        self._snapshot.load()
        self._started = True
        self._stopping = False
        self.refresh()

    def refresh(self) -> None:
        if not self._started or self._stopping:
            return
        for root in self.roots:
            self._scan_root(root)

    def stop(self) -> None:
        self._stopping = True
        # Wait for active scans
        active = list(self._active_scans)
        for t in active:
            t.join(timeout=30)
        self._snapshot.save_if_dirty()
        self._started = False

    # ── scanning ─────────────────────────────────────────────────────────

    def _scan_root(self, root: str) -> None:
        if not self._started or self._stopping:
            return

        resolved = str(Path(root).resolve())
        with self._scan_lock:
            # Single scan per root at a time — if already scanning, mark dirty
            pass  # We use a simpler fire-and-forget model

        t = threading.Thread(target=self._scan_root_impl, args=(resolved,), daemon=True)
        self._active_scans.add(t)
        t.start()

    def _scan_root_impl(self, root: str) -> None:
        stats = ScanStats()
        started_at = time.monotonic()
        try:
            current_files: set[str] = set()
            candidates: list[dict] = []
            self._walk_directory(root, root, current_files, stats, candidates)
            self._sync_candidates(root, candidates, stats)
            if not self._stopping:
                self._prune_deleted(root, current_files, stats)
                self._known_files[root] = current_files
                self._snapshot.save_if_dirty()
            elapsed = (time.monotonic() - started_at) * 1000
            self._log.info(
                "[markdown-ingest] %s scan complete root=%s dirs=%d pruned=%d "
                "markdown=%d included=%d skipped=%d unchanged=%d ingested=%d "
                "deleted=%d deferred=%d errors=%d durationMs=%d",
                self.kind, root,
                stats.directories_scanned, stats.directories_pruned,
                stats.markdown_files_seen, stats.files_included,
                stats.files_skipped, stats.files_unchanged,
                stats.files_ingested, stats.files_deleted,
                stats.files_deferred, stats.sync_errors,
                int(elapsed),
            )
        except Exception as exc:
            self._log.warning("[markdown-ingest] scan failed for root=%s: %s", root, exc)
        finally:
            self._active_scans.discard(threading.current_thread())

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
            if self._stopping:
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

    def _sync_candidates(self, root: str, candidates: list[dict], stats: ScanStats) -> None:
        sorted_candidates = self._sort_candidates(candidates)
        for candidate in sorted_candidates:
            if self._stopping:
                return
            estimated_tokens = max(1, candidate["size"] // APPROX_CHARS_PER_TOKEN)
            if estimated_tokens > self.max_tokens_per_file:
                stats.files_deferred += 1
                continue
            try:
                result = self._sync_markdown_file(
                    root, candidate["path"],
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

    def _sync_markdown_file(
        self,
        root: str,
        file_path: str,
        size: int,
        mtime_ms: int,
        ctime_ms: int,
    ) -> str:
        """Sync a single markdown file. Returns 'ingested', 'unchanged', 'deleted', or 'skipped'."""
        relative_path = str(Path(file_path).relative_to(root)).replace(os.sep, "/")
        source_doc = file_path

        # Check snapshot for unchanged files
        cached = self._snapshot.get(source_doc)
        if cached and cached.get("size") == size and cached.get("mtimeMs") == mtime_ms:
            return "unchanged"

        # Read file (with size cap)
        max_bytes = self.max_tokens_per_file * 4 + 3
        try:
            file_stat = Path(file_path).stat()
        except OSError:
            self._snapshot.delete(source_doc)
            return "deleted"

        if file_stat.st_size > max_bytes:
            return "skipped"

        try:
            text = Path(file_path).read_text()
        except Exception:
            self._snapshot.delete(source_doc)
            return "deleted"

        file_hash = hash_text(text)

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

        # Ingest via gRPC
        self._ingest_markdown_document(
            source_doc=source_doc,
            text=text,
            source_root=root,
            source_path=relative_path,
            file_hash=file_hash,
            source_size=size,
            source_mtime_ms=mtime_ms,
            source_ctime_ms=ctime_ms,
        )

        self._snapshot.set(source_doc, {
            "root": root,
            "sourceDoc": source_doc,
            "relativePath": relative_path,
            "fileHash": file_hash,
            "size": size,
            "mtimeMs": mtime_ms,
        })
        return "ingested"

    # ── gRPC ingest ──────────────────────────────────────────────────────

    def _ingest_markdown_document(
        self,
        source_doc: str,
        text: str,
        source_root: str,
        source_path: str,
        file_hash: str,
        source_size: int,
        source_mtime_ms: int,
        source_ctime_ms: int,
    ) -> None:
        req = pb.IngestMarkdownDocumentRequest(
            source_doc=source_doc,
            text=text,
            tokenizer_id=DEFAULT_TOKENIZER_ID,
            core_doc=True,
            user_id=self._user_id,
            source_meta=pb.MarkdownSourceMeta(
                source_root=source_root,
                source_path=source_path,
                source_kind=self.kind,
                file_hash=file_hash,
                source_size=source_size,
                source_mtime_ms=source_mtime_ms,
                source_ctime_ms=source_ctime_ms,
                ingest_version=MARKDOWN_INGEST_VERSION,
                hash_backend=HASH_BACKEND,
            ),
        )
        self._rpc_caller("IngestMarkdownDocument", req)

    def _delete_source_document(self, source_doc: str) -> None:
        try:
            req = pb.DeleteAuthoredDocumentRequest(source_doc=source_doc)
            self._rpc_caller("DeleteAuthoredDocument", req)
        except Exception as exc:
            self._log.debug("[markdown-ingest] delete failed for %s: %s", source_doc, exc)

    # ── pruning ──────────────────────────────────────────────────────────

    def _prune_deleted(self, root: str, current_files: set[str], stats: ScanStats) -> None:
        previous = self._known_files.get(root, self._snapshot.files_for_root(root))
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
