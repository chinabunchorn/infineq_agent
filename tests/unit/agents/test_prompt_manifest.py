from __future__ import annotations

from pathlib import Path

import pytest

from infineq.agents.prompt_manifest import (
    INVESTIGATOR_PROMPT_VERSION,
    load_investigator_prompt,
    load_prompt_manifest,
)

PROJECT_ROOT = Path(__file__).parents[3]


def test_investigator_prompt_is_versioned_and_hashes_deterministically() -> None:
    first = load_prompt_manifest(project_root=PROJECT_ROOT)
    second = load_prompt_manifest(project_root=PROJECT_ROOT)

    assert first.version == INVESTIGATOR_PROMPT_VERSION
    assert first.version == "investigator-v14"
    assert len(first.prompt_sha256) == 64
    assert first.prompt_sha256 == second.prompt_sha256
    assert first.prompt_sha256 == first.prompt_sha256.casefold()
    assert first.prompt_path.endswith("src/infineq/agents/prompts/investigator_v14.md")


def test_prompt_contains_all_nine_frozen_constraints() -> None:
    manifest = load_prompt_manifest(project_root=PROJECT_ROOT)
    prompt = load_investigator_prompt(project_root=PROJECT_ROOT)

    assert len(manifest.constraints) == 9
    for constraint in manifest.constraints:
        assert constraint in prompt
    assert "chain-of-thought" in prompt.casefold()
    assert "hidden reasoning" in prompt.casefold()
    assert "hard budget of six successful tool calls" in prompt.casefold()
    assert "investigation_id, incident_id, completed_at, disposition, summary" in prompt.casefold()
    assert "do not use top-level `coverage` or `missing_evidence`" in prompt.casefold()
    assert "hypothesis_id, family, rank, evidence_coverage" in prompt.casefold()
    assert "supporting_evidence_ids and contradicting_evidence_ids" in prompt.casefold()
    assert "plan_id, incident_id, action_type, target" in prompt.casefold()
    assert "investigation-<episode>" in prompt.casefold()
    assert "target must be an object with `kind` and `service_id`" in prompt.casefold()
    assert "always serialize `limitations`" in prompt.casefold()
    assert "include all four exact family enum values" in prompt.casefold()
    assert "replica_or_deployment_regression" in prompt.casefold()
    assert "must call `get_incident_packet` first" in prompt.casefold()
    assert "every evidence id used in a hypothesis" in prompt.casefold()
    assert "do not use the words `proven`" in prompt.casefold()
    assert '"leading_hypothesis_id": null' in prompt.casefold()
    assert "keep the summary under 700 characters" in prompt.casefold()
    assert "do not repeat long evidence-id lists" in prompt.casefold()
    assert (
        "when a tool call returns successfully, treat its returned payload as evidence"
        in prompt.casefold()
    )
    assert (
        "elevated ttft plus persistent queue depth/wait with stable itl and no "
        "contradicting error signal supports `capacity_queueing`" in prompt.casefold()
    )
    assert (
        "when complete grounded packet evidence supports that family, emit `diagnosed`"
        in prompt.casefold()
    )
    assert (
        "do not claim that no tools were available after a successful function call"
        in prompt.casefold()
    )
    assert "within the six-call application maximum" in prompt.casefold()
    assert (
        "after a successful get_incident_packet call with complete evidence, immediately "
        "return the final json on the next response" in prompt.casefold()
    )
    assert "never return four hypotheses" in prompt.casefold()
    assert "strict json schema derived from `investigationresultv1`" in prompt.casefold()
    assert "omits only the defaulted `schema_version` property" in prompt.casefold()
    assert "every other admitted property is required" in prompt.casefold()


def test_prompt_loader_fails_closed_when_the_versioned_file_is_missing(tmp_path: Path) -> None:
    (tmp_path / "src" / "infineq" / "agents" / "prompts").mkdir(parents=True)

    with pytest.raises(FileNotFoundError):
        load_prompt_manifest(project_root=tmp_path)


def test_prompt_manifest_does_not_expose_environment_values() -> None:
    manifest = load_prompt_manifest(project_root=PROJECT_ROOT)
    serialized = manifest.model_dump_json()

    assert "AZURE_AI_PROJECT_ENDPOINT" not in serialized
    assert "subscription" not in serialized.casefold()
    assert "tenant" not in serialized.casefold()
