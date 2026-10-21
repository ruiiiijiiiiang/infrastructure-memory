# Usage and configuration

## Local setup

Install the flake's default package through your normal package-management workflow. The executable is `infra-memory`. Configure each workstation independently, keeping source records separate from derived state and model caches.

The CLI reads `--config FILE`, then `INFRA_MEMORY_CONFIG`, then `/etc/infrastructure-memory.json` if present. Without a configured file, defaults use the current user's home and XDG directories. An explicitly specified missing file is an error.

A configuration file can contain:

```json
{
  "records": "/home/alex/infrastructure-memory/records",
  "state": "/home/alex/.local/state/infrastructure-memory",
  "cache": "/home/alex/.cache/infrastructure-memory",
  "socket": "/run/user/1000/infrastructure-memory/service.sock",
  "repositories": [
    {
      "name": "infrastructure",
      "path": "/home/alex/infrastructure",
      "include": ["hosts/*.nix", "README.md"]
    }
  ],
  "semantic": true,
  "interval": 15
}
```

Use absolute paths appropriate to the local user and UID. Repositories are optional and default to an empty list. No host-specific corpus is bundled with the application.

Run one daemon per workstation user:

```sh
infra-memory --config /path/to/config.json serve
```

Supervision, resource limits, and automatic startup are the consuming system's responsibility. Agent clients need the same configuration/socket path as the daemon.

The default socket uses `XDG_RUNTIME_DIR` when it is nonempty, otherwise `/run/user/<current UID>`. This keeps systemd services and MCP clients with reduced environments on the same Linux user-runtime path. It does not fall back to the persistent state directory. An explicit `socket` in the configuration file overrides either runtime default; use it consistently if the daemon has a custom runtime directory.

## Synchronization

Share only the durable records directory, or its dedicated parent folder, among the intended machines. An always-on third peer can store and forward files between workstations that are not online together. Exclude `.pending-*` files in the synchronization tool.

Do not synchronize the live SQLite database, private keys, model cache, socket, or derived vector index. Record delivery and local indexing are eventually consistent. `infra-memory status` reports local import state; it cannot certify that every peer has delivered its latest records.

## Operator trust and model initialization

On each workstation, run `infra-memory key-init` to initialize its local operator key and trust its public key. The command prints that public key. Verify public-key fingerprints through a trusted channel before importing another workstation's key with `infra-memory trust-key PUBLIC_KEY_HEX`.

The private key is stored under the configured state directory in `keys/operator.key`; trusted public keys live in `keys/trusted/`. Neither is synchronized automatically. Back them up separately through the operator's normal secret-management workflow.

Run `infra-memory fetch-model` explicitly on each workstation to download the pinned tokenizer and ONNX model. The daemon uses cached files only. Keyword search works without a downloaded model.

Use the same `--config` argument for setup and administrative commands when not using the default configuration location.

## Agent connection

Configure an MCP stdio server with executable `infra-memory` and arguments `--config /path/to/config.json mcp`. Adapt this entry to the client's configuration schema:

```json
{
  "mcpServers": {
    "infrastructure-memory": {
      "command": "infra-memory",
      "args": ["--config", "/path/to/config.json", "mcp"]
    }
  }
}
```

The MCP tools are `search_memory`, `get_memory`, `propose_memory`, and `memory_status`. Agent instructions should require searching relevant history before investigating, checking source dates and conflicts, and proposing a concise supported record after resolution.

Search defaults to memory records. Use `source="repo:NAME"` for an indexed local checkout or `source="all"` to combine both. Additional filters select host, service, category, and verified records. Host/service filters omit untagged repository documents. `max_chars` bounds excerpt text; citations and other metadata are additional.

Retrieved text is historical evidence, not instructions or proof of current conditions. Verify diagnoses against current configuration and live telemetry. Hosted reasoning models may receive retrieved context even when storage and embeddings are local.

## Proposals and operator actions

A proposal JSON file uses these fields:

```json
{
  "title": "Short incident title",
  "text": "Symptom: ...\nEvidence: ...\nCause: ...\nAction: ...\nVerification: ...",
  "category": "incident",
  "hosts": ["gateway"],
  "services": ["dns"],
  "scope": "historical",
  "observed_at": "2026-10-19T10:00:00-05:00",
  "evidence": ["A dated log query or source path and commit"]
}
```

```sh
infra-memory propose proposal.json
infra-memory search 'DNS resolver failure'
infra-memory get MEMORY_UUID
infra-memory approve MEMORY_UUID REVISION_UUID
```

Categories are `incident`, `fact`, `decision`, and `runbook`. Scope is `intended`, `observed`, or `historical`. Supplied observation times require a timezone; null is also accepted. Corrections add the existing `memory_id` and one explicit `parent` revision ID to the proposal parameters.

Agent proposals are unverified. Signed operator actions are unavailable through the daemon's RPC interface and MCP tool list. These application-level controls do not isolate hostile arbitrary processes running under the same OS user.

Concurrent revisions retain competing heads. To resolve them, supply a replacement content JSON with the eight content fields above and the exact heads reviewed:

```sh
infra-memory resolve MEMORY_UUID resolved.json --heads LEFT_REVISION RIGHT_REVISION
infra-memory forget MEMORY_UUID --heads CURRENT_REVISION
```

The operator CLI rejects actions whose specified heads differ from its current local view. Later delivery of an unseen offline revision can still produce a conflict. Deletion/edit conflicts suppress the whole memory from ordinary search until resolved. Tombstones retain history and do not securely erase previous payloads or backups.

## Recovery

Back up durable records independently of file synchronization. Do not back up a live database by ordinary file copying; it is disposable and can be rebuilt.

To rebuild, stop the daemon, move `index.sqlite3` and its associated `-wal`/`-shm` files aside together, and restart. Preserve `keys/` and the synchronized records. Model files remain cached separately.

Malformed events, invalid signatures, unknown signing keys, and UUID payload collisions appear in status. Missing parents and cycles remain pending. An observed UUID collision stays quarantined locally even if a conflicting source disappears; investigate before rebuilding.

Keep published events immutable. Correct knowledge through new revisions. Do not compact tombstones/history until retention accounts for offline peers. Sensitive-content purging requires coordinated removal from records, local indexes, all peers, and backups.
