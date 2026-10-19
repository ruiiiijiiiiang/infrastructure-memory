"""Opt-in local benchmark. Uses temporary records and a separately downloaded model."""

import argparse
import json
import resource
import tempfile
import time
from pathlib import Path

from infrastructure_memory.config import Config
from infrastructure_memory.events import new_event
from infrastructure_memory.search import Search
from infrastructure_memory.store import Store


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--config",
        required=True,
        help="Configuration pointing at an existing model cache",
    )
    parser.add_argument(
        "--repository",
        type=Path,
        help="Optional approved local repository to benchmark",
    )
    args = parser.parse_args()
    config = Config.load(args.config)
    with tempfile.TemporaryDirectory(
        prefix="infrastructure-memory-benchmark-"
    ) as temporary:
        root = Path(temporary)
        config.records, config.state, config.socket = (
            root / "records",
            root / "state",
            root / "socket",
        )
        config.semantic = True
        config.repositories = []
        if args.repository:
            config.repositories = [
                {
                    "name": "nixos-config",
                    "path": str(args.repository),
                    "include": [
                        "hosts/*.nix",
                        "lib/consts.nix",
                        "modules/services/*.nix",
                    ],
                }
            ]
        store = Store(config)
        try:
            fixtures = [
                (
                    "DNS gateway incident",
                    "The gateway resolver stopped answering queries. Inspect vm-network DNS service logs.",
                ),
                (
                    "Image upload incident",
                    "Immich image ingestion failed. Check database availability and storage permissions.",
                ),
                (
                    "Backup decision",
                    "Restic backup destinations must be mounted before scheduled backups can complete.",
                ),
            ]
            for title, text in fixtures:
                store.add(
                    new_event(
                        "proposal",
                        {
                            "title": title,
                            "text": text,
                            "category": "incident",
                            "hosts": [],
                            "services": [],
                            "scope": "historical",
                            "observed_at": None,
                            "evidence": [
                                "Synthetic benchmark fixture; not a real incident"
                            ],
                        },
                    )
                )
            search = Search(store)
            started = time.perf_counter()
            search.refresh_vectors(max_new=100000, seconds=600)
            ingestion = time.perf_counter() - started
            if search.error:
                raise RuntimeError(search.error)
            queries = [
                "Domain names cannot be resolved",
                "Photographs are failing to import",
                "Restic backup destinations",
            ]
            results = []
            for query in queries:
                started = time.perf_counter()
                found = search.search(query, limit=3)
                results.append(
                    {
                        "query": query,
                        "seconds": time.perf_counter() - started,
                        "titles": [item["title"] for item in found["results"]],
                        "memory_titles": [
                            item["title"]
                            for item in search.search(query, source="memory", limit=3)[
                                "results"
                            ]
                        ],
                        "all_source_titles": [
                            item["title"]
                            for item in search.search(query, source="all", limit=3)[
                                "results"
                            ]
                        ],
                    }
                )
            started = time.perf_counter()
            restarted = Search(store)
            restarted.refresh_vectors(max_new=100000, seconds=600)
            warm_ingestion = time.perf_counter() - started
            print(
                json.dumps(
                    {
                        "fixture_notice": "Synthetic examples validate plumbing; they do not measure real incident quality.",
                        "documents": store.status()["documents"],
                        "model": config.model,
                        "revision": config.revision,
                        "ingestion_seconds": ingestion,
                        "cached_reload_seconds": warm_ingestion,
                        "peak_rss_mib": resource.getrusage(
                            resource.RUSAGE_SELF
                        ).ru_maxrss
                        / 1024,
                        "queries": results,
                    },
                    indent=2,
                )
            )
        finally:
            store.close()


if __name__ == "__main__":
    main()
