# infrastructure-memory

Persistent operational knowledge for local AI agents. Each workstation runs its own memory daemon, SQLite search index, and CPU embedding model. Portable immutable records can be synchronized with Syncthing or another file transport.

The daemon has no listening TCP port. Agents connect through an MCP stdio adapter and a private Unix socket. Their tools can search, read, propose, and inspect status. Approval, resolution, and deletion use signed operator CLI actions.

This repository contains the application, its tests, and an independent Nix flake. Host enablement, Syncthing peers, backup policies, and repository allowlists belong in the consuming infrastructure configuration.

## Features

- Immutable JSON events with atomic publication, revisions, conflict detection, and tombstones.
- Operator signatures bound to the exact reviewed revision payloads.
- Local SQLite/FTS5, exact-identifier search, and dense-vector retrieval with citations.
- A pinned MiniLM ONNX model using CPU inference, with keyword fallback when unavailable.
- Disposable indexes rebuilt from the synchronized records; private keys remain local.
- Local-checkout indexing restricted to explicitly allowlisted tracked files.

See [usage and configuration](docs/usage.md) and [record and retrieval design](docs/design.md).

## Flake outputs

Outputs are provided for `x86_64-linux` and `aarch64-linux`:

| Output | Purpose |
| --- | --- |
| `packages.<system>.default` | The `infra-memory` application |
| `packages.<system>.infrastructure-memory` | Named alias for the application |
| `apps.<system>.default` | CLI entry point |
| `checks.<system>.package` | Package build with the unit/integration suite |
| `devShells.<system>.default` | Python dependencies, Ruff, and Git |
| `formatter.<system>` | Nix formatter |

The flake pins nixpkgs in its own lock file. It does not import or depend on a `nixos-config` checkout. A consuming flake can make `infrastructure-memory.inputs.nixpkgs` follow its own nixpkgs input.

## Development

From this repository, enter the development environment and run:

```sh
nix develop
PYTHONPATH=src python -B -m unittest discover -s tests -v
ruff check .
ruff format --check .
```

The normal test suite uses temporary records and a real MCP SDK client, and requires no model download or running infrastructure. The integration test needs permission to create a local Unix socket.

The commands below build Nix derivations; run them only when builds are authorized in your environment:

```sh
nix build
nix flake check
```

Development-shell realization can also build missing dependencies. Evaluation-only inspection can use `nix eval .#packages.x86_64-linux.default.name --option allow-import-from-derivation false`.

The optional benchmark uses temporary records and a separately downloaded model:

```sh
PYTHONPATH=src python -B tests/benchmark.py --config /path/to/config.json
```

Benchmark examples are synthetic and do not measure real incident troubleshooting quality. Curate real incident questions before relying on retrieval during investigations.
