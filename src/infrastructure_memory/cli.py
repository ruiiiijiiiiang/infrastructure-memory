import argparse
import json
import os
import sys
from pathlib import Path

from .config import Config
from .events import init_key, new_event, publish, sign_event, trust_key
from .server import request, run_mcp, serve


def parser():
    root = argparse.ArgumentParser(
        description="Local infrastructure memory; operator actions are CLI-only."
    )
    root.add_argument(
        "--config",
        help="JSON configuration (default: INFRA_MEMORY_CONFIG or /etc/infrastructure-memory.json)",
    )
    commands = root.add_subparsers(dest="command", required=True)
    for name in ("serve", "mcp", "status", "key-init", "fetch-model"):
        commands.add_parser(name)
    trust = commands.add_parser(
        "trust-key",
        help="Trust an operator's public key after verifying its fingerprint",
    )
    trust.add_argument("public_key")
    search = commands.add_parser("search")
    search.add_argument("query")
    search.add_argument("--verified-only", action="store_true")
    search.add_argument("--source", default="memory", help="memory, repo:NAME, or all")
    get = commands.add_parser("get")
    get.add_argument("memory_id")
    proposal = commands.add_parser(
        "propose", help="Read proposal parameters from a JSON file"
    )
    proposal.add_argument("file", type=Path)
    approve = commands.add_parser(
        "approve", help="Approve an explicit revision after reviewing it"
    )
    approve.add_argument("memory_id")
    approve.add_argument("revision")
    for name in ("forget", "resolve"):
        command = commands.add_parser(name)
        command.add_argument("memory_id")
        command.add_argument(
            "--heads",
            nargs="+",
            required=True,
            help="Exact heads reviewed by the operator",
        )
        if name == "resolve":
            command.add_argument(
                "file", type=Path, help="JSON content for the resolved memory"
            )
    return root


def main():
    args = parser().parse_args()
    os.umask(0o077)
    try:
        config = Config.load(args.config)
        command = args.command
        if command == "serve":
            return serve(config)
        if command == "mcp":
            return run_mcp(config)
        if command == "fetch-model":
            from .embedding import fetch_model

            result = {"model_path": fetch_model(config)}
        elif command == "key-init":
            result = {
                "public_key": init_key(config.keys),
                "notice": "Verify and trust this public key separately on the other workstation.",
            }
        elif command == "trust-key":
            result = {"trusted_fingerprint": trust_key(config.keys, args.public_key)}
        elif command == "status":
            result = request(config, "memory_status")
        elif command == "search":
            result = request(
                config,
                "search_memory",
                {
                    "query": args.query,
                    "verified_only": args.verified_only,
                    "source": args.source,
                },
            )
        elif command == "get":
            result = request(config, "get_memory", {"memory_id": args.memory_id})
        elif command == "propose":
            result = request(
                config, "propose_memory", json.loads(args.file.read_text())
            )
        else:
            memory = request(config, "get_memory", {"memory_id": args.memory_id})
            if not memory:
                raise ValueError("unknown memory")
            current = {head["id"] for head in memory["heads"]}
            if command == "approve":
                if args.revision not in current:
                    raise ValueError(
                        "revision is not a current head; review current memory first"
                    )
                event = new_event(
                    "approve", memory_id=args.memory_id, parents=[args.revision]
                )
            else:
                if set(args.heads) != current:
                    raise ValueError(
                        "heads changed; review the current memory before retrying"
                    )
                content = (
                    json.loads(args.file.read_text()) if command == "resolve" else None
                )
                event = new_event(
                    "resolve" if command == "resolve" else "delete",
                    content,
                    args.memory_id,
                    args.heads,
                )
            signed = sign_event(
                event,
                config.keys,
                {
                    head["id"]: {
                        key: value for key, value in head.items() if key != "verified"
                    }
                    for head in memory["heads"]
                },
            )
            publish(config.records, signed)
            result = {
                "event_id": signed["id"],
                "memory_id": signed["memory_id"],
                "notice": "Published; local and peer services import on their next reconciliation.",
            }
        print(json.dumps(result, indent=2, ensure_ascii=False))
    except (OSError, ValueError, TypeError, KeyError) as exc:
        print(f"infra-memory: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
