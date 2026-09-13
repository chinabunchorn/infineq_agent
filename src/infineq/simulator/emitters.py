"""Deterministic observed-artifact emitters and evidence indexing."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from statistics import median

from infineq.evidence.ids import evidence_id, normalized_content_hash
from infineq.simulator.engine import SimulationResult
from infineq.simulator.serialization import (
    sha256_file,
    timestamp_for_seconds,
    write_stable_jsonl,
)


@dataclass(frozen=True, slots=True)
class ObservedChange:
    """A sanitized deployment/configuration change visible to the agent."""

    timestamp_s: float
    changed_fields: tuple[str, ...]
    actor_class: str = "synthetic_control_plane"


@dataclass(frozen=True, slots=True)
class TelemetryProfile:
    """Non-causal controls for missing, stale, or contradictory observations."""

    omitted_signals: frozenset[str] = frozenset()
    stale_signals: frozenset[str] = frozenset()
    contradictory_replicas: bool = False
    changes: tuple[ObservedChange, ...] = ()


@dataclass(frozen=True, slots=True)
class EmittedArtifacts:
    """Names and content hashes of one observed episode's files."""

    checksums: dict[str, str]


_SIGNAL_ORDER = (
    "arrival_rate",
    "running_requests",
    "waiting_requests",
    "queue_depth",
    "queue_wait",
    "ttft",
    "prefill",
    "itl",
    "end_to_end",
    "input_tokens",
    "output_tokens",
    "prompt_throughput",
    "generation_throughput",
    "error_rate",
)


def _percentile(values: list[float], quantile: float) -> float:
    ordered = sorted(values)
    if not ordered:
        raise ValueError("percentile requires observations")
    position = (len(ordered) - 1) * quantile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _metric_value(result: SimulationResult, signal: str, start_s: float, end_s: float) -> float:
    window = [event for event in result.requests if start_s <= event.submit_s < end_s]
    if signal == "arrival_rate":
        return len(window) / (end_s - start_s)
    if signal == "running_requests":
        return float(
            sum(
                event.service_start_s <= end_s and event.completion_s > end_s
                for event in result.requests
            )
        )
    if signal == "waiting_requests":
        return float(result.queue_depth_at(end_s))
    if signal == "queue_depth":
        return float(max(result.queue_depth_at(start_s), result.queue_depth_at(end_s)))
    if signal == "queue_wait":
        return _percentile([event.queue_wait_s * 1_000 for event in window], 0.95)
    if signal == "ttft":
        return _percentile([event.ttft_s * 1_000 for event in window], 0.95)
    if signal == "prefill":
        return median([event.prefill_s * 1_000 for event in window])
    if signal == "itl":
        return median([event.itl_s * 1_000 for event in window])
    if signal == "end_to_end":
        return _percentile([event.end_to_end_s * 1_000 for event in window], 0.95)
    if signal == "input_tokens":
        return _percentile([float(event.input_tokens) for event in window], 0.50)
    if signal == "output_tokens":
        return _percentile([float(event.output_tokens) for event in window], 0.50)
    if signal == "prompt_throughput":
        return sum(event.input_tokens for event in window) / (end_s - start_s)
    if signal == "generation_throughput":
        return sum(event.output_tokens for event in window) / (end_s - start_s)
    if signal == "error_rate":
        return sum(event.status != "success" for event in window) / len(window)
    raise ValueError(f"unknown service signal: {signal}")


def _metric_unit(signal: str) -> str:
    if signal in {"queue_wait", "ttft", "prefill", "itl", "end_to_end"}:
        return "ms"
    if signal in {"input_tokens", "output_tokens"}:
        return "tokens"
    if signal in {"arrival_rate", "prompt_throughput", "generation_throughput"}:
        return "requests_per_second" if signal == "arrival_rate" else "tokens_per_second"
    if signal == "error_rate":
        return "ratio"
    return "requests"


def _metric_aggregation(signal: str) -> str:
    if signal in {"ttft", "queue_wait", "end_to_end"}:
        return "p95"
    if signal in {"prefill", "itl", "input_tokens", "output_tokens"}:
        return "p50"
    if signal == "error_rate":
        return "rate"
    return "gauge"


