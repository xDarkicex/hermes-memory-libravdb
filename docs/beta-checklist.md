# Beta Tester Checklist

A 5-minute verification path for LibraVDB memory provider testers.

## 1. Install

```bash
pip install hermes-memory-libravdb
```

The daemon must be running before the plugin connects:

```bash
# macOS
brew tap xDarkicex/homebrew-libravdbd
brew install libravdbd
brew services start libravdbd

# Linux
sudo apt install libravdbd
systemctl --user enable --now libravdbd
```

## 2. Configure

Run the guided setup:

```bash
hermes memory setup
```

Select **libravdb** when prompted. This writes `~/.hermes/libravdb.json` and sets `memory.provider: "libravdb"` in `~/.hermes/config.yaml`.

If your daemon listens on a non-default endpoint, set it before running setup:

```bash
export LIBRAVDB_GRPC_ENDPOINT="tcp:127.0.0.1:37421"
```

For authenticated daemons (HMAC-SHA256), set the shared secret:

```bash
export LIBRAVDB_AUTH_SECRET="your-secret-here"
```

## 3. Verify

Restart Hermes or start a new session, then run:

```bash
hermes libravdb status
```

Expected output: `ok: true` with memory counts.

```bash
hermes libravdb health
```

Expected: healthy response from the daemon.

## 4. Search Test

Start a Hermes conversation, exchange a few messages so the daemon has content to index, then test search:

```bash
hermes libravdb search "test query" --limit 5
```

Expected: JSON results with `id`, `score`, and `text` fields.

## 5. Tools Test

Inside a Hermes conversation, ask the agent to use the tools:

> Use libravdb_search to find recent information about X.

The agent should call `libravdb_search` and return results.

> Use libravdb_status to check the memory backend.

The agent should call `libravdb_status` and report the daemon state.

## 6. Context Engine (Optional)

If you want to test the context engine, add to `~/.hermes/config.yaml`:

```yaml
context:
  engine: "libravdb"
```

Restart Hermes. The context engine adds daemon-side assembly, exact recall augmentation, and predictive context injection to every turn.

## Failure Modes

| Symptom | Check |
|---|---|
| `hermes libravdb status` returns error | Is `libravdbd` running? Try `brew services list` or `systemctl --user status libravdbd` |
| Connection refused | Is `LIBRAVDB_GRPC_ENDPOINT` correct? Default is `unix:$HOME/.libravdbd/run/libravdb.sock` |
| Auth failures | Does `LIBRAVDB_AUTH_SECRET` match the daemon's configured secret? |
| No search results | Have you had a conversation yet? The daemon needs messages to index |
| Plugin not found | Is `memory.provider: "libravdb"` in `~/.hermes/config.yaml`? |

## How to Report

For each failure, include:

1. Plugin version: `pip show hermes-memory-libravdb | grep Version`
2. Daemon version: `libravdbd --version`
3. Hermes version: `hermes --version`
4. The exact command and full error output
5. Whether auth is enabled (`LIBRAVDB_AUTH_SECRET` set or not — don't share the secret)
6. The output of `hermes libravdb status`
