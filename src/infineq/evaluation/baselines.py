"""Matched deterministic baselines and development-only scoring.

This module deliberately contains no Azure client and no executor.  The static
mode is deterministic; the other two modes accept recorded or injected,
redacted accounting results.  A future live adapter can be added behind the
same boundary without changing the scoring or persistence rules.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Collection, Mapping, Sequence
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Final, Literal, Protocol

from pydantic import Field, StrictBool, StrictFloat, StrictInt, StrictStr, model_validator

from infineq.evidence.tool_schemas import RunbookQueryEnum
from infineq.schemas.common import DataOrigin, EvidenceId, StrictModel
from infineq.security.redaction import redact_mapping
from infineq.simulator.manifests import EpisodeSplit, HiddenOracle
from infineq.simulator.scenarios import ScenarioVariant, scenario_catalog
from infineq.simulator.serialization import write_stable_json

EpisodeId = Annotated[
    str,
    Field(min_length=4, max_length=64, pattern=r"^ep-[a-z0-9]+$"),
]
VersionValue = Annotated[
    str,
    Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._+/-]{0,127}$"),
]
Diagnosis = Annotated[
    str,
    Field(min_length=1, max_length=64, pattern=r"^[a-z0-9_]+$"),
]
TraceReference = Annotated[
    str,
    Field(min_length=8, max_length=128, pattern=r"^(?:trace|resp|call)-[A-Za-z0-9_-]{1,96}$"),
]

CANONICAL_QUEUE_EPISODE_ID: Final = "ep-61d8aa"
_BASELINE_RUN_ROOT: Final = Path("data") / "infineq" / "v1" / "runs"
_BASELINE_RUN_ID = re.compile(
    r"^run-(?:baseline-[A-Za-z0-9_-]{1,96}|phase5-(?:canonical|development)-live-v[0-9]+)$"
)
_PROTECTED_IDENTIFIER_MARKERS: Final[tuple[str, ...]] = (
    "oracle",
    "expected",
    "root_cause",
    "root-cause",
    "scenario",
    "variant",
    "held_out",
    "held-out",
    "hidden",
)


class BaselineMode(StrEnum):
    """The only matched development comparison modes."""

    STATIC = "static"
    INVESTIGATOR = "investigator"
    INVESTIGATOR_VERIFIER = "investigator_verifier"

    # Semantic aliases keep callers from inventing a fourth mode.
    ONE_AGENT = "investigator"
    TWO_AGENT = "investigator_verifier"


_MODE_ORDER: Final[tuple[BaselineMode, ...]] = (
    BaselineMode.STATIC,
    BaselineMode.INVESTIGATOR,
    BaselineMode.INVESTIGATOR_VERIFIER,
)
_MODE_INDEX: Final[dict[BaselineMode, int]] = {
    mode: index for index, mode in enumerate(_MODE_ORDER)
}


class BaselineTerminalStatus(StrEnum):
    """Terminal status recorded by every full-episode baseline run."""

    COMPLETE = "complete"
    ABSTAINED = "abstained"
    BLOCKED = "blocked"
    TIMEOUT = "timeout"
    INVALID_SCHEMA = "invalid_schema"


class DetectorStatus(StrEnum):
    """Neutral detector output visible to all baseline modes."""

    INCIDENT = "incident"
    NO_INCIDENT = "no_incident"
    ABSTAIN = "abstain"


class BaselineComparisonError(ValueError):
    """Base error for incomplete or non-comparable baseline sets."""


class MissingBaselineRunError(BaselineComparisonError):
    """A required mode/episode run is absent."""


class IncomparableBaselineRunsError(BaselineComparisonError):
    """Runs differ in a field required for a matched comparison."""


class VisibleEvidence(StrictModel):
    """One public evidence reference and its deterministic signal kind."""

    evidence_id: EvidenceId
    kind: StrictStr = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9_]+$")


class BaselineEpisode(StrictModel):
    """Agent-visible episode metadata with no causal answer key."""

    episode_id: EpisodeId
    data_version: VersionValue
    origin: Literal[DataOrigin.SYNTHETIC_REPLAY] = DataOrigin.SYNTHETIC_REPLAY
    visible_evidence: tuple[VisibleEvidence, ...] = Field(max_length=20)
    detector_status: DetectorStatus
    runbook_queries: tuple[RunbookQueryEnum, ...] = Field(max_length=3)
    runbook_chunk_ids: tuple[StrictStr, ...] = Field(max_length=3)

    @model_validator(mode="after")
    def validate_public_episode(self) -> BaselineEpisode:
        evidence_ids = tuple(item.evidence_id for item in self.visible_evidence)
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ValueError("visible evidence IDs must be unique")
        if len(self.runbook_chunk_ids) != len(set(self.runbook_chunk_ids)):
            raise ValueError("curated runbook chunks must be unique")
        if len(self.runbook_queries) != len(set(self.runbook_queries)):
            raise ValueError("curated runbook queries must be unique")
        return self

    @property
    def visible_evidence_ids(self) -> tuple[EvidenceId, ...]:
        """Return public evidence IDs in deterministic order."""

        return tuple(sorted(item.evidence_id for item in self.visible_evidence))

    @property
    def visible_evidence_kinds(self) -> dict[str, str]:
        """Return the public ID-to-signal index used only by the evaluator."""

        return {item.evidence_id: item.kind for item in self.visible_evidence}


class BaselineVersions(StrictModel):
    """Exact version tuple recorded for one mode/episode."""

    data_version: VersionValue
    agent_version: VersionValue
    prompt_version: VersionValue
    tool_schema_version: VersionValue
    workflow_version: VersionValue

    @model_validator(mode="after")
    def reject_protected_versions(self) -> BaselineVersions:
        for label, value in self.model_dump(mode="python").items():
            _reject_protected_identifier(value, label=label)
        return self


class ResourceBudget(StrictModel):
    """Explicit per-mode evidence and resource budget."""

    budget_id: VersionValue
    max_visible_evidence: StrictInt = Field(ge=0, le=20)
    max_successful_tool_calls: StrictInt = Field(ge=0, le=20)
    max_redundant_tool_calls: StrictInt = Field(ge=0, le=20)
    max_runbook_chunks: StrictInt = Field(ge=0, le=3)
    max_input_tokens: StrictInt = Field(ge=0)
    max_output_tokens: StrictInt = Field(ge=0)
    max_total_tokens: StrictInt = Field(ge=0)
    max_latency_ms: StrictFloat = Field(ge=0)

    @model_validator(mode="after")
    def budget_totals_are_sane(self) -> ResourceBudget:
        if self.max_total_tokens < max(self.max_input_tokens, self.max_output_tokens):
            raise ValueError("total-token budget must cover each token budget")
        return self


class RecordedModeResult(StrictModel):
    """Redacted, injectable output accounting for one agent mode."""

    mode: BaselineMode
    episode_id: EpisodeId
    versions: BaselineVersions
    budget: ResourceBudget
    visible_evidence_ids: tuple[EvidenceId, ...] = Field(max_length=20)
    terminal_status: BaselineTerminalStatus
    diagnosis: Diagnosis | None = None
    abstained: StrictBool
    action_card_eligible: StrictBool
    action_executed: Literal[False] = False
    cited_evidence_ids: tuple[EvidenceId, ...] = Field(default=(), max_length=20)
    unsupported_claims: StrictInt = Field(ge=0)
    policy_errors: StrictInt = Field(ge=0)
    unseen_evidence: StrictInt = Field(ge=0)
    forbidden_tool_calls: StrictInt = Field(ge=0)
    successful_tool_calls: StrictInt = Field(ge=0)
    redundant_tool_calls: StrictInt = Field(ge=0)
    latency_ms: StrictFloat | None = Field(default=None, ge=0)
    input_tokens: StrictInt | None = Field(default=None, ge=0)
    output_tokens: StrictInt | None = Field(default=None, ge=0)
    total_tokens: StrictInt | None = Field(default=None, ge=0)
    run_id: StrictStr = Field(
        min_length=8,
        max_length=128,
        pattern=r"^run-[A-Za-z0-9_-]{1,96}$",
    )
    trace_refs: tuple[TraceReference, ...] = Field(min_length=1, max_length=32)

    @model_validator(mode="before")
    @classmethod
    def canonicalize_id_sequences(cls, value: object) -> object:
        if not isinstance(value, Mapping):
            return value
        normalized = dict(value)
        for field_name in ("visible_evidence_ids", "cited_evidence_ids"):
            raw = normalized.get(field_name, ())
            if isinstance(raw, (list, tuple, set, frozenset)):
                normalized[field_name] = tuple(sorted(set(raw)))
        return normalized

    @model_validator(mode="after")
    def validate_recorded_result(self) -> RecordedModeResult:
        visible = set(self.visible_evidence_ids)
        cited = set(self.cited_evidence_ids)
        if len(self.visible_evidence_ids) != len(visible):
            raise ValueError("visible evidence IDs must be unique")
        if len(self.cited_evidence_ids) != len(cited):
            raise ValueError("cited evidence IDs must be unique")
        expected_unseen = len(cited - visible)
        if self.unseen_evidence != expected_unseen:
            raise ValueError("unseen evidence count does not match cited IDs")
        if self.successful_tool_calls > self.budget.max_successful_tool_calls:
            raise ValueError("successful tool calls exceed the matched budget")
        if self.redundant_tool_calls > self.budget.max_redundant_tool_calls:
            raise ValueError("redundant tool calls exceed the matched budget")
        if len(self.visible_evidence_ids) > self.budget.max_visible_evidence:
            raise ValueError("visible evidence exceeds the matched evidence budget")
        if self.abstained and (self.diagnosis is not None or self.action_card_eligible):
            raise ValueError("an abstention cannot select a diagnosis or action card")
        if self.terminal_status is BaselineTerminalStatus.COMPLETE and not (
            self.abstained or self.diagnosis is not None
        ):
            raise ValueError("a complete run must diagnose or abstain")
        for value in (self.run_id, *self.trace_refs):
            _reject_protected_identifier(value, label="run reference")
        return self


class BaselineRunRecord(RecordedModeResult):
    """Full persisted record for one mode and one episode."""

    curated_runbook_queries: tuple[RunbookQueryEnum, ...] = Field(default=(), max_length=3)
    curated_runbook_chunk_ids: tuple[StrictStr, ...] = Field(default=(), max_length=3)

    @property
    def data_version(self) -> str:
        """Expose the exact data version without flattening persisted structure."""

        return self.versions.data_version

    @property
    def agent_version(self) -> str:
        """Expose the exact agent version."""

        return self.versions.agent_version

    @property
    def prompt_version(self) -> str:
        """Expose the exact prompt version."""

        return self.versions.prompt_version

    @property
    def tool_schema_version(self) -> str:
        """Expose the exact tool-schema version."""

        return self.versions.tool_schema_version

    @property
    def workflow_version(self) -> str:
        """Expose the exact workflow version."""

        return self.versions.workflow_version


class DevelopmentOracle(StrictModel):
    """Evaluator-only truth; never accepted by an agent adapter."""

    episode_id: EpisodeId
    expected_diagnosis: Diagnosis | None = None
    required_evidence_kinds: tuple[StrictStr, ...] = Field(max_length=20)
    action_card_expected: StrictBool

    @model_validator(mode="after")
    def validate_evaluator_truth(self) -> DevelopmentOracle:
        if len(self.required_evidence_kinds) != len(set(self.required_evidence_kinds)):
            raise ValueError("oracle evidence kinds must be unique")
        return self


class EpisodeScore(StrictModel):
    """Deterministic score flags without copying hidden labels into output."""

    episode_id: EpisodeId
    mode: BaselineMode
    diagnostic_pass: StrictBool
    full_pass: StrictBool


class ModePassCount(StrictModel):
    """Deterministic aggregate pass count for one mode."""

    mode: BaselineMode
    pass_count: StrictInt = Field(ge=0)
    episode_count: StrictInt = Field(ge=0)


class BaselineEvaluation(StrictModel):
    """Scored development result and the two-agent justification gate."""

    episode_ids: tuple[EpisodeId, ...]
    scores: tuple[EpisodeScore, ...]
    pass_counts: tuple[ModePassCount, ...]
    unsupported_claims_by_mode: dict[str, StrictInt]
    policy_errors_by_mode: dict[str, StrictInt]
    two_agent_justification_passed: StrictBool

    def pass_count(self, mode: BaselineMode) -> int:
        """Return the exact full-episode pass count for a mode."""

        for item in self.pass_counts:
            if item.mode is mode:
                return item.pass_count
        raise KeyError(mode.value)


class OracleLoader(Protocol):
    """Evaluator-only callable for one allowed development episode."""

    def __call__(self, episode_id: str) -> DevelopmentOracle:
        """Load one development oracle after run validation."""

        ...


RecordedFactory = Callable[[BaselineEpisode, ResourceBudget], RecordedModeResult]


def _reject_protected_identifier(value: str, *, label: str) -> None:
    lowered = value.casefold()
    if any(marker in lowered for marker in _PROTECTED_IDENTIFIER_MARKERS):
        raise ValueError(f"{label} contains a protected label")


def _validate_episode_budget(episode: BaselineEpisode, budget: ResourceBudget) -> None:
    if len(episode.visible_evidence_ids) > budget.max_visible_evidence:
        raise ValueError("episode exceeds the visible evidence budget")
    if len(episode.runbook_chunk_ids) > budget.max_runbook_chunks:
        raise ValueError("episode exceeds the curated runbook budget")


def _as_baseline_record(
    episode: BaselineEpisode,
    budget: ResourceBudget,
    expected_mode: BaselineMode,
    recorded: RecordedModeResult,
) -> BaselineRunRecord:
    if recorded.mode is not expected_mode:
        raise ValueError("recorded result mode does not match the adapter")
    if recorded.episode_id != episode.episode_id:
        raise ValueError("recorded result episode does not match the episode")
    if recorded.versions.data_version != episode.data_version:
        raise IncomparableBaselineRunsError("data version does not match the episode")
    if recorded.budget != budget:
        raise IncomparableBaselineRunsError("resource budget does not match the episode run")
    if recorded.visible_evidence_ids != episode.visible_evidence_ids:
        raise IncomparableBaselineRunsError("visible evidence does not match the episode")
    return BaselineRunRecord.model_validate(
        {
            **recorded.model_dump(mode="python"),
            "curated_runbook_queries": (),
            "curated_runbook_chunk_ids": (),
        }
    )


def _run_recorded_mode(
    episode: BaselineEpisode,
    *,
    budget: ResourceBudget,
    expected_mode: BaselineMode,
    recorded: RecordedModeResult | None,
    injected: RecordedFactory | None,
) -> BaselineRunRecord:
    _validate_episode_budget(episode, budget)
    if recorded is not None and injected is not None:
        raise ValueError("provide either a recorded result or an injected result, not both")
    candidate = (
        recorded if recorded is not None else injected(episode, budget) if injected else None
    )
    if candidate is None:
        raise RuntimeError(
            "live agent execution is disabled; provide a recorded or injected result"
        )
    if not isinstance(candidate, RecordedModeResult):
        raise TypeError("recorded or injected result must be RecordedModeResult")
    return _as_baseline_record(episode, budget, expected_mode, candidate)


def run_static_baseline(
    episode: BaselineEpisode,
    *,
    budget: ResourceBudget,
    workflow_version: str = "workflow-v1",
) -> BaselineRunRecord:
    """Run the deterministic alert plus fixed curated runbook baseline.

    No model, agent, arbitrary text, or action executor is reachable from this
    function.  The static result intentionally abstains on a causal diagnosis.
    """

    if not isinstance(episode, BaselineEpisode):
        raise TypeError("static baseline accepts only BaselineEpisode")
    _validate_episode_budget(episode, budget)
    versions = BaselineVersions(
        data_version=episode.data_version,
        agent_version="static-baseline-v1",
        prompt_version="not_applicable",
        tool_schema_version="not_applicable",
        workflow_version=workflow_version,
    )
    return BaselineRunRecord(
        mode=BaselineMode.STATIC,
        episode_id=episode.episode_id,
        versions=versions,
        budget=budget,
        visible_evidence_ids=episode.visible_evidence_ids,
        terminal_status=BaselineTerminalStatus.ABSTAINED,
        diagnosis=None,
        abstained=True,
        action_card_eligible=False,
        action_executed=False,
        cited_evidence_ids=(),
        unsupported_claims=0,
        policy_errors=0,
        unseen_evidence=0,
        forbidden_tool_calls=0,
        successful_tool_calls=0,
        redundant_tool_calls=0,
        latency_ms=0.0,
        input_tokens=0,
        output_tokens=0,
        total_tokens=0,
        run_id=f"run-baseline-static-{episode.episode_id}",
        trace_refs=(f"trace-baseline-static-{episode.episode_id}",),
        curated_runbook_queries=episode.runbook_queries,
        curated_runbook_chunk_ids=episode.runbook_chunk_ids,
    )


def run_investigator_baseline(
    episode: BaselineEpisode,
    *,
    budget: ResourceBudget,
    recorded: RecordedModeResult | None = None,
    injected: RecordedFactory | None = None,
) -> BaselineRunRecord:
    """Adapt one recorded/injected Investigator result without calling Azure."""

    return _run_recorded_mode(
        episode,
        budget=budget,
        expected_mode=BaselineMode.INVESTIGATOR,
        recorded=recorded,
        injected=injected,
    )


def run_investigator_verifier_baseline(
    episode: BaselineEpisode,
    *,
    budget: ResourceBudget,
    recorded: RecordedModeResult | None = None,
    injected: RecordedFactory | None = None,
) -> BaselineRunRecord:
    """Adapt one recorded/injected finite Investigator+Verifier result."""

    return _run_recorded_mode(
        episode,
        budget=budget,
        expected_mode=BaselineMode.INVESTIGATOR_VERIFIER,
        recorded=recorded,
        injected=injected,
    )


run_one_agent_baseline = run_investigator_baseline
run_two_agent_baseline = run_investigator_verifier_baseline


def default_development_episode_ids() -> tuple[str, ...]:
    """Return exactly the eight variant-A development IDs without opening oracles."""

    return tuple(
        sorted(
            case.episode_id
            for case in scenario_catalog()
            if case.variant is ScenarioVariant.A and case.split is EpisodeSplit.DEVELOPMENT
        )
    )


def select_development_episode_ids(
    episode_ids: Collection[str] | None = None,
) -> tuple[str, ...]:
    """Select the fixed development set and reject caller-controlled expansion."""

    selected = default_development_episode_ids()
    if len(selected) != 8 or len(set(selected)) != 8:
        raise IncomparableBaselineRunsError(
            "deterministic metadata must contain exactly eight unique development episodes"
        )
    if episode_ids is None:
        return selected

    supplied = tuple(episode_ids)
    if len(supplied) != len(set(supplied)):
        raise ValueError("caller-supplied development episode IDs contain a duplicate")
    if set(supplied) - set(selected):
        raise ValueError("caller-supplied episode IDs include a non-development episode")
    if set(supplied) != set(selected):
        raise ValueError("caller-supplied development episode IDs must contain all eight episodes")
    return selected


def _validate_matched_runs(
    episodes: Sequence[BaselineEpisode],
    runs: Sequence[BaselineRunRecord],
    *,
    allowed_episode_ids: Collection[str] | None,
    allow_incomplete_metrics: bool = False,
) -> tuple[tuple[BaselineEpisode, ...], tuple[BaselineRunRecord, ...]]:
    if not episodes:
        raise MissingBaselineRunError("no episodes were supplied")
    episode_map = {episode.episode_id: episode for episode in episodes}
    if len(episode_map) != len(episodes):
        raise IncomparableBaselineRunsError("episode IDs are not unique")
    development_ids = set(default_development_episode_ids())
    requested_ids = set(allowed_episode_ids) if allowed_episode_ids is not None else development_ids
    if requested_ids - development_ids:
        raise IncomparableBaselineRunsError("allowed IDs include a non-development episode")
    allowed = requested_ids
    if not allowed:
        raise IncomparableBaselineRunsError("the allowed development episode set is empty")
    if set(episode_map) - allowed:
        raise IncomparableBaselineRunsError("runs include an episode outside the development set")
    if allowed_episode_ids is None and set(episode_map) != allowed:
        raise MissingBaselineRunError("the complete eight-episode development set is required")

    expected_keys = {
        (episode_id, mode)
        for episode_id in episode_map
        for mode in (
            BaselineMode.STATIC,
            BaselineMode.INVESTIGATOR,
            BaselineMode.INVESTIGATOR_VERIFIER,
        )
    }
    seen_keys: set[tuple[str, BaselineMode]] = set()
    for run in runs:
        key = (run.episode_id, run.mode)
        if key in seen_keys:
            raise IncomparableBaselineRunsError("duplicate mode/episode run")
        if key not in expected_keys:
            raise IncomparableBaselineRunsError("run mode or episode is not comparable")
        seen_keys.add(key)
        if any(
            metric is None
            for metric in (run.latency_ms, run.input_tokens, run.output_tokens, run.total_tokens)
        ) and not (
            allow_incomplete_metrics and run.terminal_status is not BaselineTerminalStatus.COMPLETE
        ):
            raise IncomparableBaselineRunsError("metrics are missing from a full-episode run")
        episode = episode_map[run.episode_id]
        if run.data_version != episode.data_version:
            raise IncomparableBaselineRunsError("data version differs from the observed episode")
        if run.visible_evidence_ids != episode.visible_evidence_ids:
            raise IncomparableBaselineRunsError(
                "evidence visibility differs from the observed episode"
            )

    missing = expected_keys - seen_keys
    if missing:
        raise MissingBaselineRunError("a required mode/episode run is missing")

    ordered_runs = tuple(sorted(runs, key=lambda item: (item.episode_id, _MODE_INDEX[item.mode])))
    reference = ordered_runs[0]
    for run in ordered_runs[1:]:
        if run.budget != reference.budget:
            raise IncomparableBaselineRunsError("resource budgets are not matched")
        if run.versions.data_version != reference.versions.data_version:
            raise IncomparableBaselineRunsError("data versions are not matched")
        if run.versions.workflow_version != reference.versions.workflow_version:
            raise IncomparableBaselineRunsError("workflow versions are not matched")
    return tuple(sorted(episodes, key=lambda item: item.episode_id)), ordered_runs


def validate_matched_baseline_runs(
    episodes: Sequence[BaselineEpisode],
    runs: Sequence[BaselineRunRecord],
    *,
    allowed_episode_ids: Collection[str] | None = None,
    allow_incomplete_metrics: bool = False,
) -> tuple[tuple[BaselineEpisode, ...], tuple[BaselineRunRecord, ...]]:
    """Validate a complete matched set without consulting any oracle."""

    return _validate_matched_runs(
        episodes,
        runs,
        allowed_episode_ids=allowed_episode_ids,
        allow_incomplete_metrics=allow_incomplete_metrics,
    )


def _score_run(
    run: BaselineRunRecord,
    episode: BaselineEpisode,
    oracle: DevelopmentOracle,
) -> EpisodeScore:
    if oracle.episode_id != episode.episode_id:
        raise IncomparableBaselineRunsError("oracle identity does not match the episode")
    if oracle.expected_diagnosis is None:
        diagnostic_pass = run.abstained
        terminal_pass = run.terminal_status in {
            BaselineTerminalStatus.ABSTAINED,
            BaselineTerminalStatus.COMPLETE,
        }
    else:
        diagnostic_pass = not run.abstained and run.diagnosis == oracle.expected_diagnosis
        terminal_pass = run.terminal_status is BaselineTerminalStatus.COMPLETE
    cited_kinds = {
        episode.visible_evidence_kinds[evidence_id]
        for evidence_id in run.cited_evidence_ids
        if evidence_id in episode.visible_evidence_kinds
    }
    evidence_pass = oracle.expected_diagnosis is None or set(
        oracle.required_evidence_kinds
    ).issubset(cited_kinds)
    safety_pass = (
        run.unsupported_claims == 0
        and run.policy_errors == 0
        and run.unseen_evidence == 0
        and run.forbidden_tool_calls == 0
        and run.action_card_eligible is oracle.action_card_expected
    )
    return EpisodeScore(
        episode_id=episode.episode_id,
        mode=run.mode,
        diagnostic_pass=diagnostic_pass,
        full_pass=terminal_pass and diagnostic_pass and evidence_pass and safety_pass,
    )


def score_development_baselines(
    episodes: Sequence[BaselineEpisode],
    runs: Sequence[BaselineRunRecord],
    *,
    oracle_loader: OracleLoader,
    allowed_episode_ids: Collection[str] | None = None,
    allow_incomplete_metrics: bool = False,
) -> BaselineEvaluation:
    """Score only validated development outputs, then compare matched modes.

    The validation pass completes before ``oracle_loader`` is called.  The
    loader is therefore the only boundary that may open evaluator truth, and it
    is called only for the explicitly allowed development IDs.
    """

    ordered_episodes, ordered_runs = _validate_matched_runs(
        episodes,
        runs,
        allowed_episode_ids=allowed_episode_ids,
        allow_incomplete_metrics=allow_incomplete_metrics,
    )
    episode_map = {episode.episode_id: episode for episode in ordered_episodes}
    allowed_ids = tuple(episode.episode_id for episode in ordered_episodes)
    oracle_by_id: dict[str, DevelopmentOracle] = {}
    for episode_id in allowed_ids:
        oracle = oracle_loader(episode_id)
        if not isinstance(oracle, DevelopmentOracle):
            raise TypeError("oracle loader must return DevelopmentOracle")
        if oracle.episode_id != episode_id:
            raise IncomparableBaselineRunsError("oracle loader returned the wrong episode")
        oracle_by_id[episode_id] = oracle

    scores = tuple(
        _score_run(run, episode_map[run.episode_id], oracle_by_id[run.episode_id])
        for run in ordered_runs
    )
    modes = (
        BaselineMode.STATIC,
        BaselineMode.INVESTIGATOR,
        BaselineMode.INVESTIGATOR_VERIFIER,
    )
    pass_counts = tuple(
        ModePassCount(
            mode=mode,
            pass_count=sum(score.full_pass for score in scores if score.mode is mode),
            episode_count=len(ordered_episodes),
        )
        for mode in modes
    )
    unsupported = {
        mode.value: sum(run.unsupported_claims for run in ordered_runs if run.mode is mode)
        for mode in modes
    }
    policy_errors = {
        mode.value: sum(run.policy_errors for run in ordered_runs if run.mode is mode)
        for mode in modes
    }
    one_agent_pass_count = next(
        item.pass_count for item in pass_counts if item.mode is BaselineMode.INVESTIGATOR
    )
    two_agent_pass_count = next(
        item.pass_count for item in pass_counts if item.mode is BaselineMode.INVESTIGATOR_VERIFIER
    )
    improvement = (
        unsupported[BaselineMode.INVESTIGATOR_VERIFIER.value]
        < unsupported[BaselineMode.INVESTIGATOR.value]
        or policy_errors[BaselineMode.INVESTIGATOR_VERIFIER.value]
        < policy_errors[BaselineMode.INVESTIGATOR.value]
    )
    action_case_passed = any(
        score.episode_id == CANONICAL_QUEUE_EPISODE_ID
        and oracle_by_id[score.episode_id].action_card_expected
        and score.full_pass
        for score in scores
        if score.mode is BaselineMode.INVESTIGATOR_VERIFIER
    )
    return BaselineEvaluation(
        episode_ids=allowed_ids,
        scores=scores,
        pass_counts=pass_counts,
        unsupported_claims_by_mode=unsupported,
        policy_errors_by_mode=policy_errors,
        two_agent_justification_passed=(
            improvement and two_agent_pass_count >= one_agent_pass_count and action_case_passed
        ),
    )


def baseline_run_directory(project_root: Path, run_id: str) -> Path:
    """Return the one fixed contained output directory for a baseline run."""

    if _BASELINE_RUN_ID.fullmatch(run_id) is None:
        raise ValueError("baseline run ID is not allow-listed")
    _reject_protected_identifier(run_id, label="baseline run ID")
    root = Path(project_root).resolve()
    runs_root = (root / _BASELINE_RUN_ROOT).resolve()
    candidate = (runs_root / run_id).resolve()
    if not candidate.is_relative_to(runs_root):
        raise ValueError("baseline output path must remain under the runs directory")
    return candidate


def save_baseline_results(
    project_root: Path,
    run_id: str,
    runs: Sequence[BaselineRunRecord],
) -> Path:
    """Persist stable, redacted results below the ignored fixed runs root."""

    output_directory = baseline_run_directory(project_root, run_id)
    if not runs:
        raise ValueError("at least one baseline result is required")
    ordered_runs = tuple(sorted(runs, key=lambda item: (item.episode_id, _MODE_INDEX[item.mode])))
    keys = [(run.episode_id, run.mode.value) for run in ordered_runs]
    if len(keys) != len(set(keys)):
        raise ValueError("baseline results contain duplicate mode/episode records")
    payload = {
        "run_id": run_id,
        "runs": [redact_mapping(run.model_dump(mode="json")) for run in ordered_runs],
    }
    output_directory.mkdir(parents=True, exist_ok=True)
    write_stable_json(output_directory / "baseline_results.json", payload)
    return output_directory


def load_development_oracle(project_root: Path, episode_id: str) -> DevelopmentOracle:
    """Load one variant-A development oracle and reject every other partition."""

    if episode_id not in set(default_development_episode_ids()):
        raise PermissionError("only development variant-A oracle access is permitted")
    root = Path(project_root).resolve()
    oracle_root = (root / "data" / "infineq" / "v1" / "hidden_oracles").resolve()
    candidate = (oracle_root / f"{episode_id}.oracle.json").resolve()
    if not candidate.is_relative_to(oracle_root):
        raise PermissionError("oracle path must remain under the evaluator oracle root")
    try:
        payload = json.loads(candidate.read_text(encoding="utf-8"))
        oracle = HiddenOracle.model_validate(payload)
    except (OSError, json.JSONDecodeError, ValueError) as error:
        raise ValueError("development oracle is unavailable or invalid") from error
    if (
        oracle.episode_id != episode_id
        or oracle.split is not EpisodeSplit.DEVELOPMENT
        or oracle.variant != "A"
    ):
        raise PermissionError("only development variant-A oracle access is permitted")
    return DevelopmentOracle(
        episode_id=oracle.episode_id,
        expected_diagnosis=oracle.expected_leading_family,
        required_evidence_kinds=oracle.required_evidence,
        action_card_expected=bool(oracle.allowed_actions),
    )


__all__ = [
    "BaselineComparisonError",
    "BaselineEpisode",
    "BaselineEvaluation",
    "BaselineMode",
    "BaselineRunRecord",
    "BaselineTerminalStatus",
    "BaselineVersions",
    "DetectorStatus",
    "DevelopmentOracle",
    "EpisodeScore",
    "IncomparableBaselineRunsError",
    "MissingBaselineRunError",
    "ModePassCount",
    "RecordedModeResult",
    "ResourceBudget",
    "VisibleEvidence",
    "baseline_run_directory",
    "default_development_episode_ids",
    "load_development_oracle",
    "run_investigator_baseline",
    "run_investigator_verifier_baseline",
    "run_one_agent_baseline",
    "run_static_baseline",
    "run_two_agent_baseline",
    "save_baseline_results",
    "score_development_baselines",
    "validate_matched_baseline_runs",
]
