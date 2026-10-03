from __future__ import annotations

import json
from pathlib import Path

import pytest

from infineq.evaluation.baselines import (
    BaselineEpisode,
    BaselineMode,
    BaselineVersions,
    DevelopmentOracle,
    IncomparableBaselineRunsError,
    MissingBaselineRunError,
    RecordedModeResult,
    ResourceBudget,
    baseline_run_directory,
    default_development_episode_ids,
    load_development_oracle,
    run_investigator_baseline,
    run_investigator_verifier_baseline,
    run_static_baseline,
    save_baseline_results,
    score_development_baselines,
    select_development_episode_ids,
    validate_matched_baseline_runs,
)
from infineq.evidence.tool_schemas import RunbookQueryEnum
from infineq.schemas.common import DataOrigin

BUDGET = ResourceBudget(
    budget_id="budget-v1",
    max_visible_evidence=3,
    max_successful_tool_calls=6,
    max_redundant_tool_calls=3,
    max_runbook_chunks=3,
    max_input_tokens=10_000,
    max_output_tokens=5_000,
    max_total_tokens=15_000,
    max_latency_ms=60_000.0,
)
VERSIONS = BaselineVersions(
    data_version="corpus-v1",
    agent_version="investigator-7",
    prompt_version="investigator-v1",
    tool_schema_version="tools-v1",
    workflow_version="workflow-v1",
)
TWO_AGENT_VERSIONS = BaselineVersions(
    data_version="corpus-v1",
    agent_version="investigator-7+verifier-3",
    prompt_version="investigator-v1+verifier-v1",
    tool_schema_version="investigator-tools-v1+verifier-tools-v1",
    workflow_version="workflow-v1",
)


def evidence_id(episode_id: str, kind: str, suffix: str) -> str:
    return f"ev:{episode_id}:service_metrics:observation:{kind}:p95:{suffix}"


def make_episode(
    episode_id: str | None = None,
    *,
    data_version: str = "corpus-v1",
    evidence_kinds: tuple[str, ...] = ("queue_depth", "queue_wait", "ttft"),
) -> BaselineEpisode:
    episode_id = episode_id or default_development_episode_ids()[0]
    evidence = tuple(
        {
            "evidence_id": evidence_id(episode_id, kind, f"{index + 1:08x}"),
            "kind": kind,
        }
        for index, kind in enumerate(evidence_kinds)
    )
    return BaselineEpisode(
        episode_id=episode_id,
        data_version=data_version,
        origin=DataOrigin.SYNTHETIC_REPLAY,
        visible_evidence=evidence,
        detector_status="incident",
        runbook_queries=(
            RunbookQueryEnum.CAPACITY_QUEUEING,
            RunbookQueryEnum.BACKEND_SLOWDOWN,
        ),
        runbook_chunk_ids=("runbook-capacity-queueing", "runbook-backend-slowdown"),
    )


def make_recorded(
    episode: BaselineEpisode,
    mode: BaselineMode,
    *,
    versions: BaselineVersions = VERSIONS,
    budget: ResourceBudget = BUDGET,
    diagnosis: str | None = "capacity_queueing",
    abstained: bool = False,
    action_card_eligible: bool = True,
    cited_evidence_ids: tuple[str, ...] | None = None,
    unsupported_claims: int = 0,
    policy_errors: int = 0,
    unseen_evidence: int = 0,
    forbidden_tool_calls: int = 0,
    successful_tool_calls: int = 2,
    redundant_tool_calls: int = 0,
    latency_ms: float | None = 120.0,
    input_tokens: int | None = 100,
    output_tokens: int | None = 40,
    total_tokens: int | None = 140,
    terminal_status: str = "complete",
    run_id: str | None = None,
    trace_refs: tuple[str, ...] = ("trace-baseline-1",),
) -> RecordedModeResult:
    return RecordedModeResult(
        mode=mode,
        episode_id=episode.episode_id,
        versions=versions,
        budget=budget,
        visible_evidence_ids=episode.visible_evidence_ids,
        terminal_status=terminal_status,
        diagnosis=diagnosis,
        abstained=abstained,
        action_card_eligible=action_card_eligible,
        cited_evidence_ids=(
            cited_evidence_ids
            if cited_evidence_ids is not None
            else episode.visible_evidence_ids[:2]
        ),
        unsupported_claims=unsupported_claims,
        policy_errors=policy_errors,
        unseen_evidence=unseen_evidence,
        forbidden_tool_calls=forbidden_tool_calls,
        successful_tool_calls=successful_tool_calls,
        redundant_tool_calls=redundant_tool_calls,
        latency_ms=latency_ms,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        run_id=run_id or f"run-baseline-{mode.value.replace('_', '-')}-{episode.episode_id}",
        trace_refs=trace_refs,
    )


