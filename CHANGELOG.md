# Changelog

## v0.7.0 — 2026-06-30

**Contributor:** JezzaHehn — [PR #50](https://github.com/xDarkicex/hermes-memory-libravdb/pull/50)
**Signed off by:** xDarkicex

### Added

- **User card CRUD tools.** `get_user_card`, `update_user_card`, and `list_user_cards`
  for managing per-user identity cards via the daemon's existing gRPC RPCs
  (`UpsertUserCard`, `GetUserCard`, `ListByMeta`).
- 14 TDD tests covering schemas, happy paths, argument validation, and error handling.

### Fixed

- Removed redundant inline `import time` in `handle_tool_call` (already imported at module level).
- Fixed `metadataJson` → `metadata_json` in `list_user_cards` — Python protobuf bindings use snake_case.
