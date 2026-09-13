from collections.abc import Callable

import pytest

from infineq.errors import ToolTransportError
from infineq.foundry.client import RawModelResponse, run_model_smoke


class FakeResponder:
    def __init__(self, output_text: str) -> None:
        self.output_text = output_text
        self.calls: list[tuple[str, str]] = []

    def create_response(self, *, model: str, input_text: str) -> RawModelResponse:
        self.calls.append((model, input_text))
        return RawModelResponse(response_id="resp-aa12", output_text=self.output_text)


def sequence_clock(*values: float) -> Callable[[], float]:
    iterator = iter(values)
    return lambda: next(iterator)


def test_smoke_uses_a_protocol_and_returns_only_safe_metadata() -> None:
    client = FakeResponder("INFOUNDRY_READY")

    result = run_model_smoke(
        client,
        deployment_name="infineq-gpt-5-4-mini",
        clock=sequence_clock(10.0, 10.025),
    )

    assert client.calls == [("infineq-gpt-5-4-mini", "Return exactly: INFOUNDRY_READY")]
    assert result.exact_match is True
    assert result.latency_ms == pytest.approx(25.0)
    assert result.to_public_record() == {
        "success": True,
        "deployment_name": "infineq-gpt-5-4-mini",
        "response_id": "resp-aa12",
        "latency_ms": 25.0,
        "exact_match": True,
    }
    assert "INFOUNDRY_READY" not in str(result.to_public_record())


def test_smoke_normalizes_an_empty_model_response() -> None:
    client = FakeResponder("  ")

    with pytest.raises(ToolTransportError, match="empty response"):
        run_model_smoke(
            client,
            deployment_name="infineq-gpt-5-4-mini",
            clock=sequence_clock(10.0, 10.1),
        )
