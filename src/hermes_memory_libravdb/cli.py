from __future__ import annotations

import json
import os
from pathlib import Path

from .provider import (
    LibraVDBMemoryProvider,
    _get_hermes_home,
    _resolve_endpoint,
    _load_secret,
    _resolve_transport_config,
    _GrpcChannel,
)
from .identity import resolve_identity
from .markdown_ingest import MarkdownIngestionHandle
from .scopes import user_collection, resolve_search_scopes, resolve_durable_namespace
from libravdb.ipc.v1 import rpc_pb2 as pb


def _load_cli_config() -> dict:
    """Load plugin config for CLI usage (reads libravdb.json if it exists)."""
    config_path = _get_hermes_home() / "libravdb.json"
    if not config_path.exists():
        return {}
    try:
        return json.loads(config_path.read_text())
    except Exception:
        return {}


def _create_cli_channel() -> _GrpcChannel:
    """Create a gRPC channel from plugin config for CLI commands."""
    transport = _resolve_transport_config(_load_cli_config())
    return _GrpcChannel(
        endpoint=transport["endpoint"],
        secret=transport["secret"],
        timeout_ms=transport["timeout_ms"],
        tls_mode=transport["tls_mode"],
        tls_ca_path=transport["tls_ca_path"],
        tls_client_cert_path=transport["tls_client_cert_path"],
        tls_client_key_path=transport["tls_client_key_path"],
    )


def _resolve_cli_min_score(channel: _GrpcChannel, config: dict, explicit: float | None) -> float:
    """Resolve minScore with OpenClaw fallback chain: explicit → daemon → config → 0.35."""
    if explicit is not None:
        return float(explicit)
    try:
        resp = channel._call("Status", pb.MemoryStatusRequest())
        gt = getattr(resp, "gating_threshold", None)
        if gt is not None:
            return float(gt)
    except Exception:
        pass
    igt = config.get("ingestionGateThreshold")
    if igt is not None:
        return float(igt)
    return 0.35


def _cli_health() -> dict:
    channel = _create_cli_channel()
    try:
        resp = channel.health()
        channel.close()
        return {"ok": True, "daemon_health": resp.ok, "message": resp.message}
    except Exception as e:
        channel.close()
        return {"ok": False, "error": str(e)}


def _cli_status() -> dict:
    channel = _create_cli_channel()
    config = _load_cli_config()
    try:
        resp = channel._call("Status", pb.MemoryStatusRequest())
        channel.close()
        return {
            "ok": resp.ok,
            "message": resp.message,
            "turn_count": resp.turn_count,
            "memory_count": resp.memory_count,
            "lifecycle_hint_count": getattr(resp, "lifecycle_hint_count", None),
            "gating_threshold": getattr(resp, "gating_threshold", None) or config.get("ingestionGateThreshold", 0.35),
            "abstractive_ready": getattr(resp, "abstractive_ready", None),
            "embedding_profile": getattr(resp, "embedding_profile", None),
        }
    except Exception as e:
        channel.close()
        return {
            "ok": False,
            "error": str(e),
            "gating_threshold": config.get("ingestionGateThreshold", 0.35),
        }


def _cli_list_collections(channel, collection_patterns: list[str]) -> list[dict]:
    """Try to list IDs from each collection pattern."""
    results = []
    for coll in collection_patterns:
        try:
            resp = channel._call("ListCollection", pb.ListCollectionRequest(collection=coll))
            results.append({
                "collection": coll,
                "count": len(resp.ids) if resp and hasattr(resp, "ids") else 0,
                "ids": resp.ids[:10] if resp and hasattr(resp, "ids") else [],
            })
        except Exception:
            results.append({"collection": coll, "count": 0, "error": "unavailable"})
    return results


