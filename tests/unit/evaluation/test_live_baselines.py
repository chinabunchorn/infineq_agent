from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from infineq.agents.prompt_manifest import load_prompt_manifest, load_verifier_prompt_manifest
from infineq.agents.tool_definitions import INVESTIGATOR_TOOL_SCHEMA_SHA256
from infineq.evaluation import live_baselines
from infineq.evaluation.baselines import (
    BaselineMode,
    DevelopmentOracle,
    ResourceBudget,
)
from infineq.evaluation.live_baselines import (
    LiveArtifactError,
    LivePrerequisiteError,
    build_live_runtime,
    run_matched_live_modes,
    validate_exact_live_pins,
    validate_live_artifacts,
)
from infineq.evidence.tool_schemas import SignalEnum, WindowEnum
from infineq.foundry.agent_registry import (
    AgentVersionRecord,
    VerifierAgentVersionRecord,
)
from infineq.foundry.verifier_tools import VERIFIER_TOOL_SCHEMA_SHA256
from infineq.schemas.incident import IncidentPacketV1
from infineq.schemas.investigation import Disposition, InvestigationResultV1
from infineq.schemas.verification import VerificationResultV1, VerificationStatus
from infineq.workflow.corrections import (
    CorrectionPacketV1,
    LookupAllowanceV1,
    SignalWindowLookupV1,
)
from infineq.workflow.transitions import WorkflowState

PROJECT_ROOT = Path(__file__).parents[3]
CANONICAL_EPISODE_ID = "ep-61d8aa"
MODEL = "model-under-test"
LIVE_BUDGET = ResourceBudget(
    budget_id="budget-live-v1",
    max_visible_evidence=20,
    max_successful_tool_calls=8,
    max_redundant_tool_calls=3,
    max_runbook_chunks=3,
    max_input_tokens=50_000,
    max_output_tokens=10_000,
    max_total_tokens=60_000,
    max_latency_ms=120_000.0,
)


class FakeProject:
    def __init__(self, **_kwargs: object) -> None:
        self.agents = object()
        self.closed = False

    def close(self) -> None:
        self.closed = True


class FakeCredential:
    pass


class RetrievalOnlyRegistry:
    """Registry double that fails if the runtime tries to create or deploy anything."""

    def __init__(
        self, _agents: object, *, project_root: Path, model: str, version: str = "1"
    ) -> None:
        self.project_root = project_root
        self.model = model
        self.version = version
        self.calls: list[str] = []

    def retrieve_investigator_version(self, **_kwargs: object) -> AgentVersionRecord:
        self.calls.append("retrieve_investigator_version")
        return replace(
            _provider_records(self.project_root)[0],
            model_deployment_name=self.model,
        )

    def retrieve_verifier_version(self, **kwargs: object) -> VerifierAgentVersionRecord:
        assert kwargs["expected_version"] == self.version
        self.calls.append("retrieve_verifier_version")
        return replace(
            _provider_records(self.project_root)[1],
            model_deployment_name=self.model,
            version=self.version,
        )

    def __getattr__(self, name: str) -> object:
        if name.startswith(("create", "ensure", "deploy")):
            raise AssertionError(f"forbidden registry mutation: {name}")
        raise AttributeError(name)


def _canonical_packet() -> IncidentPacketV1:
    from tests.integration.test_workflow_paths import canonical_packet

    return canonical_packet()


def _diagnosed_investigation(packet: IncidentPacketV1) -> InvestigationResultV1:
    from tests.integration.test_workflow_paths import action_for, diagnosed_investigation

    return diagnosed_investigation(packet, proposed_action=action_for(packet))


def _verified_result(investigation: InvestigationResultV1) -> VerificationResultV1:
    from tests.integration.test_workflow_paths import verified_result

    return verified_result(investigation)


def _policy() -> Any:
    from tests.integration.test_workflow_paths import policy_for

    return policy_for()


class FakeInvestigator:
    def __init__(self, result: InvestigationResultV1, events: list[str]) -> None:
        self.result = result
        self.events = events
        self.calls: list[tuple[IncidentPacketV1, CorrectionPacketV1 | None]] = []

    def investigate(
        self,
        packet: IncidentPacketV1,
        *,
        correction_packet: CorrectionPacketV1 | None = None,
    ) -> InvestigationResultV1:
        self.events.append("investigator")
        self.calls.append((packet, correction_packet))
        return self.result


