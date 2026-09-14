"""Deterministic detector over the fixed, read-only replay evidence store."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from math import isclose
from typing import Protocol

from infineq.detection.policy_v1 import (
    EVALUATION_INTERVAL_S,
    TRAILING_WINDOW_S,
    PolicyDecision,
    PolicyObservation,
    evaluate_policy,
)
from infineq.detection.quality import QualityDataset, QualityResult, validate_data_quality
from infineq.evidence.store import EvidenceIndexRecord, ObservedEpisode
from infineq.schemas.common import DataOrigin, TimeWindow
from infineq.schemas.evidence import EvidenceRef, SignalObservation
from infineq.schemas.incident import (
    DeploymentSnapshot,
    IncidentPacketV1,
    SLOStatus,
)
from infineq.simulator.serialization import timestamp_for_seconds

POLICY_VERSION = "policy-v1"
DEFAULT_SERVICE_ID = "svc-infineq-demo"

JsonRecord = Mapping[str, object]


class EpisodeStore(Protocol):
    """Minimal read-only store boundary used by the detector."""

    def load_episode(self, episode_id: str) -> ObservedEpisode:
        """Load one opaque observed episode."""

        ...


@dataclass(frozen=True, slots=True)
class DetectorEvaluation:
    """One frozen five-second policy evaluation, without causal labels."""

    end_s: float
    observation_start_s: float
    request_count: int
    ttft_p95_ms: float | None
    error_rate: float | None
    corroborating_signals: tuple[str, ...]
    decision: PolicyDecision


@dataclass(frozen=True, slots=True)
class DetectionRun:
    """Detector output plus measured timing evidence for one episode."""

    episode_id: str
    packet: IncidentPacketV1 | None
    data_quality: QualityResult
    evaluations: tuple[DetectorEvaluation, ...]
    detector_crossing_s: float | None
    slo_crossing_s: float | None
    slo_lead_time_s: float | None

    @property
    def decision(self) -> str:
        """Return a neutral detector disposition, never a fault label."""

        if not self.data_quality.decision_ready:
            return "abstain"
        return "incident" if self.packet is not None else "no_incident"


@dataclass(frozen=True, slots=True)
class _EvidenceContext:
    episode: ObservedEpisode
    by_source_ordinal: dict[tuple[str, int], EvidenceIndexRecord]
    service_by_signal_end: dict[tuple[str, float], tuple[JsonRecord, EvidenceIndexRecord]]
    infrastructure: tuple[tuple[JsonRecord, EvidenceIndexRecord], ...]
    changes: tuple[tuple[JsonRecord, EvidenceIndexRecord], ...]


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if number == number and number not in {float("inf"), float("-inf")} else None


def _record_end(record: JsonRecord) -> float | None:
    return _number(record.get("window_end_s", record.get("timestamp_s")))


def _record_start(record: JsonRecord) -> float | None:
    return _number(record.get("window_start_s", record.get("timestamp_s")))


def _matches(value: float | None, expected: float) -> bool:
    return value is not None and isclose(value, expected, rel_tol=0.0, abs_tol=1e-6)


def _replay_datetime(seconds: float) -> datetime:
    return datetime.fromisoformat(timestamp_for_seconds(seconds).replace("Z", "+00:00")).astimezone(
        UTC
    )


def _window(start_s: float, end_s: float) -> TimeWindow:
    return TimeWindow(start=_replay_datetime(start_s), end=_replay_datetime(end_s))


def _build_context(episode: ObservedEpisode) -> _EvidenceContext:
    indexed = {(entry.source, entry.record_ordinal): entry for entry in episode.evidence_index}
    service_by_signal_end: dict[tuple[str, float], tuple[JsonRecord, EvidenceIndexRecord]] = {}
    for ordinal, record in enumerate(episode.service_metrics):
        entry = indexed[("service_metrics", ordinal)]
        signal = record.get("signal")
        end_s = _record_end(record)
        if isinstance(signal, str) and end_s is not None:
            service_by_signal_end[(signal, end_s)] = (record, entry)
    infrastructure = tuple(
        (record, indexed[("infrastructure_events", ordinal)])
        for ordinal, record in enumerate(episode.infrastructure_events)
    )
    changes = tuple(
        (record, indexed[("change_events", ordinal)])
        for ordinal, record in enumerate(episode.change_events)
    )
    return _EvidenceContext(
        episode=episode,
        by_source_ordinal=indexed,
        service_by_signal_end=service_by_signal_end,
        infrastructure=infrastructure,
        changes=changes,
    )


def _service_record(
    context: _EvidenceContext,
    signal: str,
    *,
    end_s: float,
    start_s: float,
) -> tuple[JsonRecord, EvidenceIndexRecord] | None:
    candidate = context.service_by_signal_end.get((signal, end_s))
    if candidate is None:
        return None
    record, _entry = candidate
    if not _matches(_record_start(record), start_s):
        return None
    return candidate


def _request_count(
    episode: ObservedEpisode,
    *,
    start_s: float,
    end_s: float,
) -> int:
    return len(_requests_in_window(episode, start_s=start_s, end_s=end_s))


def _requests_in_window(
    episode: ObservedEpisode,
    *,
    start_s: float,
    end_s: float,
) -> tuple[tuple[int, JsonRecord], ...]:
    selected: list[tuple[int, JsonRecord]] = []
    for ordinal, record in enumerate(episode.requests):
        timestamp = _number(record.get("submit_s"))
        if timestamp is not None and start_s <= timestamp < end_s:
            selected.append((ordinal, record))
    return tuple(selected)


def _latest_infrastructure(
    context: _EvidenceContext, *, end_s: float
) -> tuple[JsonRecord, EvidenceIndexRecord] | None:
    selected = [
        item
        for item in context.infrastructure
        if (_number(item[0].get("timestamp_s")) or -1.0) <= end_s
    ]
    return selected[-1] if selected else None


def _baseline_value(
    context: _EvidenceContext,
    signal: str,
    *,
    end_s: float,
) -> float | None:
    start_s = max(0.0, end_s - TRAILING_WINDOW_S)
    candidate = _service_record(context, signal, end_s=end_s, start_s=start_s)
    if candidate is None:
        return None
    return _number(candidate[0].get("value"))


def _corroborating_signals(
    context: _EvidenceContext,
    *,
    start_s: float,
    end_s: float,
) -> tuple[str, ...]:
    signals: list[str] = []

    def add(signal: str) -> None:
        if signal not in signals:
            signals.append(signal)

    def current_value(signal: str) -> float | None:
        candidate = _service_record(context, signal, end_s=end_s, start_s=start_s)
        return None if candidate is None else _number(candidate[0].get("value"))

    queue_depth = current_value("queue_depth")
    queue_wait = current_value("queue_wait")
    if (queue_depth is not None and queue_depth > 0.0) or (
        queue_wait is not None and queue_wait > 0.0
    ):
        if queue_depth is not None and queue_depth > 0.0:
            add("queue_depth")
        if queue_wait is not None and queue_wait > 0.0:
            add("queue_wait")

    itl = current_value("itl")
    baseline_itl = _baseline_value(context, "itl", end_s=start_s)
    if (
        itl is not None
        and baseline_itl is not None
        and baseline_itl != 0.0
        and itl > baseline_itl * 1.20
    ):
        add("itl")

    input_tokens = current_value("input_tokens")
    baseline_input = _baseline_value(context, "input_tokens", end_s=start_s)
    prefill = current_value("prefill")
    baseline_prefill = _baseline_value(context, "prefill", end_s=start_s)
    if (
        input_tokens is not None
        and baseline_input is not None
        and baseline_input != 0.0
        and input_tokens > baseline_input * 1.20
    ):
        add("input_tokens")
    elif (
        prefill is not None
        and baseline_prefill is not None
        and baseline_prefill != 0.0
        and prefill > baseline_prefill * 1.20
    ):
        add("prefill")

    infrastructure = _latest_infrastructure(context, end_s=end_s)
    if infrastructure is not None:
        infrastructure_record = infrastructure[0]
        desired = _number(infrastructure_record.get("desired_replicas"))
        ready = _number(infrastructure_record.get("ready_replicas"))
        if desired is not None and ready is not None and ready < desired:
            add("ready_replicas")

    if any(
        (_number(record.get("timestamp_s")) or -1.0) >= start_s
        and (_number(record.get("timestamp_s")) or -1.0) < end_s
        for record, _entry in context.changes
    ):
        add("restart_event")

    if any(
        record.get("status") not in {None, "success"}
        for _ordinal, record in _requests_in_window(context.episode, start_s=start_s, end_s=end_s)
    ):
        add("request_outcome")

    return tuple(signals)


def _observation(
    context: _EvidenceContext,
    *,
    end_s: float,
) -> PolicyObservation:
    start_s = max(0.0, end_s - TRAILING_WINDOW_S)
    ttft_record = _service_record(context, "ttft", end_s=end_s, start_s=start_s)
    error_record = _service_record(context, "error_rate", end_s=end_s, start_s=start_s)
    corroborating = list(_corroborating_signals(context, start_s=start_s, end_s=end_s))
    return PolicyObservation(
        end_s=end_s,
        request_count=_request_count(context.episode, start_s=start_s, end_s=end_s),
        ttft_p95_ms=None if ttft_record is None else _number(ttft_record[0].get("value")),
        error_rate=None if error_record is None else _number(error_record[0].get("value")),
        corroborating_signals=tuple(corroborating),
    )


def _evaluation_ends(episode: ObservedEpisode) -> tuple[float, ...]:
    duration = int(episode.manifest.duration_s)
    return tuple(float(seconds) for seconds in range(5, duration + 1, int(EVALUATION_INTERVAL_S)))


def _evidence_ref(
    entry: EvidenceIndexRecord,
    *,
    incident_id: str,
) -> EvidenceRef:
    return EvidenceRef(
        evidence_id=entry.evidence_id,
        incident_id=incident_id,
        origin=DataOrigin.SYNTHETIC_REPLAY,
        source=entry.source,
        observed_at=_replay_datetime(entry.window_end_s),
    )


def _value_from_record(record: JsonRecord) -> float:
    value = _number(record.get("value"))
    if value is None:
        raise ValueError("detector measurement is not numeric")
    return value


def _measurement(
    record: JsonRecord,
    entry: EvidenceIndexRecord,
    *,
    incident_id: str,
    observation_window: TimeWindow,
    signal: str | None = None,
    value: float | None = None,
    unit: str | None = None,
    aggregation: str | None = None,
) -> tuple[SignalObservation, EvidenceRef]:
    evidence = _evidence_ref(entry, incident_id=incident_id)
    measurement = SignalObservation(
        signal=signal or str(record.get("signal")),
        value=_value_from_record(record) if value is None else value,
        unit=unit or str(record.get("unit")),
        aggregation=aggregation or str(record.get("aggregation")),
        window=observation_window,
        evidence=evidence,
    )
    return measurement, evidence


def _packet(
    context: _EvidenceContext,
    *,
    trace: DetectorEvaluation,
    quality: QualityResult,
    service_id: str,
) -> IncidentPacketV1:
    episode = context.episode
    episode_id = episode.manifest.episode_id
    end_s = trace.end_s
    start_s = trace.observation_start_s
    observation_window = _window(start_s, end_s)
    baseline_end = start_s
    baseline_start = max(0.0, baseline_end - TRAILING_WINDOW_S)
    baseline_window = _window(baseline_start, baseline_end)

    measurements: list[SignalObservation] = []
    references: list[EvidenceRef] = []
    seen_evidence: set[str] = set()

    def add_measurement(
        record: JsonRecord,
        entry: EvidenceIndexRecord,
        *,
        signal: str | None = None,
        value: float | None = None,
        unit: str | None = None,
        aggregation: str | None = None,
    ) -> EvidenceRef:
        measurement, evidence = _measurement(
            record,
            entry,
            incident_id=episode_id,
            observation_window=observation_window,
            signal=signal,
            value=value,
            unit=unit,
            aggregation=aggregation,
        )
        if evidence.evidence_id not in seen_evidence:
            measurements.append(measurement)
            references.append(evidence)
            seen_evidence.add(evidence.evidence_id)
        return evidence

    primary_signal = trace.decision.primary_signal
    if primary_signal == "error_rate":
        primary = _service_record(context, "error_rate", end_s=end_s, start_s=start_s)
    else:
        primary = _service_record(context, "ttft", end_s=end_s, start_s=start_s)
    if primary is None:
        raise ValueError("triggered evaluation has no primary evidence")
    primary_ref = add_measurement(*primary)

    for signal in trace.corroborating_signals:
        if signal in {"request_outcome"}:
            candidates = [
                (ordinal, record)
                for ordinal, record in _requests_in_window(episode, start_s=start_s, end_s=end_s)
                if record.get("status") not in {None, "success"}
            ]
            if not candidates:
                continue
            ordinal, record = candidates[0]
            entry = context.by_source_ordinal[("request_events", ordinal)]
            add_measurement(
                record,
                entry,
                signal="request_outcome",
                value=float(
                    sum(
                        item.get("status") not in {None, "success"}
                        for _item_ordinal, item in _requests_in_window(
                            episode, start_s=start_s, end_s=end_s
                        )
                    )
                ),
                unit="requests",
                aggregation="count",
            )
        elif signal == "restart_event":
            change = next(
                (
                    item
                    for item in reversed(context.changes)
                    if start_s <= (_number(item[0].get("timestamp_s")) or -1.0) < end_s
                ),
                None,
            )
            if change is not None:
                add_measurement(
                    change[0],
                    change[1],
                    signal="restart_event",
                    value=1.0,
                    unit="event",
                    aggregation="event",
                )
        elif signal == "ready_replicas":
            infrastructure = _latest_infrastructure(context, end_s=end_s)
            if infrastructure is not None:
                add_measurement(
                    infrastructure[0],
                    infrastructure[1],
                    signal="ready_replicas",
                    value=_number(infrastructure[0].get("ready_replicas")) or 0.0,
                    unit="replicas",
                    aggregation="gauge",
                )
        else:
            candidate = _service_record(context, signal, end_s=end_s, start_s=start_s)
            if candidate is not None:
                add_measurement(*candidate)

    for signal in ("queue_depth", "queue_wait", "itl", "input_tokens", "prefill", "error_rate"):
        candidate = _service_record(context, signal, end_s=end_s, start_s=start_s)
        if candidate is not None:
            add_measurement(*candidate)

    infrastructure = _latest_infrastructure(context, end_s=end_s)
    if infrastructure is None:
        raise ValueError("triggered evaluation has no deployment evidence")
    deployment_record, deployment_entry = infrastructure
    deployment_ref = _evidence_ref(deployment_entry, incident_id=episode_id)
    if deployment_ref.evidence_id not in seen_evidence:
        references.append(deployment_ref)
        seen_evidence.add(deployment_ref.evidence_id)
    desired = _number(deployment_record.get("desired_replicas"))
    ready = _number(deployment_record.get("ready_replicas"))
    if desired is None or ready is None:
        raise ValueError("deployment evidence has no replica counts")
    deployment = DeploymentSnapshot(
        replicas=int(desired),
        ready_replicas=int(ready),
        revision=str(deployment_record.get("revision")),
        observed_at=_replay_datetime(_number(deployment_record.get("timestamp_s")) or end_s),
        evidence_ids=(deployment_ref.evidence_id,),
    )

    ttft_record = _service_record(context, "ttft", end_s=end_s, start_s=start_s)
    ttft_value = None if ttft_record is None else _number(ttft_record[0].get("value"))
    slo_threshold = float(episode.manifest.service.ttft_slo_ms)
    slo = SLOStatus(
        metric="ttft",
        threshold=slo_threshold,
        unit="ms",
        violated=ttft_value is not None and ttft_value >= slo_threshold,
        evidence_ids=(primary_ref.evidence_id,),
    )
    return IncidentPacketV1(
        incident_id=episode_id,
        service_id=service_id,
        detected_at=_replay_datetime(end_s),
        origin=DataOrigin.SYNTHETIC_REPLAY,
        baseline_window=baseline_window,
        observation_window=observation_window,
        slo=slo,
        signals=tuple(measurements),
        deployment=deployment,
        data_quality=quality.data_quality,
        detector_version=POLICY_VERSION,
        evidence_refs=tuple(references),
    )


class Detector:
    """Evaluate policy-v1 against one opaque episode through a read-only store."""

    def __init__(self, *, store: EpisodeStore, service_id: str = DEFAULT_SERVICE_ID) -> None:
        self._store = store
        self._service_id = service_id

    def run(self, episode_id: str) -> DetectionRun:
        """Return a packet only after all frozen gates and signals pass."""

        episode = self._store.load_episode(episode_id)
        quality = validate_data_quality(episode_to_dataset(episode))
        if not quality.decision_ready:
            return DetectionRun(
                episode_id=episode_id,
                packet=None,
                data_quality=quality,
                evaluations=(),
                detector_crossing_s=None,
                slo_crossing_s=None,
                slo_lead_time_s=None,
            )

        context = _build_context(episode)
        evaluations: list[DetectorEvaluation] = []
        previous: PolicyObservation | None = None
        slo_crossing: float | None = None
        triggered: DetectorEvaluation | None = None
        slo_threshold = float(episode.manifest.service.ttft_slo_ms)
        for end_s in _evaluation_ends(episode):
            observation = _observation(context, end_s=end_s)
            if (
                slo_crossing is None
                and observation.ttft_p95_ms is not None
                and observation.ttft_p95_ms >= slo_threshold
                and observation.request_count >= 20
            ):
                slo_crossing = end_s
            decision = evaluate_policy(observation, previous=previous)
            evaluation = DetectorEvaluation(
                end_s=end_s,
                observation_start_s=max(0.0, end_s - TRAILING_WINDOW_S),
                request_count=observation.request_count,
                ttft_p95_ms=observation.ttft_p95_ms,
                error_rate=observation.error_rate,
                corroborating_signals=observation.corroborating_signals,
                decision=decision,
            )
            evaluations.append(evaluation)
            if triggered is None and decision.triggered:
                triggered = evaluation
            previous = observation

        packet = (
            None
            if triggered is None
            else _packet(
                context,
                trace=triggered,
                quality=quality,
                service_id=self._service_id,
            )
        )
        lead_time = (
            None if triggered is None or slo_crossing is None else slo_crossing - triggered.end_s
        )
        return DetectionRun(
            episode_id=episode_id,
            packet=packet,
            data_quality=quality,
            evaluations=tuple(evaluations),
            detector_crossing_s=None if triggered is None else triggered.end_s,
            slo_crossing_s=slo_crossing,
            slo_lead_time_s=lead_time,
        )

    def detect(self, episode_id: str) -> IncidentPacketV1 | None:
        """Return only the IncidentPacketV1, or abstain without one."""

        return self.run(episode_id).packet


def episode_to_dataset(episode: ObservedEpisode) -> QualityDataset:
    """Adapt an observed episode to the quality validator without path data."""

    return QualityDataset.from_episode(episode)


def run_detection(
    store: EpisodeStore,
    episode_id: str,
    *,
    service_id: str = DEFAULT_SERVICE_ID,
) -> DetectionRun:
    """Run policy-v1 and retain measured detector/SLO timing evidence."""

    return Detector(store=store, service_id=service_id).run(episode_id)


def detect_episode(
    store: EpisodeStore,
    episode_id: str,
    *,
    service_id: str = DEFAULT_SERVICE_ID,
) -> IncidentPacketV1 | None:
    """Convenience API returning only the typed incident packet."""

    return Detector(store=store, service_id=service_id).detect(episode_id)


__all__ = [
    "DEFAULT_SERVICE_ID",
    "POLICY_VERSION",
    "DetectionRun",
    "Detector",
    "DetectorEvaluation",
    "detect_episode",
    "episode_to_dataset",
    "run_detection",
]
