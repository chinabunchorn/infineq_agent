"""Generate reviewed JSON Schema snapshots for Infineq boundary contracts."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel

from infineq.schemas.action import ActionPlanV1, HumanDecisionV1
from infineq.schemas.evidence import DataQuality, EvidenceRef, SignalObservation
from infineq.schemas.incident import IncidentPacketV1
from infineq.schemas.investigation import Hypothesis, InvestigationResultV1
from infineq.schemas.recovery import RecoveryResultV1
from infineq.schemas.verification import VerificationResultV1

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


def main() -> None:
    root = Path(__file__).parents[1] / "tests/fixtures/schema_snapshots"
    root.mkdir(parents=True, exist_ok=True)
    for model in SCHEMA_MODELS:
        rendered = json.dumps(model.model_json_schema(), indent=2, sort_keys=True) + "\n"
        (root / f"{model.__name__}.json").write_text(rendered)
    print(f"wrote {len(SCHEMA_MODELS)} schema snapshots to {root}")


if __name__ == "__main__":
    main()
