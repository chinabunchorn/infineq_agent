"""Canonical serialization helpers for simulator artifacts."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


def _json_safe(value: Any) -> Any:
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite numbers are not valid simulator JSON")
        return round(value, 6)
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def stable_json_bytes(value: Any) -> bytes:
    """Encode JSON with fixed ordering, separators, and a terminal newline."""

    return (
        json.dumps(
            _json_safe(value),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        + b"\n"
    )


def stable_json_text(value: Any) -> str:
    """Return canonical JSON text."""

    return stable_json_bytes(value).decode("utf-8")


def stable_jsonl_bytes(records: list[dict[str, Any]]) -> bytes:
    """Encode already ordered records as canonical JSONL."""

    return b"".join(stable_json_bytes(record) for record in records)


def sha256_bytes(value: bytes) -> str:
    """Hash bytes with SHA-256."""

    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    """Hash one file without including its filesystem path."""

    return sha256_bytes(path.read_bytes())


def write_stable_json(path: Path, value: Any) -> str:
    """Write one canonical JSON document and return its content hash."""

    payload = stable_json_bytes(value)
    path.write_bytes(payload)
    return sha256_bytes(payload)


def write_stable_jsonl(path: Path, records: list[dict[str, Any]]) -> str:
    """Write canonical JSONL and return its content hash."""

    payload = stable_jsonl_bytes(records)
    path.write_bytes(payload)
    return sha256_bytes(payload)


_BASE_TIME = datetime(2026, 1, 1, tzinfo=UTC)


def timestamp_for_seconds(seconds: float) -> str:
    """Render replay time from a fixed UTC epoch with millisecond precision."""

    milliseconds = round(seconds * 1_000)
    return (
        (_BASE_TIME + timedelta(milliseconds=milliseconds))
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def seconds_for_timestamp(value: str) -> float:
    """Parse one fixed-epoch replay timestamp into seconds."""

    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return (parsed.astimezone(UTC) - _BASE_TIME).total_seconds()
