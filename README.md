# ♎ LibraVDB - Memory and Context Management

<div align="center">
  <img src="./docs/assets/libravdb-logo.svg" alt="LibraVDB" width="640">
</div>

<div align="center">
  <a href="./pyproject.toml"><img src="https://img.shields.io/badge/Python-3.9%2B-3776AB?logo=python&logoColor=white" alt="Python 3.9+"></a>
  <a href="./plugin.yaml"><img src="https://img.shields.io/badge/Hermes-memory%20provider-00D8FF?logo=hermes&logoColor=white" alt="Hermes memory provider"></a>
  <a href="https://pypi.org/project/hermes-memory-libravdb/"><img src="https://img.shields.io/pypi/v/hermes-memory-libravdb?label=release&color=5B21B6&cacheSeconds=0" alt="Release"></a>
</div>

`hermes-memory-libravdb` is a native Python memory provider for Hermes Agent
backed by the `libravdbd` vector service. It replaces the lightweight default memory
path with scoped session, user, and global memory; continuity-aware prompt
assembly; durable recall; and sidecar-owned compaction.

[Install](./docs/install.md) · [Configuration](./docs/configuration.md) · [Architecture](./docs/architecture.md) · [Security](./docs/security.md) · [Contributing](./docs/contributing.md)

New install? Start here: [Install guide](./docs/install.md).

## Install

Install `libravdbd` with your system package manager, then install
the Hermes plugin.

**macOS (Homebrew)**

```bash
brew tap xDarkicex/homebrew-libravdbd
brew install libravdbd
brew services start libravdbd
```

**Linux (APT)**

```bash
curl -fsSL https://xDarkicex.github.io/apt-libravdbd/gpg.key | sudo gpg --dearmor -o /etc/apt/trusted.gpg.d/libravdbd.gpg
echo "deb https://xDarkicex.github.io/apt-libravdbd stable main" | sudo tee /etc/apt/sources.list.d/libravdbd.list
sudo apt update
sudo apt install libravdbd
systemctl --user enable --now libravdbd
```

**Linux (AUR)**

```bash
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

**Plugin (all platforms)**

```bash
# Install the pip package
pip install hermes-memory-libravdb

# Install into Hermes plugin directory (required for Hermes 0.14 discovery)
hermes-memory-libravdb-setup install
```

On Debian/Ubuntu, use pipx or the Hermes venv to avoid PEP 668 errors:

```bash
pipx install hermes-memory-libravdb
hermes-memory-libravdb-setup install
```

Then set `memory.provider: "libravdb"` in `~/.hermes/config.yaml` or run
`hermes memory setup` and select `libravdb`.

Verify the service:

```bash
hermes libravdb status
```

Healthy output should show `ok: true`, stored memory counts, and the active
gate threshold.

## Quick Start

Runtime requirements:

- Hermes Agent `>= 0.14`
- Python `>= 3.9`
- a separately installed `libravdbd` service

Compatibility note:

- this plugin is currently verified against Hermes Agent `0.14.x`

Default endpoints:

- macOS/Linux user-local service: `unix:$HOME/.libravdbd/run/libravdb.sock`
- Homebrew service on Apple Silicon: `unix:/opt/homebrew/var/libravdbd/run/libravdb.sock`
- Windows service: `tcp:127.0.0.1:37421`

If your service runs elsewhere, set the `LIBRAVDB_GRPC_ENDPOINT` environment variable:

```bash
export LIBRAVDB_GRPC_ENDPOINT="tcp:127.0.0.1:37421"
```

## Highlights

- **Native gRPC** — direct connection to `libravdbd` using Connect-RPC compatible protocol
- **HMAC nonce authentication** — same secure challenge-response scheme as the TypeScript plugin
- **Three memory scopes** — keeps active session, durable user, and global memory separate
- **Hybrid retrieval** — blends semantic similarity, scope, recency, and summary quality
- **Continuity-aware assembly** — preserves the recent working tail while fitting recalled memory into a bounded prompt budget
- **Background turn sync** — asynchronous message ingestion that doesn't block the UI
- **Local-first inference** — uses local embedding and compaction paths by default

## Security Defaults

Stored memory is treated as untrusted historical context. Retrieved memory is
framed before it reaches the downstream model, memory collections are scoped by
session/user/global namespace, and service installation is outside the Python package.

Before exposing Hermes over remote channels, read [Security](./docs/security.md).

## Operator Quick Refs

```bash
hermes libravdb status              # daemon health, collection counts
hermes libravdb health              # quick liveness check
hermes libravdb search "query"      # semantic memory search
hermes libravdb flush --user-id X   # wipe user namespace
hermes libravdb export --user-id X  # NDJSON memory export
hermes libravdb journal --session-id X  # lifecycle journal
hermes libravdb index --user-id X --force  # rebuild search index
```

## Configuration

Provider config lives in `$HERMES_HOME/libravdb.json`. All keys are optional. For the full 60+ key reference, see [Configuration](./docs/configuration.md).

| Key | Type | Default | |
|---|---|---|---|
| `endpoint` | string | `auto` | `"auto"` probes standard paths; set `unix:/path` or `tcp:host:port` to override |
| `userId` | string | auto-derived | Stable identity for cross-session durable memory |
| `topK` | number | `8` | Default number of recalled memory hits |
| `minScore` | number | `0.35` | Minimum semantic score for prefetched/tool search results |

Set `memory.provider: "libravdb"` in Hermes `config.yaml` to activate. Run `hermes memory setup` for guided configuration.

## Environment Variables

| Variable | Description |
|---|---|
| `LIBRAVDB_GRPC_ENDPOINT` | gRPC endpoint (e.g., `unix:/path` or `tcp:host:port`) |
| `LIBRAVDB_AUTH_SECRET` | HMAC secret for daemon authentication |
| `LIBRAVDB_AUTH_SECRET_FILE` | Path to file containing HMAC secret |
| `HERMES_HOME` | Hermes configuration directory (default: `~/.hermes`) |

## Docs By Goal

- New install: [Install](./docs/install.md)
- Verify your setup: [Beta Tester Checklist](./docs/beta-checklist.md)
- Understand the design: [Architecture](./docs/architecture.md)
- How it fits Hermes: [Hermes Integration](./docs/hermes-integration.md)
- Configure: [Configuration](./docs/configuration.md), [TLS configuration](./docs/TLS_configuration.md)
- Operate safely: [Security](./docs/security.md), [Uninstall](./docs/uninstall.md)
- Contributing: [Contributing](./docs/contributing.md)

## From Source

```bash
pip install -e .
pip install -e ".[dev]"
pytest
```

## Runtime Facts

- PyPI package: `hermes-memory-libravdb`
- Hermes plugin kind: `memory`
- Hermes memory provider name: `libravdb`
- minimum Python version: `>= 3.9`
- default data path: `$HOME/.libravdbd/data_nomic-embed-text-v1_5.libravdb`
- default macOS/Linux endpoint: `unix:$HOME/.libravdbd/run/libravdb.sock`