def oracle_for(episode: BaselineEpisode) -> DevelopmentOracle:
    return DevelopmentOracle(
        episode_id=episode.episode_id,
        expected_diagnosis="capacity_queueing",
        required_evidence_kinds=("queue_depth", "queue_wait"),
        action_card_expected=True,
    )


def test_static_baseline_is_deterministic_and_has_no_agent_or_action() -> None:
    episode = make_episode()

    result = run_static_baseline(episode, budget=BUDGET)

    assert result.mode is BaselineMode.STATIC
    assert result.episode_id == episode.episode_id
    assert result.data_version == episode.data_version
    assert result.agent_version == "static-baseline-v1"
    assert result.prompt_version == "not_applicable"
    assert result.tool_schema_version == "not_applicable"
    assert result.workflow_version == "workflow-v1"
    assert result.abstained is True
    assert result.diagnosis is None
    assert result.action_card_eligible is False
    assert result.action_executed is False
    assert result.successful_tool_calls == 0
    assert result.redundant_tool_calls == 0
    assert result.input_tokens == 0
    assert result.output_tokens == 0
    assert result.total_tokens == 0
    assert result.curated_runbook_chunk_ids == episode.runbook_chunk_ids
    assert result.curated_runbook_queries == episode.runbook_queries


def test_recorded_one_agent_adapter_preserves_all_accounting_without_azure() -> None:
    episode = make_episode()
    recorded = make_recorded(episode, BaselineMode.INVESTIGATOR)

    result = run_investigator_baseline(episode, budget=BUDGET, recorded=recorded)

    assert result.mode is BaselineMode.INVESTIGATOR
    assert result.terminal_status.value == "complete"
    assert result.diagnosis == "capacity_queueing"
    assert result.abstained is False
    assert result.action_card_eligible is True
    assert result.agent_version == VERSIONS.agent_version
    assert result.prompt_version == VERSIONS.prompt_version
    assert result.tool_schema_version == VERSIONS.tool_schema_version
    assert result.successful_tool_calls == 2
    assert result.redundant_tool_calls == 0
    assert result.forbidden_tool_calls == 0
    assert result.unseen_evidence == 0
    assert result.latency_ms == 120.0
    assert result.input_tokens == 100
    assert result.output_tokens == 40
    assert result.total_tokens == 140
    assert result.run_id == recorded.run_id
    assert result.trace_refs == recorded.trace_refs


def test_recorded_two_agent_adapter_accepts_injected_result_and_never_executes_action() -> None:
    episode = make_episode()
    recorded = make_recorded(
        episode,
        BaselineMode.INVESTIGATOR_VERIFIER,
        versions=TWO_AGENT_VERSIONS,
        action_card_eligible=True,
    )
    called: list[str] = []

    def injected(_episode: BaselineEpisode, _budget: ResourceBudget) -> RecordedModeResult:
        called.append("injected")
        return recorded

    result = run_investigator_verifier_baseline(episode, budget=BUDGET, injected=injected)

    assert called == ["injected"]
    assert result.mode is BaselineMode.INVESTIGATOR_VERIFIER
    assert result.action_card_eligible is True
    assert result.action_executed is False


