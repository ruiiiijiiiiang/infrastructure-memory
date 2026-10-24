# infrastructure-memory

Your services become unreachable after a router reboot. An AI agent checks logs, traces the network path, and eventually finds a gateway DHCP issue. You apply the fix, and connectivity returns. Weeks later, it happens again. In a new conversation, the agent has no record of the earlier investigation and starts from scratch.

With infrastructure-memory, the agent saves the symptoms, evidence, cause, fix, and verification from the first incident. Next time, it retrieves that context and checks whether the same cause applies, giving it a starting point for the investigation.

Persistent operational knowledge for local AI agents. Each workstation runs a memory daemon, SQLite search index, and CPU embedding model. Incidents, facts, decisions, and runbooks are stored locally as immutable JSON records. A single-machine installation works without any synchronization service.

This README is the operating guide for configuration, setup, agent connections, operator review, and recovery. [Record and retrieval design](docs/design.md) describes the underlying protocol and implementation.

The daemon listens on a private Unix socket. An MCP stdio adapter exposes search, read, proposal, and status tools to agents. Approval, conflict resolution, and deletion are signed operator CLI actions.

## Local storage

The recommended layout uses separate `shared/` and `local/` directories. `shared/records/` contains plaintext JSON event files that can be synchronized between machines by an external tool, or kept entirely local:

| Path                                                           | Purpose                                                       | Share between machines? |
| -------------------------------------------------------------- | ------------------------------------------------------------- | ------------- |
| `~/.local/state/infrastructure-memory/shared/records/`         | Durable immutable plaintext JSON events                       | Optional      |
| `~/.local/state/infrastructure-memory/local/index.sqlite3`     | Disposable materialized records, FTS5 index, and vector cache | No            |
| `~/.local/state/infrastructure-memory/local/keys/operator.key` | Local Ed25519 signing key                                     | No            |
| `~/.local/state/infrastructure-memory/local/keys/trusted/`     | Explicitly trusted public keys                                | No            |
| `~/.cache/infrastructure-memory/models/`                       | Pinned embedding model files                                  | No            |
| `$XDG_RUNTIME_DIR/infrastructure-memory/service.sock`          | Private daemon connection                                     | No            |
| `~/.config/infrastructure-memory/config.json`                  | CLI, daemon, and adapter configuration                        | No            |
| `~/.config/infrastructure-memory/mcp.json`                     | Optional client configuration fragment                        | No            |

The daemon creates its records directory when needed. Keep records separate from the database, keys, model cache, and socket, even when using only one machine.

## Configuration

Configuration selection is `--config FILE`, then `INFRA_MEMORY_CONFIG`, then `/etc/infrastructure-memory.json` if present. An explicitly selected missing file is an error. The Home Manager wrapper sets `INFRA_MEMORY_CONFIG` to the generated user configuration unless it is already set; an explicit `--config` still wins.

For a standalone installation, create a JSON configuration using the recommended layout. For example:

```json
{
  "records": "~/.local/state/infrastructure-memory/shared/records",
  "state": "~/.local/state/infrastructure-memory/local",
  "cache": "~/.cache/infrastructure-memory",
  "repositories": [],
  "semantic": true,
  "interval": 15
}
```

Paths expand `~`; use absolute paths or home-relative paths appropriate to the local user. Repository indexing is optional and defaults to an empty list. To index a checkout, add an entry such as `{"name": "my-project", "path": "~/Projects/my-project", "include": ["README.md", "docs/*.md"]}` to `repositories`. The generated Home Manager configuration contains the deployment's actual allowlists; edit its Nix declaration to change them.

Omitting `socket` selects `$XDG_RUNTIME_DIR/infrastructure-memory/service.sock` when `XDG_RUNTIME_DIR` is nonempty, otherwise `/run/user/<current UID>/infrastructure-memory/service.sock`. This keeps systemd and MCP clients with reduced environments on the same socket. A custom `socket` must agree between the daemon and every client. Records must be separate from the configured state, cache, and socket directories; the sibling `shared/` and `local/` layout satisfies this validation.

Without a configuration file, records default to `~/infrastructure-memory/records` and state to `$XDG_STATE_HOME/infrastructure-memory`, with `~/.local/state` as the state-home fallback. Supply the configuration above to use the recommended layout. The cache defaults to `$XDG_CACHE_HOME/infrastructure-memory`, falling back to `~/.cache/infrastructure-memory`.

