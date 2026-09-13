"""Runtime configuration with delayed validation of external services."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from enum import StrEnum
from pathlib import Path
from urllib.parse import urlparse

from infineq.errors import ConfigurationError


class InfineqEnvironment(StrEnum):
    """Supported execution environments."""

    LOCAL = "local"
    TEST = "test"
    FOUNDRY = "foundry"


_LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})


@dataclass(frozen=True, slots=True)
class FoundrySettings:
    """Validated settings required to call Microsoft Foundry."""

    endpoint: str = dataclass_field(repr=False)
    model_deployment_name: str = dataclass_field(repr=False)


@dataclass(frozen=True, slots=True)
class Settings:
    """Infineq settings that remain usable without cloud configuration."""

    data_root: Path
    runs_root: Path
    environment: InfineqEnvironment = InfineqEnvironment.LOCAL
    log_level: str = "INFO"
    foundry_endpoint: str | None = dataclass_field(default=None, repr=False)
    foundry_model_deployment_name: str | None = dataclass_field(default=None, repr=False)

    @classmethod
    def from_mapping(
        cls,
        values: Mapping[str, str],
        *,
        project_root: Path,
    ) -> Settings:
        """Build settings without requiring Foundry for deterministic work."""

        try:
            environment = InfineqEnvironment(values.get("INFINEQ_ENV", "local").lower())
        except ValueError as exc:
            raise ConfigurationError("Invalid Infineq configuration") from exc
        log_level = values.get("INFINEQ_LOG_LEVEL", "INFO").upper()
        if log_level not in _LOG_LEVELS:
            raise ConfigurationError("Invalid Infineq configuration")

        data_root = (project_root / values.get("INFINEQ_DATA_ROOT", "data/infineq/v1")).resolve()
        runs_root = (
            project_root / values.get("INFINEQ_RUNS_ROOT", "data/infineq/v1/runs")
        ).resolve()
        return cls(
            data_root=data_root,
            runs_root=runs_root,
            environment=environment,
            log_level=log_level,
            foundry_endpoint=values.get("AZURE_AI_PROJECT_ENDPOINT") or None,
            foundry_model_deployment_name=values.get("AZURE_AI_MODEL_DEPLOYMENT_NAME"),
        )

    def require_foundry(self) -> FoundrySettings:
        """Return Foundry settings or fail only when cloud access is requested."""

        if self.foundry_endpoint is None or self.foundry_model_deployment_name is None:
            raise ConfigurationError("Foundry configuration is incomplete")
        parsed = urlparse(self.foundry_endpoint)
        path_parts = parsed.path.strip("/").split("/")
        try:
            port = parsed.port
        except ValueError:
            port = -1
        valid_endpoint = (
            parsed.scheme == "https"
            and parsed.hostname is not None
            and parsed.hostname.endswith(".services.ai.azure.com")
            and parsed.username is None
            and parsed.password is None
            and port in (None, 443)
            and not parsed.query
            and not parsed.fragment
            and len(path_parts) == 3
            and path_parts[:2] == ["api", "projects"]
            and bool(path_parts[2])
        )
        if not valid_endpoint:
            raise ConfigurationError("Foundry project endpoint is invalid")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", self.foundry_model_deployment_name):
            raise ConfigurationError("Foundry model deployment name is invalid")
        return FoundrySettings(
            endpoint=self.foundry_endpoint,
            model_deployment_name=self.foundry_model_deployment_name,
        )