def _cli_index(user_id: str | None, session_key: str | None, collections: str | None, force: bool) -> dict:
    """Rebuild the vector index. Requires --force confirmation."""
    if not force:
        return {"ok": False, "error": "Index rebuild requires --force. This re-embeds all stored documents and may be slow."}

    channel = _create_cli_channel()
    try:
        namespace = ""
        if user_id:
            namespace = user_id
        elif session_key:
            namespace = resolve_durable_namespace(session_key=session_key)
        coll_list = None
        if collections:
            coll_list = [c.strip() for c in collections.split(",") if c.strip()]

        req = pb.RebuildIndexRequest(namespace=namespace)
        if coll_list:
            req.collections.extend(coll_list)

        resp = channel._call("RebuildIndex", req)
        channel.close()
        return {
            "ok": True,
            "collections_processed": getattr(resp, "collections_processed", 0),
            "records_reindexed": getattr(resp, "records_reindexed", 0),
            "collections_recreated": getattr(resp, "collections_recreated", 0),
            "errors": list(getattr(resp, "errors", [])),
        }
    except Exception as e:
        channel.close()
        return {"ok": False, "error": str(e)}


def _cli_status_deep(rebuild_index: bool = False, force: bool = False) -> dict:
    channel = _create_cli_channel()
    identity = resolve_identity()
    resolved_user_id = identity.user_id
    try:
        # Optionally rebuild index first (requires --force)
        if rebuild_index:
            if not force:
                channel.close()
                return {
                    "ok": False,
                    "error": "status --index performs an index rebuild. Re-run with --force to continue.",
                }
            try:
                index_resp = channel._call(
                    "RebuildIndex",
                    pb.RebuildIndexRequest(namespace="default", collections=[]),
                )
                index_result = {
                    "collections_processed": index_resp.collections_processed if hasattr(index_resp, "collections_processed") else 0,
                    "records_reindexed": index_resp.records_reindexed if hasattr(index_resp, "records_reindexed") else 0,
                    "errors": list(index_resp.errors) if hasattr(index_resp, "errors") else [],
                }
            except Exception as e:
                index_result = {"error": str(e)}
        else:
            index_result = None

        # Base status
        status_resp = channel._call("Status", pb.MemoryStatusRequest())
        base = {
            "ok": status_resp.ok,
            "message": status_resp.message,
            "turn_count": status_resp.turn_count,
            "memory_count": status_resp.memory_count,
            "lifecycle_hint_count": getattr(status_resp, "lifecycle_hint_count", None),
            "gating_threshold": getattr(status_resp, "gating_threshold", None),
            "abstractive_ready": getattr(status_resp, "abstractive_ready", None),
            "embedding_profile": getattr(status_resp, "embedding_profile", None),
        }

        # Probe collections
        collections = _cli_list_collections(channel, [
            user_collection(resolved_user_id),
            "global",
        ])

        # Resolve search mode from config
        config = _load_cli_config()
        scope_mode = "session"
        if config.get("useSessionSummarySearchExperiment"):
            scope_mode = "session_summary"
        elif config.get("useSessionRecallProjection"):
            scope_mode = "session_recall"

        threshold_source = "default"
        effective_min_score = 0.35
        daemon_gt = base.get("gating_threshold")
        if daemon_gt is not None:
            threshold_source = "daemon"
            effective_min_score = float(daemon_gt)
        elif config.get("ingestionGateThreshold") is not None:
            threshold_source = "config"
            effective_min_score = float(config["ingestionGateThreshold"])

        search_mode = {
            "crossSessionRecall": config.get("crossSessionRecall", True),
            "scopeMode": scope_mode,
            "topK": config.get("topK", 8),
            "effectiveMinScore": effective_min_score,
            "thresholdSource": threshold_source,
        }

        channel.close()

        return {
            **base,
            "reindex_result": index_result,
            "collections": collections,
            "search_mode": search_mode,
            "memory_provider_active": True,
            "context_engine_active": True,
        }
    except Exception as e:
        channel.close()
        return {
            "ok": False,
            "error": str(e),
            "memory_provider_active": False,
            "context_engine_active": False,
        }


