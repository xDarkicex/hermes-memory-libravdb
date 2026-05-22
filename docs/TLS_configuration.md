# TLS Configuration

## Current Implementation

The plugin uses TLS automatically for **remote TCP endpoints** (non-loopback hosts). There is currently **no configuration for custom CA bundles, client certificates, or disabling TLS** — these features are not yet implemented.

```python
def _create_channel(self) -> grpc.Channel:
    is_unix = self._endpoint.startswith("unix:")
    target = self._endpoint[5:] if is_unix else self._endpoint.replace("tcp:", "")

    if is_unix:
        return grpc.insecure_channel(target)  # local socket, no TLS

    host = target.split(":")[0] if ":" in target else target
    if _is_loopback_host(host):
        return grpc.insecure_channel(target)  # loopback, no TLS

    return grpc.ssl_channel_credentials()  # remote → TLS with system CAs
```

| Endpoint type | Channel | TLS |
|---|---|---|
| `unix:/path/to/socket` | `insecure_channel` | No — local socket |
| `tcp:127.0.0.1:port` / `tcp:localhost:port` | `insecure_channel` | No — loopback |
| `tcp:remote-host:port` | `ssl_channel_credentials()` | Yes — TLS with system CAs |

---

## Why TLS Does Not Apply to Local Endpoints

Unix sockets and loopback addresses are trusted by definition — the connection never leaves the machine. TLS adds overhead and complexity that provides no security benefit for local communication, so the plugin uses insecure channels for these endpoints.

TLS is used only when the plugin connects to a **remote** TCP endpoint (any host other than `localhost`, `127.0.0.1`, or `::1`).

---

## Remote TLS Details

When connecting to a remote daemon over TCP, the plugin uses `grpc.ssl_channel_credentials()` which loads the **system's default CA certificate store** to verify the server's certificate.

This means:
- The server's certificate must be signed by a CA in the system's trusted store.
- Custom CA bundles are **not yet supported** — the plugin uses system CAs only.
- Client certificates (mTLS) are **not yet supported**.

---

## Planned TLS Configuration

The following are not yet implemented but are planned for future releases:

| Setting | Purpose | Controlled by |
|---|---|---|
| Custom CA bundle | Verify server cert with a custom CA | Env var: `LIBRAVDB_GRPC_TLS_CA` (future) |
| Client certificate | mTLS client auth | Env vars: `LIBRAVDB_GRPC_TLS_CLIENT_CERT`, `LIBRAVDB_GRPC_TLS_CLIENT_KEY` (future) |
| TLS mode | Force TLS / disable TLS / auto | Env var: `LIBRAVDB_GRPC_TLS_MODE` (future) |

When implemented, `tlsMode` will support:
- `auto` — default (insecure for loopback, TLS for remote)
- `tls` — always use TLS, fail if not available
- `insecure` — always use insecure (dev only, **never in production**)

---

## Warning: Do Not Disable TLS in Production

Setting the TLS mode to `insecure` for a remote endpoint is **only for local development**. An insecure remote connection exposes all memory data to interception on the network. Always use TLS for remote deployments.

---

## For Now — Local Development

For local development with a remote-ish setup (e.g. daemon on a different machine on the same trusted network), ensure both machines are on a trusted network and the daemon is configured with a valid certificate signed by a known CA.

For production remote deployments, ensure the daemon uses a publicly trusted certificate (Let's Encrypt, etc.) and the plugin's system CA store is up to date.