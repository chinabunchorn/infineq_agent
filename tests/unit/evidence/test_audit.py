from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from infineq.evidence.audit import (
    AgentRole,
    AuditRecorder,
    ToolAuditRecord,
    ToolAuditStatus,
    redact_error,
    validated_arguments_hash,
)
from infineq.evidence.store import EvidenceStore
from infineq.evidence.tool_schemas import GetIncidentPacketRequest, GetPolicyRequest, PolicyRef
from infineq.evidence.tools import InvestigatorTools, VerifierTools

PROJECT_ROOT = Path(__file__).parents[3]


def test_validated_argument_hash_is_deterministic_and_type_bound() -> None:
    request = GetIncidentPacketRequest(incident_id="ep-61d8aa")

    first = validated_arguments_hash(request)
    second = validated_arguments_hash(
        GetIncidentPacketRequest.model_validate({"incident_id": "ep-61d8aa"})
    )

    assert first == second
    assert len(first) == 64
    assert first == first.casefold()
    assert first != validated_arguments_hash({"incident_id": "ep-61d8aa"})


def test_validated_argument_hash_preserves_scalar_types_and_ignores_mapping_order() -> None:
    first = validated_arguments_hash({"limit": 1, "window": "observation"})
    reordered = validated_arguments_hash({"window": "observation", "limit": 1})
    string_limit = validated_arguments_hash({"limit": "1", "window": "observation"})

    assert first == reordered
    assert first != string_limit


def test_audit_recorder_captures_a_typed_success_record_without_response_body() -> None:
    recorder = AuditRecorder()
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    ended_at = started_at + timedelta(milliseconds=25)
    request = GetIncidentPacketRequest(incident_id="ep-61d8aa")
    evidence_id = "ev:ep-61d8aa:service_metrics:w060-075:ttft:p95:deadbeef"

    record = recorder.record(
        tool_name="get_incident_packet",
        validated_arguments=request,
        agent_role=AgentRole.INVESTIGATOR,
        started_at=started_at,
        ended_at=ended_at,
        status=ToolAuditStatus.SUCCESS,
        returned_evidence_ids=(evidence_id,),
    )

    assert record.tool_name == "get_incident_packet"
    assert record.validated_arguments_hash == validated_arguments_hash(request)
    assert record.agent_role is AgentRole.INVESTIGATOR
    assert record.started_at == started_at
    assert record.ended_at == ended_at
    assert record.status is ToolAuditStatus.SUCCESS
    assert record.returned_evidence_ids == (evidence_id,)
    assert record.redacted_error is None
    assert recorder.records == (record,)
    assert "response" not in record.model_dump(mode="json")


def test_failed_audit_record_redacts_credentials_prompts_oracles_and_paths() -> None:
    recorder = AuditRecorder()
    request = GetIncidentPacketRequest(incident_id="ep-61d8aa")
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    ended_at = started_at + timedelta(milliseconds=25)
    unsafe_error = (
        "authorization=Bearer super-secret password=hunter2 "
        "prompt='raw user prompt' oracle_root=/hidden/oracles/root_cause.json"
    )

    record = recorder.record(
        tool_name="get_incident_packet",
        validated_arguments=request,
        agent_role=AgentRole.INVESTIGATOR,
        started_at=started_at,
        ended_at=ended_at,
        status=ToolAuditStatus.ERROR,
        redacted_error=unsafe_error,
    )

    serialized = str(record.model_dump(mode="json")).lower()
    assert "super-secret" not in serialized
    assert "hunter2" not in serialized
    assert "raw user prompt" not in serialized
    assert "oracle_root" not in serialized
    assert "/hidden" not in serialized
    assert "[redacted]" in serialized


