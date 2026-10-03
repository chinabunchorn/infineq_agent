from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from infineq.detection.detector import Detector
from infineq.evaluation import live_baselines
from infineq.evaluation.baselines import (
    BaselineEvaluation,
    BaselineMode,
    BaselineRunRecord,
    DevelopmentOracle,
    EpisodeScore,
    ModePassCount,
    baseline_run_directory,
)
from infineq.evaluation.live_baselines import (
    run_all_development_live_modes,
    validate_live_artifacts,
)
from infineq.evidence.store import EvidenceStore
from infineq.schemas.incident import IncidentPacketV1
from infineq.schemas.investigation import InvestigationResultV1
from infineq.schemas.verification import VerificationResultV1
from infineq.workflow.corrections import CorrectionPacketV1

PROJECT_ROOT = Path(__file__).parents[3]


def test_episode_metrics_do_not_claim_justification_for_noncanonical_action_case() -> None:
    episode_id = "ep-4b6fa0"
    modes = (
        BaselineMode.STATIC,
        BaselineMode.INVESTIGATOR,
        BaselineMode.INVESTIGATOR_VERIFIER,
    )
    evaluation = BaselineEvaluation(
        episode_ids=(episode_id,),
        scores=tuple(
            EpisodeScore(
                episode_id=episode_id,
                mode=mode,
                diagnostic_pass=True,
                full_pass=(mode is BaselineMode.INVESTIGATOR_VERIFIER),
            )
            for mode in modes
        ),
        pass_counts=tuple(
            ModePassCount(
                mode=mode,
                pass_count=int(mode is BaselineMode.INVESTIGATOR_VERIFIER),
                episode_count=1,
            )
            for mode in modes
        ),
        unsupported_claims_by_mode={mode.value: 0 for mode in modes},
        policy_errors_by_mode={mode.value: 0 for mode in modes},
        two_agent_justification_passed=False,
    )
    runs = tuple(
        BaselineRunRecord.model_construct(
            episode_id=episode_id,
            mode=mode,
            unsupported_claims=int(mode is BaselineMode.INVESTIGATOR),
            policy_errors=0,
            action_card_eligible=(mode is BaselineMode.INVESTIGATOR_VERIFIER),
        )
        for mode in modes
    )

    episode = live_baselines._episode_evaluation(evaluation, episode_id, runs)

    assert episode.two_agent_justification_passed is False


def test_development_live_run_id_is_allowlisted_and_contained(tmp_path: Path) -> None:
    output = baseline_run_directory(tmp_path, "run-phase5-development-live-v1")

    assert output == (
        tmp_path.resolve() / "data" / "infineq" / "v1" / "runs" / "run-phase5-development-live-v1"
    )


def test_all_development_selection_is_exact_and_rejects_caller_ids() -> None:
    from infineq.evaluation import live_baselines

    selected = live_baselines.select_development_episode_ids()

    assert len(selected) == 8
    assert len(set(selected)) == 8
    assert selected == tuple(sorted(selected))
    assert all(episode_id.startswith("ep-") for episode_id in selected)

    with pytest.raises(ValueError, match="caller-supplied"):
        live_baselines.select_development_episode_ids((selected[0],))

    with pytest.raises(ValueError, match="duplicate"):
        live_baselines.select_development_episode_ids((*selected, selected[0]))

    with pytest.raises(ValueError, match="development"):
        live_baselines.select_development_episode_ids((*selected[:-1], "ep-heldout"))


def _policy() -> Any:
    from tests.integration.test_workflow_paths import policy_for

    return policy_for()


