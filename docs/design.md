# Record and retrieval design

## Durable source and derived indexes

The durable source is one immutable JSON document per event. Each document has a schema version, event ID, memory ID, kind, parents and parent hashes, originating host/author, creation time, and content. IDs are canonical UUIDs. Record size and field validation bound input before import.

Publication writes and flushes a `.pending-*` file in the records directory, then uses a hard link to publish a unique final filename atomically without overwriting an existing record. The directory is flushed before the temporary file is removed. Startup reconciliation recovers a publication completed before local index import.

Each workstation validates/imports local events idempotently and maintains a private SQLite metadata/full-text index and vector cache. The plaintext JSON event files in `shared/records/` can optionally be synchronized by an external tool. The same reconciliation imports delivered events; transport does not need transaction-level database replication.

## Revision semantics

- Proposals create content or replace a single parent revision.
- Competing revisions remain multiple active heads; timestamps do not select a winner.
- Signed resolutions reference reviewed heads and provide replacement content.
- Signed approvals bind the exact reviewed parent payload by hash.
- Signed deletions create tombstones; a deletion/edit conflict suppresses normal retrieval until resolved.
- Missing parents remain pending. Import order does not determine the final projection.
- Duplicate IDs with different payloads are quarantined, including recognized synchronization conflict copies.
- Trusted operator public keys are configured locally and checked during reconciliation.

These controls provide an application/API boundary, not isolation from arbitrary processes sharing the operator's OS identity. Private keys and the trust store must remain outside the synchronized folder. Agent policy may delegate invocation of the approval CLI after explicit user confirmation of exact displayed content and its revision ID; the CLI handles signing internally. Direct agent access to private-key contents and agent changes to trust remain prohibited. Conversational confirmation is enforced by agent instructions, not independently authenticated by the record protocol. The [operating guide](../README.md#proposals-and-operator-review) describes review and verification.

## Retrieval

Memory content and allowlisted repository files are split into source slices with citations. Repository ingestion uses tracked files from the actual local checkout, excludes symlinks and sensitive path names, and records commit/dirty-state provenance. A checkout describes intended configuration; it is not deployment evidence.

Search combines FTS5 lexical ranking, exact identifiers, and exact cosine retrieval over normalized NumPy vectors. Rank fusion gives stronger weight to semantic matches and exact text, and filters apply before candidate ranking. Memory-only search is the default; repository sources remain independently selectable.

The default model is `ibm-granite/granite-embedding-97m-multilingual-r2`, pinned at revision `835ad14087e140460703cf0fae09f97d469d65c2`, using the official FP32 ONNX artifact on CPU. The loader reads pinned tokenizer, padding, sequence-length, and pooling metadata; it supports CLS pooling for Granite and mean pooling for an explicit MiniLM rollback. Inputs are subdivided by tokenizer offsets into at most 256 tokens including special tokens. Segment vectors are averaged and L2-normalized to produce one vector per source slice. Inference uses two intra-op threads and one inter-op thread.

Cache keys include a fingerprint of the model, revision, artifact path, token limit, and embedding pipeline version, plus the content hash. Pooling and tokenizer metadata are bound by the pinned model revision. Model or pipeline changes generate a separate derived vector cache without changing immutable records or requiring a database reset.

Reconciliation polls periodically, including after restart. Embedding work is bounded per maintenance pass. Missing model files or inference failures leave lexical retrieval available and report degraded semantic search. Adding a watcher or a more advanced vector index should follow measurements rather than changing durable record semantics.

### Embedding model benchmark (2026-10-22)

Four candidates were compared in FP32 and INT8 ONNX variants on the desktop's Ryzen 5 3600 with 32 GB RAM. Runs used two physical CPU cores, two inference threads, a 3 GiB memory cap, and 256-token segments with model-specific pooling and padding. MiniLM and multilingual Granite used official ONNX artifacts; the English Granite models used pinned community conversions.

