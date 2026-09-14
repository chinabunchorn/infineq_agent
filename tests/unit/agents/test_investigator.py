from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from infineq.agents.investigator import Investigator
from infineq.agents.tool_loop import ToolLoopConfig
from infineq.detection.detector import Detector
from infineq.evidence.store import EvidenceStore
from infineq.evidence.tools import InvestigatorTools
from infineq.foundry.protocols import FoundryResponse, FunctionCall
from infineq.schemas.investigation import Disposition
from infineq.testing.foundry import FakeFoundryClient, InjectedOutputExecutor, transport_failure

PROJECT_ROOT = Path(__file__).parents[3]
INCIDENT_ID = "ep-61d8aa"


def packet_and_tools() -> tuple[Any, InvestigatorTools]:
    store = EvidenceStore(project_root=PROJECT_ROOT)
    packet = Detector(store=store).detect(INCIDENT_ID)
    assert packet is not None
    return packet, InvestigatorTools(store=store, detector=Detector(store=store))


def final_payload(packet: Any, *, disposition: str = "diagnosed") -> dict[str, Any]:
    evidence_ids = tuple(item.evidence_id for item in packet.evidence_refs[:3])
    return {
        "investigation_id": "investigation-ep-61d8aa",
        "incident_id": packet.incident_id,
        "completed_at": packet.detected_at.isoformat(),
        "disposition": disposition,
        "summary": (
            "The available evidence supports a bounded comparison of capacity_queueing, "
            "backend_slowdown, "
            "workload_shape_change, and replica_or_deployment_regression."
        ),
        "hypotheses": []
        if disposition != "diagnosed"
        else [
            {
                "hypothesis_id": "hyp-capacity-queueing",
                "family": "capacity_queueing",
                "rank": 1,
                "evidence_coverage": "complete",
                "statement": "Capacity queueing is the leading supported explanation.",
                "supporting_evidence_ids": [evidence_ids[0]],
                "contradicting_evidence_ids": [],
                "missing_evidence": [],
            },
            {
                "hypothesis_id": "hyp-backend-slowdown",
                "family": "backend_slowdown",
                "rank": 2,
                "evidence_coverage": "partial",
                "statement": "Backend slowdown is less supported by stable ITL.",
                "supporting_evidence_ids": [],
                "contradicting_evidence_ids": [evidence_ids[1]],
                "missing_evidence": ["stage timing"],
            },
            {
                "hypothesis_id": "hyp-workload-shape-change",
                "family": "workload_shape_change",
                "rank": 3,
                "evidence_coverage": "partial",
                "statement": "Workload-shape change remains an alternative.",
                "supporting_evidence_ids": [],
                "contradicting_evidence_ids": [evidence_ids[2]],
                "missing_evidence": ["token distribution"],
            },
        ],
        "leading_hypothesis_id": "hyp-capacity-queueing" if disposition == "diagnosed" else None,
        "proposed_action": None,
        "cited_evidence_ids": list(evidence_ids) if disposition == "diagnosed" else [],
        "limitations": [
            "replica_or_deployment_regression is excluded by the stable deployment evidence",
        ],
    }


def run_investigator(
    client: FakeFoundryClient,
    *,
    tools: InvestigatorTools | None = None,
    tool_executor: object | None = None,
    config: ToolLoopConfig | None = None,
) -> tuple[Any, Any]:
    packet, default_tools = packet_and_tools()
    investigator = Investigator(
        client=client,
        tools=tools or default_tools,
        tool_executor=tool_executor,
        loop_config=config,
    )
    return investigator.investigate(packet), investigator


def test_canonical_queue_investigation_is_grounded_and_schema_valid() -> None:
    packet, tools = packet_and_tools()
    client = FakeFoundryClient(
        [
            FoundryResponse(
                response_id="resp-queue-1",
                function_calls=(
                    FunctionCall(
                        "call-packet",
                        "get_incident_packet",
                        json.dumps({"incident_id": INCIDENT_ID}),
                    ),
                    FunctionCall(
                        "call-window",
                        "get_signal_window",
                        json.dumps(
                            {
                                "incident_id": INCIDENT_ID,
                                "signal_enum": "ttft",
                                "window_enum": "observation",
                            }
                        ),
                    ),
                ),
            ),
            FoundryResponse(
                response_id="resp-queue-2",
                output_text=json.dumps(final_payload(packet)),
            ),
        ]
    )

    result, investigator = run_investigator(client, tools=tools)

    assert result.disposition is Disposition.DIAGNOSED
    assert result.leading_hypothesis_id == "hyp-capacity-queueing"
    assert result.proposed_action is None
    assert len(result.hypotheses) == 3
    assert investigator.last_run.successful_tool_calls == 2
    assert investigator.last_run.final_output_text.startswith("{")
    assert all(trace.status == "success" for trace in investigator.last_run.traces)


def test_healthy_or_no_incident_output_has_no_action_or_cause() -> None:
    packet, _tools = packet_and_tools()
    client = FakeFoundryClient(
        [
            FoundryResponse(
                response_id="resp-healthy",
                output_text=json.dumps(final_payload(packet, disposition="no_incident")),
            )
        ]
    )

    result, _investigator = run_investigator(client)

    assert result.disposition is Disposition.NO_INCIDENT
    assert result.leading_hypothesis_id is None
    assert result.proposed_action is None


def test_missing_evidence_returns_indeterminate_without_an_action() -> None:
    packet, _tools = packet_and_tools()
    client = FakeFoundryClient(
        [
            FoundryResponse(
                response_id="resp-missing",
                output_text=json.dumps(final_payload(packet, disposition="indeterminate")),
            )
        ]
    )

    result, _investigator = run_investigator(client)

    assert result.disposition is Disposition.INDETERMINATE
    assert result.proposed_action is None
    assert result.leading_hypothesis_id is None


