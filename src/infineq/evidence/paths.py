"""Safe paths for the agent-readable observed evidence tree."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Final


class EvidenceArtifact(StrEnum):
    """The only artifact files an evidence store may open."""

    MANIFEST = "manifest.json"
    REQUEST_EVENTS = "request_events.jsonl"
    SERVICE_METRICS = "service_metrics.jsonl"
    INFRASTRUCTURE_EVENTS = "infrastructure_events.jsonl"
    CHANGE_EVENTS = "change_events.jsonl"
    EVIDENCE_INDEX = "evidence_index.jsonl"


_OBSERVED_RELATIVE_ROOT: Final = Path("data") / "infineq" / "v1" / "observed"
_EPISODE_ID_PATTERN: Final = re.compile(r"^ep-[a-z0-9]+(?:-[a-z0-9]+)*$")
_ARTIFACTS_BY_FILENAME: Final = {artifact.value: artifact for artifact in EvidenceArtifact}


def _permission_denied(message: str) -> PermissionError:
    """Create a policy error without disclosing a filesystem path."""

    return PermissionError(message)


def _validate_episode_id(episode_id: object) -> str:
    if (
        not isinstance(episode_id, str)
        or len(episode_id) > 64
        or _EPISODE_ID_PATTERN.fullmatch(episode_id) is None
    ):
        raise _permission_denied("episode identity is not opaque and allow-listed")
    return episode_id


def _validate_artifact(artifact: object) -> EvidenceArtifact:
    if isinstance(artifact, EvidenceArtifact):
        return artifact
    if isinstance(artifact, str) and artifact in _ARTIFACTS_BY_FILENAME:
        return _ARTIFACTS_BY_FILENAME[artifact]
    raise _permission_denied("artifact is not allow-listed")


def _require_contained(candidate: Path, root: Path) -> Path:
    if not candidate.is_relative_to(root):
        raise _permission_denied("resolved path must remain under the observed root")
    return candidate


def _resolve_without_symlink(path: Path) -> Path:
    if path.is_symlink():
        raise _permission_denied("observed paths must not use symlinks")
    try:
        return path.resolve()
    except RuntimeError:
        raise _permission_denied("observed path resolution failed") from None


@dataclass(frozen=True, slots=True)
class PathPolicy:
    """Resolve only fixed, allow-listed paths below ``data/infineq/v1/observed``."""

    project_root: Path
    observed_root: Path = field(init=False)

    def __post_init__(self) -> None:
        try:
            resolved_project_root = Path(self.project_root).resolve()
        except RuntimeError:
            raise _permission_denied("project root resolution failed") from None
        lexical_observed_root = resolved_project_root / _OBSERVED_RELATIVE_ROOT
        resolved_observed_root = _resolve_without_symlink(lexical_observed_root)
        if resolved_observed_root != lexical_observed_root:
            raise _permission_denied("fixed observed root must not be a symlink")
        object.__setattr__(self, "project_root", resolved_project_root)
        object.__setattr__(self, "observed_root", resolved_observed_root)

    def episode_directory(self, episode_id: str) -> Path:
        """Return one opaque episode directory, rejecting traversal and symlinks out."""

        safe_episode_id = _validate_episode_id(episode_id)
        candidate = _resolve_without_symlink(self.observed_root / safe_episode_id)
        return _require_contained(candidate, self.observed_root)

    def artifact_path(self, episode_id: str, artifact: EvidenceArtifact | str) -> Path:
        """Return one canonical allow-listed artifact path under an episode."""

        episode_directory = self.episode_directory(episode_id)
        safe_artifact = _validate_artifact(artifact)
        candidate = _resolve_without_symlink(episode_directory / safe_artifact.value)
        _require_contained(candidate, episode_directory)
        return _require_contained(candidate, self.observed_root)


__all__ = ["EvidenceArtifact", "PathPolicy"]
