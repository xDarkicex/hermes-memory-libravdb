# Install

## Prerequisites

- **Hermes Agent** — version 0.14 or later. See [Hermes Agent installation guide](https://hermes-agent.nousresearch.com/docs/getting-started) if you don't have it installed.
- **Python** — version 3.10 or later.
- **libravdbd** — the LibraVDB vector service must be installed and running. Install via one of the package manager methods below.

  ```bash
  # macOS (Homebrew)
  brew tap xDarkicex/homebrew-libravdbd
  brew install libravdbd
  brew services start libravdbd

  # Linux (APT)
  curl -fsSL https://xDarkicex.github.io/apt-libravdbd/gpg.key | sudo gpg --dearmor -o /etc/apt/trusted.gpg.d/libravdbd.gpg
  echo "deb https://xDarkicex.github.io/apt-libravdbd stable main" | sudo tee /etc/apt/sources.list.d/libravdbd.list
  sudo apt update
  sudo apt install libravdbd
  systemctl --user enable --now libravdbd

  # Linux (AUR)
  yay -S libravdbd-bin
  systemctl --user enable --now libravdbd
  ```

  > **After upgrades:** Always restart the daemon so the newly installed binary takes effect:
  > ```bash
  > # macOS (Homebrew)
  > brew services restart libravdbd
  >
  > # Linux (systemd)
  > systemctl --user restart libravdbd
  >
  > # Linux (no systemd — kill and restart manually)
  > killall libravdbd && libravdbd &
  > ```
  > Failing to restart leaves the old process running — it will not auto-replace a live background service. If you see "Protocol error" or connection failures after an upgrade, this is almost always the cause.

- **Environment variable** `LIBRAVDB_GRPC_ENDPOINT` — set this if your daemon runs somewhere other than the default Unix socket path (see [Configuration](./configuration.md)).

---

## Pip Install

The fastest way to get started.

```bash
# 1. Install the pip package
pip install hermes-memory-libravdb

# 2. Install into Hermes plugin directory
hermes-memory-libravdb-setup install
```

On Debian/Ubuntu, use pipx or the Hermes venv to avoid PEP 668 errors:

```bash
pipx install hermes-memory-libravdb
hermes-memory-libravdb-setup install
```

Then configure Hermes to use libravdb:

```bash
hermes memory setup
```

Follow the prompts and choose **libravdb** when asked which memory provider to use.
This writes `memory.provider: "libravdb"` to `~/.hermes/config.yaml`.

After setup, verify the plugin is working:

```bash
hermes libravdb status
```

### Troubleshooting: libravdb doesn't appear in hermes memory setup

1. **Run the setup command** — this plugin must be installed to `$HERMES_HOME/plugins/libravdb/` for Hermes to discover it:
   ```bash
   hermes-memory-libravdb-setup install
   ```
   Verify it exists:
   ```bash
   ls ~/.hermes/plugins/libravdb/
   ```

2. **Verify the package installed**:
   ```bash
   pip show hermes-memory-libravdb
   ```

3. **Check Hermes version** — this plugin requires Hermes Agent 0.14 or later. Run `hermes --version`.

You should see output with `ok: true` and memory counts. If you see an error instead, check that `libravdbd` is running and that `LIBRAVDB_GRPC_ENDPOINT` is set correctly (see [Configuration](./configuration.md)).

---

## Manual / Dev Install

For contributing, debugging, or if you prefer not to use pip.

### Option A — Editable install from source

```bash
git clone https://github.com/xDarkicex/hermes-memory-libravdb.git
cd hermes-memory-libravdb
pip install -e .
```

Hermes will auto-discover the plugin via the `hermes_agent.plugins` entry point on next restart.

### Option B — Manual plugin directory

Create the plugin directory structure under Hermes:

```bash
mkdir -p ~/.hermes/plugins/libravdb
```

Create `~/.hermes/plugins/libravdb/__init__.py`:
```python
from hermes_memory_libravdb import register, LibraVDBMemoryProvider
```

Create `~/.hermes/plugins/libravdb/cli.py`:
```python
from hermes_memory_libravdb.cli import register_cli, libravdb_command
```

Then enable it by adding to your `~/.hermes/config.yaml`:

```yaml
memory:
  provider: "libravdb"
```

Restart Hermes to load the plugin.

### Verifying the manual install

```bash
hermes libravdb status
```

---

## Post-Install Configuration

After installation, you may want to configure optional settings. See [Configuration](./configuration.md) for all available options.

Key settings you can adjust:

- **endpoint** — change the gRPC endpoint if your daemon runs on a non-default host or port.
- **userId** — set a stable identity for cross-session durable memory.
- **topK** — adjust the default number of recalled memory hits.
- **minScore** — set the minimum semantic score threshold for search results.

Environment variables (alternative to config file):

```bash
export LIBRAVDB_GRPC_ENDPOINT="tcp:127.0.0.1:37421"   # daemon on TCP
export LIBRAVDB_AUTH_SECRET="your-secret-here"          # HMAC auth (if enabled)
```

---

## Uninstall

To switch away from libravdb as your memory provider:

1. Run `hermes memory setup` and select a different provider, OR edit `~/.hermes/config.yaml` and set `memory.provider` to a different value (or empty).

2. Remove the pip package:

   ```bash
   pip uninstall hermes-memory-libravdb
   ```

3. Remove the plugin directory:

   ```bash
   hermes-memory-libravdb-setup uninstall
   ```
   Or manually:
   ```bash
   rm -rf ~/.hermes/plugins/libravdb
   ```

To also remove all stored memory data, delete the daemon's data directory. The default location is `$HERMES_HOME/.libravdbd/` where `$HERMES_HOME` is your Hermes configuration directory (typically `~/.hermes`). See [Uninstall](./uninstall.md) for details.
