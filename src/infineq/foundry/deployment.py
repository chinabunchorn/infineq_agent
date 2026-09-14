"""Credential-free local manifests for versioned agent runs."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path

from pydantic import Field, field_validator

from infineq.foundry.agent_registry import AgentVersionRecord
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


def deployment_manifest_path(project_root: Path) -> Path:
    """Return the ignored local deployment-manifest path."""

    return project_root / ".local" / "foundry" / "investigator_deployment.json"


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


def write_run_manifest(project_root: Path, manifest: RunManifest) -> Path:
    """Persist one redacted run manifest under the ignored runs directory."""

    return _write_json(
        run_manifest_path(project_root, manifest.run_id),
        manifest.model_dump(mode="json"),
    )


__all__ = [
    "RunManifest",
    "deployment_manifest_path",
    "run_manifest_path",
    "write_deployment_manifest",
    "write_run_manifest",
]
