# Architecture

## Overview

The libravdb memory provider is a Python plugin that connects Hermes Agent to the `libravdbd` daemon via native gRPC. It handles persistent memory storage, semantic recall, and cross-session context for the agent.

```
┌─────────────────────────────────────────────────────────────────┐
│                         Hermes Agent                            │
│                                                                 │
│   MemoryManager ──► LibraVDBMemoryProvider                      │
│                       │                                         │
│                       ├── initialize(session_id, hermes_home)   │
│                       ├── get_tool_schemas() → [libravdb_search,│
│                       │                              libravdb_status]
│                       ├── prefetch(query) → recall context      │
│                       ├── sync_turn(user, assistant) → ingest   │
│                       ├── handle_tool_call(name, args)         │
│                       ├── on_session_end(messages)              │
│                       ├── on_session_switch(new_session_id)     │
│                       └── shutdown()                             │
│                                                                 │
│   CLI ──► hermes libravdb <status|health|search>               │
└─────────────────────────────────────────────────────────────────┘
                              │
                              │ gRPC (Unix socket or TCP)
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                        libravdbd daemon                         │
│                                                                 │
│   ┌─────────────┐  ┌─────────────┐  ┌──────────────────────┐   │
│   │  Health RPC │  │ SearchText  │  │ IngestMessageKernel  │   │
│   │  (nonce     │  │ (semantic   │  │  (turn ingestion,    │   │
│   │   bootstrap)│  │  recall)    │  │   session lifecycle) │   │
│   └─────────────┘  └─────────────┘  └──────────────────────┘   │
│                                                                 │
│   ┌─────────────────────────────────────────────────────────┐  │
│   │                    Vector Store                         │  │
│   │   session:<id>  │  user:<id>  │  global                 │  │
│   └─────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
```

---

## 1. Plugin Discovery and Loading

Hermes discovers memory provider plugins via the directory convention:

```
~/.hermes/plugins/memory/libravdb/
├── __init__.py    # register(ctx) entry point
└── plugin.yaml    # name, version, hooks declaration
```

When the plugin is installed via pip, the `hermes_agent.plugins` entry point in `pyproject.toml` registers `libravdb:register`. Hermes calls `register(ctx)` once during startup, which calls `ctx.register_memory_provider(LibraVDBMemoryProvider())`.

For manual installation, copy the plugin directory to `~/.hermes/plugins/memory/libravdb/` and set `memory.provider: "libravdb"` in `~/.hermes/config.yaml`.

```
Hermes startup
  │
  ├── discovers plugins/memory/libravdb/__init__.py
  ├── calls register(ctx)
  │     └── ctx.register_memory_provider(LibraVDBMemoryProvider())
  └── plugin is now the active memory provider
```

---

## 2. Plugin Lifecycle

### `initialize(session_id, **kwargs)`

Called once at agent startup. Sets up the gRPC channel and session context.

```python
def initialize(self, session_id: str, **kwargs) -> None:
    self._session_id = session_id
    self._session_key = session_id
    self._user_id = str(kwargs.get("user_id") or "").strip() or None
    self._writes_enabled = str(kwargs.get("agent_context") or "primary") == "primary"
    self._startup_error = None
    self._channel = _GrpcChannel(
        endpoint=self._endpoint,
        secret=self._secret,
    )
```

Key `kwargs`:
- `hermes_home` — the active Hermes configuration directory path. Use this for all storage, not hardcoded paths.
- `platform` — "cli", "telegram", "discord", "cron", etc.
- `agent_context` — "primary", "subagent", "cron", or "flush". Writes are skipped for non-primary contexts.
- `agent_identity` — profile name for per-profile scoping.
- `user_id` — platform user identifier (gateway sessions).

### `get_tool_schemas()`

Returns tool schemas for the two tools the plugin exposes: `libravdb_search` and `libravdb_status`. Hermes injects these into the model's tool list.

```python
def get_tool_schemas(self) -> List[Dict[str, Any]]:
    return [
        {
            "name": "libravdb_search",
            "description": "Search LibraVDB long-term memory...",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Semantic search query."},
                    "limit": {"type": "integer"},
                    "min_score": {"type": "number"},
                },
                "required": ["query"],
            },
        },
        {
            "name": "libravdb_status",
            "description": "Check whether the LibraVDB daemon is reachable...",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    ]
```

### `handle_tool_call(tool_name, args, **kwargs)`

Dispatches to the appropriate tool handler.

```python
def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
    if tool_name == "libravdb_search":
        # calls SearchText RPC
    elif tool_name == "libravdb_status":
        # calls Status RPC
```