def test_all_development_live_modes_persist_24_records_before_loading_oracles(
    tmp_path: Path,
) -> None:
    from infineq.detection.detector import Detector
    from infineq.evidence.store import EvidenceStore

    events: list[str] = []

    class FailingInvestigator:
        def investigate(self, packet: IncidentPacketV1) -> InvestigationResultV1:
            events.append(f"agent:{packet.incident_id}")
            return live_baselines._incomplete_investigation(packet)

    class NeverCalledVerifier:
        def verify(
            self,
            packet: IncidentPacketV1,
            investigation: InvestigationResultV1,
        ) -> VerificationResultV1:
            raise AssertionError("the invalid initial result must not reach the Verifier")

    def oracle_loader(episode_id: str) -> DevelopmentOracle:
        events.append(f"oracle:{episode_id}")
        return DevelopmentOracle(
            episode_id=episode_id,
            expected_diagnosis=None,
            required_evidence_kinds=(),
            action_card_expected=False,
        )

    execution = run_all_development_live_modes(
        project_root=PROJECT_ROOT,
        run_id="run-phase5-development-live-v2",
        detector=Detector(store=EvidenceStore(project_root=PROJECT_ROOT)),
        investigator=FailingInvestigator(),
        verifier=NeverCalledVerifier(),
        policy=_policy(),
        oracle_loader=oracle_loader,
        artifact_root=tmp_path,
    )

    selected = live_baselines.select_development_episode_ids()
    assert execution.episode_ids == selected
    assert len(execution.runs) == 24
    assert {run.episode_id for run in execution.runs} == set(selected)
    assert all(run.action_executed is False for run in execution.runs)
    first_oracle = next(index for index, item in enumerate(events) if item.startswith("oracle:"))
    assert first_oracle == 5
    assert all(item.startswith("agent:") for item in events[:first_oracle])
    assert all(item.startswith("oracle:") for item in events[first_oracle:])
    assert len(events[first_oracle:]) == 8

    for episode_id in selected:
        episode_root = execution.output_directory / "episodes" / episode_id
        assert (episode_root / "live_manifest.json").is_file()
        assert len(json.loads((episode_root / "baseline_results.json").read_text())["runs"]) == 3
        assert {run.mode for run in execution.runs if run.episode_id == episode_id} == {
            BaselineMode.STATIC,
            BaselineMode.INVESTIGATOR,
            BaselineMode.INVESTIGATOR_VERIFIER,
        }
    validate_live_artifacts(
        execution.output_directory,
        execution.run_id,
        require_evaluation=True,
    )


