# infrastructure-memory

Persistent operational knowledge for local AI agents. Each workstation runs a memory daemon, SQLite search index, and CPU embedding model. Immutable JSON records carry incidents, facts, decisions, and runbooks between machines through Syncthing or another file transport.

This README is the operating guide for configuration, setup, agent connections, operator review, and recovery. [Record and retrieval design](docs/design.md) describes the underlying protocol and implementation.

The daemon listens on a private Unix socket. An MCP stdio adapter exposes search, read, proposal, and status tools to agents. Approval, conflict resolution, and deletion are signed operator CLI actions.

## Deployment and ownership

In the current deployment, `framework` and `desktop` run independent daemons and indexes. `vm-app` stores and forwards the shared records through Syncthing without running a memory daemon. Workstations can operate offline and receive each other's records later.

This repository owns the application, tests, and independent Nix flake. The consuming `~/nixos-config` repository declares:

| Configuration                                              | Responsibility                                                                     |
| ---------------------------------------------------------- | ---------------------------------------------------------------------------------- |
| `homes/modules/memory.nix`                                 | Package, JSON configuration, repository allowlists, MCP fragment, and user service |
| `homes/configs/framework.nix`, `homes/configs/desktop.nix` | Enablement through `custom.home.memory.enable`                                     |
| `modules/services/apps/tools/syncthing/default.nix`        | The regular `infrastructure-memory` folder entry and its three peers               |
| `homes/files/agents.nix`                                   | Agent instructions to retrieve context and propose supported memories              |

The consuming flake currently uses `github:ruiiiijiiiiang/infrastructure-memory`, with this input's nixpkgs following the infrastructure pin. A local development override requires the application checkout on the machine evaluating the flake; publish application changes before expecting a remote input to include them.

Nix builds require explicit authorization under the infrastructure repository's rules. Deployments and operator signing/trust actions are performed manually. Editing configuration does not move existing records or keys.

## Storage and synchronization

The deployed layout uses separate `shared/` and `local/` directories:

| Path                                                           | Purpose                                                       | Synchronized? |
| -------------------------------------------------------------- | ------------------------------------------------------------- | ------------- |
| `~/.local/state/infrastructure-memory/shared/records/`         | Durable immutable JSON events                                 | Yes           |
| `~/.local/state/infrastructure-memory/local/index.sqlite3`     | Disposable materialized records, FTS5 index, and vector cache | No            |
| `~/.local/state/infrastructure-memory/local/keys/operator.key` | Local Ed25519 signing key                                     | No            |
| `~/.local/state/infrastructure-memory/local/keys/trusted/`     | Explicitly trusted public keys                                | No            |
| `~/.cache/infrastructure-memory/models/`                       | Pinned embedding model files                                  | No            |
| `$XDG_RUNTIME_DIR/infrastructure-memory/service.sock`          | Private daemon connection                                     | No            |
| `~/.config/infrastructure-memory/config.json`                  | Generated CLI, daemon, and adapter configuration              | No            |
| `~/.config/infrastructure-memory/mcp.json`                     | Generated client configuration fragment                       | No            |

Syncthing shares only `~/.local/state/infrastructure-memory/shared` between `framework`, `desktop`, and `vm-app`, retaining folder ID `infrastructure-memory`. It has no memory-specific directory rules, ignore patterns, or Restic integration. The daemon creates its records directory when needed. Keep the `local/` directory, keys, database, model cache, and socket outside every synchronized folder.

Records are eventually consistent. Status reports the local import state and cannot certify peer delivery or freshness. Check Syncthing separately for connected peers, folder errors, and synchronization completion. Replication does not provide an independent backup; arrange any record and key backups through the operator's normal backup procedure.

## Configuration

Configuration selection is `--config FILE`, then `INFRA_MEMORY_CONFIG`, then `/etc/infrastructure-memory.json` if present. An explicitly selected missing file is an error. The Home Manager wrapper sets `INFRA_MEMORY_CONFIG` to the generated user configuration unless it is already set; an explicit `--config` still wins.

For a standalone installation, create a JSON configuration using the recommended layout. For example:

```json
{
  "records": "~/.local/state/infrastructure-memory/shared/records",
  "state": "~/.local/state/infrastructure-memory/local",
  "cache": "~/.cache/infrastructure-memory",
  "repositories": [
    {
      "name": "nixos-config",
      "path": "~/nixos-config",
      "include": ["hosts/*.nix", "README.md"]
    }
  ],
  "semantic": true,
  "interval": 15
}
```

Paths expand `~`; use absolute paths or home-relative paths appropriate to the local user. Repository indexing is optional and defaults to an empty list. The generated Home Manager configuration contains the deployment's actual allowlists; edit its Nix declaration to change them.

