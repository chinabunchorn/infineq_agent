from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from infineq.foundry.client import AzureFoundryResponsesClient
from infineq.foundry.protocols import FunctionToolOutput


class FakeResponses:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return SimpleNamespace(
            id=f"resp-{len(self.calls)}",
            output=[
                SimpleNamespace(
                    type="function_call",
                    call_id="call-1",
                    name="get_incident_packet",
                    arguments='{"incident_id":"ep-61d8aa"}',
                )
            ]
            if len(self.calls) == 1
            else [],
            output_text='{"ok":true}' if len(self.calls) > 1 else "",
            usage=SimpleNamespace(input_tokens=10, output_tokens=5, total_tokens=15),
        )


class FakeProject:
    def __init__(self, responses: FakeResponses) -> None:
        self.responses = responses

    def get_openai_client(self) -> Any:
        return SimpleNamespace(responses=self.responses)


def test_responses_adapter_maps_agent_reference_and_function_outputs() -> None:
    responses = FakeResponses()
    client = AzureFoundryResponsesClient(
        FakeProject(responses),
        agent_name="infineq-investigator",
        agent_version="7",
    )

    first = client.create_response(
        input="investigate",
        previous_response_id=None,
        conversation_id="conv-1",
        allow_tools=True,
        timeout_seconds=12.0,
    )
    second = client.create_response(
        input=(FunctionToolOutput(call_id="call-1", output='{"safe":true}'),),
        previous_response_id=first.response_id,
        conversation_id="conv-1",
        allow_tools=False,
        timeout_seconds=8.0,
    )

    assert first.response_id == "resp-1"
    assert first.function_calls[0].name == "get_incident_packet"
    assert second.output_text == '{"ok":true}'
    assert responses.calls[0]["conversation"] == "conv-1"
    assert responses.calls[0]["extra_body"]["agent_reference"] == {
        "name": "infineq-investigator",
        "version": "7",
        "type": "agent_reference",
    }
    assert responses.calls[1]["input"] == [
        {"type": "function_call_output", "call_id": "call-1", "output": '{"safe":true}'}
    ]
    assert responses.calls[1]["tool_choice"] == "none"
    assert second.usage is not None
    assert second.usage.total_tokens == 15
