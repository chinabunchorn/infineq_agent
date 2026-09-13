"""Deterministic discrete-event queue simulator for synthetic replay episodes."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from heapq import heappop, heappush
from random import Random
from typing import Literal

from infineq.simulator.serialization import timestamp_for_seconds


@dataclass(frozen=True, slots=True)
class RatePhase:
    """Half-open arrival-rate interval in requests per second."""

    start_s: float
    end_s: float
    rate_per_s: float

    def __post_init__(self) -> None:
        if self.start_s < 0 or self.end_s <= self.start_s or self.rate_per_s < 0:
            raise ValueError("rate phases require non-negative ordered bounds and rate")


@dataclass(frozen=True, slots=True)
class NumericWindow:
    """A numeric profile value active over a half-open time window."""

    start_s: float
    end_s: float
    value: float

    def __post_init__(self) -> None:
        if self.start_s < 0 or self.end_s <= self.start_s:
            raise ValueError("numeric windows require non-negative ordered bounds")


@dataclass(frozen=True, slots=True)
class ReplicaChange:
    """A simulator-only capacity or readiness change at an exact timestamp."""

    timestamp_s: float
    replicas: int
    ready_replicas: int | None = None
    revision: str = "sim-v1"

    def __post_init__(self) -> None:
        ready = self.replicas if self.ready_replicas is None else self.ready_replicas
        if self.timestamp_s < 0 or self.replicas < 0 or not 0 <= ready <= self.replicas:
            raise ValueError("replica changes require valid timestamp and ready counts")
        if not self.revision or ":" in self.revision:
            raise ValueError("revision must be a simple non-empty label")

    @property
    def effective_ready_replicas(self) -> int:
        """Return the ready count applied at this change."""

        return self.replicas if self.ready_replicas is None else self.ready_replicas


@dataclass(frozen=True, slots=True)
class SimulationConfig:
    """All numerical inputs to one deterministic replay."""

    duration_s: float
    workload: tuple[RatePhase, ...]
    seed: int = 0
    initial_replicas: int = 1
    slots_per_replica: int = 8
    input_tokens: int = 256
    output_tokens: int = 64
    jitter_fraction: float = 0.05
    base_itl_s: float = 0.05
    prefill_base_s: float = 0.10
    prefill_per_input_s: float = 0.00078
    replica_changes: tuple[ReplicaChange, ...] = ()
    itl_profile: tuple[NumericWindow, ...] = ()
    input_multiplier_profile: tuple[NumericWindow, ...] = ()
    error_rate_profile: tuple[NumericWindow, ...] = ()
    timeout_rate_profile: tuple[NumericWindow, ...] = ()
    deployment_revision: str = "sim-v1"

    def __post_init__(self) -> None:
        if self.duration_s <= 0:
            raise ValueError("simulation duration must be positive")
        if self.seed < 0 or self.initial_replicas <= 0 or self.slots_per_replica <= 0:
            raise ValueError("seed must be non-negative; replica and slot counts must be positive")
        if self.input_tokens <= 0 or self.output_tokens <= 0:
            raise ValueError("token counts must be positive")
        if not 0 <= self.jitter_fraction <= 0.05:
            raise ValueError("jitter must be between zero and five percent")
        if self.base_itl_s <= 0 or self.prefill_base_s < 0 or self.prefill_per_input_s < 0:
            raise ValueError("timing parameters must be non-negative with positive ITL")
        if not self.deployment_revision:
            raise ValueError("deployment revision is required")
        ordered_phases = tuple(sorted(self.workload, key=lambda item: item.start_s))
        if ordered_phases != self.workload:
            raise ValueError("workload phases must be ordered")
        if any(phase.end_s > self.duration_s for phase in self.workload):
            raise ValueError("workload phases must end within the episode")
        if any(
            left.end_s > right.start_s
            for left, right in zip(self.workload, self.workload[1:], strict=False)
        ):
            raise ValueError("workload phases cannot overlap")
        changes = tuple(sorted(self.replica_changes, key=lambda item: item.timestamp_s))
        if changes != self.replica_changes:
            raise ValueError("replica changes must be ordered")
        if any(change.timestamp_s > self.duration_s for change in changes):
            raise ValueError("replica changes must occur during the episode")


@dataclass(frozen=True, slots=True)
class RequestEvent:
    """Normalized lifecycle timing for one synthetic request."""

    request_id: str
    submit_s: float
    service_start_s: float
    first_token_s: float
    completion_s: float
    input_tokens: int
    output_tokens: int
    queue_wait_s: float
    prefill_s: float
    decode_s: float
    ttft_s: float
    itl_s: float
    end_to_end_s: float
    status: Literal["success", "error", "timeout"]
    replica_index: int
    sequence_slot: int

    def to_record(self) -> dict[str, object]:
        """Return path-free normalized output suitable for JSONL."""

        return {
            "request_id": self.request_id,
            "submitted_at": timestamp_for_seconds(self.submit_s),
            "service_started_at": timestamp_for_seconds(self.service_start_s),
            "first_token_at": timestamp_for_seconds(self.first_token_s),
            "completed_at": timestamp_for_seconds(self.completion_s),
            "submit_s": round(self.submit_s, 6),
            "service_start_s": round(self.service_start_s, 6),
            "first_token_s": round(self.first_token_s, 6),
            "completion_s": round(self.completion_s, 6),
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "queue_wait_s": round(self.queue_wait_s, 6),
            "prefill_s": round(self.prefill_s, 6),
            "decode_s": round(self.decode_s, 6),
            "ttft_s": round(self.ttft_s, 6),
            "itl_s": round(self.itl_s, 6),
            "end_to_end_s": round(self.end_to_end_s, 6),
            "queue_wait_ms": round(self.queue_wait_s * 1_000, 3),
            "ttft_ms": round(self.ttft_s * 1_000, 3),
            "itl_ms": round(self.itl_s * 1_000, 3),
            "end_to_end_ms": round(self.end_to_end_s * 1_000, 3),
            "status": self.status,
            "replica_index": self.replica_index,
            "sequence_slot": self.sequence_slot,
            "data_origin": "synthetic",
        }


@dataclass(frozen=True, slots=True)
class ReplicaSnapshot:
    """Observed desired/readiness state at a replay timestamp."""

    timestamp_s: float
    desired_replicas: int
    ready_replicas: int
    revision: str

    def to_record(self) -> dict[str, object]:
        """Return a normalized infrastructure record."""

        return {
            "observed_at": timestamp_for_seconds(self.timestamp_s),
            "timestamp_s": round(self.timestamp_s, 6),
            "desired_replicas": self.desired_replicas,
            "ready_replicas": self.ready_replicas,
            "revision": self.revision,
            "event_type": "replica_snapshot",
            "data_origin": "synthetic",
        }


@dataclass(frozen=True, slots=True)
class RecoveryAssessment:
    """Deterministic measurements for the frozen canonical recovery rule."""

    p95_ttft_ms: float
    max_queue_depth: int
    queue_depth_checks: tuple[int, ...]
    error_rate: float
    itl_median_ms: float
    baseline_itl_median_ms: float
    ready_replicas: int

    @property
    def criteria_met(self) -> bool:
        """Whether every frozen recovery condition is satisfied."""

        itl_ok = abs(self.itl_median_ms - self.baseline_itl_median_ms) <= (
            self.baseline_itl_median_ms * 0.10
        )
        return (
            self.p95_ttft_ms <= 2_000.0
            and all(depth <= 2 for depth in self.queue_depth_checks[-3:])
            and len(self.queue_depth_checks) >= 3
            and self.error_rate < 0.01
            and itl_ok
            and self.ready_replicas == 2
        )


@dataclass(frozen=True, slots=True)
class SimulationResult:
    """Immutable output of the event engine."""

    requests: tuple[RequestEvent, ...]
    replica_snapshots: tuple[ReplicaSnapshot, ...]
    duration_s: float

    def to_records(self) -> list[dict[str, object]]:
        """Return request records in stable FCFS/request order."""

        return [event.to_record() for event in self.requests]

    def queue_depth_at(self, timestamp_s: float) -> int:
        """Count submitted requests still waiting at a replay timestamp."""

        return sum(event.submit_s <= timestamp_s < event.service_start_s for event in self.requests)

    def ready_replicas_at(self, timestamp_s: float) -> int:
        """Return the last readiness snapshot at or before a timestamp."""

        snapshots = [
            snapshot for snapshot in self.replica_snapshots if snapshot.timestamp_s <= timestamp_s
        ]
        return snapshots[-1].ready_replicas if snapshots else 0

    def desired_replicas_at(self, timestamp_s: float) -> int:
        """Return the last desired replica count at or before a timestamp."""

        snapshots = [
            snapshot for snapshot in self.replica_snapshots if snapshot.timestamp_s <= timestamp_s
        ]
        return snapshots[-1].desired_replicas if snapshots else 0

    def recovery_assessment(self) -> RecoveryAssessment:
        """Measure the final 30 seconds against the frozen recovery rule."""

        final_start = max(0.0, self.duration_s - 30.0)
        baseline_events = [event for event in self.requests if event.submit_s < 60.0]
        final_events = [
            event for event in self.requests if final_start <= event.submit_s < self.duration_s
        ]
        if not final_events:
            raise ValueError("recovery window has no request observations")
        checks = tuple(
            self.queue_depth_at(timestamp)
            for timestamp in (final_start + 5, final_start + 10, final_start + 15)
        )
        return RecoveryAssessment(
            p95_ttft_ms=_percentile([event.ttft_s * 1_000 for event in final_events], 0.95),
            max_queue_depth=max(
                self.queue_depth_at(final_start + offset) for offset in (5, 10, 15, 20, 25)
            ),
            queue_depth_checks=checks,
            error_rate=sum(event.status != "success" for event in final_events) / len(final_events),
            itl_median_ms=_percentile([event.itl_s * 1_000 for event in final_events], 0.50),
            baseline_itl_median_ms=_percentile(
                [event.itl_s * 1_000 for event in baseline_events], 0.50
            ),
            ready_replicas=self.ready_replicas_at(self.duration_s),
        )


def _round_microseconds(value: Decimal) -> float:
    return float(value.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP))


def _profile_value(windows: tuple[NumericWindow, ...], timestamp_s: float, default: float) -> float:
    for window in windows:
        if window.start_s <= timestamp_s < window.end_s:
            return window.value
    return default


@dataclass(frozen=True, slots=True)
class _Arrival:
    request_id: str
    submit_s: float
    input_tokens: int
    output_tokens: int
    itl_s: float
    status: Literal["success", "error", "timeout"]


@dataclass(slots=True)
class _Slot:
    replica_index: int
    sequence_slot: int
    enabled: bool = True
    busy: bool = False


def _arrival_schedule(config: SimulationConfig) -> tuple[_Arrival, ...]:
    random = Random(config.seed)
    arrivals: list[_Arrival] = []
    request_number = 0
    for phase in config.workload:
        if phase.rate_per_s == 0:
            continue
        current = Decimal(str(phase.start_s))
        end = Decimal(str(min(phase.end_s, config.duration_s)))
        interval = Decimal("1") / Decimal(str(phase.rate_per_s))
        while current < end:
            submit_s = _round_microseconds(current)
            input_multiplier = _profile_value(config.input_multiplier_profile, submit_s, 1.0)
            input_jitter = 1.0 + random.uniform(-config.jitter_fraction, config.jitter_fraction)
            output_jitter = 1.0 + random.uniform(-config.jitter_fraction, config.jitter_fraction)
            input_tokens = max(1, round(config.input_tokens * input_multiplier * input_jitter))
            output_tokens = max(1, round(config.output_tokens * output_jitter))
            itl_multiplier = _profile_value(config.itl_profile, submit_s, 1.0)
            error_rate = _profile_value(config.error_rate_profile, submit_s, 0.0)
            timeout_rate = _profile_value(config.timeout_rate_profile, submit_s, 0.0)
            outcome_roll = random.random()
            status: Literal["success", "error", "timeout"] = "success"
            if outcome_roll < timeout_rate:
                status = "timeout"
            elif outcome_roll < timeout_rate + error_rate:
                status = "error"
            request_number += 1
            arrivals.append(
                _Arrival(
                    request_id=f"req-{request_number:06d}",
                    submit_s=submit_s,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    itl_s=config.base_itl_s * itl_multiplier,
                    status=status,
                )
            )
            current += interval
    return tuple(arrivals)


def simulate(config: SimulationConfig) -> SimulationResult:
    """Run one deterministic FCFS discrete-event simulation."""

    arrivals = _arrival_schedule(config)
    if not arrivals:
        initial = ReplicaSnapshot(
            0.0, config.initial_replicas, config.initial_replicas, config.deployment_revision
        )
        return SimulationResult((), (initial,), config.duration_s)

    slots = [
        _Slot(replica_index=replica, sequence_slot=sequence)
        for replica in range(config.initial_replicas)
        for sequence in range(config.slots_per_replica)
    ]
    waiting: deque[_Arrival] = deque()
    completed: dict[str, RequestEvent] = {}
    snapshots = [
        ReplicaSnapshot(
            timestamp_s=0.0,
            desired_replicas=config.initial_replicas,
            ready_replicas=config.initial_replicas,
            revision=config.deployment_revision,
        )
    ]
    desired_replicas = config.initial_replicas
    ready_replicas = config.initial_replicas
    event_heap: list[tuple[float, int, int, Literal["change", "completion", "arrival"], int]] = []
    sequence_number = 0
    for index, arrival in enumerate(arrivals):
        heappush(event_heap, (arrival.submit_s, 2, sequence_number, "arrival", index))
        sequence_number += 1
    for index, change in enumerate(config.replica_changes):
        heappush(event_heap, (change.timestamp_s, 0, sequence_number, "change", index))
        sequence_number += 1

    def assign_waiting(now: float) -> None:
        nonlocal sequence_number
        while waiting:
            available = next((slot for slot in slots if slot.enabled and not slot.busy), None)
            if available is None:
                return
            arrival = waiting.popleft()
            available.busy = True
            queue_wait = now - arrival.submit_s
            prefill = config.prefill_base_s + arrival.input_tokens * config.prefill_per_input_s
            decode = max(arrival.output_tokens - 1, 0) * arrival.itl_s
            completion = now + prefill + decode
            completed[arrival.request_id] = RequestEvent(
                request_id=arrival.request_id,
                submit_s=arrival.submit_s,
                service_start_s=now,
                first_token_s=now + prefill,
                completion_s=completion,
                input_tokens=arrival.input_tokens,
                output_tokens=arrival.output_tokens,
                queue_wait_s=queue_wait,
                prefill_s=prefill,
                decode_s=decode,
                ttft_s=queue_wait + prefill,
                itl_s=arrival.itl_s,
                end_to_end_s=completion - arrival.submit_s,
                status=arrival.status,
                replica_index=available.replica_index,
                sequence_slot=available.sequence_slot,
            )
            heappush(
                event_heap, (completion, 1, sequence_number, "completion", slots.index(available))
            )
            sequence_number += 1

    while event_heap:
        now, _priority, _sequence, kind, index = heappop(event_heap)
        if kind == "arrival":
            waiting.append(arrivals[index])
            assign_waiting(now)
        elif kind == "completion":
            slots[index].busy = False
            assign_waiting(now)
        else:
            change = config.replica_changes[index]
            desired_replicas = change.replicas
            ready_replicas = change.effective_ready_replicas
            required_slots = desired_replicas * config.slots_per_replica
            while len(slots) < required_slots:
                slot_number = len(slots)
                slots.append(
                    _Slot(
                        replica_index=slot_number // config.slots_per_replica,
                        sequence_slot=slot_number % config.slots_per_replica,
                    )
                )
            for slot in slots:
                slot.enabled = slot.replica_index < ready_replicas
            snapshots.append(
                ReplicaSnapshot(now, desired_replicas, ready_replicas, change.revision)
            )
            assign_waiting(now)

    ordered = tuple(completed[key] for key in sorted(completed))
    return SimulationResult(ordered, tuple(snapshots), config.duration_s)


def _percentile(values: list[float], quantile: float) -> float:
    if not values:
        raise ValueError("percentile requires observations")
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def nominal_capacity_per_second(*, replicas: int = 1) -> float:
    """Return the frozen nominal service capacity for a replica count."""

    occupancy = 0.10 + 256 * 0.00078 + (64 - 1) * 0.05
    return (8 * replicas) / occupancy


def canonical_simulation_config(
    *, seed: int = 1001, replica_changes: tuple[ReplicaChange, ...] = ()
) -> SimulationConfig:
    """Build the frozen queue episode configuration."""

    return SimulationConfig(
        duration_s=180.0,
        workload=(
            RatePhase(0.0, 60.0, 1.2),
            RatePhase(60.0, 120.0, 2.6),
            RatePhase(120.0, 180.0, 1.2),
        ),
        seed=seed,
        replica_changes=replica_changes,
    )


def run_canonical_episode(
    *, approved_at_s: float | None = None, seed: int = 1001
) -> SimulationResult:
    """Replay the canonical queue episode with optional approved scale-out."""

    changes = () if approved_at_s is None else (ReplicaChange(approved_at_s, replicas=2),)
    return simulate(canonical_simulation_config(seed=seed, replica_changes=changes))
