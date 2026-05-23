# Beta Tester Checklist

A 5-minute verification path for LibraVDB memory provider testers.

## 1. Install

```bash
# Install the pip package (use Hermes venv — check with: head -1 $(which hermes))
pip install hermes-memory-libravdb

# Copy to the memory provider directory
PROVIDER_DIR="$HOME/.hermes/plugins/libravdb"
mkdir -p "$PROVIDER_DIR"
HERMES_PYTHON=$(head -1 $(which hermes) | cut -d' ' -f2)
"$HERMES_PYTHON" -c "
import site, shutil, pathlib
pkg_path = pathlib.Path(site.getsitepackages()[0]) / 'hermes_memory_libravdb'
shutil.copytree(pkg_path, '$PROVIDER_DIR', dirs_exist_ok=True)
"

# Enable the plugin
hermes plugins enable libravdb
## 1. Install Daemon

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

## 2. Install Plugin

```bash
pip install hermes-memory-libravdb
hermes-memory-libravdb-setup install
```

On Debian/Ubuntu, use pipx to avoid PEP 668 errors:

```bash
pipx install hermes-memory-libravdb
hermes-memory-libravdb-setup install
```

## 3. Configure

Set `memory.provider: "libravdb"` in `~/.hermes/config.yaml`, or run:

```bash
hermes memory setup
```

Select **libravdb** when prompted.

If your daemon listens on a non-default endpoint, set it before running setup:

```bash
export LIBRAVDB_GRPC_ENDPOINT="tcp:127.0.0.1:37421"
```

For authenticated daemons (HMAC-SHA256), set the shared secret:

```bash
export LIBRAVDB_AUTH_SECRET="your-secret-here"
```

## 4. Verify

Restart Hermes or start a new session, then run:

```bash
hermes libravdb status
```

Expected output: `ok: true` with memory counts.

```bash
hermes libravdb health
```

Expected: healthy response from the daemon.

## 5. Search Test

Start a Hermes conversation, exchange a few messages so the daemon has content to index, then test search:

```bash
hermes libravdb search "test query" --limit 5
```

Expected: JSON results with `id`, `score`, and `text` fields.

## 6. Tools Test

Inside a Hermes conversation, ask the agent to use the tools:

> Use libravdb_search to find recent information about X.

The agent should call `libravdb_search` and return results.

> Use libravdb_status to check the memory backend.

The agent should call `libravdb_status` and report the daemon state.

## 7. Context Engine (Optional)

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
