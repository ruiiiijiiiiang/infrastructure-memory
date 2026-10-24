"""Deterministic event projection and incremental, disposable search state."""

import fnmatch
import hashlib
import json
import re
import sqlite3
import subprocess
import time
from collections import defaultdict

from .config import private_dir
from .events import MAX_BYTES, canonical, publish, validate, verify_event


def digest(value):
    return hashlib.sha256(value).hexdigest()


def chunks(text, size=900):
    """Line-aware source slices. Embeddings further split at tokenizer limits."""
    lines = text.splitlines(keepends=True)
    pending, start, length = [], 1, 0
    for number, line in enumerate(lines, 1):
        if pending and length + len(line) > size:
            yield start, "".join(pending)
            pending, start, length = [], number, 0
        # Split pathological long lines too; citations retain their original line.
        while len(line) > size:
            if pending:
                yield start, "".join(pending)
                pending, length = [], 0
            yield number, line[:size]
            line = line[size:]
            start = number
        if not pending:
            start = number
        pending.append(line)
        length += len(line)
    if pending:
        yield start, "".join(pending)


class Store:
    def __init__(self, config):
        self.config = config
        private_dir(config.state)
        self.db = sqlite3.connect(config.state / "index.sqlite3")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS documents (id TEXT PRIMARY KEY, data TEXT NOT NULL);
            CREATE VIRTUAL TABLE IF NOT EXISTS lexical USING fts5(id UNINDEXED, title, text);
            CREATE TABLE IF NOT EXISTS vectors (
                fingerprint TEXT, hash TEXT, vector BLOB NOT NULL,
                PRIMARY KEY (fingerprint, hash));
            CREATE TABLE IF NOT EXISTS observed (
                id TEXT, hash TEXT, PRIMARY KEY (id, hash));
        """)
        self.memories = {}
        self.events = {}
        self.errors = {}
        self.pending = []
        self.last_scan = None
        self._projection = None

    def close(self):
        self.db.close()

    def reconcile(self):
        candidates = defaultdict(dict)
        errors = {}
        private_dir(self.config.records)
        for path in sorted(self.config.records.glob("*.json")):
            try:
                if path.is_symlink() or path.stat().st_size > MAX_BYTES:
                    raise ValueError("symlink or oversized event")
                raw = path.read_bytes()
                if len(raw) > MAX_BYTES:
                    raise ValueError("oversized event")
                event = validate(json.loads(raw))
                # Accept Syncthing conflict copies, so ID collisions are detected.
                if path.name != f"{event['id']}.json" and not path.name.startswith(
                    f"{event['id']}.sync-conflict-"
                ):
                    raise ValueError("filename does not match event ID")
                hashed = digest(canonical(event))
                candidates[event["id"]][hashed] = event
                self.db.execute(
                    "INSERT OR IGNORE INTO observed VALUES (?, ?)",
                    (event["id"], hashed),
                )
            except Exception as exc:
                errors[path.name] = f"{type(exc).__name__}: {exc}"
        collisions = {
            row[0]
            for row in self.db.execute(
                "SELECT id FROM observed GROUP BY id HAVING count(*) > 1"
            )
        }
        available = {}
        for event_id, variants in candidates.items():
            if event_id in collisions:
                errors[event_id] = "event ID has multiple payloads; quarantined"
                continue
            event = next(iter(variants.values()))
            try:
                verify_event(event, self.config.keys)
                available[event_id] = event
            except Exception as exc:
                errors[event_id] = f"{type(exc).__name__}: {exc}"
        # Topological order is independent of filenames and arrival order.
        accepted = {}
        waiting = dict(available)
        while waiting:
            ready = sorted(
                key
                for key, event in waiting.items()
                if all(p in accepted for p in event["parents"])
            )
            if not ready:
                break
            for key in ready:
                event = waiting.pop(key)
                parents = [accepted[p] for p in event["parents"]]
                if any(
                    event["parent_hashes"].get(p["id"], digest(canonical(p)))
                    != digest(canonical(p))
                    for p in parents
                ):
                    errors[key] = "reviewed parent payload hash mismatch"
                elif any(
                    p["memory_id"] != event["memory_id"] or p["kind"] == "approve"
                    for p in parents
                ):
                    errors[key] = "parents must be revisions of the same memory"
                elif event["kind"] == "proposal" and any(
                    p["kind"] == "delete" for p in parents
                ):
                    errors[key] = (
                        "only an operator resolution can restore a deleted memory"
                    )
                elif event["kind"] == "approve" and parents[0]["kind"] == "delete":
                    errors[key] = "cannot approve a deletion as content"
                else:
                    accepted[key] = event
        self.pending = sorted(waiting)
        self.events = accepted
        grouped = defaultdict(list)
        approved = {
            e["parents"][0] for e in accepted.values() if e["kind"] == "approve"
        }
        for event in accepted.values():
            if event["kind"] != "approve":
                grouped[event["memory_id"]].append(event)
        memories = {}
        for memory_id, revisions in grouped.items():
            superseded = {p for event in revisions for p in event["parents"]}
            heads = sorted(
                (e for e in revisions if e["id"] not in superseded),
                key=lambda e: e["id"],
            )
            memories[memory_id] = {
                "memory_id": memory_id,
                "conflict": len(heads) > 1,
                "deleted": bool(heads) and all(e["kind"] == "delete" for e in heads),
                "heads": [
                    {
                        **e,
                        "verified": e["id"] in approved
                        or e["kind"] in {"resolve", "delete"},
                    }
                    for e in heads
                ],
            }
        self.memories = memories
        self.errors = errors
        documents = {}
        for memory in memories.values():
            # A deletion/edit conflict suppresses the entire memory until resolved.
            if memory["deleted"] or any(e["kind"] == "delete" for e in memory["heads"]):
                continue
            for event in memory["heads"]:
                content = event["content"]
                for index, (line, text) in enumerate(chunks(content["text"])):
                    doc_id = f"memory:{event['id']}:{index}"
                    documents[doc_id] = {
                        "id": doc_id,
                        "memory_id": memory["memory_id"],
                        "revision": event["id"],
                        "title": content["title"],
                        "text": text,
                        "line": line,
                        "category": content["category"],
                        "hosts": content["hosts"],
                        "services": content["services"],
                        "verified": event["id"] in approved
                        or event["kind"] == "resolve",
                        "conflict": memory["conflict"],
                        "source": "memory",
                        "scope": content["scope"],
                        "observed_at": content["observed_at"],
                        "evidence": content["evidence"],
                        "citation": f"memory://{memory['memory_id']}/{event['id']}#L{line}",
                    }
        self._repository_documents(documents)
        previous = dict(self.db.execute("SELECT id, data FROM documents"))
        encoded = {key: canonical(value).decode() for key, value in documents.items()}
        with self.db:
            for key in previous.keys() - encoded.keys():
                self.db.execute("DELETE FROM documents WHERE id=?", (key,))
                self.db.execute("DELETE FROM lexical WHERE id=?", (key,))
            for key, value in encoded.items():
                if previous.get(key) != value:
                    self.db.execute(
                        "INSERT OR REPLACE INTO documents VALUES (?, ?)", (key, value)
                    )
                    self.db.execute("DELETE FROM lexical WHERE id=?", (key,))
                    doc = documents[key]
                    self.db.execute(
                        "INSERT INTO lexical VALUES (?, ?, ?)",
                        (key, doc["title"], doc["text"]),
                    )
        self.last_scan = time.time()

    def _repository_documents(self, documents):
        for repo in self.config.repositories:
            from pathlib import Path

            root = Path(repo["path"]).expanduser().resolve()
            name = repo["name"]
            try:

                def git(*args):
                    return subprocess.check_output(
                        ["git", "-C", str(root), *args],
                        timeout=15,
                        stderr=subprocess.DEVNULL,
                    )

                commit = git("rev-parse", "HEAD").decode().strip()
                dirty = bool(git("status", "--porcelain", "--untracked-files=no"))
                paths = git("ls-files", "-z").decode().split("\0")
                for relative in paths:
                    if not relative or not any(
                        fnmatch.fnmatchcase(relative, p) for p in repo["include"]
                    ):
                        continue
                    path = root / relative
                    parts = path.relative_to(root).parts
                    if any(
                        re.search(
                            r"secret|credential|\.age$|^\.git$|^\.env|private.*key",
                            p,
                            re.I,
                        )
                        for p in parts
                    ):
                        continue
                    # No symlinks, including symlinked parent directories.
                    if any(
                        p.is_symlink()
                        for p in [path, *list(path.parents)[: len(parts)]]
                    ):
                        continue
                    if not path.is_file() or path.stat().st_size > MAX_BYTES:
                        continue
                    try:
                        text = path.read_text()
                    except (UnicodeError, OSError):
                        continue
                    for index, (line, chunk) in enumerate(chunks(text)):
                        doc_id = f"repo:{name}:{relative}:{index}"
                        documents[doc_id] = {
                            "id": doc_id,
                            "title": f"{name}/{relative}",
                            "text": chunk,
                            "source": f"repo:{name}",
                            "path": relative,
                            "line": line,
                            "category": "configuration",
                            "hosts": [],
                            "services": [],
                            "verified": False,
                            "conflict": False,
                            "scope": "intended",
                            "commit": commit,
                            "dirty": dirty,
                            "citation": f"{path}:{line}",
                        }
            except (OSError, subprocess.SubprocessError, KeyError) as exc:
                self.errors[f"repo:{name}"] = str(exc)

    def add(self, event):
        verify_event(event, self.config.keys)
        if event["parents"]:
            for parent in event["parents"]:
                if (
                    parent not in self.events
                    or self.events[parent]["memory_id"] != event["memory_id"]
                ):
                    raise ValueError("unknown parent or different memory")
        publish(self.config.records, event)
        self.reconcile()
        if event["id"] not in self.events:
            raise ValueError(
                self.errors.get(event["id"], "event could not be materialized")
            )
        return {
            "event_id": event["id"],
            "memory_id": event["memory_id"],
            "kind": event["kind"],
        }

    def documents(self):
        return {
            key: json.loads(data)
            for key, data in self.db.execute("SELECT id, data FROM documents")
        }

    def list_unverified(self, limit=20, offset=0):
        if (
            type(limit) is not int
            or not 1 <= limit <= 100
            or type(offset) is not int
            or offset < 0
        ):
            raise ValueError("limit must be 1-100 and offset must be non-negative")
        matches = sorted(
            (
                (memory, head)
                for memory in self.memories.values()
                if not memory["deleted"]
                for head in memory["heads"]
                if not head["verified"] and head["kind"] != "delete"
            ),
            key=lambda item: (item[0]["memory_id"], item[1]["id"]),
        )
        results, size = [], 0
        for memory, head in matches[offset : offset + limit]:
            entry = {
                "memory_id": memory["memory_id"],
                "revision": head["id"],
                "conflict": memory["conflict"],
                "verified": False,
                **head["content"],
            }
            entry_size = len(json.dumps(entry).encode())
            # Keep pages below the RPC response cap without truncating content.
            # One maximum-size event still fits, even with JSON Unicode escaping.
            if results and size + entry_size > 4 * MAX_BYTES:
                break
            results.append(entry)
            size += entry_size
        next_offset = offset + len(results)
        return {
            "results": results,
            "total": len(matches),
            "next_offset": next_offset if next_offset < len(matches) else None,
        }

    def status(self):
        return {
            "memories": len(self.memories),
            "events": len(self.events),
            "documents": self.db.execute("SELECT count(*) FROM documents").fetchone()[
                0
            ],
            "conflicts": sorted(k for k, v in self.memories.items() if v["conflict"]),
            "pending_events": self.pending,
            "quarantined": self.errors,
            "last_scan": self.last_scan,
            "freshness": "Local import state only; peer delivery is managed by Syncthing.",
        }