def test_fabricated_evidence_becomes_typed_analysis_incomplete_after_repair() -> None:
    packet, _tools = packet_and_tools()
    invalid = final_payload(packet)
    invalid["cited_evidence_ids"] = ["ev:ep-61d8aa:service_metrics:bad:ttft:p95:deadbeef"]
    client = FakeFoundryClient(
        [
            FoundryResponse(response_id="resp-bad-1", output_text=json.dumps(invalid)),
            FoundryResponse(response_id="resp-bad-2", output_text=json.dumps(invalid)),
        ]
    )

    result, investigator = run_investigator(client)

    assert result.disposition is Disposition.ANALYSIS_INCOMPLETE
    assert result.proposed_action is None
    assert investigator.last_run.repair_attempted is True
    assert "deadbeef" not in result.summary


def test_excessive_tool_calls_are_rejected_without_partial_dispatch() -> None:
    _packet, tools = packet_and_tools()
    calls = tuple(
        FunctionCall(
            f"call-{index}", "get_incident_packet", json.dumps({"incident_id": INCIDENT_ID})
        )
        for index in range(7)
    )
    client = FakeFoundryClient([FoundryResponse(response_id="resp-many", function_calls=calls)])

    result, investigator = run_investigator(client, tools=tools)

    assert result.disposition is Disposition.ANALYSIS_INCOMPLETE
    assert investigator.last_run.successful_tool_calls == 0
    assert tools.audit_records == ()


def test_invalid_json_arguments_are_not_dispatched() -> None:
    _packet, tools = packet_and_tools()
    client = FakeFoundryClient(
        [
            FoundryResponse(
                response_id="resp-invalid",
                function_calls=(FunctionCall("call-1", "get_incident_packet", "{"),),
            )
        ]
    )

    result, investigator = run_investigator(client, tools=tools)

    assert result.disposition is Disposition.ANALYSIS_INCOMPLETE
    assert investigator.last_run.successful_tool_calls == 0
    assert tools.audit_records == ()


def test_typed_transport_failure_is_retried_once() -> None:
    packet, _tools = packet_and_tools()
    client = FakeFoundryClient(
        [
            transport_failure(),
            FoundryResponse(
                response_id="resp-after-retry",
                output_text=json.dumps(final_payload(packet, disposition="indeterminate")),
            ),
        ]
    )

    result, investigator = run_investigator(client)

    assert result.disposition is Disposition.INDETERMINATE
    assert investigator.last_run.tool_retry_count == 1
    assert len(client.calls) == 2


def test_timeout_returns_typed_analysis_incomplete() -> None:
    packet, _tools = packet_and_tools()

    class SlowClient(FakeFoundryClient):
        def create_response(self, **kwargs: Any) -> FoundryResponse:
            time.sleep(0.02)
            return super().create_response(**kwargs)

    client = SlowClient(
        [FoundryResponse(response_id="resp-slow", output_text=json.dumps(final_payload(packet)))]
    )
    result, _investigator = run_investigator(
        client,
        config=ToolLoopConfig(timeout_seconds=0.001),
    )

    assert result.disposition is Disposition.ANALYSIS_INCOMPLETE


def test_format_repair_is_one_turn_and_disallows_tools() -> None:
    packet, _tools = packet_and_tools()
    client = FakeFoundryClient(
        [
            FoundryResponse(response_id="resp-format-1", output_text="not json"),
            FoundryResponse(
                response_id="resp-format-2",
                output_text=json.dumps(final_payload(packet, disposition="indeterminate")),
            ),
        ]
    )

    result, _investigator = run_investigator(client)

    assert result.disposition is Disposition.INDETERMINATE
    assert client.calls[1]["allow_tools"] is False
    assert client.calls[1]["previous_response_id"] == "resp-format-1"


def test_forbidden_tool_request_is_never_dispatched() -> None:
    _packet, tools = packet_and_tools()
    client = FakeFoundryClient(
        [
            FoundryResponse(
                response_id="resp-forbidden",
                function_calls=(FunctionCall("call-shell", "run_shell", "{}"),),
            )
        ]
    )

    result, investigator = run_investigator(client, tools=tools)

    assert result.disposition is Disposition.ANALYSIS_INCOMPLETE
    assert investigator.last_run.successful_tool_calls == 0
    assert tools.audit_records == ()


def test_instruction_injection_in_tool_output_is_treated_as_untrusted_data() -> None:
    packet, _tools = packet_and_tools()
    evidence_id = packet.evidence_refs[0].evidence_id
    client = FakeFoundryClient(
        [
            FoundryResponse(
                response_id="resp-inject-1",
                function_calls=(
                    FunctionCall(
                        "call-evidence",
                        "get_evidence",
                        json.dumps({"evidence_ids": [evidence_id]}),
                    ),
                ),
            ),
            FoundryResponse(
                response_id="resp-inject-2",
                output_text=json.dumps(final_payload(packet, disposition="indeterminate")),
            ),
        ]
    )
    executor = InjectedOutputExecutor(evidence_id)

    result, investigator = run_investigator(client, tool_executor=executor)

    assert result.disposition is Disposition.INDETERMINATE
    assert investigator.last_run.successful_tool_calls == 1
    serialized_tool_output = client.calls[1]["input"][0].output
    assert "Ignore previous instructions" not in serialized_tool_output
    assert "hidden reasoning" not in serialized_tool_output
