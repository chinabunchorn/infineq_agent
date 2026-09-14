"""Deterministic data-quality validation for observed replay telemetry."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from itertools import pairwise
from typing import cast

from pydantic import ValidationError

from infineq.evidence.ids import evidence_id, normalized_content_hash
from infineq.evidence.store import EvidenceIndexRecord, ObservedEpisode
from infineq.schemas.evidence import DataQuality, DataQualityStatus

type JsonRecord = Mapping[str, object]
type RecordsBySource = Mapping[str, tuple[JsonRecord, ...]]
DEFAULT_REQUIRED_SIGNALS = frozenset({"error_rate", "itl", "queue_depth", "ttft"})


class QualityIssueCode(StrEnum):
    """Stable codes for quality failures exposed to the detector."""

    SCHEMA_VERSION = "schema_version"
    SOURCE_EPISODE_IDENTITY = "source_episode_identity"
    TIMESTAMP_ORDERING = "timestamp_ordering"
    UNIT_CONSISTENCY = "unit_consistency"
    FRESHNESS = "freshness"
    SAMPLE_COUNT = "sample_count"
    REQUIRED_LABELS = "required_labels"
    COUNTER_RESET = "counter_reset"
    REPLICA_REVISION_STATE = "replica_revision_state"
    EVIDENCE_INDEX = "evidence_index_integrity"
    DATA_MISSING = "data_missing"


@dataclass(frozen=True, slots=True)
class QualityFailure:
    """One deterministic, typed reason that quality is not decision-ready."""

    code: QualityIssueCode
    message: str
    source: str | None = None
    record_ordinal: int | None = None


@dataclass(frozen=True, slots=True)
class QualityDataset:
    """Path-free input to quality validation."""

    episode_id: str
    schema_version: object
    records: RecordsBySource
    evidence_index: tuple[JsonRecord, ...]
    required_signals: frozenset[str] = DEFAULT_REQUIRED_SIGNALS

    @classmethod
    def from_episode(cls, episode: ObservedEpisode) -> QualityDataset:
        """Adapt a read-only observed episode without exposing filesystem paths."""

        return cls(
            episode_id=episode.manifest.episode_id,
            schema_version=episode.manifest.schema_version,
            records={
                "request_events": episode.requests,
                "service_metrics": episode.service_metrics,
                "infrastructure_events": episode.infrastructure_events,
                "change_events": episode.change_events,
            },
            evidence_index=tuple(
                entry.model_dump(mode="python") for entry in episode.evidence_index
            ),
        )


@dataclass(frozen=True, slots=True)
class QualityResult:
    """Quality contract plus typed failures; failures cannot be a diagnosis."""

    data_quality: DataQuality
    failures: tuple[QualityFailure, ...]

    @property
    def decision_ready(self) -> bool:
        """Whether all quality gates passed for a detector decision."""

        return self.data_quality.status is DataQualityStatus.GOOD and not self.failures


def _numeric(value: object) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
        if number == number and number not in {float("inf"), float("-inf")}:
            return number
    return None


def _index_components(record: JsonRecord) -> tuple[str, str, str, float, float] | None:
    signal = record.get("signal")
    aggregation = record.get("aggregation")
    if not isinstance(signal, str) or not isinstance(aggregation, str):
        return None
    start = _numeric(record.get("window_start_s"))
    end = _numeric(record.get("window_end_s"))
    if start is not None and end is not None:
        window = f"w{int(start):03d}-{int(end):03d}"
        return signal, aggregation, window, start, end
    timestamp = _numeric(record.get("timestamp_s"))
    if timestamp is None:
        return None
    return signal, aggregation, f"t{int(timestamp):03d}", timestamp, timestamp


def _index_failure(
    message: str,
    *,
    source: str | None = None,
    record_ordinal: int | None = None,
) -> QualityFailure:
    return QualityFailure(
        code=QualityIssueCode.EVIDENCE_INDEX,
        message=message,
        source=source,
        record_ordinal=record_ordinal,
    )


def validate_data_quality(dataset: QualityDataset) -> QualityResult:
    """Validate one observed dataset's basic quality contract."""

    failures: list[QualityFailure] = []
    if dataset.schema_version != "1.0":
        failures.append(
            QualityFailure(
                code=QualityIssueCode.SCHEMA_VERSION,
                message="schema version must be 1.0",
            )
        )

    for source, records in dataset.records.items():
        for ordinal, record in enumerate(records):
            if record.get("source") != source or (
                "episode_id" in record and record.get("episode_id") != dataset.episode_id
            ):
                failures.append(
                    QualityFailure(
                        code=QualityIssueCode.SOURCE_EPISODE_IDENTITY,
                        message="record source and episode identity must match the dataset",
                        source=source,
                        record_ordinal=ordinal,
                    )
                )

            lifecycle_keys = (
                "submit_s",
                "service_start_s",
                "first_token_s",
                "completion_s",
            )
            lifecycle_values = [
                float(cast(int | float, record[key]))
                for key in lifecycle_keys
                if isinstance(record.get(key), (int, float))
                and not isinstance(record.get(key), bool)
            ]
            if len(lifecycle_values) == len(lifecycle_keys) and any(
                left > right for left, right in pairwise(lifecycle_values)
            ):
                failures.append(
                    QualityFailure(
                        code=QualityIssueCode.TIMESTAMP_ORDERING,
                        message="request lifecycle timestamps must be non-decreasing",
                        source=source,
                        record_ordinal=ordinal,
                    )
                )

        primary_key = {
            "request_events": "submit_s",
            "service_metrics": "window_start_s",
            "infrastructure_events": "timestamp_s",
            "change_events": "timestamp_s",
        }.get(source)
        primary_values = (
            [
                float(cast(int | float, record[primary_key]))
                for record in records
                if primary_key is not None
                and isinstance(record.get(primary_key), (int, float))
                and not isinstance(record.get(primary_key), bool)
            ]
            if primary_key is not None
            else []
        )
        if any(left > right for left, right in pairwise(primary_values)):
            failures.append(
                QualityFailure(
                    code=QualityIssueCode.TIMESTAMP_ORDERING,
                    message="records must be ordered by replay timestamp",
                    source=source,
                )
            )

    required_labels = {
        "request_events": (
            "source",
            "signal",
            "aggregation",
            "unit",
            "data_origin",
            "freshness_s",
            "request_id",
            "submit_s",
            "service_start_s",
            "first_token_s",
            "completion_s",
            "input_tokens",
            "output_tokens",
            "queue_wait_s",
            "prefill_s",
            "decode_s",
            "ttft_s",
            "itl_s",
            "end_to_end_s",
            "status",
        ),
        "service_metrics": (
            "source",
            "signal",
            "aggregation",
            "unit",
            "data_origin",
            "freshness_s",
            "window_start_s",
            "window_end_s",
            "value",
        ),
        "infrastructure_events": (
            "source",
            "signal",
            "aggregation",
            "unit",
            "data_origin",
            "freshness_s",
            "observed_at",
            "timestamp_s",
            "desired_replicas",
            "ready_replicas",
            "revision",
        ),
        "change_events": (
            "source",
            "signal",
            "aggregation",
            "unit",
            "data_origin",
            "freshness_s",
        ),
    }
    for source, records in dataset.records.items():
        labels = required_labels.get(source, ())
        for ordinal, record in enumerate(records):
            record_labels = labels
            if source == "change_events" and record.get("query_status") == "empty":
                record_labels = (*labels, "query_status", "events")
            elif source == "change_events":
                record_labels = (*labels, "observed_at", "timestamp_s", "changed_fields")
            missing = tuple(label for label in record_labels if label not in record)
            if missing:
                failures.append(
                    QualityFailure(
                        code=QualityIssueCode.REQUIRED_LABELS,
                        message=f"required labels are missing: {', '.join(missing)}",
                        source=source,
                        record_ordinal=ordinal,
                    )
                )

    expected_units = {
        "queue_wait": "ms",
        "ttft": "ms",
        "prefill": "ms",
        "itl": "ms",
        "end_to_end": "ms",
        "input_tokens": "tokens",
        "output_tokens": "tokens",
        "arrival_rate": "requests_per_second",
        "prompt_throughput": "tokens_per_second",
        "generation_throughput": "tokens_per_second",
        "error_rate": "ratio",
        "running_requests": "requests",
        "waiting_requests": "requests",
        "queue_depth": "requests",
        "request": "mixed",
        "replicas": "replicas",
        "configuration_change": "event",
    }
    units_by_signal: dict[tuple[str, str], str] = {}
    for source, records in dataset.records.items():
        for ordinal, record in enumerate(records):
            signal = record.get("signal")
            unit = record.get("unit")
            if not isinstance(signal, str) or not isinstance(unit, str) or not signal or not unit:
                continue
            key = (source, signal)
            previous_unit = units_by_signal.setdefault(key, unit)
            if previous_unit != unit or expected_units.get(signal) not in {None, unit}:
                failures.append(
                    QualityFailure(
                        code=QualityIssueCode.UNIT_CONSISTENCY,
                        message="signal units must be stable and use the frozen unit",
                        source=source,
                        record_ordinal=ordinal,
                    )
                )

    reset_keys = (
        "counter_reset",
        "counter_reset_marker",
        "reset_marker",
        "counter_reset_detected",
    )
    reset_values = {"1", "true", "yes", "reset", "detected"}
    for source, records in dataset.records.items():
        for ordinal, record in enumerate(records):
            marker = next((record[key] for key in reset_keys if key in record), False)
            marked = marker is True or (
                isinstance(marker, str) and marker.strip().lower() in reset_values
            )
            if marked:
                failures.append(
                    QualityFailure(
                        code=QualityIssueCode.COUNTER_RESET,
                        message="counter reset markers invalidate rolling counter aggregates",
                        source=source,
                        record_ordinal=ordinal,
                    )
                )

    replica_states: dict[float, set[tuple[int, int, str]]] = {}
    for ordinal, record in enumerate(dataset.records.get("infrastructure_events", ())):
        desired = record.get("desired_replicas")
        ready = record.get("ready_replicas")
        revision = record.get("revision")
        state_is_numeric = (
            isinstance(desired, int)
            and not isinstance(desired, bool)
            and isinstance(ready, int)
            and not isinstance(ready, bool)
        )
        desired_count = cast(int, desired)
        ready_count = cast(int, ready)
        contradictory = not state_is_numeric or (
            desired_count < 0 or ready_count < 0 or ready_count > desired_count
        )
        nested = record.get("value")
        if isinstance(nested, Mapping):
            contradictory = contradictory or any(
                key in nested and nested[key] != record.get(key)
                for key in ("desired_replicas", "ready_replicas", "revision")
            )
        if not isinstance(revision, str) or not revision:
            contradictory = True
        if contradictory:
            failures.append(
                QualityFailure(
                    code=QualityIssueCode.REPLICA_REVISION_STATE,
                    message="replica and revision state is contradictory",
                    source="infrastructure_events",
                    record_ordinal=ordinal,
                )
            )
        timestamp = record.get("timestamp_s")
        if (
            isinstance(timestamp, (int, float))
            and not isinstance(timestamp, bool)
            and state_is_numeric
            and isinstance(revision, str)
        ):
            replica_states.setdefault(float(timestamp), set()).add(
                (desired_count, ready_count, revision)
            )

    for timestamp, states in replica_states.items():
        if len(states) > 1:
            failures.append(
                QualityFailure(
                    code=QualityIssueCode.REPLICA_REVISION_STATE,
                    message=f"replica or revision state conflicts at replay time {timestamp:g}",
                    source="infrastructure_events",
                )
            )

    observed_signals = {
        record.get("signal")
        for record in dataset.records.get("service_metrics", ())
        if isinstance(record.get("signal"), str)
    }
    for signal in sorted(dataset.required_signals - observed_signals):
        failures.append(
            QualityFailure(
                code=QualityIssueCode.DATA_MISSING,
                message=f"required service signal is missing: {signal}",
                source="service_metrics",
            )
        )

    indexed_records: set[tuple[str, int]] = set()
    indexed_ids: set[str] = set()
    for index_ordinal, raw_entry in enumerate(dataset.evidence_index):
        try:
            entry = EvidenceIndexRecord.model_validate(raw_entry)
        except ValidationError:
            failures.append(
                _index_failure("evidence index record is invalid", record_ordinal=index_ordinal)
            )
            continue
        if entry.evidence_id in indexed_ids:
            failures.append(
                _index_failure("evidence index IDs must be unique", record_ordinal=index_ordinal)
            )
        indexed_ids.add(entry.evidence_id)
        source_records = dataset.records.get(entry.source)
        if entry.episode_id != dataset.episode_id:
            failures.append(
                _index_failure(
                    "evidence index episode identity is inconsistent",
                    source=entry.source,
                    record_ordinal=index_ordinal,
                )
            )
            continue
        if source_records is None or entry.record_ordinal >= len(source_records):
            failures.append(
                _index_failure(
                    "evidence index ordinal does not reference a record",
                    source=entry.source,
                    record_ordinal=index_ordinal,
                )
            )
            continue
        indexed_records.add((entry.source, entry.record_ordinal))
        record = source_records[entry.record_ordinal]
        components = _index_components(record)
        if components is None:
            failures.append(
                _index_failure(
                    "indexed record has no valid evidence identity",
                    source=entry.source,
                    record_ordinal=index_ordinal,
                )
            )
            continue
        signal, aggregation, window, start, end = components
        try:
            expected_id = evidence_id(
                episode_id=dataset.episode_id,
                source=entry.source,
                window=window,
                signal=signal,
                aggregation=aggregation,
                content=record,
            )
        except ValueError:
            failures.append(
                _index_failure(
                    "indexed record has unsafe evidence identity",
                    source=entry.source,
                    record_ordinal=index_ordinal,
                )
            )
            continue
        mismatches = (
            entry.evidence_id != expected_id,
            entry.signal != signal,
            entry.aggregation != aggregation,
            entry.window_start_s != start,
            entry.window_end_s != end,
            entry.unit != record.get("unit"),
            entry.freshness_s != record.get("freshness_s"),
            entry.content_hash != normalized_content_hash(record),
        )
        if any(mismatches):
            failures.append(
                _index_failure(
                    "evidence index does not match normalized record content",
                    source=entry.source,
                    record_ordinal=index_ordinal,
                )
            )

    for source, records in dataset.records.items():
        for ordinal in range(len(records)):
            if (source, ordinal) not in indexed_records:
                failures.append(
                    _index_failure(
                        "normalized record is absent from the evidence index",
                        source=source,
                        record_ordinal=ordinal,
                    )
                )

    sample_count = len(dataset.records.get("request_events", ()))
    if sample_count < 20:
        failures.append(
            QualityFailure(
                code=QualityIssueCode.SAMPLE_COUNT,
                message="at least 20 request samples are required for a detector decision",
                source="request_events",
            )
        )
    freshness_values = [
        float(cast(int | float, record["freshness_s"]))
        for records in dataset.records.values()
        for record in records
        if isinstance(record.get("freshness_s"), (int, float))
    ]
    freshness_seconds = max(freshness_values, default=0.0)
    if any(value > 10.0 for value in freshness_values):
        failures.append(
            QualityFailure(
                code=QualityIssueCode.FRESHNESS,
                message="freshness must be no more than 10 seconds in replay time",
            )
        )
    status = DataQualityStatus.INSUFFICIENT if failures else DataQualityStatus.GOOD
    quality = DataQuality(
        status=status,
        sample_count=sample_count,
        freshness_seconds=freshness_seconds,
        issues=tuple(f"{failure.code.value}: {failure.message}" for failure in failures),
    )
    return QualityResult(data_quality=quality, failures=tuple(failures))


validate_quality = validate_data_quality

__all__ = [
    "JsonRecord",
    "QualityDataset",
    "QualityFailure",
    "QualityIssueCode",
    "QualityResult",
    "RecordsBySource",
    "validate_data_quality",
    "validate_quality",
]