def _record_metadata(
    *,
    source: str,
    signal: str,
    aggregation: str,
    start_s: float,
    end_s: float,
    freshness_s: float,
) -> dict[str, object]:
    return {
        "source": source,
        "signal": signal,
        "aggregation": aggregation,
        "window_start_s": round(start_s, 6),
        "window_end_s": round(end_s, 6),
        "window_start": timestamp_for_seconds(start_s),
        "window_end": timestamp_for_seconds(end_s),
        "window": {
            "start": timestamp_for_seconds(start_s),
            "end": timestamp_for_seconds(end_s),
        },
        "freshness_s": freshness_s,
        "data_origin": "synthetic",
    }


def _index_records(
    episode_id: str,
    source: str,
    records: list[dict[str, object]],
) -> list[dict[str, object]]:
    indexed: list[dict[str, object]] = []
    for ordinal, record in enumerate(records):
        signal = str(record.get("signal", "event"))
        aggregation = str(record.get("aggregation", "event"))
        if "window_start_s" in record:
            start_value = record["window_start_s"]
            end_value = record["window_end_s"]
            if not isinstance(start_value, (int, float)) or not isinstance(end_value, (int, float)):
                raise ValueError("evidence window boundaries must be numeric")
            start = float(start_value)
            end = float(end_value)
            window = f"w{int(start):03d}-{int(end):03d}"
        else:
            timestamp_value = record.get("timestamp_s", 0.0)
            if not isinstance(timestamp_value, (int, float)):
                raise ValueError("evidence timestamp must be numeric")
            start = float(timestamp_value)
            end = start
            window = f"t{int(start):03d}"
        content_hash = normalized_content_hash(record)
        identifier = evidence_id(
            episode_id=episode_id,
            source=source,
            window=window,
            signal=signal,
            aggregation=aggregation,
            content=record,
        )
        index_record: dict[str, object] = {
            "evidence_id": identifier,
            "episode_id": episode_id,
            "source": source,
            "signal": signal,
            "aggregation": aggregation,
            "window_start_s": round(start, 6),
            "window_end_s": round(end, 6),
            "window": record.get(
                "window",
                {
                    "start": timestamp_for_seconds(start),
                    "end": timestamp_for_seconds(end),
                },
            ),
            "unit": record.get("unit", "event"),
            "freshness_s": record.get("freshness_s", 0.0),
            "data_origin": "synthetic",
            "record_ordinal": ordinal,
            "content_hash": content_hash,
        }
        if "value" in record:
            index_record["value"] = record["value"]
        indexed.append(index_record)
    return indexed