def test_all_development_live_modes_persist_exact_matrix_and_continue_after_failure(
    tmp_path: Path,
) -> None:
    from tests.integration.test_workflow_paths import (
        action_for,
        diagnosed_investigation,
        verified_result,
    )

    selected = live_baselines.select_development_episode_ids()
    failed_episode_id = selected[0]
    events: list[str] = []
    real_detector = Detector(store=EvidenceStore(project_root=PROJECT_ROOT))

    class DevelopmentDetector:
        def run(self, episode_id: str) -> object:
            events.append(f"detector:{episode_id}")
            return real_detector.run(episode_id)

    class DevelopmentInvestigator:
        def __init__(self) -> None:
            self.calls: list[tuple[str, CorrectionPacketV1 | None]] = []
            self.results: dict[str, InvestigationResultV1] = {}

        def investigate(
            self,
            packet: IncidentPacketV1,
            *,
            correction_packet: CorrectionPacketV1 | None = None,
        ) -> InvestigationResultV1:
            events.append(f"investigator:{packet.incident_id}")
            self.calls.append((packet.incident_id, correction_packet))
            if correction_packet is not None:
                raise AssertionError("the deterministic Verifier must not request a correction")
            if packet.incident_id == failed_episode_id:
                raise RuntimeError("typed provider failure")
            result = diagnosed_investigation(packet, proposed_action=action_for(packet))
            self.results[packet.incident_id] = result
            return result

    class DevelopmentVerifier:
        def __init__(self, investigator: DevelopmentInvestigator) -> None:
            self.calls: list[tuple[str, InvestigationResultV1]] = []
            self._investigator = investigator

        def verify(
            self,
            packet: IncidentPacketV1,
            investigation: InvestigationResultV1,
        ) -> VerificationResultV1:
            events.append(f"verifier:{packet.incident_id}")
            self.calls.append((packet.incident_id, investigation))
            assert investigation is self._investigator.results[packet.incident_id]
            return verified_result(investigation)

    investigator = DevelopmentInvestigator()
    verifier = DevelopmentVerifier(investigator)
    output_root = tmp_path / "data" / "infineq" / "v1" / "runs"
    oracle_ids: list[str] = []

    def oracle_loader(episode_id: str) -> DevelopmentOracle:
        assert episode_id in selected
        episode_root = output_root / "run-phase5-development-live-v5" / "episodes"
        assert all(
            (episode_root / selected_id / "live_manifest.json").is_file()
            and (episode_root / selected_id / "baseline_results.json").is_file()
            for selected_id in selected
        )
        oracle_ids.append(episode_id)
        events.append(f"oracle:{episode_id}")
        return DevelopmentOracle(
            episode_id=episode_id,
            expected_diagnosis=None,
            required_evidence_kinds=(),
            action_card_expected=False,
        )

    execution = run_all_development_live_modes(
        project_root=PROJECT_ROOT,
        run_id="run-phase5-development-live-v5",
        detector=DevelopmentDetector(),  # type: ignore[arg-type]
        investigator=investigator,  # type: ignore[arg-type]
        verifier=verifier,  # type: ignore[arg-type]
        policy=_policy(),
        oracle_loader=oracle_loader,
        artifact_root=tmp_path,
    )

    assert execution.episode_ids == selected
    assert len(execution.episodes) == 8
    assert len(execution.runs) == 24
    assert {(run.episode_id, run.mode) for run in execution.runs} == {
        (episode_id, mode)
        for episode_id in selected
        for mode in (
            BaselineMode.STATIC,
            BaselineMode.INVESTIGATOR,
            BaselineMode.INVESTIGATOR_VERIFIER,
        )
    }
    assert len(investigator.calls) == 5
    assert all(correction is None for _episode_id, correction in investigator.calls)
    assert all(
        investigation is investigator.results[episode_id]
        for episode_id, investigation in verifier.calls
    )
    assert oracle_ids == list(selected)
    first_oracle = next(index for index, event in enumerate(events) if event.startswith("oracle:"))
    assert all(not event.startswith("oracle:") for event in events[:first_oracle])
    assert all(event.startswith("oracle:") for event in events[first_oracle:])

    for run in execution.runs:
        assert run.action_executed is False
        assert run.forbidden_tool_calls == 0
        assert run.unseen_evidence == 0
        if run.mode is BaselineMode.STATIC:
            assert (
                run.agent_version,
                run.prompt_version,
                run.tool_schema_version,
            ) == ("static-baseline-v1", "not_applicable", "not_applicable")
        elif run.mode is BaselineMode.INVESTIGATOR:
            assert (
                run.agent_version,
                run.prompt_version,
                run.tool_schema_version,
            ) == ("14", "investigator-v14", "investigator-tools-v1")
        else:
            assert (
                run.agent_version,
                run.prompt_version,
                run.tool_schema_version,
            ) == (
                "investigator-14+verifier-1",
                "investigator-v14+verifier-v1",
                "investigator-tools-v1+verifier-tools-v1",
            )

    failed_runs = tuple(run for run in execution.runs if run.episode_id == failed_episode_id)
    assert len(failed_runs) == 3
    assert all(run.terminal_status.value == "invalid_schema" for run in failed_runs[1:])
    failure_path = execution.output_directory / "episodes" / failed_episode_id / "failure.json"
    assert failure_path.is_file()
    assert json.loads(failure_path.read_text(encoding="utf-8"))["action_executed"] is False

    for episode_id in selected:
        episode_root = execution.output_directory / "episodes" / episode_id
        assert episode_root.is_relative_to(execution.output_directory)
        baseline_payload = json.loads(
            (episode_root / "baseline_results.json").read_text(encoding="utf-8")
        )
        assert len(baseline_payload["runs"]) == 3
        assert {item["mode"] for item in baseline_payload["runs"]} == {
            "static",
            "investigator",
            "investigator_verifier",
        }
        for path in episode_root.rglob("*"):
            if path.is_file():
                assert path.resolve().is_relative_to(episode_root.resolve())
                text = path.read_text(encoding="utf-8").casefold()
                assert '"prompt":' not in text
                assert '"completion":' not in text
                assert "https://" not in text
                assert "file://" not in text
                assert "hidden_oracle" not in text
                assert "expected_label" not in text
        episode_manifest = json.loads(
            (episode_root / "live_manifest.json").read_text(encoding="utf-8")
        )
        accounting = json.loads(
            (episode_root / "investigator_verifier" / "accounting.json").read_text(encoding="utf-8")
        )
        assert episode_manifest["scope"] == "development_episode"
        assert accounting["action_execution_count"] == 0
        assert accounting["oracle_loaded_after_agent_outputs"] is True

    assert execution.aggregate_metrics["episode_count"] == 8
    assert execution.aggregate_metrics["mode_record_count"] == 24
    assert execution.aggregate_metrics["action_execution_count"] == 0
    assert execution.evaluation.unsupported_claims_by_mode == {
        "static": 0,
        "investigator": 0,
        "investigator_verifier": 0,
    }
    assert execution.evaluation.policy_errors_by_mode == {
        "static": 0,
        "investigator": 1,
        "investigator_verifier": 1,
    }
    unready = tuple(run for run in execution.runs if run.episode_id == "ep-4b6fa0")
    assert all(run.action_card_eligible is False for run in unready)
    assert execution.evaluation.pass_count(
        BaselineMode.INVESTIGATOR
    ) == execution.evaluation.pass_count(BaselineMode.INVESTIGATOR_VERIFIER)
    assert execution.evaluation.two_agent_justification_passed is False
    assert execution.aggregate_metrics["two_agent_justification_passed"] is False
    for episode_id in execution.episode_ids:
        episode_evaluation = json.loads(
            (execution.output_directory / "episodes" / episode_id / "evaluation.json").read_text(
                encoding="utf-8"
            )
        )
        assert episode_evaluation["two_agent_justification_passed"] is False
    assert json.loads(
        (execution.output_directory / "aggregate_metrics.json").read_text(encoding="utf-8")
    ) == json.loads(json.dumps(execution.aggregate_metrics))
    assert (
        live_baselines._aggregate_development_metrics(
            run_id=execution.run_id,
            episode_ids=execution.episode_ids,
            runs=execution.runs,
            evaluation=execution.evaluation,
        )
        == execution.aggregate_metrics
    )
    validate_live_artifacts(
        execution.output_directory,
        execution.run_id,
        require_evaluation=True,
    )
    evaluation_path = execution.output_directory / "evaluation.json"
    aggregate_path = execution.output_directory / "aggregate_metrics.json"
    saved_evaluation = json.loads(evaluation_path.read_text(encoding="utf-8"))
    saved_aggregate = json.loads(aggregate_path.read_text(encoding="utf-8"))
    saved_evaluation["two_agent_justification_passed"] = True
    saved_aggregate["two_agent_justification_passed"] = True
    evaluation_path.write_text(json.dumps(saved_evaluation), encoding="utf-8")
    aggregate_path.write_text(json.dumps(saved_aggregate), encoding="utf-8")
    with pytest.raises(live_baselines.LiveArtifactError, match="justification gate"):
        validate_live_artifacts(
            execution.output_directory,
            execution.run_id,
            require_evaluation=True,
        )

    detection = Detector(store=EvidenceStore(project_root=PROJECT_ROOT)).run("ep-e35192")

    class NeverCalledInvestigator:
        def investigate(self, packet: IncidentPacketV1) -> InvestigationResultV1:
            raise AssertionError("quality abstention must not invoke the Investigator")

    class NeverCalledVerifier:
        def verify(
            self,
            packet: IncidentPacketV1,
            investigation: InvestigationResultV1,
        ) -> VerificationResultV1:
            raise AssertionError("quality abstention must not invoke the Verifier")

    outcome = live_baselines.WorkflowOrchestrator(
        investigator=NeverCalledInvestigator(),
        verifier=NeverCalledVerifier(),
        policy=_policy(),
    ).run_detection(detection, workflow_id="workflow-quality-abstention")

    assert detection.decision == "abstain"
    assert outcome.final_state is live_baselines.WorkflowState.CLOSED
    assert outcome.action_card is None
    assert outcome.failure is None
