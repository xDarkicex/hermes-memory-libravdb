# Configuration

LibraVDB is configured through `~/.hermes/libravdb.json` (written by `hermes memory setup`) and environment variables. Environment variables take precedence over config file values.

---

## Config Fields

### endpoint

Controls how the plugin connects to the `libravdbd` daemon.

| | |
|---|---|
| **Config key** | `endpoint` |
| **Env variable** | `LIBRAVDB_GRPC_ENDPOINT` |
| **Default** | `auto` (probe standard socket paths) |
| **Choices** | `auto`, `unix:/path/to/socket`, `tcp:host:port` |

`auto` resolves to `unix:$HOME/.libravdbd/run/libravdb.sock` by default. Set this if your daemon runs on a non-default socket path or uses TCP.

```bash
# Unix socket (default on macOS/Linux)
export LIBRAVDB_GRPC_ENDPOINT="unix:/home/user/.libravdbd/run/libravdb.sock"

# TCP (for remote or non-default host)
export LIBRAVDB_GRPC_ENDPOINT="tcp:127.0.0.1:37421"
```

---

### userId

A stable identity for cross-session durable memory. When set, memories are stored under `user:<userId>` and persist across sessions. When empty, the plugin auto-derives an identity from the OS username and hostname.

| | |
|---|---|
| **Config key** | `userId` |
| **Env variable** | — (no env var for this field) |
| **Default** | `""` (auto-derived from OS user + hostname) |

```json
// ~/.hermes/libravdb.json
{
  "userId": "my-user-id"
}
```

---

### topK

Default number of recalled memory hits returned by search and prefetch operations.

| | |
|---|---|
| **Config key** | `topK` |
| **Env variable** | — |
| **Default** | `8` |

```json
// ~/.hermes/libravdb.json
{
  "topK": 12
}
```

---

### minScore

Minimum semantic score threshold for prefetched and tool search results. Results with a score below this threshold are filtered out.

| | |
|---|---|
| **Config key** | `minScore` |
| **Env variable** | — |
| **Default** | `0.35` |

```json
// ~/.hermes/libravdb.json
{
  "minScore": 0.4
}
```

---

## Environment Variables

These are read at runtime and do not appear in the config file schema.

### LIBRAVDB_GRPC_ENDPOINT

Overrides the `endpoint` config field. See [endpoint](#endpoint) above.

### LIBRAVDB_AUTH_SECRET

HMAC secret for authenticated RPCs to the daemon. When set, all non-Health RPCs are signed with HMAC-SHA256 using this secret and the current nonce.

| | |
|---|---|
| **Env variable** | `LIBRAVDB_AUTH_SECRET` |
| **Alternative** | `LIBRAVDB_AUTH_SECRET_FILE` (path to a file containing the secret) |

```bash
# Direct secret
export LIBRAVDB_AUTH_SECRET="your-hmac-secret-here"

# Or point to a file (secret is read from the file contents)
export LIBRAVDB_AUTH_SECRET_FILE="/path/to/secret.txt"
```

### HERMES_HOME

Sets the Hermes configuration directory path. All plugin config and data directories are relative to this path.

| | |
|---|---|
| **Env variable** | `HERMES_HOME` |
| **Default** | `~/.hermes` |

```bash
export HERMES_HOME="/home/user/.hermes"
```

---

## Config File Location

The plugin writes its config to `$HERMES_HOME/libravdb.json` via `save_config(values, hermes_home)`. This is not the same as `~/.hermes/config.yaml` — that file is Hermes's global config. The plugin stores its own settings in `libravdb.json`.

```python
# Where the plugin stores its config:
Path(hermes_home) / "libravdb.json"
```

---

## Minimal Config Example

```yaml
# ~/.hermes/config.yaml
memory:
  provider: "libravdb"
```

The plugin will use all defaults: `auto` endpoint discovery, auto-derived userId, `topK=8`, `minScore=0.35`.

---

## Full Config Example

```yaml
# ~/.hermes/config.yaml
memory:
  provider: "libravdb"
```

The provider selection is the only thing that lives in Hermes config.yaml. All other settings go in the provider's own config file:

```json
// ~/.hermes/libravdb.json
{
  "endpoint": "auto",
  "userId": "",
  "topK": 8,
  "minScore": 0.35
}
```

This file is written by `hermes memory setup` or can be edited directly. See the [full config reference](#config-fields) for the 60+ available tuning parameters.

With environment overrides:

```bash
export LIBRAVDB_GRPC_ENDPOINT="tcp:127.0.0.1:37421"
export LIBRAVDB_AUTH_SECRET="my-secret-key"
export HERMES_HOME="/home/user/.hermes"
```

---

## Runtime Verification

After changing configuration, verify the plugin is connected:

```bash
hermes libravdb status
```

If you see `ok: true`, the plugin is connected to the daemon. If you see a connection error, check that:
1. `libravdbd` is running
2. `LIBRAVDB_GRPC_ENDPOINT` is correct (or remove it for default socket discovery)
3. If using auth, `LIBRAVDB_AUTH_SECRET` matches the daemon's configured secret