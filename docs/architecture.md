# Architecture

## Overview

The libravdb memory provider is a Hermes Agent plugin that connects to the `libravdbd` daemon via native gRPC. It implements both the MemoryProvider and ContextEngine interfaces, delivering dynamic per-turn memory retrieval, predictive context injection, and cross-session recall alongside Hermes' built-in MEMORY.md/USER.md.

```
┌─────────────────────────────────────────────────────────────────────┐
│                         Hermes Agent                                 │
│                                                                      │
│   MemoryManager ──► LibraVDBMemoryProvider                           │
│                       │                                              │
│                       ├── initialize(session_id, hermes_home)        │
│                       ├── system_prompt_block() → static provider    │
│                       │      info injected once per session          │
│                       ├── prefetch(query) → per-turn semantic search │
│                       ├── queue_prefetch(query) → background warm    │
│                       ├── sync_turn(user, assistant) → async ingest  │
│                       ├── on_memory_write(action, target, content)   │
│                       │      → mirror curated memory to daemon       │
│                       ├── on_delegation(task, result)                │
│                       │      → persist subagent findings             │
│                       ├── on_pre_compress(messages)                  │
│                       │      → save context before compression       │
│                       ├── on_session_end(messages) → lifecycle hint  │
│                       ├── get_tool_schemas() → [libravdb_search,     │
│                       │      libravdb_status]                        │
│                       ├── handle_tool_call(name, args)               │
│                       └── shutdown()                                 │
│                                                                      │
│   ContextEngine ──► _LibraVDBContextEngine                           │
│                       │                                              │
│                       ├── bootstrap() → BootstrapSessionKernel       │
│                       ├── assemble(context) → daemon context         │
│                       │      + exact recall + predictive cache       │
│                       ├── afterTurn(turn) → cache predictions        │
│                       ├── compress(messages) → CompactSession RPC    │
│                       ├── should_compress(tokens) → threshold check  │
│                       └── update_from_response(usage) → track tokens │
│                                                                      │
│   Hooks ──► on_session_start / on_session_end /                      │
│             on_session_finalize / on_session_reset                   │
│                                                                      │
│   CLI ──► hermes libravdb <status|health|search|flush|               │
│             export|journal|index|dream-promote|markdown-ingest>      │
└─────────────────────────────────────────────────────────────────────┘
                              │
                              │ gRPC (Unix socket or TCP/TLS)
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│                        libravdbd daemon                              │
│                                                                      │
│   ┌────────────────┐  ┌──────────────────┐  ┌───────────────────┐  │
│   │ Health         │  │ SearchText       │  │ IngestMessage-    │  │
│   │ (nonce         │  │ SearchText-      │  │ Kernel            │  │
│   │  bootstrap)    │  │ Collections      │  │                   │  │
│   └────────────────┘  └──────────────────┘  └───────────────────┘  │
│                                                                      │
│   ┌────────────────┐  ┌──────────────────┐  ┌───────────────────┐  │
│   │ Assemble-      │  │ AfterTurn-       │  │ CompactSession    │  │
│   │ ContextInternal│  │ Kernel           │  │                   │  │
│   └────────────────┘  └──────────────────┘  └───────────────────┘  │
│                                                                      │
│   ┌────────────────┐  ┌──────────────────┐  ┌───────────────────┐  │
│   │ Bootstrap-     │  │ SessionLifecycle │  │ MarkdownIngest    │  │
│   │ SessionKernel  │  │ Hint             │  │                   │  │
│   └────────────────┘  └──────────────────┘  └───────────────────┘  │
│                                                                      │
│   ┌──────────────────────────────────────────────────────────────┐  │
│   │                    Vector Store                               │  │
│   │   session:<id>  │  user:<id>  │  global                       │  │
│   └──────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 1. Plugin Discovery and Loading

Hermes discovers memory provider plugins via the directory convention:

```
~/.hermes/plugins/memory/libravdb/
├── __init__.py    # register(ctx) entry point
└── plugin.yaml    # name, version, hooks declaration
```

When installed via pip, the `hermes_agent.plugins` entry point in `pyproject.toml` registers the plugin:

```toml
[project.entry-points."hermes_agent.plugins"]
libravdb-memory = "hermes_memory_libravdb:register"
```

Hermes calls `register(ctx)` once during startup. Our single authoritative registration path in `__init__.py` wires everything:

```
Hermes startup
  │
  ├── discovers hermes_agent.plugins entry point
  ├── calls register(ctx)
  │     ├── ctx.register_memory_provider(LibraVDBMemoryProvider())
  │     ├── ctx.register_context_engine("libravdb", _build_context_engine)
  │     ├── ctx.register_hook("on_session_start", _on_session_start)
  │     ├── ctx.register_hook("on_session_end", _on_session_end)
  │     ├── ctx.register_hook("on_session_finalize", _on_session_finalize)
  │     └── ctx.register_hook("on_session_reset", _on_session_reset)
  └── plugin is now the active memory provider + context engine