def emit_episode_artifacts(
    result: SimulationResult,
    *,
    episode_id: str,
    output_dir: Path,
    telemetry: TelemetryProfile | None = None,
) -> EmittedArtifacts:
    """Write the five frozen observed JSONL files and an immutable evidence index."""

    profile = telemetry or TelemetryProfile()
    output_dir.mkdir(parents=True, exist_ok=True)
    request_records = [
        {
            **event.to_record(),
            **_record_metadata(
                source="request_events",
                signal="request",
                aggregation="event",
                start_s=event.submit_s,
                end_s=event.completion_s,
                freshness_s=0.0,
            ),
            "unit": "mixed",
            "value": {
                "queue_wait_s": round(event.queue_wait_s, 6),
                "ttft_s": round(event.ttft_s, 6),
                "itl_s": round(event.itl_s, 6),
                "status": event.status,
            },
        }
        for event in result.requests
    ]

    service_records: list[dict[str, object]] = []
    for check_s in range(5, int(result.duration_s) + 1, 5):
        start_s = max(0.0, check_s - 15.0)
        end_s = float(check_s)
        window = [event for event in result.requests if start_s <= event.submit_s < end_s]
        if not window:
            continue
        for signal in _SIGNAL_ORDER:
            if signal in profile.omitted_signals:
                continue
            record = _record_metadata(
                source="service_metrics",
                signal=signal,
                aggregation=_metric_aggregation(signal),
                start_s=start_s,
                end_s=end_s,
                freshness_s=999.0 if signal in profile.stale_signals else 0.0,
            )
            record["value"] = round(_metric_value(result, signal, start_s, end_s), 6)
            record["unit"] = _metric_unit(signal)
            service_records.append(record)

    infrastructure_records: list[dict[str, object]] = []
    for check_s in range(0, int(result.duration_s) + 1, 5):
        desired = result.desired_replicas_at(float(check_s))
        ready = result.ready_replicas_at(float(check_s))
        if profile.contradictory_replicas:
            ready = desired + 1
        infrastructure_records.append(
            {
                "observed_at": timestamp_for_seconds(float(check_s)),
                "timestamp_s": float(check_s),
                "desired_replicas": desired,
                "ready_replicas": ready,
                "revision": "sim-v1",
                "event_type": "replica_snapshot",
                "source": "infrastructure_events",
                "signal": "replicas",
                "aggregation": "snapshot",
                "unit": "replicas",
                "window_start_s": float(check_s),
                "window_end_s": float(check_s),
                "window_start": timestamp_for_seconds(float(check_s)),
                "window_end": timestamp_for_seconds(float(check_s)),
                "window": {
                    "start": timestamp_for_seconds(float(check_s)),
                    "end": timestamp_for_seconds(float(check_s)),
                },
                "value": {
                    "desired_replicas": desired,
                    "ready_replicas": ready,
                    "revision": "sim-v1",
                },
                "freshness_s": 999.0 if "replicas" in profile.stale_signals else 0.0,
                "data_origin": "synthetic",
            }
        )

    if profile.changes:
        change_records = [
            {
                "observed_at": timestamp_for_seconds(change.timestamp_s),
                "timestamp_s": round(change.timestamp_s, 6),
                "changed_fields": list(change.changed_fields),
                "actor_class": change.actor_class,
                "source": "change_events",
                "signal": "configuration_change",
                "aggregation": "event",
                "unit": "event",
                "window_start_s": round(change.timestamp_s, 6),
                "window_end_s": round(change.timestamp_s, 6),
                "window_start": timestamp_for_seconds(change.timestamp_s),
                "window_end": timestamp_for_seconds(change.timestamp_s),
                "window": {
                    "start": timestamp_for_seconds(change.timestamp_s),
                    "end": timestamp_for_seconds(change.timestamp_s),
                },
                "value": list(change.changed_fields),
                "freshness_s": 0.0,
                "data_origin": "synthetic",
            }
            for change in sorted(profile.changes, key=lambda item: item.timestamp_s)
        ]
    else:
        change_records = [
            {
                "query_status": "empty",
                "events": [],
                "signal": "configuration_change",
                "aggregation": "empty",
                "unit": "event",
                "freshness_s": 0.0,
                "source": "change_events",
                "window_start_s": 0.0,
                "window_end_s": result.duration_s,
                "window_start": timestamp_for_seconds(0.0),
                "window_end": timestamp_for_seconds(result.duration_s),
                "window": {
                    "start": timestamp_for_seconds(0.0),
                    "end": timestamp_for_seconds(result.duration_s),
                },
                "value": [],
                "data_origin": "synthetic",
            }
        ]

    records_by_file = {
        "request_events.jsonl": request_records,
        "service_metrics.jsonl": service_records,
        "infrastructure_events.jsonl": infrastructure_records,
        "change_events.jsonl": change_records,
    }
    index_records: list[dict[str, object]] = []
    for filename in (
        "request_events.jsonl",
        "service_metrics.jsonl",
        "infrastructure_events.jsonl",
        "change_events.jsonl",
    ):
        source = filename.removesuffix(".jsonl")
        index_records.extend(_index_records(episode_id, source, records_by_file[filename]))
    records_by_file["evidence_index.jsonl"] = index_records

    checksums: dict[str, str] = {}
    for filename, records in records_by_file.items():
        write_stable_jsonl(output_dir / filename, records)
        checksums[filename] = sha256_file(output_dir / filename)
    return EmittedArtifacts(checksums=checksums)