Run one daemon per workstation user. The deployed installation uses `infrastructure-memory.service` under the user systemd manager. A standalone daemon can be started with:

```sh
infra-memory --config ~/.config/infrastructure-memory/config.json serve
```

Supervision and resource limits belong to the consuming system. The current Home Manager service limits CPU to two cores, sets a 2 GiB memory high watermark and 3 GiB maximum, and hides an existing private operator key from the daemon.

## First-time setup

After installing the application, save the configuration above as `~/.config/infrastructure-memory/config.json`. Use the same `--config` argument for setup, daemon, adapter, and administrative commands. Commands below omit it for brevity; you can set `INFRA_MEMORY_CONFIG` instead:

```sh
export INFRA_MEMORY_CONFIG="$HOME/.config/infrastructure-memory/config.json"
```

Then:

1. Run `infra-memory key-init` yourself. It creates a key if missing, retains an existing key, trusts its public key locally, and prints the public key. This enables local operator approval, resolution, and deletion. Keep private keys and trust management outside agent workflows.
2. Run `infra-memory fetch-model` to enable semantic search. The daemon uses cached files only; keyword search works without downloading the model. Set `"semantic": false` for keyword-only use.
3. Start the daemon with `infra-memory serve` in a separate terminal using the same configuration. If using the Home Manager service, restart it after initializing keys so its private-key restriction applies: `systemctl --user restart infrastructure-memory.service`.
4. Run `infra-memory status` and configure each agent's MCP connection as described below. A fresh installation has no memories; if you configured repository indexing, check it with `infra-memory search 'QUERY' --source repo:NAME`.

## Optional synchronization

An external tool can synchronize the plaintext JSON files in `shared/records/`. Keep the `local/` directory, keys, database, model cache, and socket outside every synchronized folder. Each workstation runs its own daemon and index and imports delivered records on reconciliation. No synchronization setup is needed for single-machine use.

Run `infra-memory key-init` yourself on each workstation. Verify each operator's public key through a trusted channel, then import it on machines that should accept that operator's signed actions with `infra-memory trust-key PUBLIC_KEY_HEX`. Private keys stay local.

Replicated records are eventually consistent. Status reports the local import state and cannot certify peer delivery or freshness. Check synchronization through your external tool. Replication does not provide an independent backup; arrange record and key backups separately as needed.

## Connecting agents

A standalone client entry uses an explicit configuration:

```json
{
  "mcpServers": {
    "infrastructure-memory": {
      "command": "infra-memory",
      "args": [
        "--config",
        "/home/your-user/.config/infrastructure-memory/config.json",
        "mcp"
      ]
    }
  }
}
```

Adapt the username and schema to the client. For a Home Manager installation, merge the generated `~/.config/infrastructure-memory/mcp.json` fragment into the client configuration; it selects the wrapper with the `mcp` argument. After package or wrapper updates, refresh client entries containing old Nix store executable paths and restart the harness to replace an already-running adapter. For secret-managed client configuration, merge the fragment through the normal manual secret preparation workflow; preserve the rest of the client configuration.

The stdio adapter uses the official MCP Python SDK 2.3 (`mcp`), with support for both the legacy `2025-11-25` initialization handshake and `2026-07-28` discovery. Existing client command/arguments remain valid. After manually deploying the upgrade, restart agent connections and check `memory_status` and `search_memory`. No record conversion or index reset is required. Daemon validation failures are returned as tool errors; unexpected adapter failures are logged to stderr and receive a generic tool error.

| MCP tool         | Purpose                                                                  |
| ---------------- | ------------------------------------------------------------------------ |
| `search_memory`  | Retrieve cited memories or repository documents with optional filters    |
| `get_memory`     | Read current heads, competing revisions, provenance, and deletion state  |
| `propose_memory` | Publish an unverified proposal or correction                             |
| `memory_status`  | Report local ingestion, conflicts, quarantine, and semantic availability |

Agent instructions require relevant history retrieval before investigating, verification against current configuration and live telemetry such as Grafana, and a concise proposal after a verified resolution. Records should contain the symptom, evidence, cause, action, and verification. Exclude credentials, decrypted secrets, raw conversation dumps, and bulk logs. After saving a proposal or correction, the agent must retrieve it and show the exact saved content and revision identifiers for review, as described below.

