# Security

## Data Storage

### Plugin Config — `$HERMES_HOME/libravdb.json`

The plugin stores configuration in:

```
$HERMES_HOME/libravdb.json
```

This file is written by `save_config(values, hermes_home)` and contains only non-secret settings:

- `endpoint` — gRPC endpoint string
- `userId` — durable memory namespace
- `topK` — recall hit count
- `minScore` — semantic score threshold

**No secrets are written to this file.** The HMAC auth secret is never stored in the config — it is read exclusively from environment variables at runtime.

### Daemon Data — `$HERMES_HOME/.libravdbd/`

The `libravdbd` daemon stores its own data separately from the plugin. The default data directory is:

```
$HERMES_HOME/.libravdbd/
```

This path is controlled by the daemon, not the plugin. See the [libravdbd documentation](https://github.com/xDarkicex/libravdbd) for the full data layout and encryption settings.

### Permissions

The `$HERMES_HOME` directory and its contents should be readable only by the user running Hermes:

```
drwx------  hermes_user  hermes_user  hermes_home/
```

The `libravdb.json` config file should have `0600` permissions (read/write for owner only), as it may contain user-scoped identifiers.

The daemon's data directory (`$HERMES_HOME/.libravdbd/`) should be similarly restricted. If the daemon runs as a different user, ensure the plugin's `$HERMES_HOME` path is accessible to that user, or configure the daemon's data path separately.

---

## Credentials and Tokens

### Auth Secret — Environment Variables Only

The HMAC authentication secret is **never written to disk** by the plugin. It is loaded exclusively from environment variables at runtime:

```python
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
```

| Env Variable | Purpose |
|---|---|
| `LIBRAVDB_AUTH_SECRET` | Direct HMAC secret string |
| `LIBRAVDB_AUTH_SECRET_FILE` | Path to file containing the secret |

The secret is held in memory in `_NonceState._secret` and used to sign RPCs. It is never serialized to disk, logged, or included in error messages.

### Endpoint Configuration

The gRPC endpoint URL is set via:

| Config | Env Variable | Notes |
|---|---|---|
| `endpoint` in `libravdb.json` | `LIBRAVDB_GRPC_ENDPOINT` | Env var takes precedence |

If using TCP endpoints, ensure the network path is trusted — gRPC traffic is not encrypted by default unless TLS is configured on the daemon.

---

## Encryption

### In Transit

The plugin communicates with `libravdbd` over gRPC. The channel type determines encryption:

| Endpoint | Channel Type | Encrypted |
|---|---|---|
| `unix:/path/to/socket` | `grpc.insecure_channel` | No — Unix socket is trusted locally |
| `tcp:127.0.0.1:port` / `tcp:localhost:port` | `grpc.insecure_channel` | No — loopback is trusted locally |
| `tcp:remote-host:port` | `grpc.ssl_channel_credentials` | Yes — TLS to remote endpoint |

**Remote TCP connections use TLS.** Configure TLS on the daemon for remote deployments. The plugin uses `grpc.ssl_channel_credentials()` for non-loopback TCP endpoints by default.

### At Rest

Encryption at rest is a **daemon concern**, not a plugin concern. The plugin does not manage or configure the daemon's storage encryption. See the [libravdbd security documentation](https://github.com/xDarkicex/libravdbd) for how data is encrypted at rest.

The plugin's `libravdb.json` config file contains no sensitive data — only connection preferences and non-secret tuning parameters.

---

## Profile Isolation

Each Hermes profile has its own `$HERMES_HOME` directory, set via the `hermes_home` kwarg in `initialize(**kwargs)`:

```python
def initialize(self, session_id: str, **kwargs) -> None:
    hermes_home = kwargs.get("hermes_home")  # profile-scoped path
    config_path = Path(hermes_home) / "libravdb.json"
```

This means:

- **Config isolation** — Profile A's `libravdb.json` is `$HERMES_HOME/.hermes/libravdb.json`, Profile B's is `$HERMES_HOME/.hermes.coder/libravdb.json`. They do not share config.
- **userId isolation** — Each profile can set its own `userId` in its config, scoping durable memory to that identity.
- **No cross-profile reads** — The plugin only reads from the `$HERMES_HOME` it was initialized with. It cannot access another profile's config or data.

If the same `libravdbd` daemon is used by multiple profiles, the `userId` field in each profile's `libravdb.json` ensures durable memories remain namespace-separated by user identity.

---

## Secrets That Are Never Saved

These values are intentionally **never written to disk** by `save_config()`:

| Field | Reason |
|---|---|
| `LIBRAVDB_AUTH_SECRET` | Used at runtime only, never persisted |
| `LIBRAVDB_AUTH_SECRET_FILE` | Path to secret, not the secret itself — also not persisted |
| `LIBRAVDB_GRPC_ENDPOINT` | If set via env var, it's not in the config file; if set in config, it's not sensitive |

`save_config(values, hermes_home)` only writes the four non-secret fields (`endpoint`, `userId`, `topK`, `minScore`) to `libravdb.json`. Any sensitive values are expected to be supplied via environment variables at runtime.

---

## Threat Model Summary

| Threat | Mitigation |
|---|---|
| Plugin config exposed | Config file contains only non-secret settings; no credentials written to disk |
| Auth secret in memory | Secret held only in `_NonceState._secret`, never logged or serialized |
| Cross-profile data leakage | Profile isolation via per-profile `$HERMES_HOME`; plugin cannot access other profiles' paths |
| Remote gRPC interception | TLS is used automatically for non-loopback TCP endpoints |
| Local socket access | Unix socket access controlled by filesystem permissions on `$HERMES_HOME` |
| Config file tampering | Config file should be owned by the Hermes user with `0600` permissions |