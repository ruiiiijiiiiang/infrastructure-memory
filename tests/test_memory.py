import asyncio
import copy
import itertools
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import numpy as np

from infrastructure_memory.config import Config
from infrastructure_memory.events import (
    canonical,
    init_key,
    new_event,
    publish,
    sign_event,
    trust_key,
)
from infrastructure_memory.search import Search
from infrastructure_memory.server import Service, request
from infrastructure_memory.store import Store


def content(text="Router DNS resolution failed after the gateway restarted.", **kwargs):
    return {
        "title": "DNS incident",
        "text": text,
        "category": "incident",
        "hosts": ["vm-network"],
        "services": ["dns"],
        "evidence": ["Grafana query, observed during investigation"],
        "scope": "historical",
        "observed_at": "2026-10-19T10:00:00-05:00",
        **kwargs,
    }


class MemoryTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.config = self.make_config("a")
        self.store = Store(self.config)

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def make_config(self, name):
        root = self.root / name
        return Config(
            root / "shared/records",
            root / "state",
            root / "cache",
            root / "run/service.sock",
            semantic=False,
            interval=1,
        )

    def add(self, text="router DNS failure", parent=None, memory_id=None):
        event = new_event(
            "proposal",
            content(text),
            memory_id,
            [parent] if parent else [],
            author="test",
        )
        self.store.add(event)
        return event

    def operator(self, kind, event, replacement=None, parents=None):
        init_key(self.config.keys)
        action = sign_event(
            new_event(kind, replacement, event["memory_id"], parents or [event["id"]]),
            self.config.keys,
            self.store.events,
        )
        self.store.add(action)
        return action

    def test_list_unverified_current_revisions_and_full_content(self):
        self.assertEqual(
            {"results": [], "total": 0, "next_offset": None},
            self.store.list_unverified(),
        )
        original = self.add("Full incident details.\n" * 100)
        self.assertEqual(
            [
                {
                    "memory_id": original["memory_id"],
                    "revision": original["id"],
                    "conflict": False,
                    "verified": False,
                    **original["content"],
                }
            ],
            self.store.list_unverified()["results"],
        )
        self.operator("approve", original)
        self.assertEqual([], self.store.list_unverified()["results"])
        correction = self.add(
            "corrected details", original["id"], original["memory_id"]
        )
        self.assertEqual(
            [correction["id"]],
            [entry["revision"] for entry in self.store.list_unverified()["results"]],
        )
        self.operator("resolve", correction, content("reviewed resolution"))
        self.assertEqual([], self.store.list_unverified()["results"])
        deleted = self.add("deleted incident")
        self.operator("delete", deleted)
        self.assertEqual([], self.store.list_unverified()["results"])

    def test_list_unverified_competing_heads_and_deletion_conflict(self):
        original = self.add()
        left = self.add("left", original["id"], original["memory_id"])
        right = self.add("right", original["id"], original["memory_id"])
        page = self.store.list_unverified()
        self.assertEqual(2, page["total"])
        self.assertEqual(
            sorted([left["id"], right["id"]]),
            [entry["revision"] for entry in page["results"]],
        )
        self.assertTrue(all(entry["conflict"] for entry in page["results"]))
        self.operator("approve", left)
        self.assertEqual(
            [right["id"]],
            [entry["revision"] for entry in self.store.list_unverified()["results"]],
        )
        self.operator("delete", right, parents=[left["id"], right["id"]])
        self.assertEqual([], self.store.list_unverified()["results"])
        offline = self.add("offline edit", original["id"], original["memory_id"])
        page = self.store.list_unverified()
        self.assertEqual(1, page["total"])
        self.assertEqual(offline["id"], page["results"][0]["revision"])
        self.assertTrue(page["results"][0]["conflict"])

    def test_list_unverified_pagination(self):
        events = [self.add(f"incident {number}") for number in range(5)]
        expected = sorted((event["memory_id"], event["id"]) for event in events)
        actual = []
        for offset, next_offset in ((0, 2), (2, 4), (4, None)):
            page = self.store.list_unverified(limit=2, offset=offset)
            self.assertEqual(5, page["total"])
            self.assertEqual(next_offset, page["next_offset"])
            actual.extend(
                (entry["memory_id"], entry["revision"]) for entry in page["results"]
            )
        self.assertEqual(expected, actual)
        self.assertEqual(
            {"results": [], "total": 5, "next_offset": None},
            self.store.list_unverified(offset=10),
        )

    def test_list_unverified_large_unicode_pages_preserve_full_content(self):
        text = "\U0001f642" * 50000
        events = [self.add(text) for _ in range(3)]
        revisions, offset = [], 0
        while offset is not None:
            page = self.store.list_unverified(limit=100, offset=offset)
            self.assertEqual(1, len(page["results"]))
            self.assertLess(len(json.dumps({"result": page}).encode()), 2 * 1024 * 1024)
            self.assertEqual(text, page["results"][0]["text"])
            revisions.append(page["results"][0]["revision"])
            offset = page["next_offset"]
        self.assertCountEqual([event["id"] for event in events], revisions)

    def test_list_unverified_rejects_invalid_pagination(self):
        for params in (
            {"limit": 0},
            {"limit": 101},
            {"limit": True},
            {"limit": "2"},
            {"limit": 1.5},
            {"offset": -1},
            {"offset": True},
            {"offset": "0"},
            {"offset": 0.5},
        ):
            with self.subTest(params=params), self.assertRaises(ValueError):
                self.store.list_unverified(**params)

    def test_reimport_and_rebuild(self):
        event = self.add()
        first = copy.deepcopy(self.store.memories)
        self.store.reconcile()
        self.assertEqual(first, self.store.memories)
        second_config = self.make_config("rebuilt")
        second_config.records = self.config.records
        with_store = Store(second_config)
        try:
            with_store.reconcile()
            self.assertEqual(first, with_store.memories)
            self.assertEqual(1, with_store.status()["events"])
            self.assertIn(event["memory_id"], with_store.memories)
        finally:
            with_store.close()

    def test_pending_parent_arrival(self):
        parent = new_event("proposal", content(), author="a")
        child = new_event(
            "proposal", content("corrected"), parent["memory_id"], [parent["id"]]
        )
        publish(self.config.records, child)
        self.store.reconcile()
        self.assertEqual([child["id"]], self.store.pending)
        self.assertFalse(self.store.memories)
        publish(self.config.records, parent)
        self.store.reconcile()
        self.assertFalse(self.store.pending)
        self.assertEqual(
            child["id"], self.store.memories[parent["memory_id"]]["heads"][0]["id"]
        )

    def test_concurrent_edit_resolution_and_stale_revision(self):
        original = self.add()
        left = self.add("left", original["id"], original["memory_id"])
        right = self.add("right", original["id"], original["memory_id"])
        self.assertTrue(self.store.memories[original["memory_id"]]["conflict"])
        resolved = self.operator(
            "resolve", original, content("merged"), [left["id"], right["id"]]
        )
        memory = self.store.memories[original["memory_id"]]
        self.assertFalse(memory["conflict"])
        self.assertTrue(memory["heads"][0]["verified"])
        stale = self.add("late offline edit", original["id"], original["memory_id"])
        self.assertTrue(self.store.memories[original["memory_id"]]["conflict"])
        self.assertEqual(
            {resolved["id"], stale["id"]},
            {e["id"] for e in self.store.memories[original["memory_id"]]["heads"]},
        )

    def test_arrival_permutations_converge(self):
        original = new_event("proposal", content(), author="a")
        left = new_event(
            "proposal", content("left"), original["memory_id"], [original["id"]]
        )
        right = new_event(
            "proposal", content("right"), original["memory_id"], [original["id"]]
        )
        expected = None
        for number, order in enumerate(itertools.permutations([original, left, right])):
            store = Store(self.make_config(f"order-{number}"))
            try:
                for event in order:
                    publish(store.config.records, event)
                    store.reconcile()
                if expected is None:
                    expected = store.memories
                self.assertEqual(expected, store.memories)
            finally:
                store.close()

    def test_signature_trust_and_tamper(self):
        event = self.add()
        public = init_key(self.config.keys)
        approval = self.operator("approve", event)
        peer = Store(self.make_config("peer"))
        try:
            for item in (event, approval):
                publish(peer.config.records, item)
            peer.reconcile()
            self.assertFalse(peer.memories[event["memory_id"]]["heads"][0]["verified"])
            self.assertIn(approval["id"], peer.errors)
            trust_key(peer.config.keys, public)
            peer.reconcile()
            self.assertTrue(peer.memories[event["memory_id"]]["heads"][0]["verified"])
            tampered = copy.deepcopy(approval)
            tampered["host"] = "forged-host"
            # A different payload under the same ID is quarantined, even after replacement.
            path = peer.config.records / f"{approval['id']}.json"
            path.write_bytes(canonical(tampered))
            peer.reconcile()
            self.assertIn("multiple payloads", peer.errors[approval["id"]])
        finally:
            peer.close()

    def test_unsigned_privileged_event_rejected(self):
        event = self.add()
        forged = new_event(
            "approve", memory_id=event["memory_id"], parents=[event["id"]]
        )
        self.config.records.joinpath(f"{forged['id']}.json").write_text(
            json.dumps(forged)
        )
        self.store.reconcile()
        self.assertIn(forged["id"] + ".json", self.store.errors)
        self.assertFalse(
            self.store.memories[event["memory_id"]]["heads"][0]["verified"]
        )

    def test_approval_binds_reviewed_payload_after_clean_rebuild(self):
        original = self.add()
        approval = self.operator("approve", original)
        altered = copy.deepcopy(original)
        altered["content"]["text"] = (
            "An altered proposal must not inherit the old approval."
        )
        peer = Store(self.make_config("fresh-peer"))
        try:
            public = init_key(self.config.keys)
            trust_key(peer.config.keys, public)
            publish(peer.config.records, altered)
            publish(peer.config.records, approval)
            peer.reconcile()
            self.assertFalse(
                peer.memories[original["memory_id"]]["heads"][0]["verified"]
            )
            self.assertIn("payload hash mismatch", peer.errors[approval["id"]])
        finally:
            peer.close()

    def test_deletion_and_offline_edit_suppresses_search(self):
        original = self.add()
        deletion = self.operator("delete", original)
        self.assertTrue(self.store.memories[original["memory_id"]]["deleted"])
        self.add("router DNS offline edit", original["id"], original["memory_id"])
        memory = self.store.memories[original["memory_id"]]
        self.assertTrue(memory["conflict"])
        self.assertEqual([], Search(self.store).search("router")["results"])
        restore = new_event(
            "proposal", content("restore"), original["memory_id"], [deletion["id"]]
        )
        publish(self.config.records, restore)
        self.store.reconcile()
        self.assertIn(restore["id"], self.store.errors)

    def test_event_collision_and_malformed_input_do_not_stop_ingestion(self):
        event = self.add()
        changed = copy.deepcopy(event)
        changed["content"]["text"] = "collision"
        path = self.config.records / f"{event['id']}.sync-conflict-20261019-host.json"
        path.write_bytes(canonical(changed))
        self.config.records.joinpath("bad.json").write_text("{")
        second = self.add("another incident")
        self.assertIn(second["memory_id"], self.store.memories)
        self.assertNotIn(event["memory_id"], self.store.memories)
        self.assertIn(event["id"], self.store.errors)
        self.assertIn("bad.json", self.store.errors)

    def test_cross_memory_and_cycle_parents_rejected(self):
        event = self.add()
        unrelated = new_event("proposal", content(), parents=[event["id"]])
        publish(self.config.records, unrelated)
        a, b = new_event("proposal", content()), new_event("proposal", content())
        b["memory_id"] = a["memory_id"]
        a["parents"], b["parents"] = [b["id"]], [a["id"]]
        for item in (a, b):
            publish(self.config.records, item)
        self.store.reconcile()
        self.assertIn(unrelated["id"], self.store.errors)
        self.assertEqual({a["id"], b["id"]}, set(self.store.pending))

    def test_operator_actions_not_in_rpc(self):
        service = Service(self.make_config("service"))
        try:
            for method in (
                "approve_memory",
                "resolve_memory",
                "forget_memory",
                "approve",
                "trust_key",
            ):
                with self.assertRaises(ValueError):
                    service.dispatch(method, {})
            with self.assertRaises(ValueError):
                service.dispatch(
                    "propose_memory", {"title": "bad", "text": "bad", "verified": True}
                )
        finally:
            service.store.close()

    def test_filters_exact_identifiers_and_sql_syntax(self):
        event = self.add(
            "podman-librechat-rag failed with custom.services.apps.ai.memory.enable"
        )
        search = Search(self.store)
        self.assertEqual(
            event["id"], search.search("podman-librechat-rag")["results"][0]["revision"]
        )
        self.assertEqual([], search.search("podman", host="desktop")["results"])
        self.assertEqual([], search.search("podman", verified_only=True)["results"])
        self.operator("approve", event)
        self.assertEqual(1, len(search.search("podman", verified_only=True)["results"]))
        self.assertEqual([], search.search('" OR 1=1 -- *')["results"])

    def test_context_budget_and_chunk_deduplication(self):
        self.add("DNS router failure.\n" * 500)
        results = Search(self.store).search("DNS", max_chars=200)["results"]
        self.assertEqual(1, len(results))
        self.assertLessEqual(sum(len(item["text"]) for item in results), 200)

    def test_semantic_cache_and_fusion(self):
        self.config.semantic = True
        self.add("Names cannot be resolved by the gateway")

        class FakeEmbedder:
            fingerprint = "deterministic-test-embedder"
            calls = 0

            def load(self):
                pass

            def encode(self, texts):
                self.calls += len(texts)
                return np.asarray([[1.0, 0.0] for _ in texts], dtype=np.float32)

        fake = FakeEmbedder()
        search = Search(self.store, fake)
        search.refresh_vectors()
        self.assertEqual(1, fake.calls)
        search.refresh_vectors()
        self.assertEqual(1, fake.calls)
        self.assertEqual(1, len(search.search("DNS outage")["results"]))
        other = Search(self.store, fake)
        other.refresh_vectors()
        self.assertEqual(
            2, fake.calls
        )  # Only the query added a call; reindex used cached vectors.

    def test_model_failure_preserves_lexical_search(self):
        self.config.semantic = True
        self.add()

        class BrokenEmbedder:
            def load(self):
                raise OSError("model unavailable")

        search = Search(self.store, BrokenEmbedder())
        search.refresh_vectors()
        self.assertIn("model unavailable", search.error)
        self.assertEqual(1, len(search.search("DNS")["results"]))

    def test_vector_backlog_is_bounded_and_filters_apply_first(self):
        self.config.semantic = True
        first = self.add("gateway DNS one")
        second = self.add("gateway DNS two")

        class FakeEmbedder:
            fingerprint = "bounded-test-embedder"

            def load(self):
                pass

            def encode(self, texts):
                return np.asarray([[1.0, 0.0] for _ in texts], dtype=np.float32)

        search = Search(self.store, FakeEmbedder())
        search.refresh_vectors(max_new=1)
        self.assertEqual(1, len(search.ids))
        self.assertIn("backlog", search.error)
        search.refresh_vectors(max_new=1)
        self.assertEqual(2, len(search.ids))
        self.assertIsNone(search.error)
        self.operator("approve", second)
        found = search.search("DNS", verified_only=True, limit=1)["results"]
        self.assertEqual(second["id"], found[0]["revision"])
        self.assertNotEqual(first["id"], found[0]["revision"])

    def test_explicit_missing_configuration_fails_closed(self):
        with self.assertRaises(FileNotFoundError):
            Config.load(self.root / "nonexistent.json")

    def test_repository_scope_dirty_provenance_and_removal(self):
        repo = self.root / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        (repo / "hosts").mkdir()
        (repo / "secrets").mkdir()
        (repo / "hosts/server.nix").write_text("service.enable = true;\n")
        (repo / "secrets/leak.md").write_text("sensitive-credential")
        (repo / "hosts/link.nix").symlink_to(repo / "secrets/leak.md")
        subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.invalid",
                "commit",
                "-qm",
                "fixture",
            ],
            check=True,
        )
        self.config.repositories = [
            {"name": "fixture", "path": str(repo), "include": ["hosts/*", "secrets/*"]}
        ]
        self.store.reconcile()
        docs = list(self.store.documents().values())
        self.assertEqual(1, len(docs))
        self.assertEqual("intended", docs[0]["scope"])
        self.assertFalse(docs[0]["dirty"])
        (repo / "hosts/server.nix").write_text("service.enable = false;\n")
        self.store.reconcile()
        self.assertTrue(next(iter(self.store.documents().values()))["dirty"])
        (repo / "hosts/server.nix").unlink()
        self.store.reconcile()
        self.assertFalse(self.store.documents())

    def test_sync_via_third_peer_without_workstation_overlap(self):
        event = self.add()
        relay = self.root / "relay"
        shutil.copytree(self.config.records, relay)
        # A's daemon can be offline while B receives from the relay.
        config_b = self.make_config("b")
        shutil.copytree(relay, config_b.records)
        store_b = Store(config_b)
        try:
            store_b.reconcile()
            self.assertIn(event["memory_id"], store_b.memories)
            b_event = new_event(
                "proposal", content("desktop incident"), author="desktop-agent"
            )
            store_b.add(b_event)
            for path in config_b.records.glob("*.json"):
                shutil.copy2(path, relay / path.name)
            for path in relay.glob("*.json"):
                if not (self.config.records / path.name).exists():
                    shutil.copy2(path, self.config.records / path.name)
            self.store.reconcile()
            self.assertEqual(store_b.memories, self.store.memories)
        finally:
            store_b.close()