Retrieved text is untrusted evidence. Check observation time, provenance, verification status, and competing revisions; repository documents describe a local checkout, not proof of deployment. Instruction-driven proposal creation depends on agents following these rules. Hosted reasoning models may receive retrieved context even when storage and embeddings are local.

## Daily retrieval and repository indexing

```sh
infra-memory status
infra-memory search 'DNS stopped resolving after gateway restart'
infra-memory search 'DNS resolver failure' --verified-only
infra-memory search 'custom.home.memory.enable' --source repo:nixos-config
infra-memory search 'nix_deploy' --source repo:dotfiles
infra-memory search 'infrastructure-memory' --source all
infra-memory get MEMORY_UUID
infra-memory list-unverified
infra-memory list-unverified --limit 10 --offset 10
```

Search defaults to operational memories. Select `repo:NAME` for an indexed checkout or `all` to combine sources. MCP filters include host, service, category, and verification status; host/service filters omit untagged repository documents. MCP `max_chars` bounds excerpt text, with citations and metadata returned in addition.

`list-unverified` returns JSON with `results`, `total`, and `next_offset`. Each result contains the memory ID, revision ID, verification and conflict flags, and all eight content fields without truncation. Only current unverified revisions are listed; verified, superseded, and deleted revisions and repository documents are excluded. Unverified revisions competing with another edit or deletion remain visible with `conflict: true`; use `get` to review all heads before taking action.

Results are ordered by memory ID and revision ID. Pages default to at most 20 revisions (`--limit` accepts 1-100); large entries can produce shorter pages to fit the RPC response size. Continue with `--offset` set to the returned `next_offset` until it is null. Pagination reflects the daemon's local import state and can shift as memories change. The command requires the updated daemon to be running.

Repository ingestion reads explicitly allowlisted Git-tracked files from the local checkout. It skips symlinks, sensitive path names, `.age` files, oversized files, and non-UTF-8 data. Index source files in the repository rather than symlinks installed under `~/.config`. Include patterns match the full repository-relative path using Python `fnmatchcase`, where `*` can match directory separators. Review allowlisted content for inline credentials as well as sensitive filenames.

Each result carries source path, line, commit, and a repository-level dirty flag. The current deployment indexes allowlists for `~/nixos-config` and `~/dotfiles`; their exact contents are declared in `homes/modules/memory.nix`. No host-specific corpus is bundled with this application.

Reconciliation polls every 15 seconds by default, importing delivered records and refreshing tracked repository changes. It removes documents when source files disappear. Embedding work is bounded per maintenance pass, so allow initial indexing to finish.

The default CPU model is `ibm-granite/granite-embedding-97m-multilingual-r2`, pinned to revision `835ad14087e140460703cf0fae09f97d469d65c2`. It uses the official FP32 `onnx/model.onnx` artifact and produces 384-dimensional vectors with CLS pooling. Inference uses two threads and 256-token segments, including special tokens; long source slices are subdivided by tokenizer offsets, their pooled vectors averaged, and the result L2-normalized. The model's larger native context window is not used. `infra-memory fetch-model` downloads the model, tokenizer, padding, sequence-length, and pooling metadata required for offline inference.

Embeddings are cached by model/pipeline fingerprint and content hash. Keyword and exact-identifier search remain available if the model is absent or inference fails; inspect `semantic_error` in status and search results.

## Proposals and operator review

An agent uses `propose_memory`; a human can submit a JSON parameter file with `infra-memory propose proposal.json`:

```json
{
  "title": "Short incident title",
  "text": "Symptom: ...\nEvidence: ...\nCause: ...\nAction: ...\nVerification: ...",
  "category": "incident",
  "hosts": ["vm-network"],
  "services": ["dns"],
  "scope": "historical",
  "observed_at": "2026-10-22T10:00:00-05:00",
  "evidence": ["A dated Grafana query or source path and commit"]
}
```

Categories are `incident`, `fact`, `decision`, and `runbook`. Scope is `intended`, `observed`, or `historical`. Observation time may be null; supplied timestamps require a timezone. Corrections add the existing `memory_id` and one explicit `parent` revision ID to the proposal parameters.

Proposals are unverified. After saving a proposal or correction, the agent must retrieve it with `get_memory` and show its exact saved content in the user-visible response: the full title, text, category, scope, hosts, services, observed_at, and evidence, plus the memory ID and revision ID. A save notification, summary, or link is insufficient. The same review applies when approving an existing memory.

