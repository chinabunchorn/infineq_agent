from infineq.evidence.ids import evidence_id, normalized_content_hash


def test_evidence_id_uses_frozen_segments_and_content_hash_prefix() -> None:
    record = {"value": 1.25, "unit": "ms", "source": "service_metrics"}

    identifier = evidence_id(
        episode_id="ep-opaque1",
        source="service_metrics",
        window="w000-015",
        signal="ttft",
        aggregation="p95",
        content=record,
    )

    assert identifier.startswith("ev:ep-opaque1:service_metrics:w000-015:ttft:p95:")
    assert identifier[-8:] == normalized_content_hash(record)[:8]


def test_evidence_content_hash_is_independent_of_filesystem_context() -> None:
    content = {"signal": "queue_depth", "value": 3, "unit": "requests"}

    assert normalized_content_hash(content) == normalized_content_hash(
        {**content, "path": "/tmp/a"}
    )


def test_evidence_id_rejects_path_and_delimiter_injection() -> None:
    try:
        evidence_id(
            episode_id="ep-opaque1",
            source="../hidden_oracles",
            window="w000-015",
            signal="ttft",
            aggregation="p95",
            content={},
        )
    except ValueError as exc:
        assert "evidence" in str(exc)
    else:
        raise AssertionError("unsafe evidence source should be rejected")
