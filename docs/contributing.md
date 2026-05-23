# Contributing

## Dev Setup

```bash
git clone https://github.com/xDarkicex/hermes-memory-libravdb.git
cd hermes-memory-libravdb
pip install -e ".[dev]"
```

This installs the package in editable mode with dev dependencies.

For a full environment with the daemon:
```bash
# Start libravdbd (from its own repo)
libravdbd &

# Verify connectivity
hermes libravdb status
```

---

## Project Layout

```
src/hermes_memory_libravdb/
├── __init__.py          # register(ctx) entry point, lifecycle hooks,
│                         # context engine (_LibraVDBContextEngine)
├── provider.py          # LibraVDBMemoryProvider, _GrpcChannel, IngestQueue
├── cli.py               # hermes libravdb <subcommand> CLI
├── scopes.py            # Collection naming, search scopes, durable namespace
├── identity.py          # OS/hostname identity resolution
└── markdown_ingest.py   # Directory scanning, chunked REPLACE/APPEND ingest

docs/                    # User-facing documentation
plugin.yaml              # Hermes plugin manifest (name, version, hooks)
pyproject.toml           # Package config, entry points, dependencies
```

---

## Coding Style

- **Python 3.9+** — use type hints throughout.
- **PEP 8** — 4-space indentation, snake_case for variables/functions.
- **No emojis in code or comments.**
- Keep functions small. If a function exceeds ~80 lines, consider splitting.
- Docstrings only when the WHY is non-obvious — well-named code is self-documenting.

Linting (when set up):
```bash
ruff check src/
```

---

## Running Tests

```bash
pytest
```

For watch mode during development:
```bash
pytest --watch
```

Test files (when added) should live alongside the code they test:
```
tests/
├── test_provider.py    # Unit tests for provider.py
└── test_cli.py         # Unit tests for cli.py
```

---

## Submitting Changes

1. **Branch** — create a feature branch from `main`:
   ```bash
   git checkout -b feature/my-feature
   ```

2. **Commit** — keep commits focused. Do not mix unrelated changes.
   ```bash
   git commit -m "描述"
   ```

3. **Push** — push your branch and open a pull request:
   ```bash
   git push origin feature/my-feature
   ```

4. **PR description** — explain what changed and why. Link any relevant issues.

5. **CI** — the release workflow runs on tags. PRs are tested by the test suite (when added).

---

## Before Submitting

- `pytest` passes with no errors
- No new lint errors introduced
- New features include tests
- Documentation updated if behavior changed