```

The user activates it via `memory.provider: "libravdb"` in `config.yaml` and optionally `context.engine: "libravdb"`.

---

## 2. Plugin Lifecycle

### `initialize(session_id, **kwargs)`

Called once at agent startup. Sets up the gRPC channel, resolves identity, loads config, and optionally starts markdown ingestion.

```python
def initialize(self, session_id: str, **kwargs) -> None:
    self._session_id = session_id
    self._session_key = session_id
    self._resolved_identity = resolve_identity(...)
    self._writes_enabled = str(kwargs.get("agent_context") or "primary") == "primary"
    self._channel = _GrpcChannel(...)
    # Optionally: start MarkdownIngestionHandle if markdownIngestionEnabled
```

Key `kwargs`:
- `hermes_home` — the active Hermes configuration directory. Use for all storage paths.
- `platform` — "cli", "telegram", "discord", "cron", etc.
- `agent_context` — "primary", "subagent", "cron", or "flush". Writes are skipped for non-primary.
- `agent_identity` — profile name for per-profile scoping.
- `user_id` — platform user identifier (gateway sessions).

### `system_prompt_block()`

Called once during system prompt assembly. Returns static provider info injected into the prompt. This is intentionally static per Hermes' prompt caching model — dynamic per-turn context flows through `prefetch` and the context engine.

### `prefetch(query)` — Per-Turn Dynamic

Called before each API call. Runs a live semantic search against session, user, and global collections:

```
prefetch(query)
  └── resolve_search_scopes() → [session:..., user:..., global]
  └── SearchTextCollections(collections, text=query, k=topK)
  └── Filter by minScore (4-level fallback chain)
  └── Return formatted context string → injected into prompt
```

### `sync_turn(user, assistant)` — Non-Blocking

Called after each completed turn. Uses `IngestQueue` with daemon thread for fire-and-forget ingestion:

```
sync_turn(user_content, assistant_content)
  └── IngestQueue enqueues both messages
  └── Thread splits into chunks, retries with jitter
  └── IngestMessageKernel RPC → daemon indexes turn
```

This is non-blocking per Hermes' threading contract. The daemon handles compaction and summarization server-side.

### `queue_prefetch(query)` — Background Warming

Called after each turn. Runs a background `SearchText`/`SearchTextCollections` to populate daemon-side LRU/vector caches so the next synchronous `prefetch` returns with minimal latency.

### `on_memory_write(action, target, content)` — Curated Memory Mirroring

Called when the agent writes to Hermes built-in MEMORY.md or USER.md. Mirrors the curated content into LibraVDB as durable memory entries. Skips non-durable targets and low-signal operations.

### `on_delegation(task, result)` — Subagent Persistence

Called after each `delegate_task` child completes. Emits a `SessionLifecycleHint(hook="delegation")` and ingests the task description and result summary for cross-session recall of subagent findings.

### `on_pre_compress(messages)` — Pre-Discard Persistence

Called before Hermes compresses context. Extracts the last few substantive messages and ingests them into the daemon to preserve context continuity across compressions. Returns a brief note for the system prompt. The actual persistence is fire-and-forget.

### `on_session_end(messages)`

Emits a `SessionLifecycleHint(hook="session_end")` to the daemon so it can finalize the session state.

### `shutdown()`

Stops markdown ingestion, flushes the ingest queue, and closes the gRPC channel.

---

## 3. Context Engine

LibraVDB registers a context engine via `ctx.register_context_engine("libravdb", ...)`. It implements the Hermes `ContextEngine` ABC with required attributes (`last_prompt_tokens`, `last_completion_tokens`, `last_total_tokens`, `threshold_tokens`, `context_length`, `compression_count`) and all required methods.

### Per-Turn Assembly Flow

```
assemble(context)
  ├── 1. AssembleContextInternal RPC → daemon's live system_prompt_addition
  │      (clipped to effective token budget if oversized)
  ├── 2. Exact recall augmentation (if crossSessionRecall enabled)
  │      ├── extract_exact_recall_tokens(prompt) → 3 regex patterns
  │      ├── SearchTextCollections per token → user + global collections
  │      └── format as <exact_recalled_memory> block (up to 10% of budget)
  ├── 3. Predictive context from cached afterTurn predictions
  │      (remaining token budget after daemon + recall)
  └── Return combined result ≤ token_budget - headroom - current_turn_reserve
