from __future__ import annotations

import json
import uuid
import os
from pathlib import Path

from .provider import LibraVDBMemoryProvider, _get_hermes_home, _resolve_endpoint, _load_secret, _GrpcChannel
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


def libravdb_command(args) -> None:
    subcommand = getattr(args, "libravdb_subcommand", None)

    if subcommand == "status":
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
        session_id = f"cli-{uuid.uuid4().hex[:12]}"
        try:
            req = pb.SearchTextRequest(
                collection="session",
                text=args.query,
                k=int(args.limit or 8),
            )
            resp = channel._call("SearchText", req)
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

    print("Usage: hermes libravdb <status|health|search>")


def register_cli(subparser) -> None:
    subparser.description = "Manage the LibraVDB Hermes memory provider"
    subs = subparser.add_subparsers(dest="libravdb_subcommand")

    status = subs.add_parser("status", help="Show LibraVDB daemon status")
    status.set_defaults(func=libravdb_command)

    health = subs.add_parser("health", help="Check daemon health")
    health.set_defaults(func=libravdb_command)

    search = subs.add_parser("search", help="Search memory")
    search.add_argument("query", help="Semantic search query")
    search.add_argument("--limit", help="Maximum results")
    search.add_argument("--json", action="store_true", help="Print structured JSON")
    search.set_defaults(func=libravdb_command)