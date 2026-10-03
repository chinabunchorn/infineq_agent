"""Credential-free local manifests for versioned agent runs."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, Field, ValidationError, field_validator

from infineq.foundry.agent_registry import AgentVersionRecord, VerifierAgentVersionRecord
from infineq.foundry.verifier_tools import VERIFIER_TOOL_SCHEMA_SHA256
from infineq.schemas.common import StrictModel

_RUN_ID = re.compile(r"^run-[A-Za-z0-9_-]{1,96}$")


class RunManifest(StrictModel):
    """Redacted operational metadata stored below the ignored runs directory."""

    run_id: str = Field(pattern=r"^run-[A-Za-z0-9_-]{1,96}$")
    agent_name: str = Field(min_length=1, max_length=63)
    agent_version: str = Field(min_length=1, max_length=64)
    prompt_version: str = Field(pattern=r"^investigator-v[0-9]+$")
    prompt_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    episode_ids: tuple[str, ...] = Field(max_length=8)
    successful_tool_calls: int = Field(ge=0, le=6)
    forbidden_tool_calls: int = Field(ge=0)
    status: str = Field(pattern=r"^(complete|analysis_incomplete|blocked)$")
    response_ids: tuple[str, ...] = Field(max_length=32)
    latency_ms: float | None = Field(default=None, ge=0)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    unseen_evidence_count: int = Field(default=0, ge=0)
    raw_output_path: str = ""
    redacted_output_path: str = ""
    trace_path: str = ""

    @field_validator("raw_output_path", "redacted_output_path", "trace_path")
    @classmethod
    def relative_safe_paths(cls, value: str) -> str:
        if value.startswith("/") or ".." in Path(value).parts:
            raise ValueError("manifest paths must be relative and contained")
        return value


class VerifierDeploymentManifest(StrictModel):
    """Exact local identity needed before reusing a Verifier version."""

    agent_name: Literal["infineq-evidence-verifier"]
    agent_version: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    prompt_version: Literal["verifier-v1", "verifier-v2"]
    prompt_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    model_deployment_name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    tool_schema_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    created_at: AwareDatetime

    @field_validator("created_at")
    @classmethod
    def normalize_creation_timestamp(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)


class DeploymentManifestError(ValueError):
    """A verifier deployment manifest is absent or cannot be trusted."""


def deployment_manifest_path(project_root: Path) -> Path:
    """Return the ignored local deployment-manifest path."""

    return project_root / ".local" / "foundry" / "investigator_deployment.json"


def verifier_deployment_manifest_path(project_root: Path) -> Path:
    """Return the ignored local Verifier deployment-manifest path."""

    return project_root / ".local" / "foundry" / "verifier_deployment.json"


def run_manifest_path(project_root: Path, run_id: str) -> Path:
    """Return a contained path below the ignored designated runs directory."""

    if not _RUN_ID.fullmatch(run_id):
        raise ValueError("run ID is not allow-listed")
    return project_root / "data" / "infineq" / "v1" / "runs" / run_id / "run_manifest.json"


def _write_json(path: Path, payload: Mapping[str, object]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=True, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def write_deployment_manifest(project_root: Path, record: AgentVersionRecord) -> Path:
    """Persist only the pinned agent identity and non-secret model metadata."""

    return _write_json(deployment_manifest_path(project_root), record.to_manifest())


def write_verifier_deployment_manifest(
    project_root: Path,
    record: VerifierAgentVersionRecord,
) -> Path:
    """Persist only the Verifier's exact safe identity and creation metadata."""

    if record.tool_schema_sha256 != VERIFIER_TOOL_SCHEMA_SHA256:
        raise ValueError("verifier tool schema hash does not match the pinned tool schema")
    manifest = VerifierDeploymentManifest.model_validate(record.to_manifest())
    return _write_json(
        verifier_deployment_manifest_path(project_root),
        manifest.model_dump(mode="json"),
    )


def load_verifier_deployment_manifest(project_root: Path) -> VerifierDeploymentManifest:
    """Read and validate the exact local Verifier reuse manifest."""

    path = verifier_deployment_manifest_path(project_root)
    if not path.exists():
        raise FileNotFoundError("verifier deployment manifest is missing")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise DeploymentManifestError("verifier deployment manifest is unreadable") from error
    if not isinstance(payload, dict):
        raise DeploymentManifestError("verifier deployment manifest must be a JSON object")
    try:
        return VerifierDeploymentManifest.model_validate(payload)
    except ValidationError as error:
        raise DeploymentManifestError("verifier deployment manifest is invalid") from error


def write_run_manifest(project_root: Path, manifest: RunManifest) -> Path:
    """Persist one redacted run manifest under the ignored runs directory."""

    return _write_json(
        run_manifest_path(project_root, manifest.run_id),
        manifest.model_dump(mode="json"),
    )


__all__ = [
    "DeploymentManifestError",
    "RunManifest",
    "VerifierDeploymentManifest",
    "deployment_manifest_path",
    "load_verifier_deployment_manifest",
    "run_manifest_path",
    "verifier_deployment_manifest_path",
    "write_deployment_manifest",
    "write_run_manifest",
    "write_verifier_deployment_manifest",
]
