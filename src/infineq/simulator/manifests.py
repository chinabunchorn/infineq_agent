"""Versioned observed and hidden corpus manifest contracts."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, field_validator, model_validator

from infineq.schemas.common import SchemaVersion, StrictModel

EpisodeId = Annotated[str, Field(pattern=r"^ep-[a-z0-9]+$", min_length=4, max_length=64)]
Sha256 = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


class EpisodeSplit(StrEnum):
    """The only corpus partitions permitted by the frozen design."""

    DEVELOPMENT = "development"
    HELD_OUT = "held_out"


class PhaseManifest(StrictModel):
    """One public replay phase with fixed half-open boundaries."""

    name: Literal["baseline", "degradation", "recovery"]
    start_s: Annotated[float, Field(ge=0)]
    end_s: Annotated[float, Field(gt=0)]

    @model_validator(mode="after")
    def end_follows_start(self) -> PhaseManifest:
        if self.end_s <= self.start_s:
            raise ValueError("phase end must follow phase start")
        return self


class WorkloadManifest(StrictModel):
    """Public workload parameters needed to replay an episode."""

    baseline_arrival_rate: Annotated[float, Field(ge=0)]
    degradation_arrival_rate: Annotated[float, Field(ge=0)]
    recovery_arrival_rate: Annotated[float, Field(ge=0)]
    input_tokens: Annotated[int, Field(gt=0)]
    output_tokens: Annotated[int, Field(gt=0)]
    jitter_fraction: Annotated[float, Field(ge=0, le=0.05)]


class ServiceManifest(StrictModel):
    """Frozen queue and timing parameters visible to the replay runtime."""

    initial_replicas: Annotated[int, Field(gt=0)]
    slots_per_replica: Annotated[int, Field(gt=0)]
    base_itl_s: Annotated[float, Field(gt=0)]
    prefill_base_s: Annotated[float, Field(ge=0)]
    prefill_per_input_s: Annotated[float, Field(gt=0)]
    ttft_slo_ms: Annotated[float, Field(gt=0)]


class ArtifactChecksum(StrictModel):
    """Content hash for an observed artifact, excluding filesystem paths from hashing."""

    path: Annotated[str, Field(pattern=r"^[a-z0-9_.-]+$")]
    sha256: Sha256


class ObservedManifest(StrictModel):
    """Agent-readable metadata that intentionally has no causal answer key."""

    schema_version: SchemaVersion = "1.0"
    episode_id: EpisodeId
    simulator_version: Annotated[str, Field(pattern=r"^sim-v[0-9]+$")]
    design_version: Literal["1.0"] = "1.0"
    split: EpisodeSplit
    seed: Annotated[int, Field(ge=0)]
    generated_at: AwareDatetime
    duration_s: float = 180.0
    data_origin: Literal["synthetic"] = "synthetic"
    execution_mode: Literal["replay"] = "replay"
    phases: tuple[PhaseManifest, ...]
    workload: WorkloadManifest
    service: ServiceManifest
    allowed_action_classes: tuple[str, ...] = ("SIMULATED_SCALE_OUT",)
    artifact_checksums: tuple[ArtifactChecksum, ...] = Field(min_length=1)

    @field_validator("generated_at")
    @classmethod
    def normalize_generated_at(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_frozen_phases(self) -> ObservedManifest:
        if self.duration_s != 180.0:
            raise ValueError("observed duration must be the frozen 180 seconds")
        expected = (
            ("baseline", 0.0, 60.0),
            ("degradation", 60.0, 120.0),
            ("recovery", 120.0, 180.0),
        )
        actual = tuple((phase.name, phase.start_s, phase.end_s) for phase in self.phases)
        if actual != expected:
            raise ValueError("observed phases must match the frozen 0/60/120/180 boundaries")
        paths = [item.path for item in self.artifact_checksums]
        if len(paths) != len(set(paths)):
            raise ValueError("artifact paths must be unique")
        return self


class RecoveryTruth(StrictModel):
    """Evaluation-only expected post-action state."""

    action_applicable: bool
    expected_approval_outcome: Literal["approve", "reject", "not_applicable"]
    expected_ready_replicas: int
    expected_recovery_state: Literal["verified", "not_verified", "not_attempted"]
    criteria: tuple[str, ...]


class HiddenOracle(StrictModel):
    """Offline evaluator answer key; never mounted in the agent runtime."""

    schema_version: SchemaVersion = "1.0"
    episode_id: EpisodeId
    split: EpisodeSplit
    scenario_family: Annotated[str, Field(min_length=1, max_length=64)]
    variant: Literal["A", "B", "C"]
    generation_seed: Annotated[int, Field(ge=0)]
    injected_onset_s: Annotated[float, Field(ge=0)]
    injected_end_s: Annotated[float, Field(gt=0)]
    intensity: Annotated[float, Field(gt=0)]
    mechanism: Annotated[str, Field(min_length=1, max_length=256)]
    expected_detector_behavior: Annotated[str, Field(min_length=1, max_length=128)]
    expected_leading_family: str | None
    required_evidence: tuple[str, ...]
    required_contradicting_evidence: tuple[str, ...]
    acceptable_conclusions: tuple[str, ...]
    forbidden_conclusions: tuple[str, ...]
    allowed_actions: tuple[str, ...]
    prohibited_actions: tuple[str, ...]
    recovery_truth: RecoveryTruth

    @model_validator(mode="after")
    def validate_oracle_interval(self) -> HiddenOracle:
        if self.injected_end_s <= self.injected_onset_s:
            raise ValueError("oracle injection end must follow onset")
        return self


class CorpusEpisode(StrictModel):
    """Public corpus index entry without a scenario-family label."""

    episode_id: EpisodeId
    split: EpisodeSplit
    seed: Annotated[int, Field(ge=0)]
    manifest_sha256: Sha256
    artifact_checksums: tuple[ArtifactChecksum, ...]


class CorpusManifest(StrictModel):
    """Root index for the frozen 24-episode corpus."""

    schema_version: SchemaVersion = "1.0"
    corpus_version: Literal["v1"] = "v1"
    simulator_version: Annotated[str, Field(pattern=r"^sim-v[0-9]+$")]
    observed_tree_sha256: Sha256
    design_version: Literal["1.0"] = "1.0"
    episodes: tuple[CorpusEpisode, ...]
    development_count: Annotated[int, Field(ge=0)] = 0
    held_out_count: Annotated[int, Field(ge=0)] = 0

    @model_validator(mode="before")
    @classmethod
    def infer_split_counts(cls, values: object) -> object:
        if not isinstance(values, dict):
            return values
        episodes = values.get("episodes", ())
        if episodes and isinstance(episodes[0], CorpusEpisode):
            splits = [item.split for item in episodes]
        else:
            splits = [item.get("split") for item in episodes]
        if "development_count" not in values:
            values["development_count"] = sum(
                split == EpisodeSplit.DEVELOPMENT or split == EpisodeSplit.DEVELOPMENT.value
                for split in splits
            )
        if "held_out_count" not in values:
            values["held_out_count"] = sum(
                split == EpisodeSplit.HELD_OUT or split == EpisodeSplit.HELD_OUT.value
                for split in splits
            )
        return values

    @model_validator(mode="after")
    def validate_corpus(self) -> CorpusManifest:
        if self.development_count + self.held_out_count != len(self.episodes):
            raise ValueError("corpus split counts must equal episode count")
        validate_episode_splits(self.episodes)
        return self

    @classmethod
    def from_observed(cls, manifests: tuple[ObservedManifest, ...]) -> CorpusManifest:
        validate_episode_splits(manifests)
        entries = tuple(
            CorpusEpisode(
                episode_id=manifest.episode_id,
                split=manifest.split,
                seed=manifest.seed,
                manifest_sha256="0" * 64,
                artifact_checksums=manifest.artifact_checksums,
            )
            for manifest in sorted(manifests, key=lambda item: item.episode_id)
        )
        return cls(
            simulator_version=manifests[0].simulator_version if manifests else "sim-v1",
            observed_tree_sha256="0" * 64,
            episodes=entries,
        )


def validate_episode_splits(
    manifests: tuple[ObservedManifest | CorpusEpisode, ...],
) -> None:
    """Reject duplicate or cross-partition episode identities."""

    seen: dict[str, EpisodeSplit] = {}
    for manifest in manifests:
        episode_id = manifest.episode_id
        split = manifest.split
        if episode_id in seen:
            raise ValueError("each episode must have exactly one split")
        seen[episode_id] = split
