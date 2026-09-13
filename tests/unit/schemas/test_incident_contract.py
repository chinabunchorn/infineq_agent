from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from infineq.schemas.common import DataOrigin, TimeWindow
from infineq.schemas.evidence import DataQuality, DataQualityStatus, EvidenceRef, SignalObservation
from infineq.schemas.incident import DeploymentSnapshot, IncidentPacketV1, SLOStatus

NOW = datetime(2026, 9, 13, 3, 0, tzinfo=UTC)
INCIDENT_ID = "inc-7f31a9"


def make_window() -> TimeWindow:
    return TimeWindow(start=NOW - timedelta(seconds=15), end=NOW)


def make_evidence() -> EvidenceRef:
    return EvidenceRef(
        evidence_id="ev:inc-7f31a9:metrics:w15:ttft:p95:aa12bb34",
        incident_id=INCIDENT_ID,
        origin=DataOrigin.SYNTHETIC_REPLAY,
        source="service_metrics",
        observed_at=NOW,
    )


def make_packet() -> IncidentPacketV1:
    evidence = make_evidence()
    signal = SignalObservation(
        signal="ttft",
        value=1_250.0,
        unit="ms",
        aggregation="p95",
        window=make_window(),
        evidence=evidence,
    )
    return IncidentPacketV1(
        incident_id=INCIDENT_ID,
        service_id="svc-infineq-demo",
        detected_at=NOW,
        origin=DataOrigin.SYNTHETIC_REPLAY,
        baseline_window=TimeWindow(
            start=NOW - timedelta(seconds=60), end=NOW - timedelta(seconds=30)
        ),
        observation_window=make_window(),
        slo=SLOStatus(
            metric="ttft",
            threshold=2_000.0,
            unit="ms",
            violated=False,
            evidence_ids=(evidence.evidence_id,),
        ),
        signals=(signal,),
        deployment=DeploymentSnapshot(
            replicas=1,
            ready_replicas=1,
            revision="sim-v1",
            observed_at=NOW,
            evidence_ids=(evidence.evidence_id,),
        ),
        data_quality=DataQuality(
            status=DataQualityStatus.GOOD,
            sample_count=30,
            freshness_seconds=0.0,
        ),
        detector_version="detector-v1",
        evidence_refs=(evidence,),
    )


def test_incident_packet_accepts_complete_synthetic_evidence() -> None:
    packet = make_packet()

    assert packet.schema_version == "1.0"
    assert packet.evidence_refs[0].origin is DataOrigin.SYNTHETIC_REPLAY
    assert packet.model_dump(mode="json")["detected_at"].endswith("Z")


def test_boundary_models_reject_unknown_fields_and_naive_timestamps() -> None:
    with pytest.raises(ValidationError):
        EvidenceRef(
            evidence_id="ev:inc-7f31a9:metrics:w15:ttft:p95:aa12bb34",
            incident_id=INCIDENT_ID,
            origin=DataOrigin.SYNTHETIC_REPLAY,
            source="service_metrics",
            observed_at=datetime(2026, 9, 13, 3, 0),
            hidden_answer="queue_saturation",
        )


def test_deployment_rejects_more_ready_replicas_than_total_replicas() -> None:
    payload = make_packet().deployment.model_dump()
    payload["ready_replicas"] = 2

    with pytest.raises(ValidationError):
        DeploymentSnapshot.model_validate(payload)


def test_incident_packet_rejects_cross_incident_evidence() -> None:
    payload = make_packet().model_dump()
    payload["signals"][0]["evidence"]["incident_id"] = "inc-other-22"

    with pytest.raises(ValidationError):
        IncidentPacketV1.model_validate(payload)


def test_incident_packet_rejects_an_unindexed_evidence_id() -> None:
    payload = make_packet().model_dump()
    payload["slo"]["evidence_ids"] = ("ev:inc-7f31a9:metrics:w15:queue:max:ccccdddd",)

    with pytest.raises(ValidationError):
        IncidentPacketV1.model_validate(payload)
