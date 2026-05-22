from __future__ import annotations

import os
import hmac
import hashlib
import threading
import time
import logging
import random
from pathlib import Path
from typing import Any, Dict, List

import grpc
from libravdb.ipc.v1 import rpc_pb2 as pb
from libravdb.ipc.v1 import rpc_pb2_grpc as services

from .identity import resolve_identity, ResolvedIdentity
from .scopes import (
    resolve_search_scopes,
    session_collection,
    validate_collection_name,
)

logger = logging.getLogger(__name__)

DEFAULT_TOP_K = 8
DEFAULT_MIN_SCORE = 0.35

# Ingest queue defaults
_INGEST_CHUNK_TOKENS = 8192
_INGEST_RETRY_BASE_DELAY_MS = 500
_INGEST_MAX_RETRIES = 4


def _get_hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")))


def _resolve_endpoint(endpoint: str | None = None) -> str:
    if endpoint and endpoint != "auto":
        return endpoint
    return os.environ.get(
        "LIBRAVDB_GRPC_ENDPOINT",
        f"unix:{os.path.expanduser('~/.libravdbd/run/libravdb.sock')}",
    )


def _load_secret() -> str | None:
    secret = os.environ.get("LIBRAVDB_AUTH_SECRET")
    if secret:
        return secret
    secret_path = os.environ.get("LIBRAVDB_AUTH_SECRET_FILE")
    if secret_path:
        try:
            return Path(secret_path).read_text().strip()
        except Exception:
            return None
    return None


def _is_loopback_host(host: str) -> bool:
    return host.lower() in ("localhost", "127.0.0.1", "::1")


class _NonceState:
    def __init__(self, secret: str | None):
        self._secret = secret
        self._nonce: str | None = None
        self._recovering = False

    def should_sign(self, method_name: str) -> bool:
        return bool(self._secret and self._nonce and method_name != "Health")

    def update_nonce(self, nonce: str | None):
        self._nonce = nonce

    def get_nonce(self) -> str | None:
        return self._nonce

    def build_metadata(self, method_name: str) -> list[tuple[str, str]]:
        if not self.should_sign(method_name):
            return []
        hmac_val = hmac.new(
            self._secret.encode(),
            self._nonce.encode(),
            hashlib.sha256,
        ).hexdigest()
        return [
            ("x-libravdb-auth", hmac_val),
            ("x-libravdb-nonce", self._nonce),
        ]


class _GrpcChannel:
    def __init__(
        self,
        endpoint: str,
        secret: str | None,
        timeout_ms: int = 30000,
    ):
        self._endpoint = endpoint
        self._secret = secret
        self._timeout_ms = timeout_ms
        self._channel: grpc.Channel | None = None
        self._stub: services.LibravDBStub | None = None
        self._nonce_state = _NonceState(secret)
        self._rpc_lock = threading.Lock()
        self._closed = False

    def _create_channel(self) -> grpc.Channel:
        is_unix = self._endpoint.startswith("unix:")
        target = self._endpoint[5:] if is_unix else self._endpoint.replace("tcp:", "")

        if is_unix:
            return grpc.insecure_channel(target)

        host = target.split(":")[0] if ":" in target else target
        if _is_loopback_host(host):
            return grpc.insecure_channel(target)

        return grpc.ssl_channel_credentials()

    def _get_stub(self) -> services.LibravDBStub:
        if self._stub is None:
            self._channel = self._create_channel()
            self._stub = services.LibravDBStub(self._channel)
        return self._stub

    def _call(self, method_name: str, req) -> Any:
        if self._closed:
            raise RuntimeError("Channel is closed")
        with self._rpc_lock:
            metadata = self._nonce_state.build_metadata(method_name)
            try:
                stub = self._get_stub()
                method = getattr(stub, method_name)
                resp = method(req, metadata=metadata, timeout=self._timeout_ms / 1000)
                self._update_nonce_from_response(resp, method_name)
                return resp
            except grpc.RpcError as e:
                self._nonce_state.update_nonce(None)
                raise

    def _update_nonce_from_response(self, resp, method_name: str):
        try:
            metadata = resp.initial_metadata()
            nonce = metadata.get("x-libravdb-nonce") if metadata else None
            if nonce:
                self._nonce_state.update_nonce(nonce)
            elif method_name == "Health" and not self._nonce_state.get_nonce():
                logger.warning("No x-libravdb-nonce in Health response — auth may be disabled on this server")
        except Exception:
            pass

    async def health(self) -> pb.HealthResponse:
        return self._call("Health", pb.HealthRequest())

    def close(self):
        self._closed = True
        if self._channel:
            self._channel.close()
            self._channel = None
            self._stub = None