class FakeVerifier:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.calls: list[tuple[IncidentPacketV1, InvestigationResultV1]] = []

    def verify(
        self,
        packet: IncidentPacketV1,
        investigation: InvestigationResultV1,
    ) -> VerificationResultV1:
        self.events.append("verifier")
        self.calls.append((packet, investigation))
        return _verified_result(investigation)


class BlockingVerifier:
    def verify(
        self,
        packet: IncidentPacketV1,
        investigation: InvestigationResultV1,
    ) -> VerificationResultV1:
        from infineq.schemas.verification import ClaimCheck

        return VerificationResultV1(
            verification_id=f"verification-{packet.incident_id}",
            incident_id=packet.incident_id,
            investigation_id=investigation.investigation_id,
            checked_at=packet.detected_at,
            status=VerificationStatus.BLOCKED,
            claim_checks=(
                ClaimCheck(
                    claim_id="claim-action-policy",
                    evidence_ids=(investigation.cited_evidence_ids[0],),
                    supported=False,
                    note="The proposed action violates the fixed TTL policy.",
                ),
            ),
            issues=("action TTL does not match policy",),
            correction_requests=(),
            policy_ref="policy-infineq-v1",
        )


class StrictSignatureInvestigator:
    """Concrete-adapter shape: correction is not part of Investigator v14."""

    def __init__(self, result: InvestigationResultV1) -> None:
        self.result = result
        self.calls: list[IncidentPacketV1] = []
        self.correction_calls: list[tuple[IncidentPacketV1, CorrectionPacketV1]] = []

    def investigate(self, packet: IncidentPacketV1) -> InvestigationResultV1:
        self.calls.append(packet)
        return self.result

    def investigate_with_correction(
        self,
        packet: IncidentPacketV1,
        correction_packet: CorrectionPacketV1,
    ) -> InvestigationResultV1:
        self.correction_calls.append((packet, correction_packet))
        return self.result


class RevisionThenVerifiedVerifier:
    """Deterministic Verifier that exercises exactly one correction cycle."""

    def __init__(self, *, lookup_allowance: LookupAllowanceV1 | None = None) -> None:
        self.calls: list[tuple[IncidentPacketV1, InvestigationResultV1]] = []
        self.lookup_allowance = lookup_allowance

    def verify(
        self,
        packet: IncidentPacketV1,
        investigation: InvestigationResultV1,
    ) -> VerificationResultV1:
        self.calls.append((packet, investigation))
        if len(self.calls) == 1:
            return VerificationResultV1(
                verification_id=f"verification-{packet.incident_id}",
                incident_id=packet.incident_id,
                investigation_id=investigation.investigation_id,
                checked_at=datetime(2026, 9, 14, tzinfo=UTC),
                status=VerificationStatus.REVISION_REQUIRED,
                claim_checks=(),
                issues=(),
                correction_requests=(
                    "retrieve evidence for every cited claim"
                    if self.lookup_allowance is not None
                    else "add a credible competing explanation",
                ),
                policy_ref="policy-infineq-v1",
            )
        return _verified_result(investigation)


class CorrectionLookupInvestigator(StrictSignatureInvestigator):
    """Deterministic delegate that records one correction lookup."""

    def __init__(self, result: InvestigationResultV1) -> None:
        super().__init__(result)
        self._successful_tool_calls = 0

    @property
    def last_run(self) -> object:
        return SimpleNamespace(
            successful_tool_calls=self._successful_tool_calls,
            returned_evidence_ids=frozenset(self.result.cited_evidence_ids),
            usage=(1, 1, 2),
            response_ids=(),
            traces=(),
            tool_retry_count=0,
            forbidden_tool_calls=0,
            repair_attempted=False,
            final_output_text=self.result.model_dump_json(),
        )

    def investigate_with_correction(
        self,
        packet: IncidentPacketV1,
        correction_packet: CorrectionPacketV1,
    ) -> InvestigationResultV1:
        self._successful_tool_calls += 1
        return super().investigate_with_correction(packet, correction_packet)


class IncompleteInvestigator(StrictSignatureInvestigator):
    """Typed failed Investigator run that must not create a Verifier result."""