def test_all_modes_share_exact_visible_evidence_and_resource_budget() -> None:
    episode = make_episode()
    static = run_static_baseline(episode, budget=BUDGET)
    one = run_investigator_baseline(
        episode,
        budget=BUDGET,
        recorded=make_recorded(episode, BaselineMode.INVESTIGATOR),
    )
    two = run_investigator_verifier_baseline(
        episode,
        budget=BUDGET,
        recorded=make_recorded(
            episode,
            BaselineMode.INVESTIGATOR_VERIFIER,
            versions=TWO_AGENT_VERSIONS,
        ),
    )

    assert {static.visible_evidence_ids, one.visible_evidence_ids, two.visible_evidence_ids} == {
        episode.visible_evidence_ids
    }
    assert static.budget == one.budget == two.budget == BUDGET


def test_justification_rejects_improvement_that_blocks_the_only_action_case() -> None:
    episode = make_episode()
    one = run_investigator_baseline(
        episode,
        budget=BUDGET,
        recorded=make_recorded(
            episode,
            BaselineMode.INVESTIGATOR,
            unsupported_claims=1,
            policy_errors=1,
        ),
    )
    two = run_investigator_verifier_baseline(
        episode,
        budget=BUDGET,
        recorded=make_recorded(
            episode,
            BaselineMode.INVESTIGATOR_VERIFIER,
            versions=TWO_AGENT_VERSIONS,
            unsupported_claims=0,
            policy_errors=1,
        ),
    )
    static = run_static_baseline(episode, budget=BUDGET)

    evaluation = score_development_baselines(
        (episode,),
        (static, one, two),
        oracle_loader=lambda episode_id: oracle_for(episode),
        allowed_episode_ids={episode.episode_id},
    )

    assert evaluation.pass_count(BaselineMode.INVESTIGATOR) == 0
    assert evaluation.pass_count(BaselineMode.INVESTIGATOR_VERIFIER) == 0
    assert (
        evaluation.unsupported_claims_by_mode[BaselineMode.INVESTIGATOR_VERIFIER.value]
        < (evaluation.unsupported_claims_by_mode[BaselineMode.INVESTIGATOR.value])
    )
    assert evaluation.two_agent_justification_passed is False
    assert evaluation.policy_errors_by_mode[BaselineMode.INVESTIGATOR.value] == 1
    assert evaluation.policy_errors_by_mode[BaselineMode.INVESTIGATOR_VERIFIER.value] == 1


def test_justification_requires_the_canonical_queue_action_case() -> None:
    episode = make_episode("ep-a91e7c")
    static = run_static_baseline(episode, budget=BUDGET)
    one = run_investigator_baseline(
        episode,
        budget=BUDGET,
        recorded=make_recorded(episode, BaselineMode.INVESTIGATOR, unsupported_claims=1),
    )
    two = run_investigator_verifier_baseline(
        episode,
        budget=BUDGET,
        recorded=make_recorded(
            episode,
            BaselineMode.INVESTIGATOR_VERIFIER,
            versions=TWO_AGENT_VERSIONS,
        ),
    )
    evaluation = score_development_baselines(
        (episode,),
        (static, one, two),
        oracle_loader=lambda episode_id: oracle_for(episode),
        allowed_episode_ids={episode.episode_id},
    )
    assert evaluation.pass_count(BaselineMode.INVESTIGATOR_VERIFIER) == 1
    assert evaluation.two_agent_justification_passed is False


