from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from infineq.foundry import deployment as deployment_module
from infineq.foundry.agent_registry import AgentVersionRecord, VerifierAgentVersionRecord
from infineq.foundry.deployment import (
    RunManifest,
    deployment_manifest_path,
    run_manifest_path,
    write_deployment_manifest,
    write_run_manifest,
)
from infineq.foundry.verifier_tools import VERIFIER_TOOL_SCHEMA_SHA256


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


def test_verifier_manifest_persists_safe_hashes_and_normalized_utc_timestamp(
    tmp_path: Path,
) -> None:
    created_at = datetime(2026, 9, 14, 19, 0, tzinfo=timezone(timedelta(hours=7)))
    record = VerifierAgentVersionRecord(
        name="infineq-evidence-verifier",
        version="3",
        prompt_version="verifier-v1",
        prompt_sha256="a" * 64,
        model_deployment_name="model-under-test",
        tool_schema_sha256=VERIFIER_TOOL_SCHEMA_SHA256,
        created_at=created_at,
    )

    path = deployment_module.write_verifier_deployment_manifest(tmp_path, record)

    assert path == deployment_module.verifier_deployment_manifest_path(tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload == {
        "agent_name": "infineq-evidence-verifier",
        "agent_version": "3",
        "created_at": "2026-09-14T12:00:00Z",
        "model_deployment_name": "model-under-test",
        "prompt_sha256": "a" * 64,
        "prompt_version": "verifier-v1",
        "tool_schema_sha256": VERIFIER_TOOL_SCHEMA_SHA256,
    }
    assert "AZURE_AI_PROJECT_ENDPOINT" not in path.read_text(encoding="utf-8")
    assert "secret" not in path.read_text(encoding="utf-8").casefold()


def test_verifier_manifest_accepts_new_version_without_relabeling_archived_v1(
    tmp_path: Path,
) -> None:
    record = VerifierAgentVersionRecord(
        name="infineq-evidence-verifier",
        version="2",
        prompt_version="verifier-v2",
        prompt_sha256="b" * 64,
        model_deployment_name="model-under-test",
        tool_schema_sha256=VERIFIER_TOOL_SCHEMA_SHA256,
        created_at=datetime(2026, 9, 14, 12, 0, tzinfo=UTC),
    )
    deployment_module.write_verifier_deployment_manifest(tmp_path, record)
    loaded = deployment_module.load_verifier_deployment_manifest(tmp_path)
    assert loaded.prompt_version == "verifier-v2"
    assert loaded.agent_version == "2"


def test_verifier_manifest_rejects_a_tool_hash_that_is_not_the_pinned_schema(
    tmp_path: Path,
) -> None:
    record = VerifierAgentVersionRecord(
        name="infineq-evidence-verifier",
        version="3",
        prompt_version="verifier-v1",
        prompt_sha256="a" * 64,
        model_deployment_name="model-under-test",
        tool_schema_sha256="0" * 64,
        created_at=datetime(2026, 9, 14, 12, 0, tzinfo=UTC),
    )

    with pytest.raises(ValueError, match="tool schema"):
        deployment_module.write_verifier_deployment_manifest(tmp_path, record)
