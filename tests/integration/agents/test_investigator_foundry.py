from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from dotenv import dotenv_values

from infineq.agents.investigator import FROZEN_INCIDENT_FAMILIES, validate_investigation_result
from infineq.detection.detector import Detector
from infineq.evidence.store import EvidenceStore
from infineq.foundry.deployment import run_manifest_path
from infineq.schemas.action import ActionType
from infineq.schemas.investigation import Disposition, InvestigationResultV1

PROJECT_ROOT = Path(__file__).parents[3]
SCRIPT = PROJECT_ROOT / "scripts" / "run_development_investigator.py"
CANONICAL_ID = "ep-61d8aa"
RUN_ID = "run-integration-canonical-gate"


def _live_prerequisites_exist() -> bool:
    values = dict(dotenv_values(PROJECT_ROOT / ".env"))
    values.update(os.environ)
    return bool(
        values.get("AZURE_AI_PROJECT_ENDPOINT")
        and values.get("AZURE_AI_MODEL_DEPLOYMENT_NAME")
        and (PROJECT_ROOT / ".local" / "foundry" / "investigator_deployment.json").exists()
    )


def _live_gate_skip_reason() -> str | None:
    if os.environ.get("INFIN_EQ_RUN_LIVE_FOUNDRY") != "1":
        return "INFIN_EQ_RUN_LIVE_FOUNDRY=1 is required"
    if not _live_prerequisites_exist():
        return "live Foundry endpoint/model and exact local agent manifest are unavailable"
    return None


def test_live_gate_requires_explicit_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("INFIN_EQ_RUN_LIVE_FOUNDRY", raising=False)

    def fail_if_prerequisites_are_checked() -> bool:
        raise AssertionError("prerequisites must not be inspected without opt-in")

    monkeypatch.setattr(
        sys.modules[__name__], "_live_prerequisites_exist", fail_if_prerequisites_are_checked
    )

    assert _live_gate_skip_reason() == "INFIN_EQ_RUN_LIVE_FOUNDRY=1 is required"


def test_live_gate_runs_when_opted_in_and_prerequisites_exist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("INFIN_EQ_RUN_LIVE_FOUNDRY", "1")
    monkeypatch.setattr(sys.modules[__name__], "_live_prerequisites_exist", lambda: True)

    assert _live_gate_skip_reason() is None


def _load_run_artifacts() -> tuple[
    dict[str, object], InvestigationResultV1, list[dict[str, object]]
]:
    manifest_path = run_manifest_path(PROJECT_ROOT, f"{RUN_ID}-{CANONICAL_ID}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert isinstance(manifest, dict)
    redacted_path = manifest_path.parent / str(manifest["redacted_output_path"])
    trace_path = manifest_path.parent / str(manifest["trace_path"])
    result = InvestigationResultV1.model_validate(
        json.loads(redacted_path.read_text(encoding="utf-8"))
    )
    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    assert isinstance(trace, list)
    assert all(isinstance(record, dict) for record in trace)
    return manifest, result, trace


@pytest.mark.integration
def test_canonical_development_episode_foundry_phase4_gate() -> None:
    skip_reason = _live_gate_skip_reason()
    if skip_reason is not None:
        pytest.skip(skip_reason)

    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--project-root",
            str(PROJECT_ROOT),
            "--episode-id",
            CANONICAL_ID,
            "--run-id-prefix",
            RUN_ID.removesuffix(f"-{CANONICAL_ID}"),
        ],
        cwd=PROJECT_ROOT,
        env={**os.environ, "PYTHONPATH": ""},
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["agent_name"] == "infineq-investigator"
    assert len(payload["episodes"]) == 1
    episode = payload["episodes"][0]
    assert episode["episode_id"] == CANONICAL_ID
    assert episode["disposition"] == "diagnosed"
    assert episode["status"] == "complete"
    assert episode["successful_tool_calls"] <= 6

    manifest, result, trace = _load_run_artifacts()
    assert manifest["status"] == "complete"
    assert manifest["forbidden_tool_calls"] == 0
    assert manifest["unseen_evidence_count"] == 0
    assert manifest["successful_tool_calls"] == len(trace)
    assert result.disposition is Disposition.DIAGNOSED
    assert result.incident_id == CANONICAL_ID

    returned_evidence_ids = {
        evidence_id for record in trace for evidence_id in record.get("returned_evidence_ids", [])
    }
    packet = Detector(store=EvidenceStore(project_root=PROJECT_ROOT)).detect(CANONICAL_ID)
    assert packet is not None
    validated = validate_investigation_result(
        result.model_dump(mode="json"),
        packet=packet,
        returned_evidence_ids=returned_evidence_ids,
    )
    assert validated.disposition is Disposition.DIAGNOSED

    compared_text = " ".join(
        (
            result.summary,
            *result.limitations,
            *(hypothesis.statement for hypothesis in result.hypotheses),
            *(
                missing
                for hypothesis in result.hypotheses
                for missing in hypothesis.missing_evidence
            ),
        )
    )
    represented_families = {hypothesis.family.value for hypothesis in result.hypotheses}
    for family in FROZEN_INCIDENT_FAMILIES:
        assert family in represented_families or family in compared_text

    action = result.proposed_action
    assert action is not None
    assert action.action_type is ActionType.SIMULATED_SCALE_OUT
    assert action.target.kind == "simulator"
    assert action.from_replicas == 1
    assert action.to_replicas == 2
    assert action.requires_human_approval is True
    assert action.policy_ref == "policy-infineq-v1"