Under the current deployment's agent policy, explicit conversational confirmation authorizes the agent to approve that exact displayed revision through the existing CLI. The agent must perform the approval without asking the user to run the command or requesting a second conversational confirmation. Permission to save, confirmation that the incident is resolved, silence, or approval of another revision does not authorize approval. Other installations must explicitly authorize this delegation in their agent instructions; MCP-only clients cannot invoke the approval CLI.

Before approving, the agent retrieves the memory again and checks that the displayed revision remains a current head with identical content. If it changed or was superseded, the agent shows the current content and obtains fresh confirmation. Approval applies only to that revision; competing heads and later corrections need separate review and confirmation.

The agent uses the daemon's configuration for the CLI command below, then waits for reconciliation and retrieves the memory to confirm that the same revision reports `verified: true`. It reports success only after this check, or reports the failure or pending state if approval cannot be confirmed. A human can also run these commands directly:

```sh
infra-memory get MEMORY_UUID
infra-memory approve MEMORY_UUID REVISION_UUID
```

Approval binds the exact reviewed parent payload by hash. `verified: true` indicates accepted operator approval or a signed operator resolution; it does not independently establish factual accuracy. An approval from an unknown signing key is quarantined until the operator establishes trust locally.

Signed operator actions are absent from the MCP tools and daemon RPC interface. User-confirmed approval permits the agent to invoke the signing CLI, which handles the private key internally; agents must not directly read, copy, export, or display operator keys or change the trust store. If CLI access or signing is unavailable, the agent reports the blocker. Deletion and conflict resolution remain manual operator actions. Delegating approval relies on the agent honoring the conversational confirmation; the signature does not independently prove that confirmation occurred. These controls provide an application/API boundary and do not isolate arbitrary processes running under the same OS user.

Concurrent offline revisions retain competing heads; timestamps and arrival order do not select a winner. To resolve a conflict, review all heads and prepare a replacement JSON containing the eight content fields in the proposal example. To delete a memory, review its heads and publish a tombstone:

```sh
infra-memory resolve MEMORY_UUID resolved.json --heads LEFT_REVISION RIGHT_REVISION
infra-memory forget MEMORY_UUID --heads CURRENT_REVISION
```

The CLI refuses actions whose specified heads differ from its current local view. Later delivery of an unseen offline revision can still produce a conflict. A deletion/edit conflict suppresses the entire memory from ordinary search until resolved. Tombstones retain history and do not securely erase previous payloads or backups.

## Health checks and troubleshooting

Check the local service and CLI first:

```sh
infra-memory status
infra-memory search 'QUERY'
```

For the Home Manager installation, also check `systemctl --user status infrastructure-memory.service`. Then call MCP `memory_status` and `search_memory` through the agent harness. A fresh installation can have zero memories/events while still reporting configured repository documents. Review conflicts, pending events, quarantined events, and `semantic_error`. If synchronization is configured, check its transport separately before claiming all peers are up to date.

| Symptom                                               | Checks and action                                                                                                                                                                                                                            |
| ----------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| CLI works but MCP returns `No such file or directory` | Verify client configuration and socket selection. The adapter needs the same configuration as the daemon and the runtime-directory fallback described above. Refresh old store-path client entries after deployment and restart the harness. |
| Ordinary search misses repository content             | Use `--source repo:NAME` or `--source all`; memory-only search is the default. Check the file is tracked, allowlisted, and eligible for ingestion.                                                                                           |
| Semantic search unavailable                           | Check `semantic_error`, the configured model cache, and whether `infra-memory fetch-model` has completed. Keyword search remains available.                                                                                                  |
| Peer records missing (when synchronization is configured) | Check the transport's connectivity, shared-folder path, folder errors, and completion. Local ingestion status cannot establish peer freshness.                                                                                             |
| Events pending or quarantined                         | Inspect status and source evidence. Missing parents and cycles remain pending; malformed events, invalid/unknown signatures, and UUID payload collisions are quarantined. Trust changes require operator action.                             |

Use the configured Grafana service for infrastructure logs. Do not edit published events to repair ordinary knowledge; propose a revision. An observed UUID collision remains quarantined locally even if one conflicting file disappears, so investigate before rebuilding the index.

## Recovery and reset