def _cli_markdown_ingest_status() -> dict:
    """Show markdown ingestion status (what's configured, snapshot file counts)."""
    config = _load_cli_config()
    enabled = config.get("markdownIngestionEnabled") is True
    generic_roots = config.get("markdownIngestionRoots") or []
    obsidian_roots = (
        config.get("markdownIngestionObsidianRoots") or []
        if config.get("markdownIngestionObsidianEnabled") is True
        else []
    )

    if not enabled:
        return {
            "enabled": False,
            "message": "Markdown ingestion is disabled. Set markdownIngestionEnabled=true in libravdb.json.",
        }

    adapters = []
    if generic_roots:
        snapshot_path = MarkdownIngestionHandle._resolve_snapshot_path(
            "generic", config.get("markdownIngestionSnapshotPath")
        )
        adapters.append({
            "kind": "generic",
            "roots": generic_roots,
            "snapshotPath": snapshot_path,
            "includePatterns": config.get("markdownIngestionInclude") or [],
            "excludePatterns": config.get("markdownIngestionExclude") or [],
            "priorityMode": config.get("markdownIngestionPriorityMode", "mtime"),
            "maxTokensPerFile": config.get("markdownIngestionMaxTokensPerFile", 128000),
        })
    if obsidian_roots:
        snapshot_path = MarkdownIngestionHandle._resolve_snapshot_path(
            "obsidian", config.get("markdownIngestionObsidianSnapshotPath")
        )
        adapters.append({
            "kind": "obsidian",
            "roots": obsidian_roots,
            "snapshotPath": snapshot_path,
            "includePatterns": config.get("markdownIngestionObsidianInclude") or [],
            "excludePatterns": config.get("markdownIngestionObsidianExclude") or [],
            "priorityMode": config.get("markdownIngestionPriorityMode", "mtime"),
            "maxTokensPerFile": config.get("markdownIngestionMaxTokensPerFile", 128000),
        })

    return {
        "enabled": True,
        "adapters": adapters,
    }


def _cli_markdown_ingest_scan() -> dict:
    """Run a one-shot markdown ingestion scan and report results."""
    config = _load_cli_config()
    enabled = config.get("markdownIngestionEnabled") is True
    if not enabled:
        return {"ok": False, "error": "Markdown ingestion is not enabled in libravdb.json"}

    channel = _create_cli_channel()
    identity = resolve_identity()

    try:
        handle = MarkdownIngestionHandle(
            config=config,
            rpc_caller=channel._call,
            user_id=identity.user_id,
        )
        if not handle.is_active:
            channel.close()
            return {"ok": False, "error": "No markdown ingestion roots configured"}

        handle.refresh()
        channel.close()
        return {
            "ok": True,
            "scanned": True,
            "adapters": [
                {
                    "kind": a.kind,
                    "roots": a.roots,
                    "fileCount": len(a._snapshot._files),
                }
                for a in handle.adapters
            ],
        }
    except Exception as e:
        channel.close()
        return {"ok": False, "error": str(e)}


