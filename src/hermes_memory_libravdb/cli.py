from __future__ import annotations

import json
import os
from pathlib import Path

from .provider import LibraVDBMemoryProvider, _get_hermes_home, _resolve_endpoint, _load_secret, _GrpcChannel
from .identity import resolve_identity
from .scopes import user_collection, resolve_search_scopes
from libravdb.ipc.v1 import rpc_pb2 as pb


def _cli_health() -> dict:
    channel = _GrpcChannel(
        endpoint=_resolve_endpoint(),
        secret=_load_secret(),
    )
    try:
        resp = channel.health()
        channel.close()
        return {"ok": True, "daemon_health": resp.ok, "message": resp.message}
    except Exception as e:
        channel.close()
        return {"ok": False, "error": str(e)}


def _cli_status() -> dict:
    channel = _GrpcChannel(
        endpoint=_resolve_endpoint(),
        secret=_load_secret(),
    )
    try:
        resp = channel._call("Status", pb.MemoryStatusRequest())
        channel.close()
        return {
            "ok": resp.ok,
            "message": resp.message,
            "turn_count": resp.turn_count,
            "memory_count": resp.memory_count,
        }
    except Exception as e:
        channel.close()
        return {"ok": False, "error": str(e)}


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


def _cli_status_deep(rebuild_index: bool = False) -> dict:
    channel = _GrpcChannel(
        endpoint=_resolve_endpoint(),
        secret=_load_secret(),
    )
    identity = resolve_identity()
    resolved_user_id = identity.user_id
    try:
        # Optionally rebuild index first
        if rebuild_index:
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

        channel.close()

        return {
            **base,
            "reindex_result": index_result,
            "collections": collections,
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


def libravdb_command(args) -> None:
    subcommand = getattr(args, "libravdb_subcommand", None)

    if subcommand == "status":
        if getattr(args, "index", False):
            result = _cli_status_deep(rebuild_index=True)
            print(json.dumps(result, indent=2))
            return
        if getattr(args, "deep", False):
            result = _cli_status_deep(rebuild_index=False)
            print(json.dumps(result, indent=2))
            return
        result = _cli_status()
        print(json.dumps(result, indent=2))
        return

    if subcommand == "health":
        result = _cli_health()
        print(json.dumps(result, indent=2))
        return

    if subcommand == "search":
        channel = _GrpcChannel(
            endpoint=_resolve_endpoint(),
            secret=_load_secret(),
        )
        identity = resolve_identity()
        k = int(args.limit or 8)
        collections = resolve_search_scopes(
            user_id=identity.user_id,
            session_id=None,  # CLI searches across user+global (no session)
            cross_session_recall=True,
        )
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
            ]
            if getattr(args, "json", False):
                print(json.dumps({"results": results}, indent=2))
            else:
                for r in results:
                    print(f"[{r['score']:.2f}] {r['text'][:200]}")
            return
        finally:
            channel.close()

    if subcommand == "flush":
        user_id = getattr(args, "user_id", None)
        if not user_id:
            print(json.dumps({"error": "--user-id is required"}))
            return
        print(f"Flushing namespace for user-id={user_id} ... (ctrl-c to abort)")
        try:
            input("Press Enter to confirm: ")
        except EOFError:
            pass
        channel = _GrpcChannel(endpoint=_resolve_endpoint(), secret=_load_secret())
        try:
            resp = channel._call("FlushNamespace", pb.FlushNamespaceRequest(user_id=user_id, namespace=""))
            channel.close()
            print(json.dumps({"ok": resp.ok if hasattr(resp, "ok") else True}))
        except Exception as e:
            channel.close()
            print(json.dumps({"ok": False, "error": str(e)}))
        return

    if subcommand == "export":
        user_id = getattr(args, "user_id", None)
        if not user_id:
            print(json.dumps({"error": "--user-id is required"}))
            return
        channel = _GrpcChannel(endpoint=_resolve_endpoint(), secret=_load_secret())
        try:
            resp = channel._call("ExportMemory", pb.ExportMemoryRequest(user_id=user_id, namespace=""))
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
        channel = _GrpcChannel(endpoint=_resolve_endpoint(), secret=_load_secret())
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
        channel = _GrpcChannel(endpoint=_resolve_endpoint(), secret=_load_secret())
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

    print("Usage: hermes libravdb <status|health|search|flush|export|journal|dream-promote>")


def register_cli(subparser) -> None:
    subparser.description = "Manage the LibraVDB Hermes memory provider"
    subs = subparser.add_subparsers(dest="libravdb_subcommand")

    status = subs.add_parser("status", help="Show LibraVDB daemon status")
    status.add_argument("--deep", action="store_true", help="Probe all collections and report per-collection document count and index health")
    status.add_argument("--index", action="store_true", help="Rebuild the index before running status")
    status.set_defaults(func=libravdb_command)

    health = subs.add_parser("health", help="Check daemon health")
    health.set_defaults(func=libravdb_command)

    search = subs.add_parser("search", help="Search memory")
    search.add_argument("query", help="Semantic search query")
    search.add_argument("--limit", help="Maximum results")
    search.add_argument("--json", action="store_true", help="Print structured JSON")
    search.set_defaults(func=libravdb_command)

    flush = subs.add_parser("flush", help="Wipe all data for a given user-id namespace")
    flush.add_argument("--user-id", required=True, help="User ID namespace to wipe")
    flush.set_defaults(func=libravdb_command)

    export = subs.add_parser("export", help="Export all memories for a user-id as NDJSON to stdout")
    export.add_argument("--user-id", required=True, help="User ID namespace to export")
    export.set_defaults(func=libravdb_command)

    journal = subs.add_parser("journal", help="Print the lifecycle journal for a session")
    journal.add_argument("--session-id", required=True, help="Session ID to journal")
    journal.add_argument("--limit", default=50, help="Maximum number of entries to return")
    journal.set_defaults(func=libravdb_command)

    dream_promote = subs.add_parser("dream-promote", help="Promote dream/diary entries into user memory collection")
    dream_promote.add_argument("--user-id", required=True, help="User ID namespace")
    dream_promote.add_argument("--dream-file", required=True, help="Path to dream/diary file")
    dream_promote.set_defaults(func=libravdb_command)