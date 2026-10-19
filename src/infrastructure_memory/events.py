"""Versioned immutable events. Operator actions are signed, agent proposals are not."""

import hashlib
import json
import os
import socket
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path

from .config import private_dir

MAX_BYTES = 256 * 1024
KINDS = {"proposal", "resolve", "delete", "approve"}
PRIVILEGED = KINDS - {"proposal"}


def canonical(value):
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode()


def uuid_string(value):
    if not isinstance(value, str) or str(uuid.UUID(value)) != value:
        raise ValueError("IDs must be canonical UUID strings")
    return value


def validate(event):
    required = {
        "schema",
        "id",
        "memory_id",
        "kind",
        "parents",
        "parent_hashes",
        "host",
        "author",
        "created_at",
        "content",
    }
    if (
        not isinstance(event, dict)
        or not required <= event.keys()
        or event.keys() - required - {"signer", "signature"}
    ):
        raise ValueError("invalid event fields")
    if (
        event["schema"] != 1
        or type(event["schema"]) is not int
        or event["kind"] not in KINDS
    ):
        raise ValueError("unsupported schema or event type")
    uuid_string(event["id"])
    uuid_string(event["memory_id"])
    parents = event["parents"]
    if (
        not isinstance(parents, list)
        or len(parents) > 100
        or len(set(parents)) != len(parents)
    ):
        raise ValueError("invalid parents")
    for parent in parents:
        uuid_string(parent)
    hashes = event["parent_hashes"]
    if not isinstance(hashes, dict) or any(
        key not in parents
        or not isinstance(value, str)
        or len(value) != 64
        or any(c not in "0123456789abcdef" for c in value)
        for key, value in hashes.items()
    ):
        raise ValueError("invalid parent hashes")
    if event["kind"] in PRIVILEGED and set(hashes) != set(parents):
        raise ValueError("operator actions must bind all reviewed parent payloads")
    if event["id"] in parents:
        raise ValueError("self-parent")
    if event["kind"] == "proposal" and len(parents) > 1:
        raise ValueError("only an operator may resolve competing revisions")
    if event["kind"] == "approve" and len(parents) != 1:
        raise ValueError("approval must reference exactly one revision")
    if event["kind"] in {"delete", "resolve"} and not parents:
        raise ValueError("operator revision must reference existing heads")
    for key in ("host", "author", "created_at"):
        if not isinstance(event[key], str) or not 0 < len(event[key]) <= 500:
            raise ValueError(f"invalid {key}")
    if datetime.fromisoformat(event["created_at"]).utcoffset() is None:
        raise ValueError("created_at must have a timezone")
    if event["kind"] in {"proposal", "resolve"}:
        content = event["content"]
        fields = {
            "title",
            "text",
            "category",
            "hosts",
            "services",
            "evidence",
            "scope",
            "observed_at",
        }
        if not isinstance(content, dict) or content.keys() != fields:
            raise ValueError(
                "invalid content fields; verification cannot be supplied by an agent"
            )
        for key, limit in (("title", 500), ("text", 100000)):
            if not isinstance(content[key], str) or not 0 < len(content[key]) <= limit:
                raise ValueError(f"invalid {key}")
        if content["category"] not in {"incident", "fact", "decision", "runbook"}:
            raise ValueError("invalid category")
        if content["scope"] not in {"intended", "observed", "historical"}:
            raise ValueError("invalid scope")
        for key in ("hosts", "services", "evidence"):
            if (
                not isinstance(content[key], list)
                or len(content[key]) > 100
                or any(
                    not isinstance(item, str) or len(item) > 2000
                    for item in content[key]
                )
            ):
                raise ValueError(f"invalid {key}")
        if content["observed_at"] is not None:
            if (
                not isinstance(content["observed_at"], str)
                or datetime.fromisoformat(content["observed_at"]).utcoffset() is None
            ):
                raise ValueError("observed_at must have a timezone")
    elif event["content"] is not None:
        raise ValueError("approval/deletion cannot contain replacement content")
    if event["kind"] == "proposal" and ("signer" in event or "signature" in event):
        raise ValueError("proposals cannot claim an operator signature")
    if event["kind"] in PRIVILEGED and not {"signer", "signature"} <= event.keys():
        raise ValueError("operator signature required")
    if len(canonical(event)) > MAX_BYTES:
        raise ValueError("event too large")
    return event


def new_event(kind, content=None, memory_id=None, parents=(), author="operator"):
    return {
        "schema": 1,
        "id": str(uuid.uuid4()),
        "memory_id": memory_id or str(uuid.uuid4()),
        "kind": kind,
        "parents": sorted(parents),
        "parent_hashes": {},
        "host": socket.gethostname(),
        "author": author,
        "created_at": datetime.now(UTC).isoformat(),
        "content": content,
    }


def fingerprint(public):
    return hashlib.sha256(public).hexdigest()


def init_key(keys):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    private_dir(keys)
    private_dir(keys / "trusted")
    path = keys / "operator.key"
    if not path.exists():
        key = Ed25519PrivateKey.generate()
        raw = key.private_bytes(
            serialization.Encoding.Raw,
            serialization.PrivateFormat.Raw,
            serialization.NoEncryption(),
        )
        with path.open("xb") as stream:
            os.chmod(path, 0o600)
            stream.write(raw)
    key = Ed25519PrivateKey.from_private_bytes(path.read_bytes())
    public = key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    trust_key(keys, public.hex())
    return public.hex()


def trust_key(keys, public_hex):
    public = bytes.fromhex(public_hex.strip())
    if len(public) != 32:
        raise ValueError("expected an Ed25519 public key (64 hex characters)")
    private_dir(keys / "trusted")
    (keys / "trusted" / f"{fingerprint(public)}.pub").write_text(public.hex() + "\n")
    return fingerprint(public)


def sign_event(event, keys, parents):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    key = Ed25519PrivateKey.from_private_bytes((keys / "operator.key").read_bytes())
    public = key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    # Bind the action to the exact reviewed payloads, not just mutable UUID references.
    event = {
        **event,
        "signer": fingerprint(public),
        "parent_hashes": {
            parent: hashlib.sha256(canonical(parents[parent])).hexdigest()
            for parent in event["parents"]
        },
    }
    event["signature"] = key.sign(canonical(event)).hex()
    return validate(event)


def verify_event(event, keys):
    validate(event)
    if event["kind"] not in PRIVILEGED:
        return
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    signer = event["signer"]
    if (
        not isinstance(signer, str)
        or len(signer) != 64
        or any(c not in "0123456789abcdef" for c in signer)
    ):
        raise ValueError("invalid signer")
    path = keys / "trusted" / f"{signer}.pub"
    if not path.is_file():
        raise ValueError(f"untrusted operator key: {signer}")
    public = bytes.fromhex(path.read_text().strip())
    if fingerprint(public) != signer:
        raise ValueError("public key fingerprint mismatch")
    unsigned = {k: v for k, v in event.items() if k != "signature"}
    Ed25519PublicKey.from_public_bytes(public).verify(
        bytes.fromhex(event["signature"]), canonical(unsigned)
    )


def publish(records, event):
    validate(event)
    private_dir(records)
    payload = canonical(event) + b"\n"
    fd, temporary = tempfile.mkstemp(prefix=".pending-", dir=records)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        # A hard link publishes atomically without overwriting an existing event.
        os.link(temporary, records / f"{event['id']}.json")
        directory = os.open(records, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return event
