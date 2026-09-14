#!/usr/bin/env python3
"""Run the read-only Investigator against development-visible episodes only."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Collection
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any

from azure.ai.projects import AIProjectClient
from azure.identity import DefaultAzureCredential
from dotenv import dotenv_values

from infineq.agents.investigator import FROZEN_INCIDENT_FAMILIES, Investigator
from infineq.agents.prompt_manifest import load_prompt_manifest
from infineq.config import Settings
from infineq.errors import ConfigurationError
from infineq.evidence.store import EvidenceStore
from infineq.evidence.tools import InvestigatorTools
from infineq.foundry.agent_registry import INVESTIGATOR_AGENT_NAME, PromptAgentRegistry
from infineq.foundry.client import AzureFoundryResponsesClient
from infineq.foundry.deployment import (
    RunManifest,
    deployment_manifest_path,
    run_manifest_path,
    write_run_manifest,
)
from infineq.schemas.investigation import Disposition, InvestigationResultV1
from infineq.security.redaction import redact_mapping, redact_text
from infineq.simulator.scenarios import scenario_catalog


def development_episode_ids() -> tuple[str, ...]:
    """Return only the eight development-visible scenario IDs; never load oracles."""

    return tuple(case.episode_id for case in scenario_catalog() if case.variant.value == "A")


def _run_status(result: InvestigationResultV1) -> str:
    return (
        "analysis_incomplete"
        if result.disposition is Disposition.ANALYSIS_INCOMPLETE
        else "complete"
    )


def unseen_evidence_count(final_output: str, returned_evidence_ids: Collection[str]) -> int:
    """Count cited IDs absent from this run's returned evidence, without oracle access."""

    try:
        payload = json.loads(final_output)
    except (TypeError, ValueError, json.JSONDecodeError):
        return 0
    if not isinstance(payload, dict):
        return 0
    cited = payload.get("cited_evidence_ids")
    if not isinstance(cited, list):
        return 0
    returned = set(returned_evidence_ids)
    return sum(isinstance(item, str) and item not in returned for item in cited)


def write_episode_artifacts(
    project_root: Path,
    *,
    run_id: str,
    episode_id: str,
    result: InvestigationResultV1,
    final_output: str,
    trace_records: tuple[dict[str, object], ...],
    successful_tool_calls: int,
    forbidden_tool_calls: int,
    latency_ms: float | None,
    input_tokens: int | None,
    output_tokens: int | None,
    agent_name: str,
    agent_version: str,
    prompt_version: str,
    prompt_sha256: str,
    total_tokens: int | None = None,
    unseen_evidence_count: int = 0,
) -> RunManifest:
    """Write raw/redacted output and safe traces below the ignored run directory."""

    run_root = run_manifest_path(project_root, run_id).parent
    episode_root = run_root / episode_id
    episode_root.mkdir(parents=True, exist_ok=True)
    raw_relative = f"{episode_id}/raw_output.json"
    redacted_relative = f"{episode_id}/redacted_output.json"
    trace_relative = f"{episode_id}/trace.json"
    (episode_root / "raw_output.json").write_text(redact_text(final_output), encoding="utf-8")
    (episode_root / "redacted_output.json").write_text(
        result.model_dump_json(indent=2),
        encoding="utf-8",
    )
    (episode_root / "trace.json").write_text(
        json.dumps([redact_mapping(record) for record in trace_records], sort_keys=True, indent=2)
        + "\n",
        encoding="utf-8",
    )
    manifest = RunManifest(
        run_id=run_id,
        agent_name=agent_name,
        agent_version=agent_version,
        prompt_version=prompt_version,
        prompt_sha256=prompt_sha256,
        episode_ids=(episode_id,),
        successful_tool_calls=successful_tool_calls,
        forbidden_tool_calls=forbidden_tool_calls,
        status=_run_status(result),
        response_ids=tuple(
            str(record["response_id"])
            for record in trace_records
            if isinstance(record.get("response_id"), str)
        ),
        latency_ms=latency_ms,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        unseen_evidence_count=unseen_evidence_count,
        raw_output_path=raw_relative,
        redacted_output_path=redacted_relative,
        trace_path=trace_relative,
    )
    write_run_manifest(project_root, manifest)
    return manifest


def _environment(project_root: Path) -> dict[str, str]:
    values: dict[str, str] = {
        key: value
        for key, value in dotenv_values(project_root / ".env").items()
        if isinstance(value, str)
    }
    values.update({key: value for key, value in os.environ.items() if isinstance(value, str)})
    return values


def _deployed_version(project_root: Path) -> str:
    path = deployment_manifest_path(project_root)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("exact local Investigator deployment manifest is required") from exc
    if not isinstance(payload, dict) or payload.get("agent_name") != INVESTIGATOR_AGENT_NAME:
        raise RuntimeError("local deployment manifest is not for the Investigator")
    version = payload.get("agent_version")
    if not isinstance(version, str) or not version:
        raise RuntimeError("local deployment manifest has no exact agent version")
    return version