Returns a JSON string with the result or an error.

### `system_prompt_block()`

Returns static text injected into the system prompt. If the plugin failed to initialize, returns a degraded notice so Hermes still functions with built-in MEMORY.md and USER.md.

---

## 3. Threading Contract and Non-Blocking Sync

`sync_turn()` is called after every completed turn. **It must not block** — if the gRPC call is slow, the entire Hermes event loop stalls.

**Current implementation:** `sync_turn()` iterates over the user and assistant messages and calls `IngestMessageKernel` synchronously via `_channel._call()`. This blocks. The contract requires this to be non-blocking.

**Required pattern** (per Hermes threading contract):
```python
def sync_turn(self, user_content: str, assistant_content: str, *, session_id: str = "") -> None:
    if not self._channel or not self._writes_enabled:
        return

    def _fire_and_forget():
        try:
            for role, content in [("user", user_content), ("assistant", assistant_content)]:
                req = pb.IngestMessageKernelRequest(
                    session_id=session_id or self._session_id,
                    session_key=self._session_key,
                    user_id=self._user_id or "",
                    message=pb.KernelMessage(role=role, content=content),
                )
                self._channel._call("IngestMessageKernel", req)
        except Exception as exc:
            logger.debug("LibraVDB sync_turn failed: %s", exc)

    thread = threading.Thread(target=_fire_and_forget, daemon=True)
    thread.start()
```

The `_GrpcChannel._call()` method uses a threading lock (`_rpc_lock`) internally to ensure thread-safe access to the gRPC stub. All RPCs are serialized through this lock.

---

## 4. How Memories Are Stored and Queried

### Storage (write path — `sync_turn`)

```
sync_turn(user_content, assistant_content)
  └── _fire_and_forget() thread
        └── _GrpcChannel._call("IngestMessageKernel", req)
              └── gRPC unary call to libravdbd
                    └── Stores in session:<sessionId> collection
```

Each turn is stored as a `KernelMessage` with `role` (user/assistant) and `content`. Messages are scoped to the current session.

### Query (read path — `prefetch`)

```
prefetch(query, session_id)
  └── _GrpcChannel._call("SearchText", req)
        ├── collection="session"  (current session only)
        ├── text=query
        └── k=topK (from config, default 8)
```

The `prefetch` method searches the current session collection only. It returns formatted recall context as a string for injection into the conversation context.

### Tool-based search (`handle_tool_call` → `libravdb_search`)

```
handle_tool_call("libravdb_search", args)
  └── SearchText(collection="session", text=query, k=limit)
        └── Returns JSON with results: [{id, score, text}, ...]
```

Search currently queries only the `session` collection. Cross-session and user-level recall is available via the daemon's `searchTextCollections` RPC, which queries multiple collections at once.

---

## 5. Profile Isolation

All plugin storage is relative to `hermes_home` passed via `initialize(**kwargs)`. The plugin never hardcodes `~/.hermes`.

```
hermes_home = kwargs.get("hermes_home")  # e.g. ~/.hermes or ~/.hermes.coder
config_path = Path(hermes_home) / "libravdb.json"
```

This means:
- Each Hermes profile gets its own `libravdb.json` config
- Config and data are isolated per profile
- If the same `libravdbd` daemon is used across profiles, the userId in `libravdb.json` scopes the durable memory correctly

---

## 6. gRPC Channel

The `_GrpcChannel` class manages the connection to `libravdbd`:

```
endpoint resolution:
  config "auto"  →  LIBRAVDB_GRPC_ENDPOINT env var  →  unix:$HOME/.libravdbd/run/libravdb.sock

channel creation:
  unix:...  →  grpc.insecure_channel(target)
  tcp:...   →  grpc.insecure_channel(target) if host is loopback
  otherwise →  grpc.ssl_channel_credentials()

nonce auth:
  _NonceState holds secret + current nonce
  build_metadata(method) → [("x-libravdb-auth", hmac-sha256), ("x-libravdb-nonce", nonce)]
  _rpc_lock serializes all non-Health RPCs through the mutex
```

The channel is created lazily on first RPC call via `_get_stub()`. The `_rpc_lock` ensures thread-safe access to the shared gRPC stub.

---

## 7. CLI

The plugin registers CLI commands via `cli.py` with `register_cli(subparser)`:

```bash
hermes libravdb status     # daemon health, turn/memory counts
hermes libravdb health    # quick liveness check
hermes libravdb search "query"  # semantic search from CLI
```

CLI commands only appear when `libravdb` is the active memory provider. The CLI is discovered via the memory plugin CLI convention — no entry point needed.