def libravdb_command(args) -> None:
    subcommand = getattr(args, "libravdb_subcommand", None)

    if subcommand == "status":
        if getattr(args, "index", False):
            force = getattr(args, "force", False)
            result = _cli_status_deep(rebuild_index=True, force=force)
            print(json.dumps(result, indent=2))
            return
        if getattr(args, "deep", False):
            result = _cli_status_deep(rebuild_index=False)
            print(json.dumps(result, indent=2))
            return
        result = _cli_status()
        print(json.dumps(result, indent=2))
        return

    if subcommand == "index":
        user_id = getattr(args, "user_id", None)
        session_key = getattr(args, "session_key", None)
        collections = getattr(args, "collections", None)
        force = getattr(args, "force", False)
        result = _cli_index(user_id, session_key, collections, force)
        print(json.dumps(result, indent=2))
        return

    if subcommand == "health":
        result = _cli_health()
        print(json.dumps(result, indent=2))
        return

    if subcommand == "search":
        channel = _create_cli_channel()
        config = _load_cli_config()
        identity = resolve_identity()
        k = int(args.limit or config.get("topK", 8))
        collections = resolve_search_scopes(
            user_id=identity.user_id,
            session_id=None,  # CLI searches across user+global (no session)
            cross_session_recall=config.get("crossSessionRecall", True),
            use_session_summary_search=config.get("useSessionSummarySearchExperiment", False),
            use_session_recall_projection=config.get("useSessionRecallProjection", False),
        )
        # Resolve minScore: explicit --min-score → daemon gatingThreshold → config → default
        explicit = getattr(args, "min_score", None)
        min_score = _resolve_cli_min_score(channel, config, explicit)
        try:
            if len(collections) == 1:
                resp = channel._call("SearchText", pb.SearchTextRequest(
                    collection=collections[0],
                    text=args.query,
                    k=k,
                ))
            else:
                resp = channel._call("SearchTextCollections", pb.SearchTextCollectionsRequest(
                    collections=collections,
                    text=args.query,
                    k=k,
                    exclude_by_collection={},
                ))
            results = [
                {"id": r.id, "score": r.score, "text": r.text}
                for r in resp.results
                if r.score >= min_score
            ]
            if getattr(args, "json", False):
                print(json.dumps({"results": results, "minScore": min_score, "topK": k}, indent=2))
            else:
                for r in results:
                    print(f"[{r['score']:.2f}] {r['text'][:200]}")
            return
        except Exception as e:
            print(json.dumps({"ok": False, "error": str(e)}))
            return
        finally:
            channel.close()

    if subcommand == "flush":
        user_id = getattr(args, "user_id", None)
        session_key = getattr(args, "session_key", None)
        if not user_id and not session_key:
            print(json.dumps({"error": "--user-id or --session-key is required"}))
            return
        namespace = user_id if user_id else resolve_durable_namespace(session_key=session_key)
        print(f"Flushing namespace {namespace} ... (ctrl-c to abort)")
        try:
            input("Press Enter to confirm: ")
        except EOFError:
            pass
        channel = _create_cli_channel()
        try:
            resp = channel._call("FlushNamespace", pb.FlushNamespaceRequest(user_id=namespace, namespace=""))
            channel.close()
            print(json.dumps({"ok": resp.ok if hasattr(resp, "ok") else True}))
        except Exception as e:
            channel.close()
            print(json.dumps({"ok": False, "error": str(e)}))
        return

    if subcommand == "export":
        user_id = getattr(args, "user_id", None)
        session_key = getattr(args, "session_key", None)
        if not user_id and not session_key:
            print(json.dumps({"error": "--user-id or --session-key is required"}))
            return
        namespace = user_id if user_id else resolve_durable_namespace(session_key=session_key)
        channel = _create_cli_channel()
        try:
            resp = channel._call("ExportMemory", pb.ExportMemoryRequest(user_id=namespace, namespace=""))
            channel.close()
            if hasattr(resp, "records"):
                for rec in resp.records:
                    print(json.dumps({
                        "collection": rec.collection,
                        "id": rec.id,
                        "text": rec.text,
                    }))
            else:
                print(json.dumps({"records": []}))
        except Exception as e:
            channel.close()
            print(json.dumps({"error": str(e)}))
        return

    if subcommand == "journal":
        session_id = getattr(args, "session_id", None)
        limit = int(getattr(args, "limit", 50) or 50)
        if not session_id:
            print(json.dumps({"error": "--session-id is required"}))
            return
        channel = _create_cli_channel()
        try:
            resp = channel._call("ListLifecycleJournal", pb.ListLifecycleJournalRequest(session_id=session_id, limit=limit))
            channel.close()
            entries = []
            if hasattr(resp, "entries"):
                for e in resp.entries:
                    entries.append({"hook": getattr(e, "hook", ""), "session_id": getattr(e, "session_id", ""), "reason": getattr(e, "reason", "")})
            print(json.dumps({"ok": True, "entries": entries}, indent=2))
        except Exception as e:
            channel.close()
            print(json.dumps({"ok": False, "error": str(e)}))
        return

    if subcommand == "dream-promote":
        user_id = getattr(args, "user_id", None)
        dream_file = getattr(args, "dream_file", None)
        if not user_id or not dream_file:
            print(json.dumps({"error": "--user-id and --dream-file are both required"}))
            return
        path = Path(dream_file).expanduser()
        if not path.exists():
            print(json.dumps({"error": f"File not found: {dream_file}"}))
            return
        try:
            text = path.read_text()
        except Exception as e:
            print(json.dumps({"error": f"Cannot read file: {e}"}))
            return
        channel = _create_cli_channel()
        try:
            resp = channel._call(
                "PromoteDreamEntries",
                pb.PromoteDreamEntriesRequest(
                    user_id=user_id,
                    source_doc=str(path),
                    source_root=str(path.parent),
                    source_path=str(path),
                    source_kind="dream",
                    file_hash="",
                    source_size=path.stat().st_size,
                    source_mtime_ms=int(path.stat().st_mtime * 1000),
                    entries=[pb.DreamPromotionEntry(text=text, score=1.0)],
                    ingest_version=0,
                    hash_backend="md5",
                    source_ctime_ms=int(path.stat().st_ctime * 1000),
                ),
            )
            channel.close()
            print(json.dumps({
                "ok": True,
                "promoted": getattr(resp, "promoted", 0),
                "rejected": getattr(resp, "rejected", 0),
            }))
        except Exception as e:
            channel.close()
            print(json.dumps({"ok": False, "error": str(e)}))
        return

    if subcommand == "markdown-ingest":
        ingest_action = getattr(args, "ingest_action", None)
        if ingest_action == "status":
            result = _cli_markdown_ingest_status()
            print(json.dumps(result, indent=2))
            return
        if ingest_action == "scan":
            result = _cli_markdown_ingest_scan()
            print(json.dumps(result, indent=2))
            return
        print("Usage: hermes libravdb markdown-ingest <status|scan>")
        return

    print("Usage: hermes libravdb <status|index|health|search|flush|export|journal|dream-promote|markdown-ingest>")


