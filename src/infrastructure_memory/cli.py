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
        description="Local infrastructure memory; operator actions are CLI-only.",
        epilog="Run %(prog)s COMMAND --help for command-specific options.",
    )
    root.add_argument(
        "--config",
        help="JSON configuration (default: INFRA_MEMORY_CONFIG or /etc/infrastructure-memory.json)",
    )
    commands = root.add_subparsers(
        dest="command", title="available commands", metavar="COMMAND"
    )
    for name, help_text in (
        ("serve", "Run the local memory daemon"),
        ("mcp", "Run the MCP server over stdio"),
        ("status", "Show daemon status and memory statistics"),
        ("key-init", "Initialize the operator signing key"),
        ("fetch-model", "Download the configured embedding model"),
    ):
        commands.add_parser(name, help=help_text)
    trust = commands.add_parser(
        "trust-key",
        help="Trust an operator's public key after verifying its fingerprint",
    )
    trust.add_argument("public_key")
    search = commands.add_parser("search", help="Search infrastructure memories")
    search.add_argument("query")
    search.add_argument("--verified-only", action="store_true")
    search.add_argument("--source", default="memory", help="memory, repo:NAME, or all")
    get = commands.add_parser("get", help="Show a memory and its current revisions")
    get.add_argument("memory_id")
    unverified = commands.add_parser(
        "list-unverified", help="List current unverified memory revisions"
    )
    unverified.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Maximum revisions per page (1-100; default: 20)",
    )
    unverified.add_argument(
        "--offset", type=int, default=0, help="Starting position (default: 0)"
    )
    proposal = commands.add_parser(
        "propose", help="Read proposal parameters from a JSON file"
    )
    proposal.add_argument("file", type=Path)
    approve = commands.add_parser(
        "approve", help="Approve an explicit revision after reviewing it"
    )
    approve.add_argument("memory_id")
    approve.add_argument("revision")
    for name, help_text in (
        ("forget", "Delete a memory after reviewing its current heads"),
        ("resolve", "Resolve competing revisions using a JSON file"),
    ):
        command = commands.add_parser(name, help=help_text)
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
    root = parser()
    args = root.parse_args()
    if args.command is None:
        root.print_help()
        return 0
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
        elif command == "list-unverified":
            result = request(
                config,
                "list_unverified",
                {"limit": args.limit, "offset": args.offset},
            )
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
