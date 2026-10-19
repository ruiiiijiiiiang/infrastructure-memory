import json
import os
from dataclasses import dataclass, field
from pathlib import Path

MODEL = "sentence-transformers/all-MiniLM-L6-v2"
REVISION = "1110a243fdf4706b3f48f1d95db1a4f5529b4d41"


@dataclass
class Config:
    records: Path
    state: Path
    cache: Path
    socket: Path
    repositories: list = field(default_factory=list)
    model: str = MODEL
    revision: str = REVISION
    semantic: bool = True
    interval: int = 15

    @property
    def keys(self):
        return self.state / "keys"

    @classmethod
    def load(cls, filename=None):
        home = Path.home()
        state = Path(os.environ.get("XDG_STATE_HOME", home / ".local/state"))
        cache = Path(os.environ.get("XDG_CACHE_HOME", home / ".cache"))
        runtime = Path(os.environ.get("XDG_RUNTIME_DIR", state))
        data = {
            "records": str(home / "infrastructure-memory/records"),
            "state": str(state / "infrastructure-memory"),
            "cache": str(cache / "infrastructure-memory"),
            "socket": str(runtime / "infrastructure-memory/service.sock"),
        }
        requested = filename or os.environ.get("INFRA_MEMORY_CONFIG")
        filename = requested or "/etc/infrastructure-memory.json"
        if requested or Path(filename).exists():
            data.update(json.loads(Path(filename).read_text()))
        for key in ("records", "state", "cache", "socket"):
            data[key] = Path(data[key]).expanduser().absolute()
        config = cls(**data)
        if config.interval < 1:
            raise ValueError("interval must be positive")
        if len(config.revision) != 40 or any(
            c not in "0123456789abcdef" for c in config.revision
        ):
            raise ValueError(
                "embedding revision must be a pinned 40-character commit hash"
            )
        # The synchronized directory must never contain derived state or secrets.
        for path in (config.state, config.cache, config.socket):
            if path.is_relative_to(config.records) or config.records.is_relative_to(
                path
            ):
                raise ValueError(
                    "records, state, cache and runtime paths must be separate"
                )
        return config


def private_dir(path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)