Omitting `socket` selects `$XDG_RUNTIME_DIR/infrastructure-memory/service.sock` when `XDG_RUNTIME_DIR` is nonempty, otherwise `/run/user/<current UID>/infrastructure-memory/service.sock`. This keeps systemd and MCP clients with reduced environments on the same socket. A custom `socket` must agree between the daemon and every client. Records must be separate from the configured state, cache, and socket directories; the sibling `shared/` and `local/` layout satisfies this validation.

Without a configuration file, the standalone application's legacy defaults remain `~/infrastructure-memory/records` for records and `$XDG_STATE_HOME/infrastructure-memory` for state, with `~/.local/state` as the state-home fallback. Supply the configuration above to use the deployed layout. The cache defaults to `$XDG_CACHE_HOME/infrastructure-memory`, falling back to `~/.cache/infrastructure-memory`.

Run one daemon per workstation user. The deployed installation uses `infrastructure-memory.service` under the user systemd manager. A standalone daemon can be started with:

```sh
infra-memory --config ~/.config/infrastructure-memory/config.json serve
```

Supervision and resource limits belong to the consuming system. The current Home Manager service limits CPU to two cores, sets a 2 GiB memory high watermark and 3 GiB maximum, and hides an existing private operator key from the daemon.

## First-time setup

After manually installing or deploying the configuration:

1. Confirm Syncthing's folder path and its intended peers. Synchronize only `shared/`.
2. On each workstation, run `infra-memory key-init` yourself. It creates a key if missing, retains an existing key, trusts its public key locally, and prints the public key. Verify the other workstation's public key through a trusted channel, then explicitly import it with `infra-memory trust-key PUBLIC_KEY_HEX`. Keep private keys and trust management outside agent workflows.
3. Restart the user service after initializing keys so its private-key restriction applies: `systemctl --user restart infrastructure-memory.service`.
4. Run `infra-memory fetch-model` explicitly on each workstation to download the pinned model assets. The daemon uses cached files only; keyword search works before this download completes.
5. Run `infra-memory status` and a repository search, then configure each agent's MCP connection as described below.

For standalone installations, use the same `--config` argument for setup, daemon, adapter, and administrative commands. Existing installations should preserve their signing keys and trusted public keys through path changes.

## Connecting agents

Merge the generated `~/.config/infrastructure-memory/mcp.json` fragment into each agent client's configuration using that client's schema. It selects the Home Manager wrapper with the `mcp` argument. After package or wrapper updates, refresh client entries containing old Nix store executable paths and restart the harness to replace an already-running adapter.

A standalone client entry can use an explicit configuration:

```json
{
  "mcpServers": {
    "infrastructure-memory": {
      "command": "infra-memory",
      "args": [
        "--config",
        "/home/rui/.config/infrastructure-memory/config.json",
        "mcp"
      ]
    }
  }
}
```

Adapt the username and schema to the client. For secret-managed client configuration, merge the fragment through the normal manual secret preparation workflow; preserve the rest of the client configuration.

The stdio adapter uses the official MCP Python SDK 2.3 (`mcp`), with support for both the legacy `2025-11-25` initialization handshake and `2026-07-28` discovery. Existing client command/arguments remain valid. After manually deploying the upgrade, restart agent connections and check `memory_status` and `search_memory`. No record conversion or index reset is required. Daemon validation failures are returned as tool errors; unexpected adapter failures are logged to stderr and receive a generic tool error.

| MCP tool         | Purpose                                                                  |
| ---------------- | ------------------------------------------------------------------------ |
| `search_memory`  | Retrieve cited memories or repository documents with optional filters    |
| `get_memory`     | Read current heads, competing revisions, provenance, and deletion state  |
| `propose_memory` | Publish an unverified proposal or correction                             |
| `memory_status`  | Report local ingestion, conflicts, quarantine, and semantic availability |

