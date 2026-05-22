from __future__ import annotations

import os
import hmac
import hashlib
import threading
import logging
from pathlib import Path
from typing import Any, Dict, List

import grpc
from libravdb.ipc.v1 import rpc_pb2 as pb
from libravdb.ipc.v1 import rpc_pb2_grpc as services

logger = logging.getLogger(__name__)

DEFAULT_TOP_K = 8
DEFAULT_MIN_SCORE = 0.35


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
        if method_name == "Health":
            return
        try:
            if hasattr(resp, 'initial_metadata'):
                for key, value in resp.initial_metadata():
                    if key == "x-libravdb-nonce":
                        self._nonce_state.update_nonce(value)
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
        self._user_id: str | None = None
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

    def initialize(self, session_id: str, **kwargs) -> None:
        self._session_id = session_id
        self._session_key = session_id
        self._user_id = str(kwargs.get("user_id") or "").strip() or None
        self._writes_enabled = str(kwargs.get("agent_context") or "primary") == "primary"
        self._startup_error = None
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
            req = pb.SearchTextRequest(
                collection="session",
                text=query,
                k=self._config.get("topK", DEFAULT_TOP_K),
            )
            resp = self._channel._call("SearchText", req)
            return self._format_prefetch(resp)
        except Exception as exc:
            logger.debug("LibraVDB prefetch failed: %s", exc)
            return ""

    def sync_turn(self, user_content: str, assistant_content: str, *, session_id: str = "") -> None:
        if not self._channel or not self._writes_enabled:
            return
        try:
            session = session_id or self._session_id
            for role, content in [("user", user_content), ("assistant", assistant_content)]:
                req = pb.IngestMessageKernelRequest(
                    session_id=session,
                    session_key=session,
                    user_id=self._user_id or "",
                    message=pb.KernelMessage(role=role, content=content),
                )
                self._channel._call("IngestMessageKernel", req)
        except Exception as exc:
            logger.debug("LibraVDB sync_turn failed: %s", exc)

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
                req = pb.SearchTextRequest(
                    collection="session",
                    text=query,
                    k=args.get("limit", self._config.get("topK", DEFAULT_TOP_K)),
                )
                resp = self._channel._call("SearchText", req)
                return json.dumps({"results": [self._result_to_dict(r) for r in resp.results]})
            if tool_name == "libravdb_status":
                resp = self._channel._call("Status", pb.MemoryStatusRequest())
                return json.dumps({"ok": resp.ok, "message": resp.message})
            return json.dumps({"error": f"Unknown tool: {tool_name}"})
        except Exception as exc:
            return json.dumps({"error": str(exc)})

    def _result_to_dict(self, r) -> dict:
        return {"id": r.id, "score": r.score, "text": r.text}

    def on_session_switch(
        self,
        new_session_id: str,
        *,
        parent_session_id: str = "",
        reset: bool = False,
        **kwargs,
    ) -> None:
        self._session_id = new_session_id
        self._session_key = new_session_id

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
            {
                "key": "userId",
                "description": "Optional stable durable-memory namespace override",
                "default": "",
            },
            {
                "key": "topK",
                "description": "Default number of recalled memory hits",
                "default": str(DEFAULT_TOP_K),
            },
            {
                "key": "minScore",
                "description": "Minimum semantic score for prefetched/tool search results",
                "default": str(DEFAULT_MIN_SCORE),
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