class IntegrationTest(unittest.TestCase):
    def test_real_daemon_and_mcp_client(self):
        self.exercise_mcp(legacy=True)

    def test_real_daemon_and_mcp_discover_client(self):
        self.exercise_mcp(legacy=False)

    def exercise_mcp(self, legacy):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            configuration = {
                "records": str(root / "shared/records"),
                "state": str(root / "state"),
                "cache": str(root / "cache"),
                "socket": str(root / "run/service.sock"),
                "semantic": False,
                "interval": 1,
            }
            filename = root / "config.json"
            filename.write_text(json.dumps(configuration))
            config = Config.load(filename)
            args = [
                sys.executable,
                "-B",
                "-m",
                "infrastructure_memory.cli",
                "--config",
                str(filename),
            ]
            daemon = subprocess.Popen(
                [*args, "serve"], stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
            try:
                for _ in range(100):
                    if config.socket.exists():
                        break
                    if daemon.poll() is not None:
                        self.fail(daemon.communicate()[1].decode())
                    time.sleep(0.05)
                self.assertTrue(config.socket.exists())
                self.assertEqual(0o600, config.socket.stat().st_mode & 0o777)
                self.assertEqual(0, request(config, "memory_status")["events"])

                async def exercise():
                    from mcp import ClientSession, StdioServerParameters
                    from mcp.client.stdio import stdio_client

                    params = StdioServerParameters(
                        command=args[0], args=[*args[1:], "mcp"], env={**os.environ}
                    )
                    async with stdio_client(params) as (read, write):
                        async with ClientSession(read, write) as session:
                            if legacy:
                                await session.initialize()
                                self.assertEqual("2025-11-25", session.protocol_version)
                            else:
                                await session.discover()
                                self.assertEqual("2026-07-28", session.protocol_version)
                            tools = {
                                tool.name: tool
                                for tool in (await session.list_tools()).tools
                            }
                            self.assertEqual(
                                {
                                    "search_memory",
                                    "get_memory",
                                    "propose_memory",
                                    "memory_status",
                                },
                                set(tools),
                            )
                            self.assertEqual(
                                ["query"],
                                tools["search_memory"].input_schema["required"],
                            )
                            self.assertEqual(
                                ["title", "text"],
                                tools["propose_memory"].input_schema["required"],
                            )
                            result = await session.call_tool("memory_status", {})
                            self.assertFalse(result.is_error, str(result))
                            self.assertEqual(
                                json.loads(result.content[0].text),
                                result.structured_content,
                            )
                            result = await session.call_tool(
                                "search_memory", {"query": ""}
                            )
                            self.assertTrue(result.is_error, str(result))
                            self.assertIn(
                                "query must contain 1-2000 characters",
                                result.content[0].text,
                            )
                            result = await session.call_tool(
                                "propose_memory",
                                {
                                    "title": "invalid",
                                    "text": "missing parent",
                                    "memory_id": "x",
                                },
                            )
                            self.assertTrue(result.is_error, str(result))
                            self.assertIn(
                                "explicit parent revision", result.content[0].text
                            )
                            result = await session.call_tool(
                                "propose_memory",
                                {
                                    "title": "MCP fixture",
                                    "text": "DNS gateway incident",
                                },
                            )
                            self.assertFalse(result.is_error, str(result))
                            saved = json.loads(result.content[0].text)
                            result = await session.call_tool(
                                "search_memory", {"query": "DNS"}
                            )
                            self.assertFalse(result.is_error, str(result))
                            found = json.loads(result.content[0].text)
                            self.assertEqual(
                                saved["memory_id"],
                                found["results"][0]["memory_id"],
                            )
                            self.assertFalse(found["results"][0]["verified"])
                            result = await session.call_tool(
                                "get_memory", {"memory_id": saved["memory_id"]}
                            )
                            self.assertFalse(result.is_error, str(result))
                            self.assertEqual(
                                saved["memory_id"],
                                result.structured_content["memory_id"],
                            )
                            return saved

                saved = asyncio.run(exercise())
                listing = subprocess.run(
                    [*args, "list-unverified", "--limit", "1", "--offset", "0"],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                page = json.loads(listing.stdout)
                self.assertEqual(1, page["total"])
                self.assertIsNone(page["next_offset"])
                self.assertEqual(saved["memory_id"], page["results"][0]["memory_id"])
                self.assertEqual(saved["event_id"], page["results"][0]["revision"])
                self.assertEqual("DNS gateway incident", page["results"][0]["text"])
                self.assertFalse(page["results"][0]["verified"])
                empty = subprocess.run(
                    [*args, "list-unverified", "--offset", "1"],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual([], json.loads(empty.stdout)["results"])
                invalid = subprocess.run(
                    [*args, "list-unverified", "--limit", "0"],
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(1, invalid.returncode)
                self.assertIn("limit must be 1-100", invalid.stderr)
                # A separately invoked operator approves the explicit reviewed head.
                subprocess.run([*args, "key-init"], check=True, capture_output=True)
                subprocess.run(
                    [*args, "approve", saved["memory_id"], saved["event_id"]],
                    check=True,
                    capture_output=True,
                )
                for _ in range(100):
                    found = request(
                        config, "search_memory", {"query": "DNS", "verified_only": True}
                    )
                    if found["results"]:
                        break
                    time.sleep(0.05)
                self.assertEqual(1, len(found["results"]))
                self.assertEqual(
                    {"results": [], "total": 0, "next_offset": None},
                    request(config, "list_unverified"),
                )
                with self.assertRaises(ValueError):
                    request(config, "forget_memory", {"memory_id": saved["memory_id"]})
            finally:
                daemon.terminate()
                try:
                    daemon.communicate(timeout=10)
                except subprocess.TimeoutExpired:
                    daemon.kill()
                    daemon.communicate()


if __name__ == "__main__":
    unittest.main()