def test_canonical_action_case_and_grounding_improvement_can_pass_gate() -> None:
    episode = make_episode("ep-61d8aa")
    static = run_static_baseline(episode, budget=BUDGET)
    one = run_investigator_baseline(
        episode,
        budget=BUDGET,
        recorded=make_recorded(episode, BaselineMode.INVESTIGATOR, unsupported_claims=1),
    )
    two = run_investigator_verifier_baseline(
        episode,
        budget=BUDGET,
        recorded=make_recorded(
            episode,
            BaselineMode.INVESTIGATOR_VERIFIER,
            versions=TWO_AGENT_VERSIONS,
        ),
    )
    evaluation = score_development_baselines(
        (episode,),
        (static, one, two),
        oracle_loader=lambda episode_id: oracle_for(episode),
        allowed_episode_ids={episode.episode_id},
    )
    assert evaluation.two_agent_justification_passed is True


def test_full_episode_cannot_pass_with_an_invalid_schema_terminal_status() -> None:
    episode = make_episode()
    invalid = run_investigator_verifier_baseline(
        episode,
        budget=BUDGET,
        recorded=make_recorded(
            episode,
            BaselineMode.INVESTIGATOR_VERIFIER,
            versions=TWO_AGENT_VERSIONS,
            terminal_status="invalid_schema",
        ),
    )
    one = run_investigator_baseline(
        episode,
        budget=BUDGET,
        recorded=make_recorded(episode, BaselineMode.INVESTIGATOR),
    )
    evaluation = score_development_baselines(
        (episode,),
        (run_static_baseline(episode, budget=BUDGET), one, invalid),
        oracle_loader=lambda episode_id: oracle_for(episode),
        allowed_episode_ids={episode.episode_id},
    )
    assert evaluation.pass_count(BaselineMode.INVESTIGATOR_VERIFIER) == 0


def test_no_change_is_not_improvement_even_when_pass_count_is_equal() -> None:
    episode = make_episode()
    static = run_static_baseline(episode, budget=BUDGET)
    one = run_investigator_baseline(
        episode,
        budget=BUDGET,
        recorded=make_recorded(episode, BaselineMode.INVESTIGATOR, unsupported_claims=1),
    )
    two = run_investigator_verifier_baseline(
        episode,
        budget=BUDGET,
        recorded=make_recorded(
            episode,
            BaselineMode.INVESTIGATOR_VERIFIER,
            versions=TWO_AGENT_VERSIONS,
            unsupported_claims=1,
        ),
    )

    evaluation = score_development_baselines(
        (episode,),
        (static, one, two),
        oracle_loader=lambda _episode_id: oracle_for(episode),
        allowed_episode_ids={episode.episode_id},
    )

    assert evaluation.two_agent_justification_passed is False


def test_reduced_two_agent_pass_count_rejects_justification() -> None:
    episode = make_episode()
    second_episode = make_episode(default_development_episode_ids()[1])
    third_episode = make_episode(default_development_episode_ids()[2])
    static = tuple(
        run_static_baseline(item, budget=BUDGET)
        for item in (episode, second_episode, third_episode)
    )
    one = (
        run_investigator_baseline(
            episode,
            budget=BUDGET,
            recorded=make_recorded(episode, BaselineMode.INVESTIGATOR, unsupported_claims=1),
        ),
        run_investigator_baseline(
            second_episode,
            budget=BUDGET,
            recorded=make_recorded(second_episode, BaselineMode.INVESTIGATOR),
        ),
        run_investigator_baseline(
            third_episode,
            budget=BUDGET,
            recorded=make_recorded(third_episode, BaselineMode.INVESTIGATOR),
        ),
    )
    two = (
        run_investigator_verifier_baseline(
            episode,
            budget=BUDGET,
            recorded=make_recorded(
                episode,
                BaselineMode.INVESTIGATOR_VERIFIER,
                versions=TWO_AGENT_VERSIONS,
                unsupported_claims=0,
            ),
        ),
        run_investigator_verifier_baseline(
            second_episode,
            budget=BUDGET,
            recorded=make_recorded(
                second_episode,
                BaselineMode.INVESTIGATOR_VERIFIER,
                versions=TWO_AGENT_VERSIONS,
                diagnosis=None,
                abstained=True,
                action_card_eligible=False,
            ),
        ),
        run_investigator_verifier_baseline(
            third_episode,
            budget=BUDGET,
            recorded=make_recorded(
                third_episode,
                BaselineMode.INVESTIGATOR_VERIFIER,
                versions=TWO_AGENT_VERSIONS,
                diagnosis=None,
                abstained=True,
                action_card_eligible=False,
            ),
        ),
    )

    evaluation = score_development_baselines(
        (episode, second_episode, third_episode),
        (*static, *one, *two),
        oracle_loader=lambda episode_id: oracle_for(
            {item.episode_id: item for item in (episode, second_episode, third_episode)}[episode_id]
        ),
        allowed_episode_ids={
            episode.episode_id,
            second_episode.episode_id,
            third_episode.episode_id,
        },
    )

    assert evaluation.pass_count(BaselineMode.INVESTIGATOR) == 2
    assert evaluation.pass_count(BaselineMode.INVESTIGATOR_VERIFIER) == 1
    assert evaluation.two_agent_justification_passed is False


