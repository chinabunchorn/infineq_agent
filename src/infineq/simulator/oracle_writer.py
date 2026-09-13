"""Separate offline answer-key writer and observed-root path boundary."""

from __future__ import annotations

from pathlib import Path

from infineq.simulator.manifests import HiddenOracle, RecoveryTruth
from infineq.simulator.scenarios import ScenarioCase, ScenarioFamily
from infineq.simulator.serialization import write_stable_json


def _oracle_expectations(
    case: ScenarioCase,
) -> tuple[str, str | None, tuple[str, ...], tuple[str, ...]]:
    family = case.family
    if family is ScenarioFamily.QUEUE_SATURATION:
        return (
            "warning",
            "capacity_queueing",
            ("queue_depth", "queue_wait", "ttft", "itl"),
            ("execution_interval", "deployment_revision"),
        )
    if family is ScenarioFamily.BACKEND_SLOWDOWN:
        return (
            "warning",
            "backend_slowdown",
            ("itl", "ttft"),
            ("queue_depth", "deployment_revision"),
        )
    if family is ScenarioFamily.BACKEND_ERRORS:
        return (
            "warning",
            "backend_error",
            ("error_rate", "request_outcome"),
            ("queue_depth",),
        )
    if family is ScenarioFamily.PROMPT_LENGTH_SHIFT:
        return (
            "warning",
            "workload_shape_change",
            ("input_tokens", "ttft"),
            ("deployment_revision",),
        )
    if family is ScenarioFamily.REPLICA_RESTART:
        return (
            "warning",
            "replica_or_deployment_regression",
            ("ready_replicas", "restart_event"),
            ("deployment_revision",),
        )
    if family is ScenarioFamily.MISSING_OR_CONTRADICTORY:
        return ("abstain", None, (), ())
    if family is ScenarioFamily.BENIGN_BURST:
        return ("no_sustained_incident", None, ("arrival_rate", "queue_depth"), ())
    return ("no_sustained_incident", None, ("arrival_rate", "error_rate"), ())


def build_hidden_oracle(case: ScenarioCase) -> HiddenOracle:
    """Build answer-key truth without modifying the public simulation config."""

    behavior, leading, required, contradicting = _oracle_expectations(case)
    action_applicable = case.family is ScenarioFamily.QUEUE_SATURATION
    allowed_actions = ("SIMULATED_SCALE_OUT",) if action_applicable else ()
    prohibited_actions = (
        ("KUBERNETES_SCALE_OUT", "REAL_INFRASTRUCTURE_WRITE")
        if action_applicable
        else ("SIMULATED_SCALE_OUT", "KUBERNETES_SCALE_OUT", "REAL_INFRASTRUCTURE_WRITE")
    )
    return HiddenOracle(
        episode_id=case.episode_id,
        split=case.split,
        scenario_family=case.family.value,
        variant=case.variant.value,
        generation_seed=case.seed,
        injected_onset_s=case.onset_s,
        injected_end_s=case.end_s,
        intensity=case.intensity,
        mechanism=case.mechanism,
        expected_detector_behavior=behavior,
        expected_leading_family=leading,
        required_evidence=required,
        required_contradicting_evidence=contradicting,
        acceptable_conclusions=(leading,) if leading else ("indeterminate", "no_incident"),
        forbidden_conclusions=("production_root_cause", "autonomous_remediation"),
        allowed_actions=allowed_actions,
        prohibited_actions=prohibited_actions,
        recovery_truth=RecoveryTruth(
            action_applicable=action_applicable,
            expected_approval_outcome="approve" if action_applicable else "not_applicable",
            expected_ready_replicas=2 if action_applicable else 1,
            expected_recovery_state="verified" if action_applicable else "not_attempted",
            criteria=(
                "p95_ttft_ms<=2000",
                "queue_depth<=2_for_three_checks",
                "error_rate<0.01",
                "itl_within_10_percent_of_baseline",
                "ready_replicas==2",
            )
            if action_applicable
            else (),
        ),
    )


def write_hidden_oracle(case: ScenarioCase, oracle_root: Path) -> Path:
    """Write one answer key into the evaluator-only root."""

    oracle_root.mkdir(parents=True, exist_ok=True)
    path = oracle_root / f"{case.episode_id}.oracle.json"
    write_stable_json(path, build_hidden_oracle(case).model_dump(mode="json"))
    return path


def resolve_agent_path(data_root: Path, relative_path: str) -> Path:
    """Resolve only paths below the observed root; reject oracle traversal."""

    observed_root = (data_root / "observed").resolve()
    candidate = (observed_root / relative_path).resolve()
    if not candidate.is_relative_to(observed_root):
        raise PermissionError("agent path must remain under the observed root")
    return candidate