def test_investigator_audits_one_external_tool_call_and_only_returned_evidence_ids() -> None:
    recorder = AuditRecorder()
    tools = InvestigatorTools(
        store=EvidenceStore(project_root=PROJECT_ROOT),
        audit_recorder=recorder,
    )
    request = GetIncidentPacketRequest(incident_id="ep-61d8aa")

    packet = tools.get_incident_packet(request)

    assert len(recorder.records) == 1
    record = recorder.records[0]
    assert record.tool_name == "get_incident_packet"
    assert record.agent_role is AgentRole.INVESTIGATOR
    assert record.status is ToolAuditStatus.SUCCESS
    assert record.validated_arguments_hash == validated_arguments_hash(request)
    assert record.returned_evidence_ids == tuple(
        reference.evidence_id for reference in packet.evidence_refs
    )
    assert record.started_at.tzinfo is not None
    assert record.ended_at.tzinfo is not None
    assert record.ended_at >= record.started_at
    assert "evidence_refs" not in record.model_dump(mode="json")


def test_verifier_audits_policy_calls_with_the_verifier_role() -> None:
    recorder = AuditRecorder()
    tools = VerifierTools(
        store=EvidenceStore(project_root=PROJECT_ROOT),
        audit_recorder=recorder,
    )
    request = GetPolicyRequest(policy_ref=PolicyRef.INFINEQ_V1)

    response = tools.get_policy(request)

    assert response.policy_ref.value == "policy-infineq-v1"
    assert len(recorder.records) == 1
    record = recorder.records[0]
    assert record.tool_name == "get_policy"
    assert record.agent_role is AgentRole.VERIFIER
    assert record.status is ToolAuditStatus.SUCCESS
    assert record.validated_arguments_hash == validated_arguments_hash(request)
    assert record.returned_evidence_ids == ()


def test_tool_failure_is_re_raised_and_recorded_as_redacted_error() -> None:
    class FailingDetector:
        def run(self, _incident_id: str) -> object:
            raise RuntimeError(
                "authorization=Bearer do-not-store prompt='secret prompt' "
                "oracle_root=/hidden/oracle.json"
            )

    recorder = AuditRecorder()
    tools = InvestigatorTools(
        store=EvidenceStore(project_root=PROJECT_ROOT),
        detector=FailingDetector(),  # type: ignore[arg-type]
        audit_recorder=recorder,
    )
    request = GetIncidentPacketRequest(incident_id="ep-61d8aa")

    try:
        tools.get_incident_packet(request)
    except RuntimeError as error:
        assert "do-not-store" in str(error)
    else:
        raise AssertionError("the detector failure must be re-raised")

    assert len(recorder.records) == 1
    record = recorder.records[0]
    assert record.status is ToolAuditStatus.ERROR
    assert record.returned_evidence_ids == ()
    serialized = str(record.model_dump(mode="json")).lower()
    assert "do-not-store" not in serialized
    assert "secret prompt" not in serialized
    assert "oracle_root" not in serialized
    assert "/hidden" not in serialized
    assert "[redacted]" in serialized


def test_error_redaction_survives_an_exception_with_a_broken_string_representation() -> None:
    class UnprintableError:
        def __str__(self) -> str:
            raise RuntimeError("string conversion failed")

    assert redact_error(UnprintableError()) == "[REDACTED]"


def test_error_redaction_drops_unlabelled_paths_and_raw_tool_body_shapes() -> None:
    for unsafe_error in (
        "data at /srv/infineq/records.json",
        r"data at C:\Users\frances\records.json",
        '{"tool_body":"opaque raw content"}',
    ):
        assert redact_error(unsafe_error) == "[REDACTED]"


def test_error_redaction_drops_free_form_prompt_and_oracle_terms() -> None:
    for unsafe_error in (
        "raw prompt text supplied by the caller",
        "hidden oracle scenario family answer",
    ):
        assert redact_error(unsafe_error) == "[REDACTED]"


def test_audit_record_is_frozen_and_rejects_raw_oracle_fields() -> None:
    recorder = AuditRecorder()
    record = recorder.record(
        tool_name="get_policy",
        validated_arguments=GetIncidentPacketRequest(incident_id="ep-61d8aa"),
        agent_role=AgentRole.VERIFIER,
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
        ended_at=datetime(2026, 1, 1, tzinfo=UTC),
        status=ToolAuditStatus.SUCCESS,
    )

    with pytest.raises(ValidationError):
        record.status = ToolAuditStatus.ERROR  # type: ignore[misc]
    with pytest.raises(ValidationError):
        ToolAuditRecord.model_validate(
            {**record.model_dump(mode="json"), "oracle_root": "/hidden/oracle"}
        )