def test_comparison_rejects_missing_runs_and_does_not_open_oracles() -> None:
    episode = make_episode()
    one = run_investigator_baseline(
        episode,
        budget=BUDGET,
        recorded=make_recorded(episode, BaselineMode.INVESTIGATOR),
    )
    opened: list[str] = []

    with pytest.raises(MissingBaselineRunError):
        score_development_baselines(
            (episode,),
            (one,),
            oracle_loader=lambda episode_id: opened.append(episode_id) or oracle_for(episode),
            allowed_episode_ids={episode.episode_id},
        )

    assert opened == []


def test_agent_visible_contract_has_no_oracle_or_held_out_fields() -> None:
    episode = make_episode()
    recorded = make_recorded(episode, BaselineMode.INVESTIGATOR)
    serialized = json.dumps(
        {"episode": episode.model_dump(mode="json"), "run": recorded.model_dump(mode="json")}
    )

    assert "oracle" not in serialized.casefold()
    assert "expected_leading_family" not in serialized
    assert "scenario_family" not in serialized
    assert "variant" not in serialized.casefold()


def test_comparison_rejects_incomparable_versions_evidence_and_budgets() -> None:
    episode = make_episode()
    static = run_static_baseline(episode, budget=BUDGET)
    one = run_investigator_baseline(
        episode,
        budget=BUDGET,
        recorded=make_recorded(episode, BaselineMode.INVESTIGATOR),
    )
    two = run_investigator_verifier_baseline(
        episode,
        budget=BUDGET,
        recorded=make_recorded(
            episode,
            BaselineMode.INVESTIGATOR_VERIFIER,
            versions=TWO_AGENT_VERSIONS,
        ),
    )

    with pytest.raises(IncomparableBaselineRunsError, match="budget"):
        mismatched_budget_two = two.model_copy(
            update={"budget": BUDGET.model_copy(update={"budget_id": "budget-v2"})}
        )
        score_development_baselines(
            (episode,),
            (static, one, mismatched_budget_two),
            oracle_loader=lambda _episode_id: oracle_for(episode),
            allowed_episode_ids={episode.episode_id},
        )

    mismatched_version_two = two.model_copy(
        update={
            "versions": two.versions.model_copy(update={"workflow_version": "workflow-v2"}),
        }
    )
    with pytest.raises(IncomparableBaselineRunsError, match="workflow"):
        score_development_baselines(
            (episode,),
            (static, one, mismatched_version_two),
            oracle_loader=lambda _episode_id: oracle_for(episode),
            allowed_episode_ids={episode.episode_id},
        )

    mismatched_episode = make_episode(evidence_kinds=("queue_depth", "ttft", "itl"))
    mismatched_two = run_investigator_verifier_baseline(
        mismatched_episode,
        budget=BUDGET,
        recorded=make_recorded(
            mismatched_episode,
            BaselineMode.INVESTIGATOR_VERIFIER,
            versions=TWO_AGENT_VERSIONS,
        ),
    )
    with pytest.raises(IncomparableBaselineRunsError, match="evidence"):
        score_development_baselines(
            (episode,),
            (static, one, mismatched_two),
            oracle_loader=lambda _episode_id: oracle_for(episode),
            allowed_episode_ids={episode.episode_id},
        )