def register_cli(subparser) -> None:
    subparser.description = "Manage the LibraVDB Hermes memory provider"
    subs = subparser.add_subparsers(dest="libravdb_subcommand")

    status = subs.add_parser("status", help="Show LibraVDB daemon status")
    status.add_argument("--deep", action="store_true", help="Probe all collections and report per-collection document count and index health")
    status.add_argument("--index", action="store_true", help="Rebuild the index before running status")
    status.add_argument("--force", action="store_true", help="Required with --index: confirm index rebuild")
    status.set_defaults(func=libravdb_command)

    index_cmd = subs.add_parser("index", help="Rebuild LibraVDB memory vector index (requires --force)")
    index_cmd.add_argument("--user-id", help="User ID namespace to reindex")
    index_cmd.add_argument("--session-key", help="Session key whose derived namespace should be reindexed")
    index_cmd.add_argument("--collections", help="Comma-separated collection names to reindex")
    index_cmd.add_argument("--force", action="store_true", required=True, help="Required: confirm index rebuild")
    index_cmd.set_defaults(func=libravdb_command)

    health = subs.add_parser("health", help="Check daemon health")
    health.set_defaults(func=libravdb_command)

    search = subs.add_parser("search", help="Search memory")
    search.add_argument("query", help="Semantic search query")
    search.add_argument("--limit", help="Maximum results")
    search.add_argument("--min-score", type=float, dest="min_score", help="Minimum semantic score threshold (default: daemon gating threshold, or config ingestionGateThreshold, or 0.35)")
    search.add_argument("--json", action="store_true", help="Print structured JSON")
    search.set_defaults(func=libravdb_command)

    flush = subs.add_parser("flush", help="Wipe all data for a given namespace")
    flush.add_argument("--user-id", help="User ID namespace to wipe")
    flush.add_argument("--session-key", help="Session key whose derived namespace should be wiped")
    flush.set_defaults(func=libravdb_command)

    export = subs.add_parser("export", help="Export all memories for a namespace as NDJSON to stdout")
    export.add_argument("--user-id", help="User ID namespace to export")
    export.add_argument("--session-key", help="Session key whose derived namespace should be exported")
    export.set_defaults(func=libravdb_command)

    journal = subs.add_parser("journal", help="Print the lifecycle journal for a session")
    journal.add_argument("--session-id", required=True, help="Session ID to journal")
    journal.add_argument("--limit", default=50, help="Maximum number of entries to return")
    journal.set_defaults(func=libravdb_command)

    dream_promote = subs.add_parser("dream-promote", help="Promote dream/diary entries into user memory collection")
    dream_promote.add_argument("--user-id", required=True, help="User ID namespace")
    dream_promote.add_argument("--dream-file", required=True, help="Path to dream/diary file")
    dream_promote.set_defaults(func=libravdb_command)

    md_ingest = subs.add_parser("markdown-ingest", help="Manage markdown file ingestion")
    md_subs = md_ingest.add_subparsers(dest="ingest_action")
    md_status = md_subs.add_parser("status", help="Show ingestion configuration and snapshot status")
    md_status.set_defaults(func=libravdb_command)
    md_scan = md_subs.add_parser("scan", help="Run a one-shot markdown directory scan")
    md_scan.set_defaults(func=libravdb_command)