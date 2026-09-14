from dataclasses import replace
from pathlib import Path

from infineq.detection.quality import QualityDataset, QualityIssueCode, validate_data_quality
from infineq.evidence.store import EvidenceStore
from infineq.schemas.evidence import DataQualityStatus

PROJECT_ROOT = Path(__file__).parents[3]


def observed_dataset() -> QualityDataset:
    episode = EvidenceStore(project_root=PROJECT_ROOT).load_episode("ep-a91e7c")
    return QualityDataset.from_episode(episode)


def test_observed_episode_with_complete_quality_evidence_is_decision_ready() -> None:
    result = validate_data_quality(observed_dataset())

    assert result.data_quality.status is DataQualityStatus.GOOD
    assert result.data_quality.sample_count >= 20
    assert result.data_quality.freshness_seconds <= 10.0
    assert result.failures == ()
    assert result.decision_ready


def test_quality_rejects_an_unknown_schema_version() -> None:
    result = validate_data_quality(replace(observed_dataset(), schema_version="2.0"))

    assert any(failure.code is QualityIssueCode.SCHEMA_VERSION for failure in result.failures)
    assert result.data_quality.status is not DataQualityStatus.GOOD
    assert not result.decision_ready


def dataset_with_record_change(
    dataset: QualityDataset, source_name: str, ordinal: int, **changes: object
) -> QualityDataset:
    records = {
        name: tuple(dict(record) for record in source_records)
        for name, source_records in dataset.records.items()
    }
    records[source_name][ordinal].update(changes)
    return replace(dataset, records=records)


def test_quality_rejects_a_record_from_the_wrong_source() -> None:
    dataset = dataset_with_record_change(
        observed_dataset(), "service_metrics", 0, source="request_events"
    )

    result = validate_data_quality(dataset)

    assert any(
        failure.code is QualityIssueCode.SOURCE_EPISODE_IDENTITY for failure in result.failures
    )
    assert not result.decision_ready


def test_quality_rejects_non_monotonic_request_lifecycle_timestamps() -> None:
    dataset = dataset_with_record_change(observed_dataset(), "request_events", 0, completion_s=-1.0)

    result = validate_data_quality(dataset)

    assert any(failure.code is QualityIssueCode.TIMESTAMP_ORDERING for failure in result.failures)
    assert not result.decision_ready


def test_quality_rejects_a_unit_change_within_one_signal() -> None:
    dataset = dataset_with_record_change(observed_dataset(), "service_metrics", 0, unit="seconds")

    result = validate_data_quality(dataset)

    assert any(failure.code is QualityIssueCode.UNIT_CONSISTENCY for failure in result.failures)
    assert not result.decision_ready


def test_quality_rejects_freshness_above_the_ten_second_replay_limit() -> None:
    dataset = dataset_with_record_change(
        observed_dataset(), "service_metrics", 0, freshness_s=10.001
    )

    result = validate_data_quality(dataset)

    assert result.data_quality.freshness_seconds == 10.001
    assert any(failure.code is QualityIssueCode.FRESHNESS for failure in result.failures)
    assert not result.decision_ready


def test_quality_requires_at_least_twenty_request_samples() -> None:
    dataset = replace(
        observed_dataset(),
        records={
            **observed_dataset().records,
            "request_events": observed_dataset().records["request_events"][:19],
        },
    )

    result = validate_data_quality(dataset)

    assert result.data_quality.sample_count == 19
    assert any(failure.code is QualityIssueCode.SAMPLE_COUNT for failure in result.failures)
    assert not result.decision_ready


def test_quality_requires_the_frozen_request_labels() -> None:
    dataset = dataset_with_record_change(observed_dataset(), "request_events", 0)
    records = {
        name: tuple(dict(record) for record in source_records)
        for name, source_records in dataset.records.items()
    }
    records["request_events"][0].pop("status")
    dataset = replace(dataset, records=records)

    result = validate_data_quality(dataset)

    assert any(failure.code is QualityIssueCode.REQUIRED_LABELS for failure in result.failures)
    assert not result.decision_ready


def test_quality_rejects_an_explicit_counter_reset_marker() -> None:
    dataset = dataset_with_record_change(
        observed_dataset(), "service_metrics", 0, counter_reset=True
    )

    result = validate_data_quality(dataset)

    assert any(failure.code is QualityIssueCode.COUNTER_RESET for failure in result.failures)
    assert not result.decision_ready


def test_quality_rejects_ready_replicas_above_desired_replicas() -> None:
    dataset = dataset_with_record_change(
        observed_dataset(), "infrastructure_events", 0, ready_replicas=2
    )

    result = validate_data_quality(dataset)

    assert any(
        failure.code is QualityIssueCode.REPLICA_REVISION_STATE for failure in result.failures
    )
    assert not result.decision_ready


def test_quality_rejects_an_evidence_index_hash_mismatch() -> None:
    dataset = observed_dataset()
    index = [dict(record) for record in dataset.evidence_index]
    index[0]["content_hash"] = "0" * 64
    result = validate_data_quality(replace(dataset, evidence_index=tuple(index)))

    assert any(failure.code is QualityIssueCode.EVIDENCE_INDEX for failure in result.failures)
    assert not result.decision_ready


def test_quality_reports_an_omitted_required_signal_as_data_missing() -> None:
    dataset = observed_dataset()
    service_records = tuple(
        record for record in dataset.records["service_metrics"] if record.get("signal") != "itl"
    )
    service_index = tuple(
        dict(record)
        for record in dataset.evidence_index
        if not (record.get("source") == "service_metrics" and record.get("signal") == "itl")
    )
    ordinal = 0
    normalized_index = []
    for record in service_index:
        updated = dict(record)
        if updated.get("source") == "service_metrics":
            updated["record_ordinal"] = ordinal
            ordinal += 1
        normalized_index.append(updated)
    result = validate_data_quality(
        replace(
            dataset,
            records={**dataset.records, "service_metrics": service_records},
            evidence_index=tuple(normalized_index),
        )
    )

    assert any(failure.code is QualityIssueCode.DATA_MISSING for failure in result.failures)
    assert result.data_quality.sample_count > 0
    assert not result.decision_ready
