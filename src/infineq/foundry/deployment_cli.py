#!/usr/bin/env python3
"""Create or retrieve a pinned Infineq prompt-agent version."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

from azure.ai.projects import AIProjectClient
from azure.identity import DefaultAzureCredential
from dotenv import dotenv_values

from infineq.agents.prompt_manifest import (
    PromptManifest,
    load_prompt_manifest,
    load_verifier_prompt_manifest,
)
from infineq.config import Settings
from infineq.errors import ConfigurationError
from infineq.foundry.agent_registry import (
    INVESTIGATOR_AGENT_NAME,
    VERIFIER_AGENT_NAME,
    PromptAgentRegistry,
)
from infineq.foundry.deployment import (
    deployment_manifest_path,
    load_verifier_deployment_manifest,
    verifier_deployment_manifest_path,
    write_deployment_manifest,
    write_verifier_deployment_manifest,
)
from infineq.foundry.verifier_tools import VERIFIER_TOOL_SCHEMA_SHA256


def _environment(project_root: Path) -> dict[str, str]:
    """Read ignored local settings and process settings without printing values."""

    values: dict[str, str] = {
        key: value
        for key, value in dotenv_values(project_root / ".env").items()
        if isinstance(value, str)
    }
    values.update({key: value for key, value in os.environ.items() if isinstance(value, str)})
    return values


def _expected_version(
    project_root: Path,
    requested: str | None,
    manifest: PromptManifest | None = None,
) -> str | None:
    if requested:
        return requested
    path = deployment_manifest_path(project_root)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("local deployment manifest is unreadable") from exc
    if not isinstance(payload, dict) or payload.get("agent_name") != INVESTIGATOR_AGENT_NAME:
        raise RuntimeError("local deployment manifest does not identify the Investigator")
    version = payload.get("agent_version")
    if not isinstance(version, str) or not version:
        raise RuntimeError("local deployment manifest has no exact agent version")
    if manifest is not None and (
        payload.get("prompt_version") != manifest.version
        or payload.get("prompt_sha256") != manifest.prompt_sha256
    ):
        return None
    return version


def _expected_verifier_version(
    project_root: Path,
    requested: str | None,
    manifest: PromptManifest,
    model_deployment_name: str,
    *,
    allow_new_version: bool = False,
) -> tuple[str | None, datetime | None]:
    """Resolve the exact Verifier pin, or explicitly allow a safe prompt upgrade."""

    if requested is not None and allow_new_version:
        raise RuntimeError("cannot combine exact retrieval with a new Verifier version")
    path = verifier_deployment_manifest_path(project_root)
    if not path.exists():
        if requested is not None:
            raise RuntimeError("verifier manifest is required for exact retrieval")
        return None, None
    local = load_verifier_deployment_manifest(project_root)
    expected = {
        "agent_name": VERIFIER_AGENT_NAME,
        "model_deployment_name": model_deployment_name,
        "tool_schema_sha256": VERIFIER_TOOL_SCHEMA_SHA256,
    }
    for key, value in expected.items():
        if getattr(local, key) != value:
            raise RuntimeError(f"verifier deployment manifest mismatch: {key}")
    if local.prompt_version != manifest.version or local.prompt_sha256 != manifest.prompt_sha256:
        if allow_new_version:
            if local.prompt_version == manifest.version:
                raise RuntimeError("verifier prompt version must change when its hash changes")
            return None, None
        raise RuntimeError("verifier deployment manifest mismatch: prompt_version or prompt_sha256")
    if allow_new_version:
        raise RuntimeError("verifier prompt already matches; no new version is needed")
    if requested is not None and local.agent_version != requested:
        raise RuntimeError("requested verifier version does not match the local manifest")
    return local.agent_version, local.created_at


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--agent",
        choices=("investigator", "verifier"),
        default="investigator",
        help="agent definition to create or retrieve",
    )
    parser.add_argument("--project-root", type=Path, default=Path(__file__).resolve().parents[3])
    parser.add_argument(
        "--agent-version", help="retrieve this exact version instead of creating a new one"
    )
    parser.add_argument(
        "--create-new-verifier-version",
        action="store_true",
        help="explicitly create a new Verifier version when the pinned prompt changed",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    project_root = args.project_root.resolve()
    values = _environment(project_root)
    is_verifier = args.agent == "verifier"
    agent_label = "Verifier" if is_verifier else "Investigator"
    try:
        if args.create_new_verifier_version and not is_verifier:
            raise RuntimeError("new Verifier version flag requires --agent verifier")
        settings = Settings.from_mapping(values, project_root=project_root)
        foundry = settings.require_foundry()
        if is_verifier:
            manifest = load_verifier_prompt_manifest(project_root=project_root)
            expected_version, creation_timestamp = _expected_verifier_version(
                project_root,
                args.agent_version,
                manifest,
                foundry.model_deployment_name,
                allow_new_version=args.create_new_verifier_version,
            )
        else:
            manifest = load_prompt_manifest(project_root=project_root)
            expected_version = _expected_version(project_root, args.agent_version, manifest)
            creation_timestamp = None

        credential = DefaultAzureCredential(exclude_interactive_browser_credential=True)
        project = AIProjectClient(endpoint=foundry.endpoint, credential=credential)
        try:
            registry = PromptAgentRegistry(project.agents, project_root=project_root)
            if is_verifier:
                verifier_record = registry.ensure_verifier_version(
                    model_deployment_name=foundry.model_deployment_name,
                    manifest=manifest,
                    expected_version=expected_version,
                    creation_timestamp=creation_timestamp,
                )
                path = write_verifier_deployment_manifest(project_root, verifier_record)
                payload = {
                    "success": True,
                    "agent_name": verifier_record.name,
                    "agent_version": verifier_record.version,
                    "prompt_version": verifier_record.prompt_version,
                    "prompt_sha256": verifier_record.prompt_sha256,
                    "model_deployment_name": verifier_record.model_deployment_name,
                    "tool_schema_sha256": verifier_record.tool_schema_sha256,
                    "created_at": verifier_record.to_manifest()["created_at"],
                    "deployment_manifest": str(path.relative_to(project_root)),
                    "previous_versions_deleted": False,
                }
            else:
                investigator_record = registry.ensure_version(
                    model_deployment_name=foundry.model_deployment_name,
                    manifest=manifest,
                    expected_version=expected_version,
                )
                path = write_deployment_manifest(project_root, investigator_record)
                payload = {
                    "success": True,
                    "agent_name": investigator_record.name,
                    "agent_version": investigator_record.version,
                    "prompt_version": investigator_record.prompt_version,
                    "prompt_sha256": investigator_record.prompt_sha256,
                    "model_deployment_name": investigator_record.model_deployment_name,
                    "deployment_manifest": str(path.relative_to(project_root)),
                    "previous_versions_deleted": False,
                }
        finally:
            project.close()
    except (ConfigurationError, FileNotFoundError, OSError, RuntimeError, ValueError) as exc:
        print(
            f"BLOCKED: Foundry {agent_label} prerequisites or manifest validation failed: {exc}",
            file=sys.stderr,
        )
        return 2
    except Exception:
        print(
            f"BLOCKED: Foundry {agent_label} registration failed; inspect Azure diagnostics "
            "separately.",
            file=sys.stderr,
        )
        return 3

    print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
