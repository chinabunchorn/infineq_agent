from __future__ import annotations

import json
from pathlib import Path

import pytest

from infineq.foundry.agent_registry import AgentVersionRecord
from infineq.foundry.deployment import (
    RunManifest,
    deployment_manifest_path,
    run_manifest_path,
    write_deployment_manifest,
    write_run_manifest,
)


def record() -> AgentVersionRecord:
    return AgentVersionRecord(
        name="infineq-investigator",
        version="7",
        prompt_version="investigator-v1",
        prompt_sha256="a" * 64,
        model_deployment_name="model-under-test",
    )


def test_deployment_manifest_is_local_and_credential_free(tmp_path: Path) -> None:
    path = write_deployment_manifest(tmp_path, record())

    assert path == deployment_manifest_path(tmp_path)
    payload = json.loads(path.read_text())
    assert payload == {
        "agent_name": "infineq-investigator",
        "agent_version": "7",
        "model_deployment_name": "model-under-test",
        "prompt_sha256": "a" * 64,
        "prompt_version": "investigator-v1",
    }
    assert "endpoint" not in payload
    assert "secret" not in path.read_text().casefold()


def test_run_manifest_rejects_path_traversal_and_writes_safe_metadata(tmp_path: Path) -> None:
    manifest = RunManifest(
        run_id="run-20260914-010000",
        agent_name="infineq-investigator",
        agent_version="7",
        prompt_version="investigator-v1",
        prompt_sha256="a" * 64,
        episode_ids=("ep-61d8aa",),
        successful_tool_calls=2,
        forbidden_tool_calls=0,
        status="complete",
        response_ids=("resp-1", "resp-2"),
        latency_ms=123.4,
        input_tokens=10,
        output_tokens=20,
        total_tokens=30,
        unseen_evidence_count=0,
        raw_output_path="raw/output.json",
        redacted_output_path="redacted/output.json",
        trace_path="trace.json",
    )
    path = write_run_manifest(tmp_path, manifest)

    assert path == run_manifest_path(tmp_path, manifest.run_id)
    payload = json.loads(path.read_text())
    assert payload["episode_ids"] == ["ep-61d8aa"]
    assert payload["raw_output_path"] == "raw/output.json"
    assert payload["total_tokens"] == 30
    assert payload["unseen_evidence_count"] == 0
    assert "AZURE_AI_PROJECT_ENDPOINT" not in path.read_text()

    with pytest.raises(ValueError):
        RunManifest(
            run_id="../escape",
            agent_name="infineq-investigator",
            agent_version="7",
            prompt_version="investigator-v1",
            prompt_sha256="a" * 64,
            episode_ids=(),
            successful_tool_calls=0,
            forbidden_tool_calls=0,
            status="incomplete",
            response_ids=(),
            latency_ms=None,
            input_tokens=None,
            output_tokens=None,
            raw_output_path="",
            redacted_output_path="",
            trace_path="",
        )
