from __future__ import annotations

from pathlib import Path

from infineq.agents.prompt_manifest import (
    VERIFIER_PROMPT_VERSION,
    load_verifier_prompt,
    load_verifier_prompt_manifest,
)

PROJECT_ROOT = Path(__file__).parents[3]


def test_verifier_prompt_is_versioned_and_hashes_deterministically() -> None:
    first = load_verifier_prompt_manifest(project_root=PROJECT_ROOT)
    second = load_verifier_prompt_manifest(project_root=PROJECT_ROOT)

    assert first.version == VERIFIER_PROMPT_VERSION == "verifier-v2"
    assert len(first.prompt_sha256) == 64
    assert first.prompt_sha256 == second.prompt_sha256
    assert first.prompt_path.endswith("src/infineq/agents/prompts/verifier_v2.md")


def test_verifier_prompt_contains_exactly_the_nine_mandatory_checks() -> None:
    manifest = load_verifier_prompt_manifest(project_root=PROJECT_ROOT)
    prompt = load_verifier_prompt(project_root=PROJECT_ROOT).casefold()

    assert len(manifest.constraints) == 9
    for constraint in manifest.constraints:
        assert constraint.casefold() in prompt

    required_phrases = (
        "every cited evidence id exists",
        "cited object supports the associated claim",
        "support and contradiction are not swapped",
        "at least one credible competing explanation",
        "required evidence is fresh and complete",
        "provenance is displayed as synthetic replay",
        "causal wording does not exceed evidence",
        "action type, target, limits, ttl, and approval requirement match policy",
        "a missing prerequisite blocks presentation",
    )
    assert all(phrase in prompt for phrase in required_phrases)
    assert "investigator tools" in prompt
    assert "no filesystem" in prompt
    assert "no free-form alternative status" in prompt


def test_verifier_prompt_distinguishes_presentation_from_execution_approval() -> None:
    prompt = load_verifier_prompt(project_root=PROJECT_ROOT).casefold()
    assert "approval is not required before presentation" in prompt
    assert "approval is required before execution" in prompt
    assert "missing approval blocks presentation" not in prompt


def test_verifier_prompt_allows_only_the_three_frozen_statuses() -> None:
    prompt = load_verifier_prompt(project_root=PROJECT_ROOT).casefold()

    assert "verified" in prompt
    assert "revision_required" in prompt
    assert "blocked" in prompt
    assert '"status"' in prompt
    assert "indeterminate" not in prompt
    assert "analysis_incomplete" not in prompt