def _no_incident_result(episode_id: str) -> InvestigationResultV1:
    families = ", ".join(FROZEN_INCIDENT_FAMILIES[:-1]) + f", and {FROZEN_INCIDENT_FAMILIES[-1]}"
    return InvestigationResultV1(
        investigation_id=f"investigation-{episode_id}",
        incident_id=episode_id,
        completed_at=datetime.now(UTC),
        disposition=Disposition.NO_INCIDENT,
        summary=(
            "The deterministic detector produced no incident packet; "
            f"{families} were not diagnosed by the Investigator."
        ),
        hypotheses=(),
        cited_evidence_ids=(),
        limitations=(
            "No IncidentPacketV1 was available, so no agent call was made; "
            f"the comparison set was {families}.",
        ),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=Path(__file__).resolve().parents[3])
    parser.add_argument("--episode-id", action="append", dest="episode_ids")
    parser.add_argument("--run-id-prefix", default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    project_root = args.project_root.resolve()
    episode_ids = tuple(args.episode_ids or development_episode_ids())
    allowed = set(development_episode_ids())
    if not episode_ids or not set(episode_ids).issubset(allowed):
        print("BLOCKED: only development-visible episode IDs are allowed.", file=sys.stderr)
        return 2
    project: Any = None
    records: list[dict[str, object]] = []
    try:
        values = _environment(project_root)
        settings = Settings.from_mapping(values, project_root=project_root)
        foundry = settings.require_foundry()
        prompt_manifest = load_prompt_manifest(project_root=project_root)
        agent_version = _deployed_version(project_root)
        credential = DefaultAzureCredential(exclude_interactive_browser_credential=True)
        project = AIProjectClient(endpoint=foundry.endpoint, credential=credential)
        registry = PromptAgentRegistry(project.agents, project_root=project_root)
        record = registry.ensure_version(
            model_deployment_name=foundry.model_deployment_name,
            manifest=prompt_manifest,
            expected_version=agent_version,
        )
        responses = AzureFoundryResponsesClient(
            project,
            agent_name=record.name,
            agent_version=record.version,
        )
        store = EvidenceStore(project_root=project_root)
        detector_tools = InvestigatorTools(store=store)
        detector = store  # keep the deterministic store as the only episode data boundary
        from infineq.detection.detector import Detector

        episode_detector = Detector(store=detector)
        for episode_id in episode_ids:
            detection = episode_detector.run(episode_id)
            trace_records: tuple[dict[str, object], ...]
            usage: tuple[int | None, int | None, int | None]
            if detection.packet is None:
                result = _no_incident_result(episode_id)
                trace_records = ({"status": "not_invoked_no_incident"},)
                successful = 0
                usage = (None, None, None)
                returned_evidence_ids: frozenset[str] = frozenset()
                forbidden = 0
                latency_ms = 0.0
                final_output = result.model_dump_json()
            else:
                investigator = Investigator(
                    client=responses,
                    tools=detector_tools,
                    loop_config=None,
                    project_root=project_root,
                )
                started = perf_counter()
                result = investigator.investigate(detection.packet)
                latency_ms = round((perf_counter() - started) * 1_000, 3)
                run = investigator.last_run
                trace_records = tuple(
                    {
                        "response_id": trace.response_id,
                        "call_id": trace.call_id,
                        "tool_name": trace.name,
                        "status": trace.status,
                        "retry_count": trace.retry_count,
                        "returned_evidence_ids": trace.returned_evidence_ids,
                    }
                    for trace in run.traces
                )
                successful = run.successful_tool_calls
                usage = run.usage or (None, None, None)
                returned_evidence_ids = run.returned_evidence_ids
                forbidden = run.forbidden_tool_calls
                final_output = run.final_output_text
            unseen = unseen_evidence_count(final_output, returned_evidence_ids)
            run_id = args.run_id_prefix or f"run-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}"
            run_id = f"{run_id}-{episode_id}"
            manifest = write_episode_artifacts(
                project_root,
                run_id=run_id,
                episode_id=episode_id,
                result=result,
                final_output=final_output,
                trace_records=trace_records,
                successful_tool_calls=successful,
                forbidden_tool_calls=forbidden,
                latency_ms=latency_ms,
                input_tokens=usage[0],
                output_tokens=usage[1],
                total_tokens=usage[2],
                unseen_evidence_count=unseen,
                agent_name=record.name,
                agent_version=record.version,
                prompt_version=record.prompt_version,
                prompt_sha256=record.prompt_sha256,
            )
            records.append(
                {
                    "episode_id": episode_id,
                    "run_id": manifest.run_id,
                    "disposition": result.disposition.value,
                    "successful_tool_calls": manifest.successful_tool_calls,
                    "status": manifest.status,
                }
            )
    except (ConfigurationError, FileNotFoundError, OSError, RuntimeError, ValueError) as exc:
        print(f"BLOCKED: development Investigator prerequisites failed: {exc}", file=sys.stderr)
        return 2
    except Exception:
        print(
            "BLOCKED: development Investigator run failed; no result was fabricated.",
            file=sys.stderr,
        )
        return 3
    finally:
        if project is not None:
            project.close()
    print(json.dumps({"agent_name": INVESTIGATOR_AGENT_NAME, "episodes": records}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
