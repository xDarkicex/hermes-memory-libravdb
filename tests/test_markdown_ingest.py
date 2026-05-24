from unittest.mock import MagicMock

from hermes_memory_libravdb.markdown_ingest import DirectorySourceAdapter


def _adapter(root):
    return DirectorySourceAdapter(
        kind="generic",
        roots=[str(root)],
        include_patterns=[],
        exclude_patterns=[],
        debounce_ms=0,
        snapshot_path=str(root / ".snapshot.json"),
        priority_mode="fifo",
        max_tokens_per_file=128000,
        rpc_caller=MagicMock(),
        user_id="user",
    )


def test_markdown_scan_skips_symlinked_files_outside_root(tmp_path):
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    secret = outside / "secret.md"
    secret.write_text("do not ingest")
    (root / "linked.md").symlink_to(secret)

    adapter = _adapter(root)

    assert adapter._sync_markdown_file(
        str(root),
        str(root / "linked.md"),
        size=secret.stat().st_size,
        mtime_ms=int(secret.stat().st_mtime * 1000),
        ctime_ms=int(secret.stat().st_ctime * 1000),
    ) == "skipped"

    adapter._rpc_caller.assert_not_called()


def test_markdown_walk_does_not_recurse_into_symlinked_directories(tmp_path):
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "secret.md").write_text("do not ingest")
    (root / "linked-dir").symlink_to(outside, target_is_directory=True)

    adapter = _adapter(root)
    current_files = set()
    candidates = []
    stats = MagicMock()
    stats.directories_pruned = 0
    stats.directories_scanned = 0
    stats.markdown_files_seen = 0
    stats.files_skipped = 0
    stats.files_included = 0

    adapter._walk_directory(str(root), str(root), current_files, stats, candidates)

    assert candidates == []
    assert current_files == set()
