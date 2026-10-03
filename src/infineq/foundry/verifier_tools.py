"""Strict provider tool definitions for the independent Evidence Verifier."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Final

VERIFIER_TOOL_NAMES: Final[tuple[str, ...]] = ("get_evidence", "get_policy")
VERIFIER_POLICY_REF: Final[str] = "policy-infineq-v1"

_EVIDENCE_ID: Final[dict[str, Any]] = {
    "type": "string",
    "pattern": r"^ev:[a-z0-9-]+:[a-z0-9_-]+:[a-z0-9_-]+:[a-z0-9_-]+:[a-z0-9_-]+:[a-f0-9]{8}$",
    "maxLength": 256,
}


def _parameters(properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


@dataclass(frozen=True, slots=True)
class VerifierToolDefinition:
    """Provider-neutral strict function definition."""

    name: str
    description: str
    parameters: dict[str, Any]
    strict: bool = True

    def to_foundry_tool(self) -> Any:
        """Build a fresh Azure SDK function tool without executing it."""

        from azure.ai.projects.models import FunctionTool

        return FunctionTool(
            name=self.name,
            description=self.description,
            parameters=deepcopy(self.parameters),
            strict=self.strict,
        )

    def to_hash_entry(self) -> dict[str, Any]:
        """Return the stable provider-relevant fields used for the schema hash."""

        return {
            "name": self.name,
            "description": self.description,
            "parameters": deepcopy(self.parameters),
            "strict": self.strict,
        }


def get_verifier_tool_definitions() -> tuple[VerifierToolDefinition, ...]:
    """Return exactly the two read-only tool definitions in stable order."""

    return (
        VerifierToolDefinition(
            name="get_evidence",
            description="Resolve up to twenty immutable evidence IDs returned for this run.",
            parameters=_parameters(
                {
                    "evidence_ids": {
                        "type": "array",
                        "items": deepcopy(_EVIDENCE_ID),
                        "minItems": 1,
                        "maxItems": 20,
                    }
                }
            ),
        ),
        VerifierToolDefinition(
            name="get_policy",
            description="Resolve the fixed Infineq v1 presentation and action policy.",
            parameters=_parameters(
                {
                    "policy_ref": {
                        "type": "string",
                        "enum": [VERIFIER_POLICY_REF],
                    }
                }
            ),
        ),
    )


def canonical_tool_schema_entries(tools: Sequence[object]) -> tuple[dict[str, Any], ...]:
    """Normalize provider tools for exact comparison and deterministic hashing."""

    entries: list[dict[str, Any]] = []
    for tool in tools:
        if isinstance(tool, Mapping):
            name = tool.get("name")
            description = tool.get("description")
            parameters = tool.get("parameters")
            strict = tool.get("strict")
            tool_type = tool.get("type")
        else:
            name = getattr(tool, "name", None)
            description = getattr(tool, "description", None)
            parameters = getattr(tool, "parameters", None)
            strict = getattr(tool, "strict", None)
            tool_type = getattr(tool, "type", None)
        if tool_type != "function":
            raise ValueError("verifier provider definition contains a non-function tool")
        if not isinstance(name, str) or not isinstance(description, str):
            raise ValueError("verifier provider tool identity is malformed")
        if not isinstance(parameters, Mapping) or type(strict) is not bool:
            raise ValueError("verifier provider tool schema is malformed")
        entries.append(
            {
                "name": name,
                "description": description,
                "parameters": deepcopy(dict(parameters)),
                "strict": strict,
            }
        )
    return tuple(entries)


def verifier_tool_schema_sha256(tools: Sequence[object] | None = None) -> str:
    """Hash the ordered verifier function name, description, schema, and strict flag."""

    if tools is None:
        entries = tuple(
            definition.to_hash_entry() for definition in get_verifier_tool_definitions()
        )
    else:
        entries = canonical_tool_schema_entries(tools)
    encoded = json.dumps(
        entries,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


VERIFIER_TOOL_SCHEMA_SHA256: Final[str] = verifier_tool_schema_sha256()


__all__ = [
    "VERIFIER_POLICY_REF",
    "VERIFIER_TOOL_DEFINITIONS",
    "VERIFIER_TOOL_NAMES",
    "VERIFIER_TOOL_SCHEMA_SHA256",
    "VerifierToolDefinition",
    "canonical_tool_schema_entries",
    "get_verifier_tool_definitions",
    "verifier_tool_schema_sha256",
]


VERIFIER_TOOL_DEFINITIONS: Final[tuple[VerifierToolDefinition, ...]] = (
    get_verifier_tool_definitions()
)
