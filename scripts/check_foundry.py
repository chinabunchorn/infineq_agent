"""Run a safe Microsoft Foundry model connectivity check."""

from __future__ import annotations

import json
import os
from pathlib import Path

from dotenv import load_dotenv

from infineq.config import Settings
from infineq.errors import InfineqError
from infineq.foundry.client import AzureFoundryModelClient, run_model_smoke

ROOT = Path(__file__).parents[1]


def main() -> int:
    load_dotenv(ROOT / ".env")
    try:
        settings = Settings.from_mapping(os.environ, project_root=ROOT)
        foundry = settings.require_foundry()
        with AzureFoundryModelClient.from_settings(foundry) as client:
            result = run_model_smoke(
                client,
                deployment_name=foundry.model_deployment_name,
            )
    except InfineqError as exc:
        print(json.dumps({"success": False, "error": exc.code.value}))
        return 1

    print(json.dumps(result.to_public_record(), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
