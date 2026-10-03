#!/usr/bin/env python3
"""Persist recorded or explicitly opted-in live development baseline results."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import cast

from pydantic import ValidationError

from infineq.detection.detector import Detector
from infineq.evaluation.baselines import (
    BaselineEpisode,
    BaselineRunRecord,
    baseline_run_directory,
    save_baseline_results,
    validate_matched_baseline_runs,
)
from infineq.evaluation.live_baselines import (
    CANONICAL_EPISODE_ID,
    LIVE_OPT_IN_ENV,
    build_live_runtime,
    run_all_development_live_modes,
    run_live_step,
    run_matched_live_modes,
    validate_live_artifacts,
)
from infineq.schemas.incident import IncidentPacketV1
from infineq.workflow.orchestrator import InvestigatorProtocol


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--project-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    parser.add_argument("--recorded-input", type=Path)
    parser.add_argument("--run-id", default="run-baseline-recorded")
    parser.add_argument(
        "--live",
        action="store_true",
        help="run exactly one canonical live baseline after explicit opt-in",
    )
    parser.add_argument(
        "--all-development-live",
        action="store_true",
        help="run all eight development episodes after explicit live opt-in",
    )
    parser.add_argument(
        "--diagnostic-path",
        type=Path,
        help="write a sanitized live boundary diagnostic only with both live opt-ins",
    )
    return parser


def _load_recorded_input(
    path: Path,
) -> tuple[tuple[BaselineEpisode, ...], tuple[BaselineRunRecord, ...]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("recorded baseline input is unreadable") from error
    if not isinstance(payload, dict):
        raise ValueError("recorded baseline input must be a JSON object")
    raw_episodes = payload.get("episodes")
    raw_runs = payload.get("runs")
    if not isinstance(raw_episodes, list) or not isinstance(raw_runs, list):
        raise ValueError("recorded baseline input requires episodes and runs lists")
    try:
        episodes = tuple(BaselineEpisode.model_validate(item) for item in raw_episodes)
        runs = tuple(BaselineRunRecord.model_validate(item) for item in raw_runs)
    except ValidationError as error:
        raise ValueError("recorded baseline input contains an invalid strict record") from error
    return episodes, runs


def run_live_baseline(
    project_root: Path,
    run_id: str,
    *,
    diagnostic_path: Path | None = None,
) -> dict[str, object]:
    """Run the one allowed live episode through retrieval-only agent boundaries."""

    if os.environ.get(LIVE_OPT_IN_ENV) != "1":
        raise ValueError(f"{LIVE_OPT_IN_ENV}=1 is required for live baselines")
    resolved_root = project_root.resolve()
    output_directory = baseline_run_directory(resolved_root, run_id)
    if output_directory.exists():
        raise ValueError("live run ID already exists; inspect it before choosing another ID")

    runtime = build_live_runtime(resolved_root, diagnostic_path=diagnostic_path)
    try:
        detector = run_live_step(
            "detector_construction",
            lambda: Detector(store=runtime.store),
            diagnostic_path=diagnostic_path,
        )

        def detect_canonical_packet() -> IncidentPacketV1:
            packet = detector.detect(CANONICAL_EPISODE_ID)
            if packet is None:
                raise ValueError("canonical development episode did not produce an incident packet")
            return cast(IncidentPacketV1, packet)

        packet = run_live_step(
            "detector_detection",
            detect_canonical_packet,
            diagnostic_path=diagnostic_path,
        )
        execution = run_live_step(
            "matched_live_modes",
            lambda: run_matched_live_modes(
                project_root=resolved_root,
                run_id=run_id,
                packet=packet,
                investigator=cast(InvestigatorProtocol, runtime.investigator),
                verifier=runtime.verifier,
                policy=runtime.policy,
                pins=runtime.pins,
            ),
            diagnostic_path=diagnostic_path,
        )
        run_live_step(
            "artifact_validation",
            lambda: validate_live_artifacts(
                execution.output_directory,
                run_id,
                require_pins=True,
                project_root=resolved_root,
            ),
            diagnostic_path=diagnostic_path,
        )

        def build_summary() -> dict[str, object]:
            modes = {
                run.mode.value: {
                    "terminal_status": run.terminal_status.value,
                    "action_executed": run.action_executed,
                    "action_card_eligible": run.action_card_eligible,
                    "forbidden_tool_calls": run.forbidden_tool_calls,
                    "unseen_evidence": run.unseen_evidence,
                    "successful_tool_calls": run.successful_tool_calls,
                    "latency_ms": run.latency_ms,
                    "input_tokens": run.input_tokens,
                    "output_tokens": run.output_tokens,
                    "total_tokens": run.total_tokens,
                }
                for run in execution.runs
            }
            return {
                "run_id": execution.run_id,
                "episode_id": execution.episode.episode_id,
                "output_directory": str(execution.output_directory.relative_to(resolved_root)),
                "live_execution": True,
                "modes": modes,
                "evaluation": execution.evaluation.model_dump(mode="json"),
                "agent_pins": (
                    execution.pins.model_dump(mode="json") if execution.pins is not None else None
                ),
            }

        return run_live_step(
            "summary_construction",
            build_summary,
            diagnostic_path=diagnostic_path,
        )
    finally:
        runtime.close()


_DEVELOPMENT_LIVE_RUN_ID_PATTERN = r"^run-phase5-development-live-v[0-9]+$"
_AGGREGATE_MODES = ("static", "investigator", "investigator_verifier")
_AGGREGATE_MODE_FIELDS = (
    "record_count",
    "pass_count",
    "unsupported_claims",
    "policy_errors",
    "unseen_evidence",
    "forbidden_tool_calls",
    "successful_tool_calls",
    "redundant_tool_calls",
    "latency_ms_total",
    "terminal_status_counts",
    "input_tokens_total",
    "input_tokens_known_count",
    "output_tokens_total",
    "output_tokens_known_count",
    "total_tokens_total",
    "total_tokens_known_count",
)


def _redacted_aggregate_status(value: object) -> dict[str, object]:
    """Allow-list aggregate counters; never print episode/provider payloads."""

    if not isinstance(value, Mapping):
        raise ValueError("all-development run did not return aggregate metrics")
    if value.get("episode_count") != 8 or value.get("mode_record_count") != 24:
        raise ValueError("all-development run did not produce the exact eight-by-three matrix")

    def mode_map(field: str) -> dict[str, object]:
        raw = value.get(field)
        if not isinstance(raw, Mapping):
            raise ValueError(f"aggregate metric {field} is invalid")
        return {mode: raw.get(mode, 0) for mode in _AGGREGATE_MODES}

    safe_modes: dict[str, dict[str, object]] = {}
    raw_modes = value.get("modes")
    if isinstance(raw_modes, Mapping):
        for mode in _AGGREGATE_MODES:
            raw_mode = raw_modes.get(mode)
            if not isinstance(raw_mode, Mapping):
                raise ValueError("aggregate mode metrics are invalid")
            safe_modes[mode] = {
                field: raw_mode[field] for field in _AGGREGATE_MODE_FIELDS if field in raw_mode
            }

    return {
        "episode_count": 8,
        "mode_record_count": 24,
        "modes": safe_modes,
        "pass_counts": mode_map("pass_counts"),
        "unsupported_claims_by_mode": mode_map("unsupported_claims_by_mode"),
        "policy_errors_by_mode": mode_map("policy_errors_by_mode"),
        "two_agent_justification_passed": value.get("two_agent_justification_passed") is True,
        "action_execution_count": value.get("action_execution_count", 0),
    }


def run_all_development_live_baseline(
    project_root: Path,
    run_id: str,
    *,
    diagnostic_path: Path | None = None,
) -> dict[str, object]:
    """Run the exact all-development matrix through one retrieval-only runtime."""

    if os.environ.get(LIVE_OPT_IN_ENV) != "1":
        raise ValueError(f"{LIVE_OPT_IN_ENV}=1 is required for live baselines")
    if re.fullmatch(_DEVELOPMENT_LIVE_RUN_ID_PATTERN, run_id) is None:
        raise ValueError("all-development live runs require run-phase5-development-live-v<digits>")
    resolved_root = project_root.resolve()
    output_directory = baseline_run_directory(resolved_root, run_id)
    if output_directory.exists():
        raise ValueError("live run ID already exists; inspect it before choosing another ID")

    runtime = build_live_runtime(resolved_root, diagnostic_path=diagnostic_path)
    try:
        detector = run_live_step(
            "detector_construction",
            lambda: Detector(store=runtime.store),
            diagnostic_path=diagnostic_path,
        )
        execution = run_live_step(
            "all_development_live_modes",
            lambda: run_all_development_live_modes(
                project_root=resolved_root,
                run_id=run_id,
                detector=detector,
                investigator=cast(InvestigatorProtocol, runtime.investigator),
                verifier=runtime.verifier,
                policy=runtime.policy,
                oracle_loader=None,
                pins=runtime.pins,
            ),
            diagnostic_path=diagnostic_path,
        )
        run_live_step(
            "artifact_validation",
            lambda: validate_live_artifacts(
                execution.output_directory,
                run_id,
                require_evaluation=True,
                require_pins=True,
                project_root=resolved_root,
            ),
            diagnostic_path=diagnostic_path,
        )
        aggregate = _redacted_aggregate_status(execution.aggregate_metrics)
        relative_output = execution.output_directory.resolve().relative_to(resolved_root)
        return {
            "live_execution": True,
            "scope": "all_development",
            "run_id": run_id,
            "output_directory": relative_output.as_posix(),
            "episode_count": aggregate["episode_count"],
            "mode_record_count": aggregate["mode_record_count"],
            "pass_counts": aggregate["pass_counts"],
            "unsupported_claims_by_mode": aggregate["unsupported_claims_by_mode"],
            "policy_errors_by_mode": aggregate["policy_errors_by_mode"],
            "two_agent_justification_passed": aggregate["two_agent_justification_passed"],
            "aggregate_metrics": aggregate,
        }
    finally:
        runtime.close()


def _redacted_cli_payload(value: object, *, run_id: str) -> dict[str, object]:
    """Build the CLI response from aggregate fields only."""

    if not isinstance(value, Mapping):
        raise ValueError("all-development live runner returned an invalid status")
    aggregate = _redacted_aggregate_status(value.get("aggregate_metrics"))
    output_directory = value.get("output_directory")
    if (
        not isinstance(output_directory, str)
        or not output_directory
        or Path(output_directory).is_absolute()
        or ".." in Path(output_directory).parts
    ):
        raise ValueError("all-development live runner returned an unsafe output path")
    return {
        "live_execution": True,
        "scope": "all_development",
        "run_id": run_id,
        "output_directory": output_directory,
        "episode_count": aggregate["episode_count"],
        "mode_record_count": aggregate["mode_record_count"],
        "pass_counts": aggregate["pass_counts"],
        "unsupported_claims_by_mode": aggregate["unsupported_claims_by_mode"],
        "policy_errors_by_mode": aggregate["policy_errors_by_mode"],
        "two_agent_justification_passed": aggregate["two_agent_justification_passed"],
        "aggregate_metrics": aggregate,
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    project_root = args.project_root.resolve()
    if args.all_development_live:
        if args.live or args.recorded_input is not None:
            print(
                "BLOCKED: --all-development-live is incompatible with canonical or recorded modes.",
                file=sys.stderr,
            )
            return 2
        try:
            if args.diagnostic_path is None:
                payload = run_all_development_live_baseline(project_root, args.run_id)
            else:
                payload = run_all_development_live_baseline(
                    project_root,
                    args.run_id,
                    diagnostic_path=args.diagnostic_path,
                )
        except Exception as error:
            print(
                "BLOCKED: all-development live baseline failed at a bounded "
                f"validation/provider boundary ({type(error).__name__}); "
                "inspect preserved artifacts if present.",
                file=sys.stderr,
            )
            return 2
        print(json.dumps(_redacted_cli_payload(payload, run_id=args.run_id), sort_keys=True))
        return 0
    if args.live:
        if args.recorded_input is not None:
            print("BLOCKED: --live is incompatible with --recorded-input.", file=sys.stderr)
            return 2
        try:
            if args.diagnostic_path is None:
                payload = run_live_baseline(project_root, args.run_id)
            else:
                payload = run_live_baseline(
                    project_root,
                    args.run_id,
                    diagnostic_path=args.diagnostic_path,
                )
        except Exception as error:
            print(
                f"BLOCKED: live baseline failed at a bounded validation/provider boundary "
                f"({type(error).__name__}); inspect preserved artifacts if present.",
                file=sys.stderr,
            )
            return 2
        print(json.dumps(payload, sort_keys=True))
        return 0
    if args.recorded_input is None:
        print("BLOCKED: provide --recorded-input or use explicit --live opt-in.", file=sys.stderr)
        return 2
    try:
        episodes, runs = _load_recorded_input(args.recorded_input)
        validate_matched_baseline_runs(episodes, runs)
        output_directory = save_baseline_results(project_root, args.run_id, runs)
    except (OSError, ValueError) as error:
        print(f"BLOCKED: recorded baseline input was rejected: {error}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "run_id": args.run_id,
                "episode_ids": sorted({episode.episode_id for episode in episodes}),
                "output_directory": str(output_directory),
                "live_execution": False,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