def test_oracle_boundary_selects_only_eight_variant_a_ids_and_never_held_out() -> None:
    development_ids = default_development_episode_ids()

    assert len(development_ids) == 8
    assert len(set(development_ids)) == 8
    assert all(item.startswith("ep-") for item in development_ids)

    with pytest.raises(PermissionError, match="development"):
        load_development_oracle(Path("/tmp/does-not-matter"), "ep-zzzzzz")

    held_out_episode = make_episode("ep-zzzzzz")
    with pytest.raises(IncomparableBaselineRunsError, match="outside"):
        validate_matched_baseline_runs((held_out_episode,), ())


def test_oracle_loader_runs_only_after_all_outputs_are_validated() -> None:
    episode = make_episode()
    one = run_investigator_baseline(
        episode,
        budget=BUDGET,
        recorded=make_recorded(episode, BaselineMode.INVESTIGATOR),
    )
    two = run_investigator_verifier_baseline(
        episode,
        budget=BUDGET,
        recorded=make_recorded(
            episode,
            BaselineMode.INVESTIGATOR_VERIFIER,
            versions=TWO_AGENT_VERSIONS,
        ),
    )
    opened: list[str] = []
    invalid_two = two.model_copy(update={"episode_id": default_development_episode_ids()[1]})

    with pytest.raises(IncomparableBaselineRunsError):
        score_development_baselines(
            (episode,),
            (one, invalid_two),
            oracle_loader=lambda episode_id: opened.append(episode_id) or oracle_for(episode),
            allowed_episode_ids={episode.episode_id},
        )

    assert opened == []


def test_result_files_use_contained_ignored_paths_stable_order_and_redaction(
    tmp_path: Path,
) -> None:
    episode = make_episode()
    one = run_investigator_baseline(
        episode,
        budget=BUDGET,
        recorded=make_recorded(episode, BaselineMode.INVESTIGATOR),
    )
    static = run_static_baseline(episode, budget=BUDGET)

    output = save_baseline_results(
        tmp_path,
        "run-baseline-recorded",
        (one, static),
    )

    assert output == baseline_run_directory(tmp_path, "run-baseline-recorded")
    assert output.is_relative_to(tmp_path / "data" / "infineq" / "v1" / "runs")
    payload = json.loads((output / "baseline_results.json").read_text(encoding="utf-8"))
    assert [item["mode"] for item in payload["runs"]] == ["static", "investigator"]
    serialized = json.dumps(payload)
    assert "api_key=secret" not in serialized
    assert '"prompt":' not in serialized.casefold()
    assert '"completion":' not in serialized.casefold()

    with pytest.raises(ValueError, match="run ID"):
        save_baseline_results(tmp_path, "../escape", (one, static))


def test_run_id_and_trace_references_reject_oracle_labels() -> None:
    episode = make_episode()

    with pytest.raises(ValueError):
        make_recorded(
            episode,
            BaselineMode.INVESTIGATOR,
            run_id=f"run-baseline-investigator-oracle-{episode.episode_id}",
        )

    with pytest.raises(ValueError):
        make_recorded(
            episode,
            BaselineMode.INVESTIGATOR,
            trace_refs=(f"trace-oracle-{episode.episode_id}",),
        )