def _provider_records(project_root: Path) -> tuple[AgentVersionRecord, VerifierAgentVersionRecord]:
    investigator_manifest = load_prompt_manifest(project_root=project_root)
    verifier_manifest = load_verifier_prompt_manifest(project_root=project_root)
    return (
        AgentVersionRecord(
            name="infineq-investigator",
            version="14",
            prompt_version=investigator_manifest.version,
            prompt_sha256=investigator_manifest.prompt_sha256,
            model_deployment_name=MODEL,
        ),
        VerifierAgentVersionRecord(
            name="infineq-evidence-verifier",
            version="1",
            prompt_version=verifier_manifest.version,
            prompt_sha256=verifier_manifest.prompt_sha256,
            model_deployment_name=MODEL,
            tool_schema_sha256=VERIFIER_TOOL_SCHEMA_SHA256,
            created_at=datetime(2026, 9, 14, tzinfo=UTC),
        ),
    )


def test_live_runtime_requires_opt_in_and_validates_before_cloud_factories(
    tmp_path: Path,
) -> None:
    calls: list[str] = []
    environment = {
        "INFIN_EQ_RUN_LIVE_BASELINES": "1",
        "AZURE_AI_PROJECT_ENDPOINT": "https://test.services.ai.azure.com/api/projects/p",
        "AZURE_AI_MODEL_DEPLOYMENT_NAME": MODEL,
    }

    with pytest.raises(LivePrerequisiteError, match="manifest"):
        build_live_runtime(
            tmp_path,
            environment=environment,
            credential_factory=lambda: calls.append("credential"),
            project_factory=lambda **_kwargs: calls.append("project"),
        )

    assert calls == []


def test_live_runtime_does_not_treat_dotenv_presence_as_opt_in(
    tmp_path: Path,
) -> None:
    calls: list[str] = []
    environment = {
        "AZURE_AI_PROJECT_ENDPOINT": "https://test.services.ai.azure.com/api/projects/p",
        "AZURE_AI_MODEL_DEPLOYMENT_NAME": MODEL,
    }

    with pytest.raises(LivePrerequisiteError, match="INFIN_EQ_RUN_LIVE_BASELINES"):
        build_live_runtime(
            tmp_path,
            environment=environment,
            credential_factory=lambda: calls.append("credential"),
            project_factory=lambda **_kwargs: calls.append("project"),
        )

    assert calls == []


def test_exact_version_selection_and_mismatched_manifest_fail_closed() -> None:
    investigator_record, verifier_record = _provider_records(PROJECT_ROOT)
    investigator_manifest = {
        "agent_name": "infineq-investigator",
        "agent_version": "14",
        "prompt_version": investigator_record.prompt_version,
        "prompt_sha256": investigator_record.prompt_sha256,
        "model_deployment_name": MODEL,
    }
    verifier_manifest = {
        "agent_name": "infineq-evidence-verifier",
        "agent_version": "1",
        "prompt_version": verifier_record.prompt_version,
        "prompt_sha256": verifier_record.prompt_sha256,
        "model_deployment_name": MODEL,
        "tool_schema_sha256": VERIFIER_TOOL_SCHEMA_SHA256,
        "created_at": "2026-09-14T00:00:00Z",
    }

    pins = validate_exact_live_pins(
        project_root=PROJECT_ROOT,
        model_deployment_name=MODEL,
        investigator_manifest=investigator_manifest,
        verifier_manifest=verifier_manifest,
        investigator_record=investigator_record,
        verifier_record=verifier_record,
    )

    assert pins.investigator.agent_version == "14"
    assert pins.investigator.tool_schema_sha256 == INVESTIGATOR_TOOL_SCHEMA_SHA256
    assert pins.verifier.agent_version == "1"
    assert pins.verifier.tool_schema_sha256 == VERIFIER_TOOL_SCHEMA_SHA256

    mismatched = dict(verifier_manifest, tool_schema_sha256="0" * 64)
    with pytest.raises(LivePrerequisiteError, match="tool schema"):
        validate_exact_live_pins(
            project_root=PROJECT_ROOT,
            model_deployment_name=MODEL,
            investigator_manifest=investigator_manifest,
            verifier_manifest=mismatched,
            investigator_record=investigator_record,
            verifier_record=verifier_record,
        )


