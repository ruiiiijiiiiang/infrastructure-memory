# Record and retrieval design

## Durable source and derived indexes

The durable source is one immutable JSON document per event. Each document has a schema version, event ID, memory ID, kind, parents and parent hashes, originating host/author, creation time, and content. IDs are canonical UUIDs. Record size and field validation bound input before import.

Publication writes and flushes a `.pending-*` file in the records directory, then uses a hard link to publish a unique final filename atomically without overwriting an existing record. The directory is flushed before the temporary file is removed. Startup reconciliation recovers a publication completed before local index import.

Transport does not need transaction-level database replication. Each workstation validates/imports received events idempotently and maintains a private SQLite metadata/full-text index and vector cache.

## Revision semantics

- Proposals create content or replace a single parent revision.
- Competing revisions remain multiple active heads; timestamps do not select a winner.
- Signed resolutions reference reviewed heads and provide replacement content.
- Signed approvals bind the exact reviewed parent payload by hash.
- Signed deletions create tombstones; a deletion/edit conflict suppresses normal retrieval until resolved.
- Missing parents remain pending. Import order does not determine the final projection.
- Duplicate IDs with different payloads are quarantined, including recognized synchronization conflict copies.
- Trusted operator public keys are configured locally and checked during reconciliation.

These controls provide an application/API boundary, not isolation from arbitrary processes sharing the operator's OS identity. Private keys and the trust store must remain outside the synchronized folder and agent workflows.

## Retrieval

Memory content and allowlisted repository files are split into source slices with citations. Repository ingestion uses tracked files from the actual local checkout, excludes symlinks and sensitive path names, and records commit/dirty-state provenance. A checkout describes intended configuration; it is not deployment evidence.

Search combines FTS5 lexical ranking, exact identifiers, and exact cosine retrieval over normalized NumPy vectors. Rank fusion gives stronger weight to semantic matches and exact text, and filters apply before candidate ranking. Memory-only search is the default; repository sources remain independently selectable.

The initial model is `sentence-transformers/all-MiniLM-L6-v2`, pinned at revision `1110a243fdf4706b3f48f1d95db1a4f5529b4d41`. ONNX inference uses CPU execution and tokenizer-aware subdivision of long inputs. Cache keys include the model fingerprint and content hash. Changing the model produces a separate derived vector cache.

Reconciliation polls periodically, including after restart. Embedding work is bounded per maintenance pass. Missing model files or inference failures leave lexical retrieval available and report degraded semantic search. Adding a watcher or a more advanced vector index should follow measurements rather than changing durable record semantics.

## Package boundary

This flake owns the application, tests, development dependencies, CLI and MCP behavior, record protocol, and generic documentation. It exports a package, app, package check, formatter, and development shell for both Linux architectures.

Infrastructure-specific user services, host names, synchronization peers, secrets, backup destinations, resource limits, and corpus allowlists belong to the consuming configuration. No infrastructure repository is a flake input or runtime prerequisite.