def test_script_recorded_input_mode_writes_framework_results_without_live_calls(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from scripts import run_development_baselines

    episodes = tuple(make_episode(episode_id) for episode_id in default_development_episode_ids())
    static = tuple(run_static_baseline(episode, budget=BUDGET) for episode in episodes)
    one = tuple(
        run_investigator_baseline(
            episode,
            budget=BUDGET,
            recorded=make_recorded(episode, BaselineMode.INVESTIGATOR),
        )
        for episode in episodes
    )
    two = tuple(
        run_investigator_verifier_baseline(
            episode,
            budget=BUDGET,
            recorded=make_recorded(
                episode,
                BaselineMode.INVESTIGATOR_VERIFIER,
                versions=TWO_AGENT_VERSIONS,
            ),
        )
        for episode in episodes
    )
    input_path = tmp_path / "recorded.json"
    input_path.write_text(
        json.dumps(
            {
                "episodes": [episode.model_dump(mode="json") for episode in episodes],
                "runs": [run.model_dump(mode="json") for run in (*static, *one, *two)],
            }
        ),
        encoding="utf-8",
    )

    exit_code = run_development_baselines.main(
        [
            "--project-root",
            str(tmp_path),
            "--recorded-input",
            str(input_path),
            "--run-id",
            "run-baseline-script",
        ]
    )

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out)["run_id"] == "run-baseline-script"
    assert (tmp_path / "data" / "infineq" / "v1" / "runs" / "run-baseline-script").exists()


