import re
import time

import numpy as np

from .embedding import Embedder
from .store import digest

# Function words otherwise give unrelated documents an extra lexical vote in RRF.
STOP_WORDS = frozenset(
    "a an and are as at be been being by can cannot could did do does for from had has have how i in is it its may might my of on or our should that the their this to was were what when where which who why will with would your".split()
)


class Search:
    def __init__(self, store, embedder=None):
        self.store = store
        self.embedder = embedder or Embedder(store.config)
        self.error = "model not loaded"
        self.ids = []
        self.matrix = None
        self._hashes = None

    def refresh_vectors(self, max_new=32, seconds=2):
        if not self.store.config.semantic:
            self.error = "semantic search disabled"
            return
        docs = self.store.documents()
        pairs = [
            (key, digest((doc["title"] + "\n" + doc["text"]).encode()))
            for key, doc in docs.items()
        ]
        if pairs == self._hashes:
            return
        try:
            self.embedder.load()
            vectors, indexed_ids = [], []
            generated = 0
            started = time.monotonic()
            for key, hashed in pairs:
                row = self.store.db.execute(
                    "SELECT vector FROM vectors WHERE fingerprint=? AND hash=?",
                    (self.embedder.fingerprint, hashed),
                ).fetchone()
                if row:
                    vector = np.frombuffer(row[0], dtype=np.float32)
                else:
                    if generated >= max_new or time.monotonic() - started >= seconds:
                        continue
                    vector = self.embedder.encode(
                        [docs[key]["title"] + "\n" + docs[key]["text"]]
                    )[0]
                    self.store.db.execute(
                        "INSERT OR REPLACE INTO vectors VALUES (?, ?, ?)",
                        (
                            self.embedder.fingerprint,
                            hashed,
                            vector.astype(np.float32).tobytes(),
                        ),
                    )
                    generated += 1
                vectors.append(vector)
                indexed_ids.append(key)
            self.store.db.commit()
            self.matrix = np.asarray(vectors, dtype=np.float32) if vectors else None
            self.ids = indexed_ids
            complete = len(indexed_ids) == len(pairs)
            self._hashes = pairs if complete else None
            self.error = (
                None
                if complete
                else f"embedding backlog: {len(pairs) - len(indexed_ids)} chunks"
            )
        except Exception as exc:
            self.store.db.rollback()
            self.error = f"{type(exc).__name__}: {exc}"
            self.matrix = None
            self.ids = []

    def search(
        self,
        query,
        host=None,
        service=None,
        category=None,
        source="memory",
        verified_only=False,
        limit=6,
        max_chars=12000,
    ):
        if not isinstance(query, str) or not query.strip() or len(query) > 2000:
            raise ValueError("query must contain 1-2000 characters")
        if (
            type(limit) is not int
            or not 1 <= limit <= 20
            or type(max_chars) is not int
            or not 200 <= max_chars <= 30000
        ):
            raise ValueError("invalid result/context limit")
        docs = {
            key: doc
            for key, doc in self.store.documents().items()
            if (not host or host in doc["hosts"])
            and (not service or service in doc["services"])
            and (not category or category == doc["category"])
            and (source in (None, "all") or source == doc["source"])
            and (not verified_only or doc["verified"])
        }
        scores = {}

        def rank(ids, weight=1):
            for position, key in enumerate(ids, 1):
                scores[key] = scores.get(key, 0) + weight / (60 + position)

        terms = [
            term
            for term in re.findall(r"\w+", query, flags=re.UNICODE)
            if term.casefold() not in STOP_WORDS
        ]
        if terms:
            expression = " OR ".join(
                '"' + term.replace('"', '""') + '"' for term in terms
            )
            lexical = self.store.db.execute(
                "SELECT id FROM lexical WHERE lexical MATCH ? ORDER BY bm25(lexical)",
                (expression,),
            )
            rank([row[0] for row in lexical if row[0] in docs])
        exact = [
            key
            for key, doc in docs.items()
            if query.casefold() in (doc["title"] + "\n" + doc["text"]).casefold()
        ]
        rank(sorted(exact), weight=3)
        if self.matrix is not None:
            try:
                vector = self.embedder.encode([query])[0]
                eligible = [i for i, key in enumerate(self.ids) if key in docs]
                if eligible:
                    similarities = self.matrix[eligible] @ vector
                    order = np.argsort(-similarities, kind="stable")[:100]
                    # Broad lexical OR matches should not overwhelm a strong semantic match.
                    rank([self.ids[eligible[i]] for i in order], weight=2)
            except Exception as exc:
                self.error = f"{type(exc).__name__}: {exc}"
        results, seen, remaining = [], set(), max_chars
        for key in sorted(scores, key=lambda k: (-scores[k], k)):
            doc = docs[key]
            group = doc.get("revision", doc.get("path", doc["id"]))
            if group in seen:
                continue
            seen.add(group)
            result = dict(doc)
            result["text"] = result["text"][:remaining]
            result["score"] = scores[key]
            results.append(result)
            remaining -= len(result["text"])
            if len(results) >= limit or remaining <= 0:
                break
        return {
            "results": results,
            "semantic_error": self.error,
            "notice": "Historical/local evidence. Verify against current configuration and live telemetry.",
        }
