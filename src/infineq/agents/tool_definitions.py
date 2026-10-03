"""Strict JSON schemas for the Investigator's application-executed functions."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Final

from infineq.evidence.tool_schemas import (
    RunbookQueryEnum,
    SignalEnum,
    WindowEnum,
)
from infineq.schemas.action import ActionType

INVESTIGATOR_TOOL_NAMES: Final[tuple[str, ...]] = (
    "get_incident_packet",
    "get_signal_window",
    "get_request_samples",
    "get_deployment_snapshot",
    "search_runbook",
    "get_evidence",
    "prepare_action_plan",
)

_INCIDENT_ID: dict[str, Any] = {
    "type": "string",
    "pattern": r"^ep-[a-z0-9]+$",
    "minLength": 4,
    "maxLength": 64,
}
_WINDOW_ENUM = [item.value for item in WindowEnum]
_SIGNAL_ENUM = [item.value for item in SignalEnum]
_ACTION_ENUM = [item.value for item in ActionType]
_RUNBOOK_ENUM = [item.value for item in RunbookQueryEnum]
_EVIDENCE_ID: dict[str, Any] = {
    "type": "string",
    "pattern": r"^ev:[a-z0-9-]+:[a-z0-9_-]+:[a-z0-9_-]+:[a-z0-9_-]+:[a-z0-9_-]+:[a-f0-9]{8}$",
    "maxLength": 256,
}


@dataclass(frozen=True, slots=True)
class InvestigatorToolDefinition:
    """Provider-neutral strict function definition."""

    name: str
    description: str
    parameters: dict[str, Any]
    strict: bool = True

    def to_foundry_tool(self) -> Any:
        """Build the current Azure SDK FunctionTool without executing it."""

        from azure.ai.projects.models import FunctionTool

        return FunctionTool(
            name=self.name,
            description=self.description,
            parameters=self.parameters,
            strict=self.strict,
        )

    def to_hash_entry(self) -> dict[str, Any]:
        """Return the provider-relevant fields used for the tool-schema hash."""

        return {
            "name": self.name,
            "description": self.description,
            "parameters": deepcopy(self.parameters),
            "strict": self.strict,
        }


def _parameters(properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


def _build_definitions() -> tuple[InvestigatorToolDefinition, ...]:
    return (
        InvestigatorToolDefinition(
            name="get_incident_packet",
            description="Fetch the normalized detector packet for the current opaque incident.",
            parameters=_parameters({"incident_id": _INCIDENT_ID}),
        ),
        InvestigatorToolDefinition(
            name="get_signal_window",
            description="Read one allow-listed signal in a fixed baseline or observation window.",
            parameters=_parameters(
                {
                    "incident_id": _INCIDENT_ID,
                    "signal_enum": {"type": "string", "enum": _SIGNAL_ENUM},
                    "window_enum": {"type": "string", "enum": _WINDOW_ENUM},
                }
            ),
        ),
        InvestigatorToolDefinition(
            name="get_request_samples",
            description="Read at most twenty sanitized request samples from a fixed window.",
            parameters=_parameters(
                {
                    "incident_id": _INCIDENT_ID,
                    "window_enum": {"type": "string", "enum": _WINDOW_ENUM},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                }
            ),
        ),
        InvestigatorToolDefinition(
            name="get_deployment_snapshot",
            description="Read the bounded deployment and replica snapshot for a fixed window.",
            parameters=_parameters(
                {
                    "incident_id": _INCIDENT_ID,
                    "window_enum": {"type": "string", "enum": _WINDOW_ENUM},
                }
            ),
        ),
        InvestigatorToolDefinition(
            name="search_runbook",
            description="Search up to three curated runbook chunks using a frozen enum.",
            parameters=_parameters(
                {
                    "query_enum": {"type": "string", "enum": _RUNBOOK_ENUM},
                    "top_k": {"type": "integer", "minimum": 1, "maximum": 3},
                }
            ),
        ),
        InvestigatorToolDefinition(
            name="get_evidence",
            description="Resolve up to twenty immutable evidence IDs returned for this run.",
            parameters=_parameters(
                {
                    "evidence_ids": {
                        "type": "array",
                        "items": _EVIDENCE_ID,
                        "minItems": 1,
                        "maxItems": 20,
                    }
                }
            ),
        ),
        InvestigatorToolDefinition(
            name="prepare_action_plan",
            description=(
                "Prepare a simulator-only dry-run action plan; the application must never "
                "execute it."
            ),
            parameters=_parameters(
                {
                    "incident_id": _INCIDENT_ID,
                    "action_type": {"type": "string", "enum": _ACTION_ENUM},
                }
            ),
        ),
    )


INVESTIGATOR_TOOL_DEFINITIONS: Final[tuple[InvestigatorToolDefinition, ...]] = _build_definitions()
TOOL_DEFINITIONS: Final[tuple[InvestigatorToolDefinition, ...]] = INVESTIGATOR_TOOL_DEFINITIONS


def get_investigator_tool_definitions() -> tuple[InvestigatorToolDefinition, ...]:
    """Return the immutable seven-tool definition tuple."""

    return INVESTIGATOR_TOOL_DEFINITIONS


def investigator_tool_schema_sha256() -> str:
    """Hash the ordered Investigator function name, schema, and strict flag."""

    encoded = json.dumps(
        tuple(definition.to_hash_entry() for definition in INVESTIGATOR_TOOL_DEFINITIONS),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


INVESTIGATOR_TOOL_SCHEMA_SHA256: Final[str] = investigator_tool_schema_sha256()


__all__ = [
    "INVESTIGATOR_TOOL_DEFINITIONS",
    "INVESTIGATOR_TOOL_NAMES",
    "INVESTIGATOR_TOOL_SCHEMA_SHA256",
    "TOOL_DEFINITIONS",
    "InvestigatorToolDefinition",
    "get_investigator_tool_definitions",
    "investigator_tool_schema_sha256",
]