def test_script_live_opt_in_is_explicitly_blocked_in_this_slice(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from scripts import run_development_baselines

    assert run_development_baselines.main(["--live"]) == 2
    assert "blocked" in capsys.readouterr().err.casefold()


def test_missing_token_metrics_are_preserved_but_rejected_for_comparison() -> None:
    episode = make_episode()
    static = run_static_baseline(episode, budget=BUDGET)
    one = run_investigator_baseline(
        episode,
        budget=BUDGET,
        recorded=make_recorded(episode, BaselineMode.INVESTIGATOR, total_tokens=None),
    )
    two = run_investigator_verifier_baseline(
        episode,
        budget=BUDGET,
        recorded=make_recorded(
            episode,
            BaselineMode.INVESTIGATOR_VERIFIER,
            versions=TWO_AGENT_VERSIONS,
        ),
    )

    with pytest.raises(IncomparableBaselineRunsError, match="metrics"):
        score_development_baselines(
            (episode,),
            (static, one, two),
            oracle_loader=lambda _episode_id: oracle_for(episode),
            allowed_episode_ids={episode.episode_id},
        )


def test_static_baseline_rejects_an_episode_that_exceeds_visible_evidence_budget() -> None:
    episode = make_episode(evidence_kinds=("queue_depth", "queue_wait", "ttft", "itl"))
    small_budget = BUDGET.model_copy(update={"max_visible_evidence": 3})

    with pytest.raises(ValueError, match="evidence budget"):
        run_static_baseline(episode, budget=small_budget)


def test_recorded_result_rejects_unseen_evidence_count_mismatch() -> None:
    episode = make_episode()
    with pytest.raises(ValueError, match="unseen evidence"):
        make_recorded(
            episode,
            BaselineMode.INVESTIGATOR,
            cited_evidence_ids=(
                episode.visible_evidence_ids[0],
                f"ev:{episode.episode_id}:service_metrics:observation:ttft:p95:deadbeef",
            ),
            unseen_evidence=0,
        )


@pytest.mark.parametrize("mode", [BaselineMode.INVESTIGATOR, BaselineMode.INVESTIGATOR_VERIFIER])
def test_live_agent_adapters_require_recorded_or_injected_results(mode: BaselineMode) -> None:
    episode = make_episode()

    runner = (
        run_investigator_baseline
        if mode is BaselineMode.INVESTIGATOR
        else run_investigator_verifier_baseline
    )
    with pytest.raises(RuntimeError, match="recorded"):
        runner(episode, budget=BUDGET)


def test_development_selection_rejects_duplicates_expansion_and_truncation() -> None:
    selected = default_development_episode_ids()

    with pytest.raises(ValueError, match="duplicate"):
        select_development_episode_ids((*selected[:-1], selected[0]))
    with pytest.raises(ValueError, match="non-development"):
        select_development_episode_ids((*selected[:-1], "ep-zzzzzz"))
    with pytest.raises(ValueError, match="all eight"):
        select_development_episode_ids(selected[:-1])


def test_recorded_adapters_reject_conflicting_and_malformed_boundaries() -> None:
    episode = make_episode()
    one = make_recorded(episode, BaselineMode.INVESTIGATOR)

    with pytest.raises(ValueError, match="either"):
        run_investigator_baseline(
            episode,
            budget=BUDGET,
            recorded=one,
            injected=lambda _episode, _budget: one,
        )
    with pytest.raises(TypeError, match="RecordedModeResult"):
        run_investigator_baseline(
            episode,
            budget=BUDGET,
            injected=lambda _episode, _budget: object(),  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError, match="mode"):
        run_investigator_baseline(
            episode,
            budget=BUDGET,
            recorded=make_recorded(episode, BaselineMode.INVESTIGATOR_VERIFIER),
        )
    with pytest.raises(ValueError, match="episode"):
        run_investigator_baseline(
            episode,
            budget=BUDGET,
            recorded=make_recorded(
                make_episode(default_development_episode_ids()[1]),
                BaselineMode.INVESTIGATOR,
            ),
        )
    with pytest.raises(IncomparableBaselineRunsError, match="budget"):
        run_investigator_baseline(
            episode,
            budget=BUDGET.model_copy(update={"budget_id": "budget-v2"}),
            recorded=one,
        )
    mismatched_evidence = one.model_copy(update={"visible_evidence_ids": ()})
    with pytest.raises(IncomparableBaselineRunsError, match="visible evidence"):
        run_investigator_baseline(episode, budget=BUDGET, recorded=mismatched_evidence)


def test_baseline_persistence_and_oracle_boundaries_fail_closed(tmp_path: Path) -> None:
    episode = make_episode()
    one = run_investigator_baseline(
        episode,
        budget=BUDGET,
        recorded=make_recorded(episode, BaselineMode.INVESTIGATOR),
    )

    with pytest.raises(ValueError, match="at least one"):
        save_baseline_results(tmp_path, "run-baseline-empty", ())
    with pytest.raises(ValueError, match="duplicate"):
        save_baseline_results(tmp_path, "run-baseline-duplicate", (one, one))
    with pytest.raises(ValueError, match="protected"):
        baseline_run_directory(tmp_path, "run-baseline-oracle")
    with pytest.raises(ValueError, match="unavailable"):
        load_development_oracle(tmp_path, default_development_episode_ids()[0])


def test_oracle_loader_type_and_identity_are_checked_after_run_validation() -> None:
    episode = make_episode()
    static = run_static_baseline(episode, budget=BUDGET)
    one = run_investigator_baseline(
        episode,
        budget=BUDGET,
        recorded=make_recorded(episode, BaselineMode.INVESTIGATOR),
    )
    two = run_investigator_verifier_baseline(
        episode,
        budget=BUDGET,
        recorded=make_recorded(
            episode,
            BaselineMode.INVESTIGATOR_VERIFIER,
            versions=TWO_AGENT_VERSIONS,
        ),
    )

    with pytest.raises(TypeError, match="DevelopmentOracle"):
        score_development_baselines(
            (episode,),
            (static, one, two),
            oracle_loader=lambda episode_id: object(),  # type: ignore[return-value]
            allowed_episode_ids={episode.episode_id},
        )
    with pytest.raises(IncomparableBaselineRunsError, match="wrong episode"):
        score_development_baselines(
            (episode,),
            (static, one, two),
            oracle_loader=lambda episode_id: DevelopmentOracle(
                episode_id=default_development_episode_ids()[1],
                expected_diagnosis="capacity_queueing",
                required_evidence_kinds=(),
                action_card_expected=True,
            ),
            allowed_episode_ids={episode.episode_id},
        )
