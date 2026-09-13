import json
from pathlib import Path

import pytest
from pydantic import BaseModel

from infineq.schemas.action import ActionPlanV1, HumanDecisionV1
from infineq.schemas.evidence import DataQuality, EvidenceRef, SignalObservation
from infineq.schemas.incident import IncidentPacketV1
from infineq.schemas.investigation import Hypothesis, InvestigationResultV1
from infineq.schemas.recovery import RecoveryResultV1
from infineq.schemas.verification import VerificationResultV1

SNAPSHOT_ROOT = Path(__file__).parents[2] / "fixtures/schema_snapshots"
SCHEMA_MODELS: tuple[type[BaseModel], ...] = (
    EvidenceRef,
    SignalObservation,
    DataQuality,
    IncidentPacketV1,
    Hypothesis,
    InvestigationResultV1,
    VerificationResultV1,
    ActionPlanV1,
    HumanDecisionV1,
    RecoveryResultV1,
)


@pytest.mark.parametrize("model", SCHEMA_MODELS, ids=lambda model: model.__name__)
def test_json_schema_matches_reviewed_snapshot(model: type[BaseModel]) -> None:
    snapshot = SNAPSHOT_ROOT / f"{model.__name__}.json"

    assert snapshot.exists(), f"missing reviewed schema snapshot: {snapshot.name}"
    assert json.loads(snapshot.read_text()) == model.model_json_schema()
