"""Microsoft Foundry client boundary and model connectivity smoke test."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from time import perf_counter
from typing import Any, Protocol, cast

from azure.ai.projects import AIProjectClient
from azure.core.credentials import TokenCredential
from azure.identity import DefaultAzureCredential
from openai import OpenAI

from infineq.config import FoundrySettings
from infineq.errors import ToolTransportError
from infineq.foundry.protocols import (
    FoundryResponse,
    FunctionCall,
    FunctionToolOutput,
    ResponseUsage,
)

_SMOKE_PROMPT = "Return exactly: INFOUNDRY_READY"
_SMOKE_EXPECTED = "INFOUNDRY_READY"


@dataclass(frozen=True, slots=True)
class RawModelResponse:
    """Minimum model response exposed by the Foundry adapter."""

    response_id: str
    output_text: str


class ModelResponder(Protocol):
    """Protocol used by deterministic code and test doubles."""

    def create_response(self, *, model: str, input_text: str) -> RawModelResponse:
        """Create one model response."""

        ...


@dataclass(frozen=True, slots=True)
class ModelSmokeResult:
    """Safe smoke-test metadata; model content is deliberately excluded."""

    deployment_name: str
    response_id: str
    latency_ms: float
    exact_match: bool

    def to_public_record(self) -> dict[str, str | float | bool]:
        """Return the only fields safe for console output."""

        return {
            "success": self.exact_match,
            "deployment_name": self.deployment_name,
            "response_id": self.response_id,
            "latency_ms": self.latency_ms,
            "exact_match": self.exact_match,
        }


class AzureFoundryModelClient:
    """Thin adapter over the Foundry project and its OpenAI Responses client."""

    def __init__(self, project_client: AIProjectClient) -> None:
        self._project_client = project_client
        self._openai_client: OpenAI = project_client.get_openai_client()

    @classmethod
    def from_settings(
        cls,
        settings: FoundrySettings,
        *,
        credential: TokenCredential | None = None,
    ) -> AzureFoundryModelClient:
        """Create an authenticated adapter without key-based credentials."""

        resolved_credential = credential or DefaultAzureCredential(
            exclude_interactive_browser_credential=True
        )
        return cls(
            AIProjectClient(
                endpoint=settings.endpoint,
                credential=resolved_credential,
            )
        )

    def create_response(self, *, model: str, input_text: str) -> RawModelResponse:
        """Request one response and normalize SDK-specific fields."""

        try:
            response = self._openai_client.responses.create(model=model, input=input_text)
        except Exception as exc:
            raise ToolTransportError("Foundry model request failed") from exc
        return RawModelResponse(
            response_id=response.id,
            output_text=response.output_text or "",
        )

    def close(self) -> None:
        """Close both SDK clients."""

        self._openai_client.close()
        self._project_client.close()

    def __enter__(self) -> AzureFoundryModelClient:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()


def _value(item: object, name: str, default: object = None) -> object:
    if isinstance(item, dict):
        return item.get(name, default)
    return getattr(item, name, default)


class AzureFoundryResponsesClient:
    """Adapt the Foundry project OpenAI Responses client to the local protocol."""

    def __init__(
        self,
        project_client: AIProjectClient | object,
        *,
        agent_name: str,
        agent_version: str,
    ) -> None:
        self._openai: Any = cast(Any, project_client).get_openai_client()
        self._agent_name = agent_name
        self._agent_version = agent_version

    def create_response(
        self,
        *,
        input: str | Sequence[FunctionToolOutput],
        previous_response_id: str | None,
        conversation_id: str | None,
        allow_tools: bool,
        timeout_seconds: float,
    ) -> FoundryResponse:
        """Create one agent-referenced response and normalize function calls."""

        if isinstance(input, str):
            sdk_input: object = input
        else:
            sdk_input = [
                {
                    "type": "function_call_output",
                    "call_id": item.call_id,
                    "output": item.output,
                }
                for item in input
            ]
        request: dict[str, object] = {
            "input": sdk_input,
            "extra_body": {
                "agent_reference": {
                    "name": self._agent_name,
                    "version": self._agent_version,
                    "type": "agent_reference",
                }
            },
            "timeout": timeout_seconds,
        }
        if conversation_id is not None:
            request["conversation"] = conversation_id
        if previous_response_id is not None:
            request["previous_response_id"] = previous_response_id
        if not allow_tools:
            request["tool_choice"] = "none"
        try:
            response = self._openai.responses.create(**request)
        except Exception as exc:
            raise ToolTransportError("Foundry Responses request failed") from exc

        calls: list[FunctionCall] = []
        output_items = _value(response, "output", ())
        if isinstance(output_items, Sequence) and not isinstance(output_items, (str, bytes)):
            for item in output_items:
                if _value(item, "type") != "function_call":
                    continue
                call_id = _value(item, "call_id")
                name = _value(item, "name")
                arguments = _value(item, "arguments")
                if not all(isinstance(value, str) for value in (call_id, name, arguments)):
                    raise ToolTransportError("Foundry returned an invalid function call")
                calls.append(
                    FunctionCall(
                        call_id=cast(str, call_id),
                        name=cast(str, name),
                        arguments=cast(str, arguments),
                    )
                )

        usage_value = _value(response, "usage")
        usage = None
        if usage_value is not None:
            counters = {
                name: _value(usage_value, name)
                for name in ("input_tokens", "output_tokens", "total_tokens")
            }
            usage = ResponseUsage(
                input_tokens=counters["input_tokens"]
                if isinstance(counters["input_tokens"], int)
                else None,
                output_tokens=counters["output_tokens"]
                if isinstance(counters["output_tokens"], int)
                else None,
                total_tokens=counters["total_tokens"]
                if isinstance(counters["total_tokens"], int)
                else None,
            )
        response_id = _value(response, "id")
        if not isinstance(response_id, str) or not response_id:
            raise ToolTransportError("Foundry response identifier was empty")
        output_text = _value(response, "output_text", "")
        if not isinstance(output_text, str):
            output_text = ""
        return FoundryResponse(
            response_id=response_id,
            function_calls=tuple(calls),
            output_text=output_text,
            usage=usage,
        )


def run_model_smoke(
    client: ModelResponder,
    *,
    deployment_name: str,
    clock: Callable[[], float] = perf_counter,
) -> ModelSmokeResult:
    """Run a minimal request and retain only safe operational metadata."""

    started = clock()
    try:
        response = client.create_response(model=deployment_name, input_text=_SMOKE_PROMPT)
    except ToolTransportError:
        raise
    except Exception as exc:
        raise ToolTransportError("Foundry model request failed") from exc
    elapsed_ms = round((clock() - started) * 1_000, 3)
    output = response.output_text.strip()
    if not output:
        raise ToolTransportError("Foundry returned an empty response")
    return ModelSmokeResult(
        deployment_name=deployment_name,
        response_id=response.response_id,
        latency_ms=elapsed_ms,
        exact_match=output == _SMOKE_EXPECTED,
    )
