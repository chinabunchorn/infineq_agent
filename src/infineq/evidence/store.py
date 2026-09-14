"""Read-only access to typed observed replay artifacts."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal, TypeAlias, cast

from pydantic import Field, ValidationError

from infineq.errors import DataMissingError, SchemaError
from infineq.evidence.paths import EvidenceArtifact, PathPolicy
from infineq.schemas.common import EvidenceId, StrictModel
from infineq.simulator.manifests import EpisodeId, ObservedManifest, Sha256

JsonObject: TypeAlias = dict[str, Any]  # noqa: UP040
JsonlRecords: TypeAlias = tuple[JsonObject, ...]  # noqa: UP040
ArtifactPayload: TypeAlias = JsonObject | JsonlRecords  # noqa: UP040

_JSONL_ARTIFACTS: Final = (
    EvidenceArtifact.REQUEST_EVENTS,
    EvidenceArtifact.SERVICE_METRICS,
    EvidenceArtifact.INFRASTRUCTURE_EVENTS,
    EvidenceArtifact.CHANGE_EVENTS,
    EvidenceArtifact.EVIDENCE_INDEX,
)
_ALLOWED_INDEX_SOURCES: Final = frozenset(
    artifact.value.removesuffix(".jsonl") for artifact in _JSONL_ARTIFACTS[:-1]
)
_EVIDENCE_ID_PATTERN: Final = re.compile(
    r"^ev:(?P<episode>ep-[a-z0-9]+):[a-z0-9_-]+:[a-z0-9_-]+:[a-z0-9_-]+:[a-z0-9_-]+:[a-f0-9]{8}$"
)


class EvidenceIndexRecord(StrictModel):
    """Validated, path-free index metadata for one observed evidence item."""

    evidence_id: EvidenceId
    episode_id: EpisodeId
    source: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9_]+$")
    signal: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9_]+$")
    aggregation: str = Field(min_length=1, max_length=32, pattern=r"^[a-z0-9_]+$")
    window_start_s: float = Field(ge=0)
    window_end_s: float = Field(ge=0)
    window: dict[str, str]
    unit: str = Field(min_length=1, max_length=32)
    freshness_s: float = Field(ge=0)
    data_origin: Literal["synthetic"]
    record_ordinal: int = Field(ge=0)
    content_hash: Sha256
    value: Any = None


@dataclass(frozen=True, slots=True)
class ObservedEpisode:
    """All agent-readable artifacts for one replay episode."""

    manifest: ObservedManifest
    requests: JsonlRecords
    service_metrics: JsonlRecords
    infrastructure_events: JsonlRecords
    change_events: JsonlRecords
    evidence_index: tuple[EvidenceIndexRecord, ...]


def _data_missing(message: str = "observed data is missing") -> DataMissingError:
    """Create a stable missing-data error without exposing filesystem details."""

    return DataMissingError(message)


def _schema_error(message: str, cause: BaseException | None = None) -> SchemaError:
    error = SchemaError(message)
    if cause is not None:
        error.__cause__ = cause
    return error


def _read_only_bytes(path: Path) -> bytes:
    """Read a regular file through a read-only, non-following file descriptor."""

    flags = os.O_RDONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    file_descriptor: int | None = None
    try:
        file_descriptor = os.open(path, flags)
        if not stat.S_ISREG(os.fstat(file_descriptor).st_mode):
            raise _data_missing()
        with os.fdopen(file_descriptor, "rb", closefd=True) as handle:
            file_descriptor = None
            return handle.read()
    except FileNotFoundError:
        raise _data_missing() from None
    except NotADirectoryError:
        raise _data_missing() from None
    except IsADirectoryError:
        raise _data_missing() from None
    except PermissionError:
        raise PermissionError("observed artifact access is denied") from None
    except OSError as error:
        if error.errno in {errno.EACCES, errno.EPERM, errno.ELOOP}:
            raise PermissionError("observed artifact access is denied") from None
        raise _data_missing() from None
    finally:
        if file_descriptor is not None:
            os.close(file_descriptor)


def _parse_json_object(payload: bytes) -> JsonObject:
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise _schema_error("observed JSON artifact is invalid", error) from error
    if not isinstance(value, dict):
        raise _schema_error("observed JSON artifact must contain an object")
    return cast(JsonObject, value)


def _parse_jsonl(payload: bytes) -> JsonlRecords:
    if not payload.strip():
        raise _schema_error("observed JSONL artifact is empty")
    records: list[JsonObject] = []
    for line in payload.splitlines():
        if not line.strip():
            raise _schema_error("observed JSONL artifact contains a blank record")
        try:
            value = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise _schema_error("observed JSONL artifact is invalid", error) from error
        if not isinstance(value, dict):
            raise _schema_error("observed JSONL records must contain objects")
        records.append(cast(JsonObject, value))
    return tuple(records)


def _validate_manifest(episode_id: str, payload: JsonObject) -> ObservedManifest:
    try:
        manifest = ObservedManifest.model_validate(payload)
    except ValidationError as error:
        raise _schema_error("observed manifest is invalid", error) from error
    if manifest.episode_id != episode_id:
        raise _schema_error("observed manifest identity does not match its episode")
    declared_paths = {artifact.path for artifact in manifest.artifact_checksums}
    expected_paths = {artifact.value for artifact in _JSONL_ARTIFACTS}
    if declared_paths != expected_paths:
        raise _schema_error("observed manifest artifact set is not allow-listed")
    return manifest


def _validate_index(
    episode_id: str,
    records: JsonlRecords,
) -> tuple[EvidenceIndexRecord, ...]:
    entries: list[EvidenceIndexRecord] = []
    seen_ids: set[str] = set()
    for record in records:
        try:
            entry = EvidenceIndexRecord.model_validate(record)
        except ValidationError as error:
            raise _schema_error("observed evidence index is invalid", error) from error
        if entry.episode_id != episode_id:
            raise _schema_error("observed evidence index crosses episode boundaries")
        identifier_match = _EVIDENCE_ID_PATTERN.fullmatch(entry.evidence_id)
        if identifier_match is None or identifier_match.group("episode") != episode_id:
            raise _schema_error("observed evidence index contains an invalid identity")
        if entry.source not in _ALLOWED_INDEX_SOURCES:
            raise _schema_error("observed evidence index contains an unapproved source")
        if entry.evidence_id in seen_ids:
            raise _schema_error("observed evidence index contains a duplicate identity")
        seen_ids.add(entry.evidence_id)
        entries.append(entry)
    return tuple(entries)


class EvidenceStore:
    """Read only the fixed observed corpus through allow-listed interfaces."""

    def __init__(self, *, project_root: Path) -> None:
        self._path_policy = PathPolicy(project_root=project_root)

    @property
    def path_policy(self) -> PathPolicy:
        """Return the immutable policy used for every file access."""

        return self._path_policy

    def _read_artifact(
        self, episode_id: str, artifact: EvidenceArtifact
    ) -> tuple[ArtifactPayload, str]:
        path = self._path_policy.artifact_path(episode_id, artifact)
        payload = _read_only_bytes(path)
        digest = hashlib.sha256(payload).hexdigest()
        if artifact is EvidenceArtifact.MANIFEST:
            return _parse_json_object(payload), digest
        return _parse_jsonl(payload), digest

    def read_artifact(
        self,
        episode_id: str,
        artifact: EvidenceArtifact,
    ) -> ArtifactPayload:
        """Read one allow-listed artifact; raw filenames and URLs are rejected."""

        if not isinstance(artifact, EvidenceArtifact):
            raise PermissionError("artifact is not allow-listed")
        payload, _ = self._read_artifact(episode_id, artifact)
        return payload

    def load_episode(self, episode_id: str) -> ObservedEpisode:
        """Load and validate all fixed observed artifacts for one episode."""

        manifest_payload, _ = self._read_artifact(episode_id, EvidenceArtifact.MANIFEST)
        if not isinstance(manifest_payload, dict):
            raise _schema_error("observed manifest is not an object")
        manifest = _validate_manifest(episode_id, manifest_payload)
        declared_checksums = {
            artifact.path: artifact.sha256 for artifact in manifest.artifact_checksums
        }

        loaded: dict[EvidenceArtifact, JsonlRecords] = {}
        for artifact in _JSONL_ARTIFACTS:
            payload, digest = self._read_artifact(episode_id, artifact)
            if not isinstance(payload, tuple):
                raise _schema_error("observed JSONL artifact has an invalid shape")
            if declared_checksums[artifact.value] != digest:
                raise _schema_error("observed artifact integrity check failed")
            loaded[artifact] = payload

        return ObservedEpisode(
            manifest=manifest,
            requests=loaded[EvidenceArtifact.REQUEST_EVENTS],
            service_metrics=loaded[EvidenceArtifact.SERVICE_METRICS],
            infrastructure_events=loaded[EvidenceArtifact.INFRASTRUCTURE_EVENTS],
            change_events=loaded[EvidenceArtifact.CHANGE_EVENTS],
            evidence_index=_validate_index(
                episode_id,
                loaded[EvidenceArtifact.EVIDENCE_INDEX],
            ),
        )

    def get_evidence(self, episode_id: str, evidence_id: str) -> EvidenceIndexRecord:
        """Resolve one exact evidence ID from the same opaque episode only."""

        self._path_policy.episode_directory(episode_id)
        if not isinstance(evidence_id, str) or len(evidence_id) > 256:
            raise _data_missing("requested evidence is missing")
        identifier_match = _EVIDENCE_ID_PATTERN.fullmatch(evidence_id)
        if identifier_match is None or identifier_match.group("episode") != episode_id:
            raise _data_missing("requested evidence is missing")

        payload, _ = self._read_artifact(episode_id, EvidenceArtifact.EVIDENCE_INDEX)
        if not isinstance(payload, tuple):
            raise _schema_error("observed evidence index has an invalid shape")
        for entry in _validate_index(episode_id, payload):
            if entry.evidence_id == evidence_id:
                return entry
        raise _data_missing("requested evidence is missing")


__all__ = [
    "EvidenceArtifact",
    "EvidenceIndexRecord",
    "EvidenceStore",
    "ObservedEpisode",
]
