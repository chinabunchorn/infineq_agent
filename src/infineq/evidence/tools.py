"""Application-executed, allow-listed evidence tools."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta

from pydantic import ValidationError

from infineq.detection.detector import Detector
from infineq.errors import DataMissingError, ToolPolicyDeniedError
from infineq.evidence.audit import (
    AgentRole,
    AuditBoundary,
    AuditRecorder,
    ToolAuditRecord,
    audited_tool,
)
from infineq.evidence.runbooks import RunbookCatalog
from infineq.evidence.store import EvidenceIndexRecord, EvidenceStore
from infineq.evidence.tool_schemas import (
    DeploymentSnapshotResponse,
    DryRunActionPlan,
    EvidenceBatchResponse,
    EvidenceRecord,
    GetDeploymentSnapshotRequest,
    GetEvidenceRequest,
    GetIncidentPacketRequest,
    GetPolicyRequest,
    GetRequestSamplesRequest,
    GetSignalWindowRequest,
    PolicyRef,
    PolicyResponse,
    PrepareActionPlanRequest,
    RequestSample,
    RequestSamplesResponse,
    RunbookSearchResponse,
    RunbookSearchStatus,
    SearchRunbookRequest,
    SignalEnum,
    SignalWindowResponse,
)
from infineq.schemas.action import ActionTarget, ActionType
from infineq.schemas.incident import DeploymentSnapshot, IncidentPacketV1

JsonRecord = Mapping[str, object]


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if number == number and number not in {float("inf"), float("-inf")} else None


def _integer(record: JsonRecord, name: str) -> int | None:
    value = record.get(name)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _record_start(record: JsonRecord) -> float | None:
    return _number(record.get("window_start_s", record.get("timestamp_s")))


def _record_end(record: JsonRecord) -> float | None:
    return _number(record.get("window_end_s", record.get("timestamp_s")))


def _observed_at(record: JsonRecord, entry: EvidenceIndexRecord) -> datetime:
    value = record.get("observed_at") or entry.window.get("end")
    if not isinstance(value, str):
        raise DataMissingError("evidence timestamp is unavailable")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise DataMissingError("evidence timestamp is unavailable") from error


def _window_seconds(
    packet: IncidentPacketV1, window: str, generated_at: datetime
) -> tuple[float, float]:
    selected = packet.baseline_window if window == "baseline" else packet.observation_window
    return (
        (selected.start - generated_at).total_seconds(),
        (selected.end - generated_at).total_seconds(),
    )


def _evidence_record(
    record: JsonRecord,
    entry: EvidenceIndexRecord,
    *,
    incident_id: str,
    signal: SignalEnum,
) -> EvidenceRecord:
    start_s = _record_start(record)
    end_s = _record_end(record)
    if start_s is None or end_s is None:
        raise DataMissingError("evidence interval is unavailable")
    try:
        return EvidenceRecord(
            evidence_id=entry.evidence_id,
            incident_id=incident_id,
            source=entry.source,
            signal=signal,
            aggregation=entry.aggregation,
            unit=entry.unit,
            value=entry.value,
            window_start_s=start_s,
            window_end_s=end_s,
            freshness_s=entry.freshness_s,
            observed_at=_observed_at(record, entry),
        )
    except ValidationError:
        raise DataMissingError("evidence is unavailable") from None


def _get_evidence(store: EvidenceStore, request: GetEvidenceRequest) -> EvidenceBatchResponse:
    """Resolve a bounded, same-episode evidence batch for either read-only role."""

    episode_ids: set[str] = set()
    for evidence_id in request.evidence_ids:
        parts = evidence_id.split(":")
        if len(parts) != 7 or parts[0] != "ev":
            raise ToolPolicyDeniedError("evidence IDs are not allow-listed")
        episode_ids.add(parts[1])
    if len(episode_ids) != 1:
        raise ToolPolicyDeniedError("evidence IDs must belong to one episode")
    incident_id = next(iter(episode_ids))
    episode = store.load_episode(incident_id)
    records_by_source: dict[str, tuple[JsonRecord, ...]] = {
        "request_events": episode.requests,
        "service_metrics": episode.service_metrics,
        "infrastructure_events": episode.infrastructure_events,
        "change_events": episode.change_events,
    }
    evidence: list[EvidenceRecord] = []
    for evidence_id in request.evidence_ids:
        entry = store.get_evidence(incident_id, evidence_id)
        source_records = records_by_source.get(entry.source)
        if source_records is None or entry.record_ordinal >= len(source_records):
            raise DataMissingError("requested evidence is unavailable")
        try:
            signal = SignalEnum(entry.signal)
        except ValueError as error:
            raise DataMissingError("requested evidence is unavailable") from error
        evidence.append(
            _evidence_record(
                source_records[entry.record_ordinal],
                entry,
                incident_id=incident_id,
                signal=signal,
            )
        )
    return EvidenceBatchResponse(incident_id=incident_id, evidence=tuple(evidence))


class InvestigatorTools:
    """The Investigator's strictly read-only tool boundary."""

    def __init__(
        self,
        *,
        store: EvidenceStore,
        detector: Detector | None = None,
        runbooks: RunbookCatalog | None = None,
        audit_recorder: AuditRecorder | None = None,
    ) -> None:
        self._store = store
        self._detector = detector or Detector(store=store)
        self._runbooks = runbooks or RunbookCatalog(project_root=store.path_policy.project_root)
        self._audit_boundary = AuditBoundary(
            recorder=audit_recorder if audit_recorder is not None else AuditRecorder(),
            agent_role=AgentRole.INVESTIGATOR,
        )

    @property
    def audit_records(self) -> tuple[ToolAuditRecord, ...]:
        """Return immutable audit records for this tool surface."""

        return self._audit_boundary.records

    @audited_tool("get_incident_packet")
    def get_incident_packet(self, request: GetIncidentPacketRequest) -> IncidentPacketV1:
        """Return a detector packet and never raw observed files."""

        result = self._detector.run(request.incident_id)
        if result.packet is None:
            raise DataMissingError("incident packet is unavailable")
        return result.packet

    @audited_tool("get_signal_window")
    def get_signal_window(self, request: GetSignalWindowRequest) -> SignalWindowResponse:
        """Return normalized signal records inside a packet's fixed window."""

        packet = self.get_incident_packet(GetIncidentPacketRequest(incident_id=request.incident_id))
        episode = self._store.load_episode(request.incident_id)
        start_s, end_s = _window_seconds(
            packet,
            request.window_enum.value,
            episode.manifest.generated_at,
        )
        evidence: list[EvidenceRecord] = []
        for ordinal, record in enumerate(episode.service_metrics):
            if record.get("signal") != request.signal_enum.value:
                continue
            record_start = _record_start(record)
            record_end = _record_end(record)
            if (
                record_start is None
                or record_end is None
                or record_start < start_s
                or record_end > end_s
                or record_start >= record_end
            ):
                continue
            entry = next(
                (
                    item
                    for item in episode.evidence_index
                    if item.source == "service_metrics" and item.record_ordinal == ordinal
                ),
                None,
            )
            if entry is not None:
                evidence.append(
                    _evidence_record(
                        record,
                        entry,
                        incident_id=request.incident_id,
                        signal=request.signal_enum,
                    )
                )
        evidence.sort(key=lambda item: (item.window_start_s, item.window_end_s, item.evidence_id))
        return SignalWindowResponse(
            incident_id=request.incident_id,
            signal=request.signal_enum,
            window=request.window_enum,
            evidence=tuple(evidence[:20]),
        )

    @audited_tool("get_request_samples")
    def get_request_samples(self, request: GetRequestSamplesRequest) -> RequestSamplesResponse:
        """Return a deterministic prefix of at most twenty safe request samples."""

        packet = self.get_incident_packet(GetIncidentPacketRequest(incident_id=request.incident_id))
        episode = self._store.load_episode(request.incident_id)
        start_s, end_s = _window_seconds(
            packet,
            request.window_enum.value,
            episode.manifest.generated_at,
        )
        samples: list[RequestSample] = []
        for ordinal, record in enumerate(episode.requests):
            submit_s = _number(record.get("submit_s"))
            if submit_s is None or not start_s <= submit_s < end_s:
                continue
            request_id = record.get("request_id")
            status = record.get("status")
            if not isinstance(request_id, str) or not isinstance(status, str):
                raise DataMissingError("request sample is unavailable")
            try:
                from infineq.evidence.tool_schemas import RequestStatus

                request_status = RequestStatus(status)
            except ValueError as error:
                raise DataMissingError("request sample is unavailable") from error
            entry = next(
                (
                    item
                    for item in episode.evidence_index
                    if item.source == "request_events" and item.record_ordinal == ordinal
                ),
                None,
            )
            if entry is None:
                raise DataMissingError("request sample evidence is unavailable")

            samples.append(
                RequestSample(
                    evidence_id=entry.evidence_id,
                    incident_id=request.incident_id,
                    request_id=request_id,
                    submit_s=submit_s,
                    completion_s=_number(record.get("completion_s")),
                    status=request_status,
                    ttft_ms=_number(record.get("ttft_ms")),
                    itl_ms=_number(record.get("itl_ms")),
                    queue_wait_ms=_number(record.get("queue_wait_ms")),
                    input_tokens=_integer(record, "input_tokens"),
                    output_tokens=_integer(record, "output_tokens"),
                    replica_index=_integer(record, "replica_index"),
                )
            )
        samples.sort(key=lambda item: (item.submit_s, item.request_id))
        return RequestSamplesResponse(
            incident_id=request.incident_id,
            window=request.window_enum,
            samples=tuple(samples[: request.limit]),
        )

    @audited_tool("get_deployment_snapshot")
    def get_deployment_snapshot(
        self, request: GetDeploymentSnapshotRequest
    ) -> DeploymentSnapshotResponse:
        """Return the latest immutable deployment snapshot at a fixed boundary."""

        packet = self.get_incident_packet(GetIncidentPacketRequest(incident_id=request.incident_id))
        episode = self._store.load_episode(request.incident_id)
        _start_s, end_s = _window_seconds(
            packet,
            request.window_enum.value,
            episode.manifest.generated_at,
        )
        candidates: list[tuple[float, int, JsonRecord]] = []
        for ordinal, record in enumerate(episode.infrastructure_events):
            timestamp_s = _number(record.get("timestamp_s"))
            if timestamp_s is not None and timestamp_s <= end_s:
                candidates.append((timestamp_s, ordinal, record))
        if not candidates:
            raise DataMissingError("deployment snapshot is unavailable")
        _timestamp_s, selected_ordinal, selected_record = max(
            candidates, key=lambda item: (item[0], item[1])
        )
        desired = selected_record.get("desired_replicas")
        ready = selected_record.get("ready_replicas")
        revision = selected_record.get("revision")
        if (
            not isinstance(desired, int)
            or isinstance(desired, bool)
            or not isinstance(ready, int)
            or isinstance(ready, bool)
            or not isinstance(revision, str)
        ):
            raise DataMissingError("deployment snapshot is unavailable")
        entry = next(
            (
                item
                for item in episode.evidence_index
                if item.source == "infrastructure_events"
                and item.record_ordinal == selected_ordinal
            ),
            None,
        )
        if entry is None:
            raise DataMissingError("deployment snapshot evidence is unavailable")
        snapshot = DeploymentSnapshot(
            replicas=desired,
            ready_replicas=ready,
            revision=revision,
            observed_at=_observed_at(selected_record, entry),
            evidence_ids=(entry.evidence_id,),
        )
        return DeploymentSnapshotResponse(
            incident_id=request.incident_id,
            window=request.window_enum,
            snapshot=snapshot,
            evidence_id=entry.evidence_id,
        )

    @audited_tool("get_evidence")
    def get_evidence(self, request: GetEvidenceRequest) -> EvidenceBatchResponse:
        """Resolve no more than twenty evidence IDs from one episode."""

        return _get_evidence(self._store, request)

    @audited_tool("search_runbook")
    def search_runbook(self, request: SearchRunbookRequest) -> RunbookSearchResponse:
        """Return up to three curated chunks selected only by symptom enum."""

        results = self._runbooks.search(
            query_enum=request.query_enum,
            top_k=request.top_k,
        )
        return RunbookSearchResponse(
            query_enum=request.query_enum,
            top_k=request.top_k,
            status=RunbookSearchStatus.AVAILABLE,
            placeholder=False,
            results=results,
            notice="curated runbook evidence",
        )

    @audited_tool("prepare_action_plan")
    def prepare_action_plan(self, request: PrepareActionPlanRequest) -> DryRunActionPlan:
        """Create a simulator-only preview; this method has no execution path."""

        if request.action_type is not ActionType.SIMULATED_SCALE_OUT:
            raise ToolPolicyDeniedError("action type is not allow-listed")
        packet = self.get_incident_packet(GetIncidentPacketRequest(incident_id=request.incident_id))
        if packet.deployment.replicas != 1 or packet.deployment.ready_replicas != 1:
            raise ToolPolicyDeniedError("simulator action boundary is not satisfied")
        return DryRunActionPlan(
            plan_id=f"plan-{request.incident_id}-simulated-scale-out",
            incident_id=request.incident_id,
            action_type=request.action_type,
            target=ActionTarget(kind="simulator", service_id=packet.service_id),
            from_replicas=1,
            to_replicas=2,
            created_at=packet.detected_at,
            expires_at=packet.detected_at + timedelta(minutes=5),
            expected_effect="Simulate one additional serving replica.",
            risks=("The observed signal may have a different cause.",),
            verification_criteria=("Re-measure the fixed latency and queue signals.",),
            policy_ref="policy-infineq-v1",
        )