Agent instructions require relevant history retrieval before investigating, verification against current configuration and live telemetry such as Grafana, and a concise proposal after a verified resolution. Records should contain the symptom, evidence, cause, action, and verification. Exclude credentials, decrypted secrets, raw conversation dumps, and bulk logs.

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
```

Search defaults to operational memories. Select `repo:NAME` for an indexed checkout or `all` to combine sources. MCP filters include host, service, category, and verification status; host/service filters omit untagged repository documents. MCP `max_chars` bounds excerpt text, with citations and metadata returned in addition.

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

Proposals are unverified. Review the evidence and current heads, then explicitly approve the exact revision yourself:

```sh
infra-memory get MEMORY_UUID
infra-memory approve MEMORY_UUID REVISION_UUID
```

Approval binds the exact reviewed parent payload by hash. A later proposal requires separate approval. An approval from an unknown signing key is quarantined until the operator establishes trust locally. Signed operator actions are absent from the MCP tools and daemon RPC interface; agents must not access operator keys or change the trust store. These controls provide an application/API boundary and do not isolate arbitrary processes running under the same OS user.

Concurrent offline revisions retain competing heads; timestamps and arrival order do not select a winner. To resolve a conflict, review all heads and prepare a replacement JSON containing the eight content fields in the proposal example. To delete a memory, review its heads and publish a tombstone:

```sh
infra-memory resolve MEMORY_UUID resolved.json --heads LEFT_REVISION RIGHT_REVISION
infra-memory forget MEMORY_UUID --heads CURRENT_REVISION
```

The CLI refuses actions whose specified heads differ from its current local view. Later delivery of an unseen offline revision can still produce a conflict. A deletion/edit conflict suppresses the entire memory from ordinary search until resolved. Tombstones retain history and do not securely erase previous payloads or backups.

## Health checks and troubleshooting

Check the local service and CLI first:

```sh
systemctl --user status infrastructure-memory.service
infra-memory status
infra-memory search 'custom.home.memory.enable' --source repo:nixos-config
```

Then call MCP `memory_status` and `search_memory` through the agent harness. A fresh installation can have zero memories/events while still reporting repository documents. Review conflicts, pending events, quarantined events, and `semantic_error`. Check Syncthing separately before claiming all peers are up to date.

| Symptom                                               | Checks and action                                                                                                                                                                                                                            |
| ----------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| CLI works but MCP returns `No such file or directory` | Verify client configuration and socket selection. The adapter needs the same configuration as the daemon and the runtime-directory fallback described above. Refresh old store-path client entries after deployment and restart the harness. |
| Ordinary search misses repository content             | Use `--source repo:NAME` or `--source all`; memory-only search is the default. Check the file is tracked, allowlisted, and eligible for ingestion.                                                                                           |
| Semantic search unavailable                           | Check `semantic_error`, the configured model cache, and whether `infra-memory fetch-model` has completed. Keyword search remains available.                                                                                                  |
| Peer records missing                                  | Check Syncthing connectivity, shared-folder path, folder errors, and completion. Local ingestion status cannot establish peer freshness.                                                                                                     |
| Events pending or quarantined                         | Inspect status and source evidence. Missing parents and cycles remain pending; malformed events, invalid/unknown signatures, and UUID payload collisions are quarantined. Trust changes require operator action.                             |

Use the configured Grafana service for infrastructure logs. Do not edit published events to repair ordinary knowledge; propose a revision. An observed UUID collision remains quarantined locally even if one conflicting file disappears, so investigate before rebuilding the index.

## Recovery, path changes, and reset

To rebuild the disposable index, stop the user service, move `local/index.sqlite3` and any associated `-wal`/`-shm` files aside together, and restart the service. Preserve `local/keys/` and `shared/records/`. The daemon reconstructs materialized records, full-text search, and embeddings; cached model files remain available offline. Do not copy a live SQLite database as an ordinary file backup.

For a legacy path migration, close agents and stop the workstation daemons and Syncthing on all peers. Move the old `~/infrastructure-memory` folder to `~/.local/state/infrastructure-memory/shared`, retaining its Syncthing marker. Move the old state directory's `keys/` and SQLite files into the sibling `local/` directory without overwriting populated destinations. Deploy the new paths before restarting services, then compare record counts, search results, and peer synchronization. Private-key moves and deployment are operator actions. Roll back paths and file moves together while services are stopped if necessary.

To start over with no retained memories, close agents and stop daemons and synchronization on every peer first. Clear the old and new shared records and the local SQLite index/sidecars on each peer; preserve signing keys and trust settings unless deliberately replacing that identity. Recreate the configured shared folder and its Syncthing marker as needed, deploy the matching configuration, and restart services. Verify zero events/memories and successful repository/MCP searches. An uncleared offline peer can reintroduce old records.

Back up records, private keys, and trust configuration separately if required; a rebuilt index cannot recover keys or trust. Do not compact tombstones or history until retention accounts for offline peers. Purging sensitive content requires coordinated removal from records, indexes, all peers, and any backups.

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

The suite uses temporary records and real MCP SDK clients without model downloads or running infrastructure. Its Unix-socket integration tests need permission to create local sockets. They exercise both MCP handshakes, tool schemas, JSON text and structured results, validation errors, and the operator CLI boundary. The suite also covers revision ordering, offline conflicts, signatures/trust, tombstones, invalid events, repository scope, and retrieval. Simulated third-peer delivery does not replace a live Syncthing check.

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