The frozen dataset contained 580 chunks: 455 from the configured NixOS repository allowlist, 101 from dotfiles, and 24 synthetic operational memories, including similar-looking distractors. The 84 agent-authored questions comprised 48 repository paraphrases, 12 exact identifiers, and 24 synthetic incident questions. Evaluation covered semantic-only search and the unchanged hybrid ranking, with file/memory-level relevance labels. Each variant had one indexing pass, a query warmup pass, and three shuffled measured passes. Latencies below include query embedding and in-process hybrid retrieval, excluding MCP transport.

| Candidate                                              | Hybrid MRR@10, FP32 / INT8 | Median query ms, FP32 / INT8 | Peak RSS MiB, FP32 / INT8 |
| ------------------------------------------------------ | -------------------------- | ---------------------------- | ------------------------- |
| `sentence-transformers/all-MiniLM-L6-v2`               | 0.660 / 0.660              | 10.6 / 9.4                   | 228 / 154                 |
| `ibm-granite/granite-embedding-small-english-r2` (47M) | 0.700 / 0.690              | 36.8 / 14.5                  | 338 / 208                 |
| `ibm-granite/granite-embedding-english-r2` (149M)      | 0.714 / 0.697              | 43.2 / 31.0                  | 712 / 317                 |
| `ibm-granite/granite-embedding-97m-multilingual-r2`    | 0.704 / 0.690              | 17.8 / 34.1                  | 780 / 384                 |

MRR@10 rewards finding the first relevant result earlier; higher is better. The selected 97M FP32 model balanced ranking, latency, official artifact availability, and indexing cost: 63 seconds for this corpus versus 358 seconds for 149M FP32, excluding downloads and daemon maintenance pacing. It ranked the expected synthetic incident first on 24/24 questions versus MiniLM's 20/24. The 149M FP32 model had the highest MRR, while 47M INT8 had the highest hybrid top-five success rate (75/84). The tested 97M INT8 artifact was slower and less accurate than its FP32 counterpart on this stack.

This was a preliminary local comparison, not a statistically decisive quality win: the paired bootstrap 95% interval for 97M FP32's MRR gain over MiniLM included zero. File-level labels can over-credit chunks that lack the answer, and synthetic incidents do not establish real troubleshooting quality. The Framework was unreachable, so laptop latency and battery use remain unmeasured. These results support the current default without requiring a higher memory cap.

## MCP adapter

The stdio adapter uses the official MCP Python SDK 2.3 `MCPServer`, replacing the SDK 1 bundled `FastMCP`. It retains the four tools (`search_memory`, `get_memory`, `propose_memory`, `memory_status`), their arguments, and JSON text responses, and publishes object schemas with matching structured results. Both legacy `2025-11-25` initialization and `2026-07-28` discovery are covered by real-client integration tests.

Synchronous tool functions run in SDK worker threads. Each call opens its own Unix RPC connection; the daemon continues to serialize store/SQLite access with its existing lock. Daemon-reported failures use a dedicated `RPCError`, translated to SDK `ToolError` so validation messages reach the client. Unexpected transport, decoding, or adapter exceptions retain SDK 2's generic client error and stderr diagnostics. Operator signing and trust actions remain available only through the CLI.

The Python dependency range is `mcp>=2.3,<3`. Nix pins SDK and `mcp-types` to 2.3.0, with the required matching HTTP client packages, through a local override of the existing nixpkgs definitions. This avoids changing the broader nixpkgs lock while it still packages SDK 2.2. Deployment does not change the daemon RPC or durable event formats.

## Package boundary

This flake owns the application, tests, development dependencies, CLI and MCP behavior, record protocol, and generic documentation. It exports a package, app, package check, formatter, and development shell for both Linux architectures.

Infrastructure-specific user services, host names, synchronization peers, secrets, backup destinations, resource limits, and corpus allowlists belong to the consuming configuration. No infrastructure repository is a flake input or runtime prerequisite.