class VerifierTools:
    """The verifier's intentionally minimal, read-only tool surface."""

    def __init__(
        self,
        *,
        store: EvidenceStore,
        audit_recorder: AuditRecorder | None = None,
    ) -> None:
        self._store = store
        self._audit_boundary = AuditBoundary(
            recorder=audit_recorder if audit_recorder is not None else AuditRecorder(),
            agent_role=AgentRole.VERIFIER,
        )

    @property
    def audit_records(self) -> tuple[ToolAuditRecord, ...]:
        """Return immutable audit records for this tool surface."""

        return self._audit_boundary.records

    @audited_tool("get_policy")
    def get_policy(self, request: GetPolicyRequest) -> PolicyResponse:
        """Return only the fixed v1 policy; no runtime or oracle data is read."""

        if request.policy_ref is not PolicyRef.INFINEQ_V1:
            raise ToolPolicyDeniedError("policy reference is not allow-listed")
        return PolicyResponse(
            policy_ref=PolicyRef.INFINEQ_V1,
            max_evidence_ids=20,
            max_request_samples=20,
            max_runbook_results=3,
            allowed_action_types=(ActionType.SIMULATED_SCALE_OUT,),
            allowed_target_kind="simulator",
            from_replicas=1,
            to_replicas=2,
            action_ttl_seconds=300,
            requires_human_approval=True,
            dry_run_only=True,
            execution_allowed=False,
        )

    @audited_tool("get_evidence")
    def get_evidence(self, request: GetEvidenceRequest) -> EvidenceBatchResponse:
        """Resolve bounded same-episode evidence without investigator tools."""

        return _get_evidence(self._store, request)


__all__ = ["InvestigatorTools", "VerifierTools"]
