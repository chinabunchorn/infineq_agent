from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from infineq.agents.tool_loop import (
    ToolLoopConfig,
    ToolLoopStatus,
    run_bounded_tool_loop,
)
from infineq.errors import ToolPolicyDeniedError, ToolTransportError
from infineq.foundry.protocols import FoundryResponse, FunctionCall


@dataclass
class FakeExecutor:
    dispatch_results: dict[str, Any]
    transport_failures: int = 0

    def __post_init__(self) -> None:
        self.validated: list[tuple[str, dict[str, Any]]] = []
        self.dispatched: list[tuple[str, dict[str, Any]]] = []

    def validate_arguments(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.validated.append((name, arguments))
        if name == "get_signal_window" and arguments.get("signal_enum") not in {"ttft", "itl"}:
            raise ValueError("signal is not allow-listed")
        return arguments

    def dispatch(self, name: str, arguments: dict[str, Any]) -> Any:
        self.dispatched.append((name, arguments))
        if self.transport_failures:
            self.transport_failures -= 1
            raise ToolTransportError("temporary tool transport failure")
        return self.dispatch_results.get(name, {"status": "ok"})


def response(
    response_id: str, *, calls: tuple[FunctionCall, ...] = (), text: str = ""
) -> FoundryResponse:
    return FoundryResponse(response_id=response_id, function_calls=calls, output_text=text)


class FakeClient:
    def __init__(self, responses: list[FoundryResponse]) -> None:
        self.responses = responses
        self.calls: list[dict[str, Any]] = []

    def create_response(self, **kwargs: Any) -> FoundryResponse:
        self.calls.append(kwargs)
        return self.responses.pop(0)


def test_loop_processes_all_calls_and_preserves_response_linkage() -> None:
    client = FakeClient(
        [
            response(
                "resp-1",
                calls=(
                    FunctionCall("call-1", "get_incident_packet", '{"incident_id":"ep-61d8aa"}'),
                    FunctionCall("call-2", "get_signal_window", '{"signal_enum":"ttft"}'),
                ),
            ),
            response("resp-2", text='{"final":true}'),
        ]
    )
    executor = FakeExecutor(
        {"get_incident_packet": {"evidence_id": "ev:ep-61d8aa:s:a:b:c:deadbeef"}}
    )

    result = run_bounded_tool_loop(
        client,
        initial_input="investigate ep-61d8aa",
        executor=executor,
        config=ToolLoopConfig(),
    )

    assert result.status is ToolLoopStatus.COMPLETE
    assert len(executor.dispatched) == 2
    assert client.calls[1]["previous_response_id"] == "resp-1"
    assert client.calls[1]["conversation_id"] == client.calls[0]["conversation_id"]
    assert [item.call_id for item in client.calls[1]["input"]] == ["call-1", "call-2"]
    assert result.response_ids == ("resp-1", "resp-2")


def test_loop_rejects_a_response_that_would_overflow_the_total_budget_before_dispatch() -> None:
    client = FakeClient(
        [
            response(
                "resp-1",
                calls=tuple(
                    FunctionCall(f"call-{index}", "get_incident_packet", "{}") for index in range(7)
                ),
            )
        ]
    )
    executor = FakeExecutor({})

    result = run_bounded_tool_loop(
        client,
        initial_input="investigate",
        executor=executor,
        config=ToolLoopConfig(max_successful_tool_calls=6),
    )

    assert result.status is ToolLoopStatus.TOOL_BUDGET_EXCEEDED
    assert executor.dispatched == []


def test_loop_disables_tools_after_the_budget_is_consumed() -> None:
    client = FakeClient(
        [
            response(
                "resp-1",
                calls=(FunctionCall("call-1", "get_incident_packet", "{}"),),
            ),
            response("resp-2", text='{"final":true}'),
        ]
    )
    executor = FakeExecutor({})

    result = run_bounded_tool_loop(
        client,
        initial_input="investigate",
        executor=executor,
        config=ToolLoopConfig(max_successful_tool_calls=1),
    )

    assert result.status is ToolLoopStatus.COMPLETE
    assert client.calls[1]["allow_tools"] is False


def test_loop_validates_json_and_arguments_before_dispatch() -> None:
    client = FakeClient(
        [
            response(
                "resp-1",
                calls=(FunctionCall("call-1", "get_signal_window", '{"signal_enum":"shell"}'),),
            )
        ]
    )
    executor = FakeExecutor({})

    result = run_bounded_tool_loop(
        client,
        initial_input="investigate",
        executor=executor,
    )

    assert result.status is ToolLoopStatus.INVALID_ARGUMENTS
    assert executor.dispatched == []
    assert executor.validated == [("get_signal_window", {"signal_enum": "shell"})]


def test_loop_turns_typed_tool_policy_denial_into_incomplete_result() -> None:
    class DenyingExecutor(FakeExecutor):
        def validate_arguments(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
            raise ToolPolicyDeniedError("cross-episode request")

    client = FakeClient(
        [response("resp-1", calls=(FunctionCall("call-1", "get_incident_packet", "{}"),))]
    )

    result = run_bounded_tool_loop(
        client,
        initial_input="investigate",
        executor=DenyingExecutor({}),
    )

    assert result.status is ToolLoopStatus.INVALID_ARGUMENTS


def test_loop_retries_one_typed_transport_failure_only() -> None:
    client = FakeClient(
        [
            response(
                "resp-1",
                calls=(FunctionCall("call-1", "get_incident_packet", "{}"),),
            ),
            response("resp-2", text="{}"),
        ]
    )
    executor = FakeExecutor({}, transport_failures=1)

    result = run_bounded_tool_loop(
        client,
        initial_input="investigate",
        executor=executor,
        config=ToolLoopConfig(max_successful_tool_calls=6),
    )

    assert result.status is ToolLoopStatus.COMPLETE
    assert len(executor.dispatched) == 2
    assert result.tool_retry_count == 1
    assert result.successful_tool_calls == 1


def test_loop_never_dispatches_a_forbidden_function() -> None:
    client = FakeClient([response("resp-1", calls=(FunctionCall("call-1", "run_shell", "{}"),))])
    executor = FakeExecutor({})

    result = run_bounded_tool_loop(client, initial_input="investigate", executor=executor)

    assert result.status is ToolLoopStatus.FORBIDDEN_TOOL
    assert result.forbidden_tool_calls == 1
    assert executor.validated == []
    assert executor.dispatched == []


def test_loop_stops_when_the_injected_clock_passes_the_overall_timeout() -> None:
    values = iter((0.0, 2.0))
    client = FakeClient([response("resp-1", text="{}")])
    executor = FakeExecutor({})

    result = run_bounded_tool_loop(
        client,
        initial_input="investigate",
        executor=executor,
        config=ToolLoopConfig(timeout_seconds=1.0),
        clock=lambda: next(values),
    )

    assert result.status is ToolLoopStatus.TIMEOUT


def test_one_format_repair_cannot_request_tools() -> None:
    client = FakeClient(
        [
            response("resp-1", text="not json"),
            response("resp-2", text='{"ok":true}'),
        ]
    )
    executor = FakeExecutor({})

    result = run_bounded_tool_loop(
        client,
        initial_input="investigate",
        executor=executor,
        parse_final=lambda text: json.loads(text),
        repair_input="Return only the required JSON object.",
    )

    assert result.status is ToolLoopStatus.COMPLETE
    assert result.parsed_output == {"ok": True}
    assert client.calls[1]["allow_tools"] is False
    assert client.calls[1]["previous_response_id"] == "resp-1"


def test_failed_format_repair_returns_analysis_incomplete() -> None:
    client = FakeClient(
        [
            response("resp-1", text="not json"),
            response("resp-2", text="still not json"),
        ]
    )
    executor = FakeExecutor({})

    result = run_bounded_tool_loop(
        client,
        initial_input="investigate",
        executor=executor,
        parse_final=lambda text: json.loads(text),
        repair_input="Return only the required JSON object.",
    )

    assert result.status is ToolLoopStatus.ANALYSIS_INCOMPLETE
    assert result.parsed_output is None
    assert result.repair_attempted is True