class LibraVDBMemoryProvider:
    def __init__(self) -> None:
        self._hermes_home = _get_hermes_home()
        self._endpoint = _resolve_endpoint()
        self._secret = _load_secret()
        self._channel: _GrpcChannel | None = None
        self._session_id = ""
        self._session_key = ""
        self._resolved_identity: ResolvedIdentity | None = None
        self._writes_enabled = True
        self._startup_error: str | None = None
        self._config = self._load_config()

    def _load_config(self) -> Dict[str, Any]:
        config_path = self._hermes_home / "libravdb.json"
        if not config_path.exists():
            return {}
        try:
            import json
            return json.loads(config_path.read_text())
        except Exception:
            return {}

    @property
    def name(self) -> str:
        return "libravdb"

    def is_available(self) -> bool:
        return True

    @property
    def user_id(self) -> str:
        """Resolved stable user identity for collection naming."""
        return self._resolved_identity.user_id if self._resolved_identity else "default"

    def initialize(self, session_id: str, **kwargs) -> None:
        self._session_id = session_id
        self._session_key = session_id
        # Resolve identity: explicit user_id arg wins, else auto-derive
        explicit_user_id = str(kwargs.get("user_id") or "").strip() or None
        self._resolved_identity = resolve_identity(
            config={"userId": explicit_user_id} if explicit_user_id else self._config,
            hermes_home=self._hermes_home,
            session_key=session_id,
        )
        self._writes_enabled = str(kwargs.get("agent_context") or "primary") == "primary"
        self._startup_error = None
        self._embedding_profile = self._config.get("embeddingProfile", "nomic-embed-text-v1.5")
        self._fallback_profile = self._config.get("fallbackProfile", "bge-small-en-v1.5")
        self._onnx_device = self._config.get("onnxDevice", "cpu")
        self._cross_session_recall = self._config.get("crossSessionRecall", True)
        self._compact_session_token_budget = int(self._config.get("compactSessionTokenBudget", 2000))
        try:
            self._channel = _GrpcChannel(
                endpoint=self._endpoint,
                secret=self._secret,
            )
        except Exception as exc:
            self._startup_error = str(exc)
            self._channel = None

    def system_prompt_block(self) -> str:
        if self._startup_error:
            return (
                "# LibraVDB Memory\n"
                f"Configured but degraded: {self._startup_error}\n"
                "Hermes built-in MEMORY.md and USER.md are still active."
            )
        return (
            "# LibraVDB Memory\n"
            "Active external memory provider backed by the libravdbd daemon via native gRPC.\n"
            "Use libravdb_search for deep semantic recall when built-in memory is not enough.\n"
            "Hermes built-in MEMORY.md and USER.md remain active alongside this provider."
        )

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        if not query.strip() or not self._channel:
            return ""
        try:
            session = session_id or self._session_id
            collections = resolve_search_scopes(
                user_id=self.user_id,
                session_id=session,
                cross_session_recall=self._cross_session_recall,
            )
            if not collections:
                return ""
            if len(collections) == 1:
                resp = self._channel._call("SearchText", pb.SearchTextRequest(
                    collection=collections[0],
                    text=query,
                    k=self._config.get("topK", DEFAULT_TOP_K),
                ))
            else:
                resp = self._channel._call("SearchTextCollections", pb.SearchTextCollectionsRequest(
                    collections=collections,
                    text=query,
                    k=self._config.get("topK", DEFAULT_TOP_K),
                    exclude_by_collection={},
                ))
            return self._format_prefetch(resp)
        except Exception as exc:
            logger.debug("LibraVDB prefetch failed: %s", exc)
            return ""

    def _ingest_async(self, user_content: str, assistant_content: str, session: str) -> None:
        try:
            for role, content in [("user", user_content), ("assistant", assistant_content)]:
                req = pb.IngestMessageKernelRequest(
                    session_id=session,
                    session_key=session,
                    user_id=self.user_id or "",
                    message=pb.KernelMessage(role=role, content=content),
                )
                self._channel._call("IngestMessageKernel", req)
        except Exception as exc:
            logger.debug("LibraVDB sync_turn failed: %s", exc)

    def sync_turn(self, user_content: str, assistant_content: str, *, session_id: str = "") -> None:
        if not self._channel or not self._writes_enabled:
            return
        session = session_id or self._session_id
        queue = IngestQueue(
            channel=self._channel,
            user_id=self.user_id,
            session_id=session,
            chunk_tokens=self._config.get("ingestChunkTokens", _INGEST_CHUNK_TOKENS),
            retry_base_delay_ms=self._config.get("ingestRetryBaseDelayMs", _INGEST_RETRY_BASE_DELAY_MS),
            max_retries=self._config.get("ingestMaxRetries", _INGEST_MAX_RETRIES),
        )
        threading.Thread(target=self._ingest_with_queue, args=(queue, user_content, assistant_content), daemon=True).start()

    def _ingest_with_queue(self, queue: "IngestQueue", user_content: str, assistant_content: str) -> None:
        try:
            queue.enqueue(user_content, role="user")
            queue.enqueue(assistant_content, role="assistant")
        except Exception as exc:
            logger.debug("LibraVDB ingest queue failed: %s", exc)

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return [
            {
                "name": "libravdb_search",
                "description": (
                    "Search LibraVDB long-term memory across recent session context, durable user memory, "
                    "and global shared memory. Use this when you need exact recall beyond Hermes' built-in "
                    "MEMORY.md and USER.md."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Semantic search query."},
                        "limit": {"type": "integer", "description": "Maximum number of results to return."},
                        "min_score": {"type": "number", "description": "Minimum semantic score threshold."},
                    },
                    "required": ["query"],
                },
            },
            {
                "name": "libravdb_status",
                "description": "Check whether the LibraVDB daemon is reachable and show memory backend health.",
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        ]

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        import json
        if not self._channel:
            return json.dumps({"error": self._startup_error or "LibraVDB bridge is not available"})
        try:
            if tool_name == "libravdb_search":
                query = str(args.get("query") or "").strip()
                if not query:
                    return json.dumps({"error": "Missing required argument: query"})
                k = args.get("limit", self._config.get("topK", DEFAULT_TOP_K))
                collections = resolve_search_scopes(
                    user_id=self.user_id,
                    session_id=self._session_id,
                    cross_session_recall=self._cross_session_recall,
                )
                if len(collections) == 1:
                    resp = self._channel._call("SearchText", pb.SearchTextRequest(
                        collection=collections[0],
                        text=query,
                        k=k,
                    ))
                else:
                    resp = self._channel._call("SearchTextCollections", pb.SearchTextCollectionsRequest(
                        collections=collections,
                        text=query,
                        k=k,
                        exclude_by_collection={},
                    ))
                results = [self._result_to_dict(r) for r in resp.results]
                min_score = args.get("min_score")
                if min_score is not None:
                    results = [r for r in results if r["score"] >= float(min_score)]
                return json.dumps({"results": results})
            if tool_name == "libravdb_status":
                resp = self._channel._call("Status", pb.MemoryStatusRequest())
                return json.dumps({"ok": resp.ok, "message": resp.message})
            return json.dumps({"error": f"Unknown tool: {tool_name}"})
        except Exception as exc:
            return json.dumps({"error": str(exc)})

    def _result_to_dict(self, r) -> dict:
        return {"id": r.id, "score": r.score, "text": r.text}

    # TODO: implement
    def on_turn_start(self, turn: Any, message: Any, **kwargs) -> None:
        logger.debug("LibraVDB on_turn_start: turn=%s", turn)

    # TODO: implement
    def on_pre_compress(self, messages: List[Dict[str, Any]]) -> str:
        return ""

    # TODO: implement
    def on_memory_write(self, action: str, target: str, content: str, metadata: Any = None) -> None:
        logger.debug("LibraVDB on_memory_write: action=%s target=%s", action, target)

    # TODO: implement
    def on_delegation(self, task: Any, result: Any, **kwargs) -> None:
        logger.debug("LibraVDB on_delegation: task=%s", task)

    # TODO: implement
    def queue_prefetch(self, query: str, session_id: str = "") -> None:
        logger.debug("LibraVDB queue_prefetch: query=%s session_id=%s", query, session_id)

    def on_session_end(self, messages: List[Dict[str, Any]]) -> None:
        if not self._channel:
            return
        try:
            self._channel._call(
                "SessionLifecycleHint",
                pb.SessionLifecycleHintRequest(
                    hook="session_end",
                    session_id=self._session_id,
                    session_key=self._session_key,
                    message_count=len(messages),
                ),
            )
        except Exception as exc:
            logger.debug("LibraVDB session_end failed: %s", exc)

    def shutdown(self) -> None:
        if self._channel:
            self._channel.close()
            self._channel = None

    def get_config_schema(self) -> List[Dict[str, Any]]:
        return [
            {
                "key": "endpoint",
                "description": "LibraVDB gRPC endpoint (`auto`, `unix:/path`, or `tcp:host:port`)",
                "default": "auto",
            },
        ]

    def save_config(self, values: Dict[str, Any], hermes_home: str) -> None:
        import json
        home = Path(hermes_home)
        path = home / "libravdb.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(values, indent=2) + "\n")

    def _format_prefetch(self, resp) -> str:
        if not resp.results:
            return ""
        lines = ["Relevant context from LibraVDB:"]
        for r in resp.results:
            snippet = r.text[:120] + "..." if len(r.text) > 120 else r.text
            lines.append(f"- [score {r.score:.2f}] {snippet}")
        return "\n".join(lines)


