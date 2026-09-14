"""Versioned Microsoft Foundry prompt-agent registration and verification."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from azure.ai.projects.models import (
    PromptAgentDefinition,
    PromptAgentDefinitionTextOptions,
    TextResponseFormatJsonSchema,
)
from azure.core.exceptions import ResourceNotFoundError

from infineq.agents.prompt_manifest import PromptManifest, load_investigator_prompt
from infineq.agents.tool_definitions import get_investigator_tool_definitions
from infineq.foundry.protocols import PromptAgentOperations
from infineq.schemas.investigation import InvestigationResultV1

INVESTIGATOR_AGENT_NAME: Final[str] = "infineq-investigator"
INVESTIGATION_RESULT_FORMAT_NAME: Final[str] = "investigation_result_v1"
_SCHEMA_VERSION_FIELD: Final[str] = "schema_version"


class VersionNotFoundError(LookupError):
    """The exact requested agent version does not exist."""


class AgentVersionMismatchError(ValueError):
    """A retrieved agent version is not the pinned definition."""


@dataclass(frozen=True, slots=True)
class AgentVersionRecord:
    """Safe version identity and pinned metadata."""

    name: str
    version: str
    prompt_version: str
    prompt_sha256: str
    model_deployment_name: str

    def to_manifest(self) -> dict[str, str]:
        """Return a local-safe deployment manifest without endpoint or credentials."""

        return {
            "agent_name": self.name,
            "agent_version": self.version,
            "prompt_version": self.prompt_version,
            "prompt_sha256": self.prompt_sha256,
            "model_deployment_name": self.model_deployment_name,
        }


def _field(record: object, name: str) -> object:
    if isinstance(record, Mapping):
        return record.get(name)
    return getattr(record, name, None)


def build_investigation_result_json_schema() -> dict[str, Any]:
    """Return the strict provider schema derived from ``InvestigationResultV1``.

    The application still parses the complete Pydantic contract.  The wire schema
    omits only the defaulted ``schema_version`` fields so the model need not repeat
    bookkeeping fields at every nested StrictModel layer.
    """

    schema = deepcopy(InvestigationResultV1.model_json_schema())

    def normalize(node: object) -> None:
        if not isinstance(node, dict):
            return
        properties = node.get("properties")
        if isinstance(properties, dict):
            properties.pop(_SCHEMA_VERSION_FIELD, None)
            node["required"] = list(properties)
            node["additionalProperties"] = False
            for property_schema in properties.values():
                normalize(property_schema)
        items = node.get("items")
        if isinstance(items, dict):
            normalize(items)
        for key in ("anyOf", "oneOf", "allOf"):
            alternatives = node.get(key)
            if isinstance(alternatives, list):
                for alternative in alternatives:
                    normalize(alternative)
        definitions = node.get("$defs")
        if isinstance(definitions, dict):
            for definition in definitions.values():
                normalize(definition)

    normalize(schema)
    return schema


def build_prompt_agent_definition(
    *,
    model_deployment_name: str,
    manifest: PromptManifest,
    project_root: Path | None = None,
) -> PromptAgentDefinition:
    """Build the SDK definition from the committed prompt and strict tool schemas."""

    if not model_deployment_name:
        raise ValueError("model deployment name is required")
    resolved_project_root = project_root or Path(__file__).resolve().parents[3]
    instructions = (
        load_investigator_prompt(project_root=resolved_project_root)
        + "\n\nPinned prompt manifest metadata: "
        + f"version={manifest.version}; sha256={manifest.prompt_sha256}."
    )
    tools = [definition.to_foundry_tool() for definition in get_investigator_tool_definitions()]
    response_format = TextResponseFormatJsonSchema(
        name=INVESTIGATION_RESULT_FORMAT_NAME,
        schema=build_investigation_result_json_schema(),
        strict=True,
    )
    return PromptAgentDefinition(
        model=model_deployment_name,
        instructions=instructions,
        tools=tools,
        temperature=0.0,
        text=PromptAgentDefinitionTextOptions(format=response_format),
    )


def _record_from_sdk(
    record: object,
    *,
    expected_version: str,
    manifest: PromptManifest,
    model_deployment_name: str,
) -> AgentVersionRecord:
    name = _field(record, "name")
    version = _field(record, "version")
    metadata = _field(record, "metadata")
    if not isinstance(name, str) or name != INVESTIGATOR_AGENT_NAME:
        raise AgentVersionMismatchError("retrieved agent name does not match")
    if not isinstance(version, str) or version != expected_version:
        raise AgentVersionMismatchError("retrieved agent version does not match")
    if not isinstance(metadata, Mapping):
        raise AgentVersionMismatchError("retrieved agent metadata is unavailable")
    expected = {
        "prompt_version": manifest.version,
        "prompt_sha256": manifest.prompt_sha256,
        "model_deployment_name": model_deployment_name,
    }
    for key, value in expected.items():
        if metadata.get(key) != value:
            raise AgentVersionMismatchError(f"retrieved agent metadata mismatch: {key}")
    return AgentVersionRecord(
        name=name,
        version=version,
        prompt_version=manifest.version,
        prompt_sha256=manifest.prompt_sha256,
        model_deployment_name=model_deployment_name,
    )


class PromptAgentRegistry:
    """Create once or retrieve one exact prompt-agent version; never delete."""

    def __init__(
        self, operations: PromptAgentOperations | object, *, project_root: Path | None = None
    ) -> None:
        self._operations: Any = operations
        self._project_root = project_root

    def retrieve_exact(self, agent_version: str) -> object:
        """Retrieve one exact version and preserve a typed not-found boundary."""

        if not agent_version:
            raise ValueError("agent version is required")
        try:
            return self._operations.get_version(
                agent_name=INVESTIGATOR_AGENT_NAME,
                agent_version=agent_version,
            )
        except VersionNotFoundError:
            raise
        except ResourceNotFoundError as error:
            raise VersionNotFoundError("agent version was not found") from error

    def ensure_version(
        self,
        *,
        model_deployment_name: str,
        manifest: PromptManifest,
        expected_version: str | None = None,
    ) -> AgentVersionRecord:
        """Retrieve an exact pinned version or create and immediately verify one."""

        if expected_version is not None:
            record = self.retrieve_exact(expected_version)
            return _record_from_sdk(
                record,
                expected_version=expected_version,
                manifest=manifest,
                model_deployment_name=model_deployment_name,
            )

        definition = build_prompt_agent_definition(
            model_deployment_name=model_deployment_name,
            manifest=manifest,
            project_root=self._project_root,
        )
        metadata = {
            "prompt_version": manifest.version,
            "prompt_sha256": manifest.prompt_sha256,
            "model_deployment_name": model_deployment_name,
        }
        created = self._operations.create_version(
            agent_name=INVESTIGATOR_AGENT_NAME,
            definition=definition,
            metadata=metadata,
            description="Infineq read-only reliability investigator",
        )
        created_version = _field(created, "version")
        if not isinstance(created_version, str) or not created_version:
            raise AgentVersionMismatchError("created agent version was not returned")
        verified = self.retrieve_exact(created_version)
        return _record_from_sdk(
            verified,
            expected_version=created_version,
            manifest=manifest,
            model_deployment_name=model_deployment_name,
        )


__all__ = [
    "INVESTIGATION_RESULT_FORMAT_NAME",
    "INVESTIGATOR_AGENT_NAME",
    "AgentVersionMismatchError",
    "AgentVersionRecord",
    "PromptAgentRegistry",
    "VersionNotFoundError",
    "build_investigation_result_json_schema",
    "build_prompt_agent_definition",
]
