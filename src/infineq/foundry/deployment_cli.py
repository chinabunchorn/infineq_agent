#!/usr/bin/env python3
"""Create or retrieve the pinned Infineq Investigator prompt-agent version."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from azure.ai.projects import AIProjectClient
from azure.identity import DefaultAzureCredential
from dotenv import dotenv_values

from infineq.agents.prompt_manifest import PromptManifest, load_prompt_manifest
from infineq.config import Settings
from infineq.errors import ConfigurationError
from infineq.foundry.agent_registry import (
    INVESTIGATOR_AGENT_NAME,
    PromptAgentRegistry,
)
from infineq.foundry.deployment import deployment_manifest_path, write_deployment_manifest


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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=Path(__file__).resolve().parents[3])
    parser.add_argument(
        "--agent-version", help="retrieve this exact version instead of creating a new one"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    project_root = args.project_root.resolve()
    values = _environment(project_root)
    try:
        settings = Settings.from_mapping(values, project_root=project_root)
        foundry = settings.require_foundry()
        manifest = load_prompt_manifest(project_root=project_root)
        expected_version = _expected_version(project_root, args.agent_version, manifest)
        credential = DefaultAzureCredential(exclude_interactive_browser_credential=True)
        project = AIProjectClient(endpoint=foundry.endpoint, credential=credential)
        try:
            registry = PromptAgentRegistry(project.agents, project_root=project_root)
            record = registry.ensure_version(
                model_deployment_name=foundry.model_deployment_name,
                manifest=manifest,
                expected_version=expected_version,
            )
            path = write_deployment_manifest(project_root, record)
        finally:
            project.close()
    except (ConfigurationError, FileNotFoundError, OSError, RuntimeError, ValueError) as exc:
        print(
            f"BLOCKED: Foundry Investigator prerequisites or manifest validation failed: {exc}",
            file=sys.stderr,
        )
        return 2
    except Exception:
        print(
            "BLOCKED: Foundry Investigator registration failed; inspect Azure diagnostics "
            "separately.",
            file=sys.stderr,
        )
        return 3

    print(
        json.dumps(
            {
                "success": True,
                "agent_name": record.name,
                "agent_version": record.version,
                "prompt_version": record.prompt_version,
                "prompt_sha256": record.prompt_sha256,
                "model_deployment_name": record.model_deployment_name,
                "deployment_manifest": str(path.relative_to(project_root)),
                "previous_versions_deleted": False,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
