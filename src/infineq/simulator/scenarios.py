"""The frozen eight-template synthetic episode catalog."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from infineq.simulator.emitters import ObservedChange, TelemetryProfile
from infineq.simulator.engine import (
    NumericWindow,
    RatePhase,
    ReplicaChange,
    SimulationConfig,
)
from infineq.simulator.manifests import (
    ArtifactChecksum,
    EpisodeSplit,
    ObservedManifest,
    PhaseManifest,
    ServiceManifest,
    WorkloadManifest,
)


class ScenarioFamily(StrEnum):
    """Internal template names; never written to observed artifacts."""

    HEALTHY = "healthy_steady_state"
    BENIGN_BURST = "benign_traffic_burst"
    QUEUE_SATURATION = "queue_saturation"
    BACKEND_SLOWDOWN = "backend_slowdown"
    BACKEND_ERRORS = "backend_errors_timeouts"
    PROMPT_LENGTH_SHIFT = "prompt_length_shift"
    REPLICA_RESTART = "replica_restart_readiness_loss"
    MISSING_OR_CONTRADICTORY = "missing_or_contradictory_telemetry"


class ScenarioVariant(StrEnum):
    """Controlled variant identity kept outside observed data."""

    A = "A"
    B = "B"
    C = "C"


@dataclass(frozen=True, slots=True)
class ScenarioCase:
    """One catalog case with private generator metadata and public replay config."""

    episode_id: str
    family: ScenarioFamily
    variant: ScenarioVariant
    split: EpisodeSplit
    seed: int
    onset_s: float
    end_s: float
    intensity: float
    mechanism: str
    simulation_config: SimulationConfig
    telemetry: TelemetryProfile
    baseline_rate: float
    degradation_rate: float
    recovery_rate: float

    def observed_manifest(
        self, *, artifact_checksums: tuple[ArtifactChecksum, ...]
    ) -> ObservedManifest:
        """Build the agent-readable manifest without private scenario metadata."""

        return ObservedManifest(
            episode_id=self.episode_id,
            simulator_version="sim-v1",
            design_version="1.0",
            split=self.split,
            seed=self.seed,
            generated_at=datetime(2026, 1, 1, tzinfo=UTC),
            phases=(
                PhaseManifest(name="baseline", start_s=0.0, end_s=60.0),
                PhaseManifest(name="degradation", start_s=60.0, end_s=120.0),
                PhaseManifest(name="recovery", start_s=120.0, end_s=180.0),
            ),
            workload=WorkloadManifest(
                baseline_arrival_rate=self.baseline_rate,
                degradation_arrival_rate=self.degradation_rate,
                recovery_arrival_rate=self.recovery_rate,
                input_tokens=self.simulation_config.input_tokens,
                output_tokens=self.simulation_config.output_tokens,
                jitter_fraction=self.simulation_config.jitter_fraction,
            ),
            service=ServiceManifest(
                initial_replicas=self.simulation_config.initial_replicas,
                slots_per_replica=self.simulation_config.slots_per_replica,
                base_itl_s=self.simulation_config.base_itl_s,
                prefill_base_s=self.simulation_config.prefill_base_s,
                prefill_per_input_s=self.simulation_config.prefill_per_input_s,
                ttft_slo_ms=2_000.0,
            ),
            artifact_checksums=artifact_checksums,
        )


_EPISODE_IDS = (
    "ep-a91e7c",
    "ep-f02b4d",
    "ep-61d8aa",
    "ep-c43f91",
    "ep-0e7ab3",
    "ep-d8c214",
    "ep-4b6fa0",
    "ep-e35192",
    "ep-7c0d48",
    "ep-b26e5f",
    "ep-93a1d7",
    "ep-5e84bc",
    "ep-18f6d2",
    "ep-c9a730",
    "ep-3d5b8e",
    "ep-f7c142",
    "ep-82e9ad",
    "ep-6a4c31",
    "ep-b8d052",
    "ep-274ef9",
    "ep-d13c86",
    "ep-49ab72",
    "ep-e7065a",
    "ep-0c3d9f",
)

_VARIANT_PARAMS: dict[ScenarioVariant, tuple[float, float, float]] = {
    ScenarioVariant.A: (60.0, 120.0, 1.0),
    ScenarioVariant.B: (66.0, 126.0, 1.1),
    ScenarioVariant.C: (54.0, 114.0, 0.9),
}


def _base_config(
    *,
    seed: int,
    baseline_rate: float = 1.2,
    degradation_rate: float = 1.2,
    recovery_rate: float = 1.2,
    itl_profile: tuple[NumericWindow, ...] = (),
    input_multiplier_profile: tuple[NumericWindow, ...] = (),
    error_rate_profile: tuple[NumericWindow, ...] = (),
    timeout_rate_profile: tuple[NumericWindow, ...] = (),
    replica_changes: tuple[ReplicaChange, ...] = (),
    arrival_window: tuple[float, float] | None = None,
) -> SimulationConfig:
    if arrival_window is None:
        workload = (
            RatePhase(0.0, 60.0, baseline_rate),
            RatePhase(60.0, 120.0, degradation_rate),
            RatePhase(120.0, 180.0, recovery_rate),
        )
    else:
        onset_s, end_s = arrival_window
        workload = (
            RatePhase(0.0, onset_s, baseline_rate),
            RatePhase(onset_s, end_s, degradation_rate),
            RatePhase(end_s, 180.0, recovery_rate),
        )
    return SimulationConfig(
        duration_s=180.0,
        workload=workload,
        seed=seed,
        replica_changes=replica_changes,
        itl_profile=itl_profile,
        input_multiplier_profile=input_multiplier_profile,
        error_rate_profile=error_rate_profile,
        timeout_rate_profile=timeout_rate_profile,
    )


def _case_for(
    family: ScenarioFamily,
    variant: ScenarioVariant,
    episode_id: str,
    index: int,
) -> ScenarioCase:
    onset_base, end_base, intensity_base = _VARIANT_PARAMS[variant]
    seed = 4_001 + index * 17
    onset = onset_base
    end = end_base
    intensity = intensity_base
    telemetry = TelemetryProfile()
    baseline_rate = recovery_rate = 1.2
    degradation_rate = 1.2
    itl_profile: tuple[NumericWindow, ...] = ()
    input_profile: tuple[NumericWindow, ...] = ()
    error_profile: tuple[NumericWindow, ...] = ()
    timeout_profile: tuple[NumericWindow, ...] = ()
    changes: tuple[ReplicaChange, ...] = ()
    arrival_window: tuple[float, float] | None = None
    mechanism = "stable_replay"

    if family is ScenarioFamily.BENIGN_BURST:
        degradation_rate = 1.65 + (intensity - 1.0) * 0.15
        arrival_window = (onset, min(end, 120.0))
        mechanism = "bounded_arrival_burst"
    elif family is ScenarioFamily.QUEUE_SATURATION:
        degradation_rate = 2.6 + (intensity - 1.0) * 0.2
        arrival_window = (onset, min(end, 120.0))
        mechanism = "arrival_rate_exceeds_nominal_capacity"
    elif family is ScenarioFamily.BACKEND_SLOWDOWN:
        multiplier = 1.45 + (intensity - 1.0) * 0.10
        itl_profile = (NumericWindow(onset, min(end, 120.0), multiplier),)
        mechanism = "execution_interval_increases"
    elif family is ScenarioFamily.BACKEND_ERRORS:
        error_profile = (NumericWindow(onset, min(end, 120.0), 0.08 + (intensity - 1.0) * 0.02),)
        timeout_profile = (NumericWindow(onset, min(end, 120.0), 0.01),)
        mechanism = "normalized_backend_outcomes_degrade"
    elif family is ScenarioFamily.PROMPT_LENGTH_SHIFT:
        input_profile = (NumericWindow(onset, min(end, 120.0), 1.75 + (intensity - 1.0) * 0.25),)
        mechanism = "input_token_distribution_shifts"
    elif family is ScenarioFamily.REPLICA_RESTART:
        changes = (
            ReplicaChange(onset, replicas=1, ready_replicas=0),
            ReplicaChange(min(end, 120.0), replicas=1, ready_replicas=1),
        )
        telemetry = TelemetryProfile(
            changes=(
                ObservedChange(onset, ("ready_replicas",)),
                ObservedChange(min(end, 120.0), ("ready_replicas",)),
            )
        )
        mechanism = "replica_readiness_changes"
    elif family is ScenarioFamily.MISSING_OR_CONTRADICTORY:
        if variant is ScenarioVariant.A:
            telemetry = TelemetryProfile(omitted_signals=frozenset({"itl"}))
        elif variant is ScenarioVariant.B:
            telemetry = TelemetryProfile(stale_signals=frozenset({"ttft"}))
        else:
            telemetry = TelemetryProfile(contradictory_replicas=True)
        mechanism = "required_telemetry_is_not_decision_ready"

    return ScenarioCase(
        episode_id=episode_id,
        family=family,
        variant=variant,
        split=EpisodeSplit.DEVELOPMENT if variant is ScenarioVariant.A else EpisodeSplit.HELD_OUT,
        seed=seed,
        onset_s=onset,
        end_s=end,
        intensity=intensity,
        mechanism=mechanism,
        simulation_config=_base_config(
            seed=seed,
            baseline_rate=baseline_rate,
            degradation_rate=degradation_rate,
            recovery_rate=recovery_rate,
            itl_profile=itl_profile,
            input_multiplier_profile=input_profile,
            error_rate_profile=error_profile,
            timeout_rate_profile=timeout_profile,
            replica_changes=changes,
            arrival_window=arrival_window,
        ),
        telemetry=telemetry,
        baseline_rate=baseline_rate,
        degradation_rate=degradation_rate,
        recovery_rate=recovery_rate,
    )


def scenario_catalog() -> tuple[ScenarioCase, ...]:
    """Return the fixed 24-case catalog in a deterministic, shuffled ID order."""

    families = tuple(ScenarioFamily)
    cases: list[ScenarioCase] = []
    for index, (family, variant) in enumerate(
        pair for variant in ScenarioVariant for pair in ((family, variant) for family in families)
    ):
        cases.append(_case_for(family, variant, _EPISODE_IDS[index], index))
    return tuple(cases)


def scenario_by_episode_id(episode_id: str) -> ScenarioCase:
    """Resolve one opaque episode ID from the fixed catalog."""

    for case in scenario_catalog():
        if case.episode_id == episode_id:
            return case
    raise KeyError("unknown episode")