def test_live_runtime_rejects_stale_verifier_pin_before_cloud_factories(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = live_baselines.load_verifier_deployment_manifest(PROJECT_ROOT)
    model = current.model_deployment_name
    stale = current.model_copy(update={"prompt_version": "verifier-v1", "prompt_sha256": "a" * 64})
    monkeypatch.setattr(live_baselines, "load_verifier_deployment_manifest", lambda _: stale)
    environment = {
        "INFIN_EQ_RUN_LIVE_BASELINES": "1",
        "AZURE_AI_PROJECT_ENDPOINT": "https://test.services.ai.azure.com/api/projects/p",
        "AZURE_AI_MODEL_DEPLOYMENT_NAME": model,
    }
    calls: list[str] = []
    with pytest.raises(LivePrerequisiteError, match="Verifier local prompt manifest"):
        build_live_runtime(
            PROJECT_ROOT,
            environment=environment,
            credential_factory=lambda: calls.append("credential"),
            project_factory=lambda **_kwargs: calls.append("project"),
        )
    assert calls == []


def test_live_runtime_retrieves_only_the_exact_existing_versions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_manifest = live_baselines.load_verifier_deployment_manifest(PROJECT_ROOT)
    model = old_manifest.model_deployment_name
    new_prompt = load_verifier_prompt_manifest(project_root=PROJECT_ROOT)
    matching_manifest = old_manifest.model_copy(
        update={
            "agent_version": "2",
            "prompt_version": new_prompt.version,
            "prompt_sha256": new_prompt.prompt_sha256,
        }
    )
    monkeypatch.setattr(
        live_baselines, "load_verifier_deployment_manifest", lambda _: matching_manifest
    )
    environment = {
        "INFIN_EQ_RUN_LIVE_BASELINES": "1",
        "AZURE_AI_PROJECT_ENDPOINT": "https://test.services.ai.azure.com/api/projects/p",
        "AZURE_AI_MODEL_DEPLOYMENT_NAME": model,
    }
    registries: list[RetrievalOnlyRegistry] = []

    def registry_factory(agents: object, *, project_root: Path) -> RetrievalOnlyRegistry:
        registry = RetrievalOnlyRegistry(
            agents, project_root=project_root, model=model, version="2"
        )
        registries.append(registry)
        return registry

    runtime = build_live_runtime(
        PROJECT_ROOT,
        environment=environment,
        credential_factory=FakeCredential,
        project_factory=FakeProject,
        registry_factory=registry_factory,
        responses_factory=lambda *_args, **_kwargs: object(),
    )
    try:
        assert runtime.pins.investigator.agent_version == "14"
        assert runtime.pins.verifier.agent_version == "2"
    finally:
        runtime.close()

    assert [call for registry in registries for call in registry.calls] == [
        "retrieve_investigator_version",
        "retrieve_verifier_version",
    ]


def test_provider_response_and_call_ids_are_normalized_to_trace_schema(
    tmp_path: Path,
) -> None:
    packet = _canonical_packet()
    investigation = _diagnosed_investigation(packet)
    episode = live_baselines.baseline_episode_from_packet(packet)
    captured = live_baselines._CapturedAgentRun(
        result=investigation,
        raw_output=investigation.model_dump_json(),
        response_ids=("resp_provider_response", "call_provider_call"),
        trace_records=(),
        returned_evidence_ids=frozenset(),
        successful_tool_calls=0,
        forbidden_tool_calls=0,
        tool_retry_count=0,
        repair_attempted=False,
        usage=(1, 2, 3),
        latency_ms=1.0,
        error=None,
    )

    record = live_baselines._record_for_mode(
        mode=BaselineMode.INVESTIGATOR,
        episode=episode,
        budget=LIVE_BUDGET,
        captured=captured,
        investigation=investigation,
        run_id="run-baseline-live-trace-shape",
        agent_version="14",
        prompt_version="investigator-v14",
        tool_schema_version="investigator-tools-v1",
    )

    assert record.trace_refs == ("resp-provider_response", "call-provider_call")
    assert record.unseen_evidence == 0
    del tmp_path


def test_matched_live_modes_reuse_investigator_result_and_budget_before_oracle(
    tmp_path: Path,
) -> None:
    packet = _canonical_packet()
    investigation = _diagnosed_investigation(packet)
    events: list[str] = []
    investigator = FakeInvestigator(investigation, events)
    verifier = FakeVerifier(events)

    execution = run_matched_live_modes(
        project_root=tmp_path,
        run_id="run-baseline-live-test-reuse",
        packet=packet,
        investigator=investigator,
        verifier=verifier,
        policy=_policy(),
        budget=LIVE_BUDGET,
        investigator_version="14",
        verifier_version="1",
        oracle_loader=lambda episode_id: (
            events.append("oracle")
            or DevelopmentOracle(
                episode_id=episode_id,
                expected_diagnosis="capacity_queueing",
                required_evidence_kinds=("queue_depth", "queue_wait"),
                action_card_expected=True,
            )
        ),
    )

    assert events == ["investigator", "verifier", "oracle"]
    assert len(investigator.calls) == 1
    assert len(verifier.calls) == 1
    assert verifier.calls[0][1] is investigation
    assert {run.mode for run in execution.runs} == {
        BaselineMode.STATIC,
        BaselineMode.INVESTIGATOR,
        BaselineMode.INVESTIGATOR_VERIFIER,
    }
    assert len({run.visible_evidence_ids for run in execution.runs}) == 1
    assert len({run.budget for run in execution.runs}) == 1
    assert execution.workflow.final_state is WorkflowState.AWAITING_HUMAN
    assert all(run.action_executed is False for run in execution.runs)
    assert all(
        event.to_state is not WorkflowState.ACTION_APPLIED
        for event in execution.workflow.audit_events
    )


def test_matched_modes_score_action_policy_symmetrically_when_verifier_blocks(
    tmp_path: Path,
) -> None:
    packet = _canonical_packet()
    investigation = _diagnosed_investigation(packet)
    assert investigation.proposed_action is not None
    invalid_action = investigation.proposed_action.model_copy(
        update={"expires_at": investigation.proposed_action.created_at + timedelta(hours=1)}
    )
    invalid = investigation.model_copy(update={"proposed_action": invalid_action})

    class PolicyInvalidInvestigator(FakeInvestigator):
        @property
        def last_run(self) -> object:
            return SimpleNamespace(
                result=self.result,
                status=SimpleNamespace(value="complete"),
                response_ids=(),
                traces=(),
                successful_tool_calls=0,
                tool_retry_count=0,
                forbidden_tool_calls=0,
                repair_attempted=False,
                returned_evidence_ids=frozenset(self.result.cited_evidence_ids),
                usage=(1, 1, 2),
                final_output_text=self.result.model_dump_json(),
            )

    execution = run_matched_live_modes(
        project_root=tmp_path,
        run_id="run-baseline-live-policy-symmetry",
        packet=packet,
        investigator=PolicyInvalidInvestigator(invalid, []),
        verifier=BlockingVerifier(),
        policy=_policy(),
        budget=LIVE_BUDGET,
        investigator_version="14",
        verifier_version="1",
        oracle_loader=lambda episode_id: DevelopmentOracle(
            episode_id=episode_id,
            expected_diagnosis="capacity_queueing",
            required_evidence_kinds=("queue_depth", "queue_wait"),
            action_card_expected=True,
        ),
    )

    one = next(run for run in execution.runs if run.mode is BaselineMode.INVESTIGATOR)
    two = next(run for run in execution.runs if run.mode is BaselineMode.INVESTIGATOR_VERIFIER)
    assert one.action_card_eligible is False
    assert one.unsupported_claims == 1
    assert one.policy_errors == 1
    assert two.action_card_eligible is False
    assert two.unsupported_claims == 1
    assert two.policy_errors == 1
    assert execution.evaluation.pass_count(BaselineMode.INVESTIGATOR) == 0
    assert execution.evaluation.pass_count(BaselineMode.INVESTIGATOR_VERIFIER) == 0
    assert execution.evaluation.two_agent_justification_passed is False


def test_unsupported_claim_counter_ignores_non_verification_results() -> None:
    assert live_baselines._unsupported_claim_count(None) == 0
    assert live_baselines._unsupported_claim_count(object()) == 0


def test_unsupported_claim_counter_counts_failed_claim_checks() -> None:
    packet = _canonical_packet()
    investigation = _diagnosed_investigation(packet)
    verification = BlockingVerifier().verify(packet, investigation)

    assert live_baselines._unsupported_claim_count(verification) == 1


def test_live_recorder_accepts_the_concrete_investigator_signature(
    tmp_path: Path,
) -> None:
    packet = _canonical_packet()
    investigation = _diagnosed_investigation(packet)

    execution = run_matched_live_modes(
        project_root=tmp_path,
        run_id="run-baseline-live-test-concrete-adapter",
        packet=packet,
        investigator=StrictSignatureInvestigator(investigation),
        verifier=FakeVerifier([]),
        policy=_policy(),
        budget=LIVE_BUDGET,
        investigator_version="14",
        verifier_version="1",
        oracle_loader=lambda episode_id: DevelopmentOracle(
            episode_id=episode_id,
            expected_diagnosis="capacity_queueing",
            required_evidence_kinds=("queue_depth", "queue_wait"),
            action_card_expected=True,
        ),
    )

    assert execution.workflow.final_state is WorkflowState.AWAITING_HUMAN


def test_correction_adapter_preserves_packet_and_completes_second_verifier_run(
    tmp_path: Path,
) -> None:
    del tmp_path
    packet = _canonical_packet()
    investigation = _diagnosed_investigation(packet)
    investigator = StrictSignatureInvestigator(investigation)
    verifier = RevisionThenVerifiedVerifier()
    recording_investigator = live_baselines._RecordingInvestigator(investigator)
    initial_result = recording_investigator.investigate(packet)
    recording_verifier = live_baselines._RecordingVerifier(verifier)

    workflow = live_baselines.WorkflowOrchestrator(
        investigator=live_baselines._ReusedInvestigator(
            recording_investigator,
            initial_result,
        ),
        verifier=recording_verifier,
        policy=_policy(),
    ).run(packet, workflow_id="workflow-correction-adapter")

    assert workflow.final_state is WorkflowState.AWAITING_HUMAN
    assert workflow.failure is None
    assert investigator.calls == [packet]
    assert len(investigator.correction_calls) == 1
    correction_packet = investigator.correction_calls[0][1]
    assert correction_packet.revision == 1
    assert correction_packet.incident_id == packet.incident_id
    assert correction_packet.reuse_evidence_ids == tuple(sorted(investigation.cited_evidence_ids))
    assert len(verifier.calls) == 2
    assert verifier.calls[0][1] is investigation
    assert verifier.calls[1][1] is investigation


def test_correction_without_lookup_allowance_cannot_consume_a_tool_call() -> None:
    packet = _canonical_packet()
    investigation = _diagnosed_investigation(packet)
    investigator = CorrectionLookupInvestigator(investigation)
    verifier = RevisionThenVerifiedVerifier()
    recording_investigator = live_baselines._RecordingInvestigator(investigator)
    initial_result = recording_investigator.investigate(packet)
    recording_verifier = live_baselines._RecordingVerifier(verifier)

    workflow = live_baselines.WorkflowOrchestrator(
        investigator=live_baselines._ReusedInvestigator(
            recording_investigator,
            initial_result,
        ),
        verifier=recording_verifier,
        policy=_policy(),
    ).run(packet, workflow_id="workflow-correction-budget")

    assert workflow.final_state is WorkflowState.ANALYSIS_INCOMPLETE
    assert workflow.failure is not None
    assert workflow.failure.value == "invalid_schema"
    assert len(verifier.calls) == 1


def test_correction_accounting_keeps_one_agent_mode_at_initial_invocation(
    tmp_path: Path,
) -> None:
    packet = _canonical_packet()
    investigation = _diagnosed_investigation(packet)
    execution = run_matched_live_modes(
        project_root=tmp_path,
        run_id="run-baseline-live-test-correction-accounting",
        packet=packet,
        investigator=cast(Any, CorrectionLookupInvestigator(investigation)),
        verifier=RevisionThenVerifiedVerifier(
            lookup_allowance=LookupAllowanceV1(
                incident_id=packet.incident_id,
                explicitly_identified_by_verifier=True,
                lookup=SignalWindowLookupV1(
                    signal=SignalEnum.TTFT,
                    window=WindowEnum.OBSERVATION,
                ),
            )
        ),
        policy=_policy(),
        budget=LIVE_BUDGET,
        investigator_version="14",
        verifier_version="1",
        oracle_loader=lambda episode_id: DevelopmentOracle(
            episode_id=episode_id,
            expected_diagnosis="capacity_queueing",
            required_evidence_kinds=("queue_depth", "queue_wait"),
            action_card_expected=True,
        ),
    )

    manifest = json.loads(
        (execution.output_directory / "live_manifest.json").read_text(encoding="utf-8")
    )
    accounting = json.loads(
        (execution.output_directory / "investigator_verifier" / "accounting.json").read_text(
            encoding="utf-8"
        )
    )
    assert manifest["modes"]["investigator"]["successful_tool_calls"] == 0
    assert accounting["investigator_invocation_count"] == 2
    assert accounting["correction_invocation_count"] == 1
    assert accounting["correction_successful_tool_calls"] == 1
    assert accounting["verifier_invocation_count"] == 2


def test_accounting_links_one_agent_to_initial_response_not_two_agent_revision(
    tmp_path: Path,
) -> None:
    accounting_dir = tmp_path / "investigator_verifier"
    accounting_dir.mkdir()
    accounting = {
        "investigator_invocation_count": 2,
        "correction_invocation_count": 1,
        "verifier_invocation_count": 2,
        "initial_investigator_successful_tool_calls": 1,
        "correction_successful_tool_calls": 0,
        "action_execution_count": 0,
        "agent_invoked": True,
        "shared_initial_investigator_result": True,
        "shared_initial_visible_evidence": True,
        "initial_investigator_response_ids": ["resp_initial"],
        "correction_response_ids": ["resp_revision"],
        "all_investigator_response_ids": ["resp_initial", "resp_revision"],
        "verifier_response_ids": ["resp_verifier"],
    }
    (accounting_dir / "accounting.json").write_text(json.dumps(accounting), encoding="utf-8")

    live_baselines._validate_saved_accounting(
        tmp_path,
        {"response_ids": ["resp_initial"]},
        {"response_ids": ["resp_verifier"]},
        require_evaluation=False,
    )

    with pytest.raises(LiveArtifactError, match="Investigator response accounting"):
        live_baselines._validate_saved_accounting(
            tmp_path,
            {"response_ids": ["resp_wrong"]},
            {"response_ids": ["resp_verifier"]},
            require_evaluation=False,
        )


def test_revision_result_is_persisted_and_linked_after_correction(
    tmp_path: Path,
) -> None:
    packet = _canonical_packet()
    investigation = _diagnosed_investigation(packet)
    execution = run_matched_live_modes(
        project_root=tmp_path,
        run_id="run-baseline-live-test-correction-evidence",
        packet=packet,
        investigator=cast(Any, StrictSignatureInvestigator(investigation)),
        verifier=RevisionThenVerifiedVerifier(),
        policy=_policy(),
        budget=LIVE_BUDGET,
        score_results=False,
    )

    manifest_path = execution.output_directory / "live_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["schema_version"] == "1.1"
    two_mode = manifest["modes"]["investigator_verifier"]
    initial_verifier = VerificationResultV1.model_validate_json(
        (execution.output_directory / two_mode["initial_verification_path"]).read_text(
            encoding="utf-8"
        )
    )
    correction = CorrectionPacketV1.model_validate_json(
        (execution.output_directory / two_mode["correction_packet_path"]).read_text(
            encoding="utf-8"
        )
    )
    assert initial_verifier.status is VerificationStatus.REVISION_REQUIRED
    assert correction.revision == 1
    assert correction.investigation_id == investigation.investigation_id
    relative = two_mode["revision_result_path"]
    corrected = InvestigationResultV1.model_validate_json(
        (execution.output_directory / relative).read_text(encoding="utf-8")
    )
    assert corrected.investigation_id == investigation.investigation_id
    assert corrected.cited_evidence_ids == investigation.cited_evidence_ids
    assert (
        validate_live_artifacts(
            execution.output_directory,
            execution.run_id,
            require_evaluation=False,
        )
        == execution.output_directory
    )

    del manifest["modes"]["investigator_verifier"]["revision_result_path"]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(LiveArtifactError, match="corrected investigation"):
        validate_live_artifacts(
            execution.output_directory,
            execution.run_id,
            require_evaluation=False,
        )


def test_incomplete_investigator_preserves_invalid_schema_without_verifier_artifact(
    tmp_path: Path,
) -> None:
    packet = _canonical_packet()
    incomplete = _diagnosed_investigation(packet).model_copy(
        update={
            "disposition": Disposition.ANALYSIS_INCOMPLETE,
            "summary": "The Investigator did not return a schema-valid result.",
            "hypotheses": (),
            "leading_hypothesis_id": None,
            "proposed_action": None,
            "cited_evidence_ids": (),
        }
    )
    verifier = FakeVerifier([])

    execution = run_matched_live_modes(
        project_root=tmp_path,
        run_id="run-baseline-live-test-no-verifier",
        packet=packet,
        investigator=IncompleteInvestigator(incomplete),
        verifier=verifier,
        policy=_policy(),
        budget=LIVE_BUDGET,
        investigator_version="14",
        verifier_version="1",
        oracle_loader=lambda episode_id: DevelopmentOracle(
            episode_id=episode_id,
            expected_diagnosis="capacity_queueing",
            required_evidence_kinds=("queue_depth", "queue_wait"),
            action_card_expected=True,
        ),
    )

    assert execution.workflow.final_state is WorkflowState.ANALYSIS_INCOMPLETE
    assert execution.workflow.failure is not None
    assert execution.workflow.failure.value == "invalid_schema"
    assert verifier.calls == []
    assert not (execution.output_directory / "verifier" / "redacted_output.json").exists()
    not_invoked = json.loads(
        (execution.output_directory / "verifier" / "not_invoked.json").read_text(encoding="utf-8")
    )
    assert not_invoked == {"invoked": False, "reason": "investigator_failed"}


def test_live_artifacts_are_redacted_and_contained(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    packet = _canonical_packet()
    investigation = _diagnosed_investigation(packet)
    events: list[str] = []
    old_manifest = live_baselines.load_verifier_deployment_manifest(PROJECT_ROOT)
    prompt = load_verifier_prompt_manifest(project_root=PROJECT_ROOT)
    matching = old_manifest.model_copy(
        update={
            "agent_version": "2",
            "prompt_version": prompt.version,
            "prompt_sha256": prompt.prompt_sha256,
        }
    )
    monkeypatch.setattr(live_baselines, "load_verifier_deployment_manifest", lambda _: matching)
    investigator_record, verifier_record = _provider_records(PROJECT_ROOT)
    live_model = matching.model_deployment_name
    investigator_record = replace(investigator_record, model_deployment_name=live_model)
    verifier_record = replace(verifier_record, model_deployment_name=live_model, version="2")
    pins = validate_exact_live_pins(
        project_root=PROJECT_ROOT,
        model_deployment_name=live_model,
        investigator_manifest=live_baselines._load_investigator_deployment_manifest(
            PROJECT_ROOT
        ).model_dump(mode="python"),
        verifier_manifest=live_baselines.load_verifier_deployment_manifest(PROJECT_ROOT).model_dump(
            mode="python"
        ),
        investigator_record=investigator_record,
        verifier_record=verifier_record,
    )
    execution = run_matched_live_modes(
        project_root=tmp_path,
        run_id="run-baseline-live-test-artifacts",
        packet=packet,
        investigator=FakeInvestigator(investigation, events),
        verifier=FakeVerifier(events),
        policy=_policy(),
        budget=LIVE_BUDGET,
        investigator_version="14",
        verifier_version="1",
        pins=pins,
        oracle_loader=lambda episode_id: DevelopmentOracle(
            episode_id=episode_id,
            expected_diagnosis="capacity_queueing",
            required_evidence_kinds=("queue_depth", "queue_wait"),
            action_card_expected=True,
        ),
    )

    output = validate_live_artifacts(
        execution.output_directory,
        execution.run_id,
        require_pins=True,
        project_root=PROJECT_ROOT,
    )
    assert output == execution.output_directory
    assert pins.verifier.agent_version == "2"
    assert execution.runs[2].agent_version == "investigator-14+verifier-2"
    assert execution.runs[2].prompt_version == "investigator-v14+verifier-v2"
    assert output.is_relative_to(tmp_path / "data" / "infineq" / "v1" / "runs")
    files = tuple(path for path in output.rglob("*") if path.is_file())
    assert files
    serialized = "\n".join(path.read_text(encoding="utf-8") for path in files)
    assert "api_key=secret" not in serialized
    assert '"prompt":' not in serialized.casefold()
    assert '"completion":' not in serialized.casefold()
    assert "https://" not in serialized
    assert "hidden_oracle" not in serialized.casefold()
    assert all(path.resolve().is_relative_to(output.resolve()) for path in files)

    baseline = json.loads((output / "baseline_results.json").read_text(encoding="utf-8"))
    assert [item["mode"] for item in baseline["runs"]] == [
        "static",
        "investigator",
        "investigator_verifier",
    ]
    accounting = json.loads(
        (output / "investigator_verifier" / "accounting.json").read_text(encoding="utf-8")
    )
    assert accounting["investigator_invocation_count"] == 1
    assert accounting["correction_invocation_count"] == 0
    assert accounting["verifier_invocation_count"] == 1
    assert accounting["shared_initial_investigator_result"] is True
    assert accounting["shared_initial_visible_evidence"] is True

    manifest = json.loads((output / "live_manifest.json").read_text(encoding="utf-8"))
    manifest["agent_pins"]["investigator"]["agent_version"] = "13"
    (output / "live_manifest.json").write_text(
        json.dumps(manifest),
        encoding="utf-8",
    )
    with pytest.raises(LiveArtifactError, match="pin"):
        validate_live_artifacts(output, execution.run_id, require_pins=True)
