# Install

## Prerequisites

- **Hermes Agent** — version 0.14.0 or later. See [Hermes Agent installation guide](https://hermes-agent.nousresearch.com/docs/getting-started) if you don't have it installed.
- **Python** — version 3.10 or later.
- **libravdbd** — the LibraVDB daemon must be installed and running separately. See the [libravdbd installation guide](https://github.com/xDarkicex/libravdbd) for your platform.

  ```bash
  # macOS (Homebrew)
  brew tap xDarkicex/homebrew-libravdbd
  brew install libravdbd
  brew services start libravdbd

  # Linux (APT)
  sudo apt install libravdbd
  systemctl --user enable --now libravdbd
  ```

- **Environment variable** `LIBRAVDB_GRPC_ENDPOINT` — set this if your daemon runs somewhere other than the default Unix socket path (see [Configuration](./configuration.md)).

---

## Pip Install

The fastest way to get started.

```bash
# Find your Hermes Python environment (path may vary)
export HERMES_PYTHON=$(head -1 $(which hermes) | cut -d' ' -f2)
HERMES_BIN=$(dirname "$HERMES_PYTHON")

# Install the pip package
"$HERMES_BIN/pip" install hermes-memory-libravdb
```

### Register as a memory provider

Create the memory provider directory and copy the plugin files:

```bash
PROVIDER_DIR="$HOME/.hermes/plugins/libravdb"
mkdir -p "$PROVIDER_DIR"

# Copy plugin package into the Hermes plugin directory
"$HERMES_BIN/python" -c "
import site, shutil, pathlib
pkg_path = pathlib.Path(site.getsitepackages()[0]) / 'hermes_memory_libravdb'
shutil.copytree(pkg_path, '$PROVIDER_DIR', dirs_exist_ok=True)
"
```

Then enable the plugin and configure it:

```bash
hermes plugins enable libravdb
hermes memory setup
```

Follow the prompts and choose **libravdb** when asked which memory provider to use.

After setup, verify the plugin is working:

```bash
hermes libravdb status
```

### Troubleshooting: libravdb doesn't appear in hermes memory setup

1. **Restart Hermes** — Hermes discovers pip-installed plugins at startup. After `pip install`, restart Hermes completely.

2. **Verify the package installed**:
   ```bash
   pip show hermes-memory-libravdb
   ```

3. **Check Hermes version** — this plugin requires Hermes Agent 0.14.0 or later. Run `hermes --version`.

4. **Plugin not found in `hermes memory setup`** — ensure the plugin directory exists:

   ```bash
   ls ~/.hermes/plugins/libravdb/__init__.py
   ```

   If missing, re-run the install copy step above.

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

Hermes discovers directory-based memory plugins from `~/.hermes/plugins/`. After the editable install, copy the plugin directory:

```bash
PROVIDER_DIR="$HOME/.hermes/plugins/libravdb"
mkdir -p "$PROVIDER_DIR"
cp -r src/hermes_memory_libravdb/* "$PROVIDER_DIR/"
```

### Option B — Manual plugin directory

Copy the plugin to Hermes's plugin directory:

```bash
cp -r /path/to/hermes-memory-libravdb/src/hermes_memory_libravdb ~/.hermes/plugins/libravdb
cp /path/to/hermes-memory-libravdb/plugin.yaml ~/.hermes/plugins/libravdb/
```

Then enable it and configure:

```bash
hermes plugins enable libravdb
hermes memory setup  # Select 'libravdb' when prompted
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

3. If you used the manual plugin directory, remove it:

   ```bash
   rm -rf ~/.hermes/plugins/memory/libravdb
   ```

To also remove all stored memory data, delete the daemon's data directory. The default location is `$HERMES_HOME/.libravdbd/` where `$HERMES_HOME` is your Hermes configuration directory (typically `~/.hermes`). See [Uninstall](./uninstall.md) for details.