"""Stable identifiers for immutable synthetic evidence."""

from __future__ import annotations

import re
from typing import Any

from infineq.simulator.serialization import sha256_bytes, stable_json_bytes

_SAFE_SEGMENT = re.compile(r"^[a-z0-9_-]+$")
_PATH_KEYS = frozenset({"path", "filesystem_path", "artifact_path", "root"})


def _without_filesystem_context(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _without_filesystem_context(item)
            for key, item in value.items()
            if str(key) not in _PATH_KEYS
        }
    if isinstance(value, list):
        return [_without_filesystem_context(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_without_filesystem_context(item) for item in value)
    return value


def normalized_content_hash(content: Any) -> str:
    """Hash canonical content after removing filesystem-only context."""

    return sha256_bytes(stable_json_bytes(_without_filesystem_context(content)))


def _validate_segment(name: str, value: str) -> str:
    if not value or not _SAFE_SEGMENT.fullmatch(value):
        raise ValueError(f"evidence {name} is unsafe")
    return value


def evidence_id(
    *,
    episode_id: str,
    source: str,
    window: str,
    signal: str,
    aggregation: str,
    content: Any,
) -> str:
    """Create the frozen evidence ID from normalized content, never a path."""

    _validate_segment(
        "episode", episode_id.removeprefix("ep-") if episode_id.startswith("ep-") else episode_id
    )
    if not episode_id.startswith("ep-"):
        raise ValueError("evidence episode is unsafe")
    segments = (
        episode_id,
        _validate_segment("source", source),
        _validate_segment("window", window),
        _validate_segment("signal", signal),
        _validate_segment("aggregation", aggregation),
    )
    return f"ev:{':'.join(segments)}:{normalized_content_hash(content)[:8]}"
