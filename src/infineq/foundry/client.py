"""Microsoft Foundry client boundary and model connectivity smoke test."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from time import perf_counter
from typing import Protocol

from azure.ai.projects import AIProjectClient
from azure.core.credentials import TokenCredential
from azure.identity import DefaultAzureCredential
from openai import OpenAI

from infineq.config import FoundrySettings
from infineq.errors import ToolTransportError

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
            "success": True,
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