To rebuild the disposable index, stop the daemon, move `index.sqlite3` and any associated `-wal`/`-shm` files aside together from the configured `state` directory, and restart the daemon. Preserve the `keys/` directory and configured records. The daemon reconstructs materialized records, full-text search, and embeddings; cached model files remain available offline. Do not copy a live SQLite database as an ordinary file backup.

To start over with no retained memories, close agents and stop the daemon. Clear the configured records and the local SQLite index/sidecars; preserve signing keys and trust settings unless deliberately replacing that identity. Restart the daemon and verify zero events/memories and working MCP searches. Repository searches should still work if configured.

If synchronization is configured, stop daemons and synchronization on every peer before resetting. Clear records and indexes on all peers before restarting; an uncleared offline peer can reintroduce old records.

Back up records, private keys, and trust configuration separately if required; a rebuilt index cannot recover keys or trust. When replicating records, do not compact tombstones or history until retention accounts for offline peers. Purging sensitive content requires removal from records, indexes, any replicas, and backups.

## Current deployment and ownership

This repository owns the application, tests, and independent Nix flake. The following describes the maintainer's deployment; these hosts and repositories are not prerequisites for standalone use.

`framework` and `desktop` run independent daemons and indexes.

The consuming `~/nixos-config` repository declares:

| Configuration                                              | Responsibility                                                                     |
| ---------------------------------------------------------- | ---------------------------------------------------------------------------------- |
| `homes/modules/memory.nix`                                 | Package, JSON configuration, repository allowlists, MCP fragment, and user service |
| `homes/configs/framework.nix`, `homes/configs/desktop.nix` | Enablement through `custom.home.memory.enable`                                     |
| `homes/files/agents.nix`                                   | Agent instructions to retrieve context and propose supported memories              |

The consuming flake currently uses `github:ruiiiijiiiiang/infrastructure-memory`, with this input's nixpkgs following the infrastructure pin. A local development override requires the application checkout on the machine evaluating the flake; publish application changes before expecting a remote input to include them.

Nix builds require explicit authorization under the infrastructure repository's rules. Deployments and operator signing/trust actions are performed manually.

## Flake outputs and development

The flake is independent of `nixos-config` and pins nixpkgs in its own lock file. Outputs are provided for `x86_64-linux` and `aarch64-linux`:

| Output                                    | Purpose                                       |
| ----------------------------------------- | --------------------------------------------- |
| `packages.<system>.default`               | The `infra-memory` application                |
| `packages.<system>.infrastructure-memory` | Named package alias                           |
| `apps.<system>.default`                   | CLI entry point                               |
| `checks.<system>.package`                 | Package build with the unit/integration suite |
| `devShells.<system>.default`              | Python dependencies, Ruff, and Git            |
| `formatter.<system>`                      | Nix formatter                                 |

When development-shell realization is authorized, enter the environment and run:

```sh
nix develop
PYTHONPATH=src python -B -m unittest discover -s tests -v
ruff check .
ruff format --check .
```

The suite uses temporary records and real MCP SDK clients without model downloads or running infrastructure. Its Unix-socket integration tests need permission to create local sockets. They exercise both MCP handshakes, tool schemas, JSON text and structured results, validation errors, and the operator CLI boundary. The suite also covers revision ordering, offline conflicts, signatures/trust, tombstones, invalid events, repository scope, and retrieval. Simulated third-peer delivery does not verify an external tool's live synchronization.

Python installations require `mcp>=2.3,<3`. The Nix package and development shell select SDK 2.3.0 through local overrides in `default.nix`: the pinned nixpkgs provides SDK 2.2, so this file overrides the SDK, matching `mcp-types`, HTTP dependencies, and the upstream test dependency locally. Remove these overrides when the pinned package set supplies compatible versions. Updating this application's SDK does not require the standalone `fastmcp` package.

The following commands build derivations; run them only when builds are authorized:

```sh
nix build
nix flake check
```

Development-shell realization can also build missing dependencies. Evaluation-only inspection can use `nix eval .#packages.x86_64-linux.default.name --option allow-import-from-derivation false`.

An optional benchmark uses temporary records and a separately downloaded model:

```sh
PYTHONPATH=src python -B tests/benchmark.py --config ~/.config/infrastructure-memory/config.json --repository ~/nixos-config
```

The benchmark measures indexing/retrieval behavior without adding synthetic incidents to durable memory. It does not measure real troubleshooting quality. Evaluate real incident questions for relevant citations, obsolete/conflicting records, unsupported claims, and time to a verified diagnosis before relying on retrieval.
