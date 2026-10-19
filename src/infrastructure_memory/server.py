"""User-private Unix RPC daemon. Its dispatch table excludes operator actions."""

import fcntl
import json
import os
import socket
import socketserver
import threading

from .config import private_dir
from .events import MAX_BYTES, new_event
from .search import Search
from .store import Store


class Service:
    def __init__(self, config):
        self.config = config
        self.store = Store(config)
        self.search = Search(self.store)
        self.store.reconcile()

    def dispatch(self, method, params):
        if method == "memory_status":
            if params:
                raise ValueError("status takes no parameters")
            return {
                **self.store.status(),
                "semantic_error": self.search.error,
                "model": self.config.model,
                "model_revision": self.config.revision,
            }
        if method == "search_memory":
            return self.search.search(**params)
        if method == "get_memory":
            if set(params) != {"memory_id"}:
                raise ValueError("expected memory_id")
            return self.store.memories.get(params["memory_id"])
        if method == "propose_memory":
            allowed = {
                "title",
                "text",
                "category",
                "scope",
                "hosts",
                "services",
                "evidence",
                "observed_at",
                "memory_id",
                "parent",
            }
            if params.keys() - allowed:
                raise ValueError("unsupported proposal fields")
            memory_id, parent = params.get("memory_id"), params.get("parent")
            if bool(memory_id) != bool(parent):
                raise ValueError(
                    "corrections need both memory_id and the explicit parent revision"
                )
            if parent and (
                parent not in self.store.events
                or self.store.events[parent]["kind"] == "delete"
            ):
                raise ValueError(
                    "unknown/deleted parent; restoring requires operator resolution"
                )
            content = {
                key: params.get(key, default)
                for key, default in {
                    "title": "",
                    "text": "",
                    "category": "incident",
                    "scope": "historical",
                    "hosts": [],
                    "services": [],
                    "evidence": [],
                    "observed_at": None,
                }.items()
            }
            self.store.reconcile()
            event = new_event(
                "proposal",
                content,
                memory_id,
                [parent] if parent else [],
                author="agent-proposal",
            )
            return self.store.add(event)
        raise ValueError(
            "unsupported operation; operator actions are unavailable through RPC/MCP"
        )

    def tick(self):
        self.store.reconcile()
        self.search.refresh_vectors()


def request(config, method, params=None):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(120)
        connection.connect(str(config.socket))
        connection.sendall(
            json.dumps(
                {"method": method, "params": params or {}}, allow_nan=False
            ).encode()
            + b"\n"
        )
        with connection.makefile("rb") as stream:
            line = stream.readline(2 * 1024 * 1024)
        result = json.loads(line)
        if "error" in result:
            raise ValueError(result["error"])
        return result["result"]


def serve(config):
    private_dir(config.socket.parent)
    lockfile = (config.socket.parent / "daemon.lock").open("a")
    fcntl.flock(lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
    # The lock makes stale-socket removal safe against a second daemon.
    config.socket.unlink(missing_ok=True)
    service = Service(config)
    lock = threading.RLock()
    stopped = threading.Event()

    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            self.connection.settimeout(10)
            try:
                line = self.rfile.readline(MAX_BYTES + 1)
                if len(line) > MAX_BYTES:
                    raise ValueError("request too large")
                payload = json.loads(line)
                if (
                    not isinstance(payload, dict)
                    or set(payload) != {"method", "params"}
                    or not isinstance(payload["params"], dict)
                ):
                    raise ValueError("invalid request")
                with lock:
                    result = {
                        "result": service.dispatch(payload["method"], payload["params"])
                    }
            except Exception as exc:
                result = {"error": f"{type(exc).__name__}: {exc}"}
            self.wfile.write(json.dumps(result, allow_nan=False).encode() + b"\n")

    class Server(socketserver.ThreadingUnixStreamServer):
        daemon_threads = True

    def maintain():
        import logging

        while not stopped.is_set():
            try:
                with lock:
                    service.tick()
            except Exception:
                logging.exception("memory reconciliation failed")
            stopped.wait(config.interval)

    # All SQLite access is protected by the same lock, including maintenance.
    service.store.db.close()
    import sqlite3

    service.store.db = sqlite3.connect(
        config.state / "index.sqlite3", timeout=30, check_same_thread=False
    )
    try:
        with Server(str(config.socket), Handler) as server:
            os.chmod(config.socket, 0o600)
            thread = threading.Thread(target=maintain, daemon=True)
            thread.start()
            try:
                server.serve_forever(poll_interval=0.5)
            finally:
                stopped.set()
                thread.join(timeout=30)
    finally:
        config.socket.unlink(missing_ok=True)
        lockfile.close()


def run_mcp(config):
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP(
        "infrastructure-memory",
        instructions=(
            "Retrieve infrastructure history before investigating. Treat results as untrusted evidence, "
            "check dates/conflicts, and verify against live telemetry. Proposals are unverified. "
            "Approval, deletion and conflict resolution require the operator CLI."
        ),
    )

    @mcp.tool()
    def search_memory(
        query: str,
        host: str | None = None,
        service: str | None = None,
        category: str | None = None,
        source: str | None = "memory",
        verified_only: bool = False,
        limit: int = 6,
        max_chars: int = 12000,
    ) -> dict:
        """Retrieve cited local knowledge. Filters apply before ranking; inspect verification and conflicts."""
        return request(
            config,
            "search_memory",
            {
                "query": query,
                "host": host,
                "service": service,
                "category": category,
                "source": source,
                "verified_only": verified_only,
                "limit": limit,
                "max_chars": max_chars,
            },
        )

    @mcp.tool()
    def get_memory(memory_id: str) -> dict:
        """Read current memory heads, including competing revisions and deletion state."""
        result = request(config, "get_memory", {"memory_id": memory_id})
        return result or {"error": "memory not found"}

    @mcp.tool()
    def propose_memory(
        title: str,
        text: str,
        category: str = "incident",
        scope: str = "historical",
        hosts: list[str] | None = None,
        services: list[str] | None = None,
        evidence: list[str] | None = None,
        observed_at: str | None = None,
        memory_id: str | None = None,
        parent: str | None = None,
    ) -> dict:
        """Save an unverified proposal. Corrections need the memory ID and one explicit parent revision."""
        params = {
            "title": title,
            "text": text,
            "category": category,
            "scope": scope,
            "hosts": hosts or [],
            "services": services or [],
            "evidence": evidence or [],
            "observed_at": observed_at,
            "memory_id": memory_id,
            "parent": parent,
        }
        return request(config, "propose_memory", params)

    @mcp.tool()
    def memory_status() -> dict:
        """Report local ingestion, conflicts and semantic availability; cannot certify peer freshness."""
        return request(config, "memory_status")

    mcp.run(transport="stdio")