```

### afterTurn → Predictive Context

```
afterTurn(turn)
  └── AfterTurnKernel RPC with current messages
  └── Cache predictions for next assemble() call
```

### Compaction

```
should_compress(prompt_tokens) → True if context ≥ threshold_tokens
compress(messages, current_tokens, focus_topic)
  └── CompactSession RPC → daemon-side compaction
  └── Returns messages unchanged (benefit flows through next assemble)
```

---

## 4. Collection Model

LibraVDB uses three collection scopes matched to Hermes' memory model:

| Scope | Collection | Purpose |
|---|---|---|
| Session | `session:<id>` | Ephemeral turn data, active conversation context |
| User | `user:<id>` | Durable cross-session memory, curated facts |
| Global | `global` | Shared reference knowledge |

Search and prefetch query across all three when `crossSessionRecall` is enabled (default). When disabled, only the session collection is searched.

Durable namespace resolution follows a priority chain: explicit `userId` → `session-key:` prefix → `agent-id:` prefix → fallback → `"default"`.

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
- Multiple profiles sharing one daemon are scoped correctly by userId

---

## 6. gRPC Channel

The `_GrpcChannel` class manages the connection to `libravdbd`:

```
endpoint resolution:
  config "auto"  →  LIBRAVDB_GRPC_ENDPOINT env var  →  probe standard paths
  macOS/Linux default: unix:$HOME/.libravdbd/run/libravdb.sock
  Homebrew: unix:/opt/homebrew/var/libravdbd/run/libravdb.sock

channel creation:
  unix:...  →  grpc.insecure_channel(target)
  tcp:...   →  grpc.insecure_channel(target) if host is loopback
  otherwise →  grpc.ssl_channel_credentials() with optional mTLS

nonce auth:
  _NonceState holds secret + current nonce
  Auto-bootstraps via Health RPC on first authenticated call
  HMAC-SHA256 signing with x-libravdb-auth + x-libravdb-nonce metadata
  _rpc_lock serializes all non-Health RPCs through the mutex
  Auto-recovers: on RPC failure, clears nonce and re-bootstraps
```

The channel is created lazily on first RPC call. The `_rpc_lock` ensures thread-safe access.

---

## 7. CLI

The plugin registers CLI commands via `cli.py` with `register_cli(subparser)`:

```bash
hermes libravdb status            # daemon health, collection counts
hermes libravdb status --index --force  # rebuild index then status
hermes libravdb health            # quick liveness check
hermes libravdb search "query"    # semantic search from CLI
hermes libravdb index --user-id X --force  # standalone index rebuild
hermes libravdb flush --user-id X # wipe user namespace
hermes libravdb export --user-id X  # NDJSON memory export
hermes libravdb journal --session-id X  # lifecycle journal
hermes libravdb markdown-ingest   # trigger markdown directory scan
```

CLI commands only appear when `libravdb` is the active memory provider.

---

## 8. Markdown Ingestion (Optional)

When `markdownIngestionEnabled` is set in config, the plugin continuously monitors configured directory roots for markdown files. Files are parsed, hashed (FNV-1a 64-bit), chunked, and ingested as searchable documents. The system uses polling-based directory watchers, debounce scheduling, and WAL capacity back-pressure gates to integrate with the daemon without overwhelming it.