def register(ctx) -> None:
    ctx.register_memory_provider(LibraVDBMemoryProvider())


# ── Ingest Queue ──────────────────────────────────────────────────────────────


class IngestQueue:
    """
    Fire-and-forget async ingest queue with chunking, retry, and back-pressure.

    Ported from openclaw-memory-libravdb ingest-queue.ts.
    Daemon handles compaction and summarization — this class handles the RPC
    layer with proper retry/backoff and burst feedback from IngestFeedback.
    """

    def __init__(
        self,
        channel: "_GrpcChannel",
        user_id: str | None,
        session_id: str,
        chunk_tokens: int = _INGEST_CHUNK_TOKENS,
        retry_base_delay_ms: int = _INGEST_RETRY_BASE_DELAY_MS,
        max_retries: int = _INGEST_MAX_RETRIES,
    ):
        self._channel = channel
        self.user_id = user_id or ""
        self._session_id = session_id
        self._chunk_tokens = chunk_tokens
        self._retry_base_delay_ms = retry_base_delay_ms
        self._max_retries = max_retries

    def _split_into_chunks(self, text: str, max_tokens: int) -> list[dict]:
        """Split text at sentence/word boundaries to stay within max_tokens."""
        max_chars = max_tokens * 4
        chunks = []
        offset = 0

        while offset < len(text):
            if offset + max_chars >= len(text):
                chunk_text = text[offset:]
            else:
                chunk_text = text[offset:offset + max_chars]
                # Walk back up to 256 chars for a sentence/word boundary
                boundary = -1
                for search_offset in range(1, min(256, len(chunk_text))):
                    pos = len(chunk_text) - search_offset
                    if pos < 0:
                        break
                    c = chunk_text[pos]
                    if c == "\n" and (pos + 1 < len(chunk_text) and chunk_text[pos + 1] == "\n"):
                        boundary = pos
                        break
                    if boundary < 0 and c == "\n":
                        boundary = pos
                    if boundary < 0 and c == " ":
                        boundary = pos
                if boundary < 0:
                    boundary = len(chunk_text) - 1
                chunk_text = chunk_text[:boundary + 1]

            if chunk_text.strip():
                chunks.append({"text": chunk_text, "ordinal": len(chunks)})
            offset += len(chunk_text) if chunk_text else max_chars

        return chunks

    def _with_retry(self, fn, label: str = ""):
        """Exponential backoff with full jitter, up to max_retries attempts."""
        for attempt in range(self._max_retries + 1):
            try:
                return fn()
            except Exception as exc:
                if attempt < self._max_retries:
                    cap = self._retry_base_delay_ms * (2 ** attempt)
                    delay = random.random() * cap
                    logger.debug(
                        "LibraVDB ingest %s attempt %d failed: %s — retry in %.1fms",
                        label, attempt, exc, delay,
                    )
                    time.sleep(delay / 1000)
                else:
                    logger.debug("LibraVDB ingest %s exhausted retries: %s", label, exc)
                    raise

    def enqueue(self, text: str, role: str = "user") -> None:
        """Ingest text as one or more chunks via IngestMessageKernel."""
        if not text.strip() or not self._channel:
            return

        chunks = self._split_into_chunks(text, self._chunk_tokens)
        if not chunks:
            return

        logger.debug(
            "LibraVDB enqueue: session_id=%s role=%s text_len=%d chunk_count=%d chunk_tokens=%d",
            self._session_id, role, len(text), len(chunks), self._chunk_tokens,
        )

        for i, chunk in enumerate(chunks):
            def ingest_chunk(chunk_text=chunk["text"]):
                return self._channel._call(
                    "IngestMessageKernel",
                    pb.IngestMessageKernelRequest(
                        session_id=self._session_id,
                        session_key=self._session_id,
                        user_id=self.user_id,
                        message=pb.KernelMessage(role=role, content=chunk_text),
                        is_heartbeat=False,
                    ),
                )

            try:
                resp = self._with_retry(ingest_chunk, label=f"chunk-{chunk['ordinal']}")

                if resp and hasattr(resp, "feedback") and resp.feedback:
                    fb = resp.feedback
                    logger.debug(
                        "LibraVDB ingest feedback: chunk=%d nodes_accepted=%d nodes_rejected=%d "
                        "tokens_ingested=%d token_burst_limit=%d accept_more=%s retry_after_ms=%d",
                        chunk["ordinal"], fb.nodes_accepted, fb.nodes_rejected,
                        fb.tokens_ingested, fb.token_burst_limit,
                        fb.accept_more, fb.retry_after_ms,
                    )
                    # Handle burst feedback — reduce chunk size on next iteration if needed
                    if (
                        fb.nodes_accepted == 0
                        and fb.token_burst_limit > 0
                        and fb.token_burst_limit < self._chunk_tokens
                    ):
                        logger.debug(
                            "LibraVDB burst limit: token_burst_limit=%d < chunk_tokens=%d — reducing",
                            fb.token_burst_limit, self._chunk_tokens,
                        )
                        self._chunk_tokens = fb.token_burst_limit

                    # Back-pressure: wait if daemon says to pause
                    if not fb.accept_more and i < len(chunks) - 1:
                        wait_ms = fb.retry_after_ms or 1000
                        logger.debug("LibraVDB back-pressure: accept_more=false — waiting %dms", wait_ms)
                        time.sleep(wait_ms / 1000)
                        time.sleep(wait_ms / 1000)

            except Exception as exc:
                logger.debug("LibraVDB ingest chunk %d failed: %s", chunk["ordinal"], exc)

    def flush(self) -> None:
        """Drain remaining work — called on shutdown."""
        pass  # daemon handles finalization via SessionLifecycleHint