# Uninstall

## Step 1 — Switch Away from libravdb

**Option A — Interactive:**

```bash
hermes memory setup
```

Select a different memory provider when prompted, or select "none" to use only Hermes built-in memory.

**Option B — Manual edit:**

In `~/.hermes/config.yaml`, remove or change the memory provider:

```yaml
memory:
  provider: ""   # empty = built-in only, no external provider
```

Or remove the `memory` section entirely.

This step stops Hermes from loading the libravdb plugin. It does not delete any data.

---

## Step 2 — Remove the Plugin Package

**Pip installation:**

```bash
pip uninstall hermes-memory-libravdb
```

**Manual installation:**

```bash
rm -rf ~/.hermes/plugins/memory/libravdb
```

This removes the plugin code. It does not delete any stored memory data.

---

## Step 3 — Delete LibraVDB Data (Optional)

This step is **optional** and **destructive**. It deletes all memories stored by the daemon — session history, user facts, global knowledge, everything.

The daemon's data directory is **not managed by the plugin** — it is controlled by `libravdbd`. The default location is:

```
$HERMES_HOME/.libravdbd/
```

To delete all stored data:

```bash
rm -rf ~/.hermes/.libravdbd
```

This removes:
- All session memories (`session:<id>` collections)
- All durable user memories (`user:<id>` collections)
- All global memories (`global` collection)
- Any search index data

**What is not deleted:**

| Path | Why |
|---|---|
| `~/.hermes/libravdb.json` | Plugin config — non-secret, safe to keep |
| `~/.hermes/config.yaml` | Hermes global config — unrelated to this plugin |
| `~/.hermes/` other profile dirs | Per-profile isolation — only the profile's data is affected |

If you use multiple profiles and want to delete data for only one of them, target only that profile's `$HERMES_HOME`:

```bash
rm -rf ~/.hermes.coder/.libravdbd
```

---

## After Uninstall

After removing the plugin and data:

- `hermes libravdb` commands will no longer be available
- All previous memories stored by libravdb are permanently deleted
- Hermes built-in memory (`MEMORY.md`, `USER.md`) continues to work as before