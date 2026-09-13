import pytest

from infineq.config import InfineqEnvironment, Settings
from infineq.errors import ConfigurationError


def test_missing_foundry_values_fail_only_when_foundry_is_requested(tmp_path) -> None:
    settings = Settings.from_mapping({}, project_root=tmp_path)

    assert settings.data_root == tmp_path / "data/infineq/v1"
    assert settings.runs_root == tmp_path / "data/infineq/v1/runs"

    with pytest.raises(ConfigurationError, match="Foundry configuration is incomplete"):
        settings.require_foundry()


def test_valid_foundry_configuration_is_typed_and_redacted_from_repr(tmp_path) -> None:
    endpoint = "https://infineq.services.ai.azure.com/api/projects/infineq-agentathon"
    settings = Settings.from_mapping(
        {
            "AZURE_AI_PROJECT_ENDPOINT": endpoint,
            "AZURE_AI_MODEL_DEPLOYMENT_NAME": "infineq-gpt-5-4-mini",
            "INFINEQ_ENV": "foundry",
            "INFINEQ_LOG_LEVEL": "DEBUG",
        },
        project_root=tmp_path,
    )

    foundry = settings.require_foundry()

    assert foundry.endpoint == endpoint
    assert foundry.model_deployment_name == "infineq-gpt-5-4-mini"
    assert settings.environment is InfineqEnvironment.FOUNDRY
    assert settings.log_level == "DEBUG"
    assert endpoint not in repr(settings)
    assert endpoint not in repr(foundry)


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://infineq.services.ai.azure.com/api/projects/infineq-agentathon",
        "https://user:password@infineq.services.ai.azure.com/api/projects/infineq-agentathon",
        "https://example.com/api/projects/infineq-agentathon",
        "https://infineq.services.ai.azure.com/",
        "https://infineq.services.ai.azure.com/api/projects/infineq-agentathon?key=secret",
        "https://infineq.services.ai.azure.com:notaport/api/projects/infineq-agentathon",
    ],
)
def test_invalid_foundry_endpoint_is_rejected_without_echoing_value(endpoint, tmp_path) -> None:
    settings = Settings.from_mapping(
        {
            "AZURE_AI_PROJECT_ENDPOINT": endpoint,
            "AZURE_AI_MODEL_DEPLOYMENT_NAME": "infineq-gpt-5-4-mini",
        },
        project_root=tmp_path,
    )

    with pytest.raises(ConfigurationError) as exc_info:
        settings.require_foundry()

    assert endpoint not in str(exc_info.value)


@pytest.mark.parametrize("deployment_name", ["", "bad deployment", "x" * 65])
def test_invalid_model_deployment_name_is_rejected(deployment_name, tmp_path) -> None:
    settings = Settings.from_mapping(
        {
            "AZURE_AI_PROJECT_ENDPOINT": (
                "https://infineq.services.ai.azure.com/api/projects/infineq-agentathon"
            ),
            "AZURE_AI_MODEL_DEPLOYMENT_NAME": deployment_name,
        },
        project_root=tmp_path,
    )

    with pytest.raises(ConfigurationError, match="deployment name is invalid"):
        settings.require_foundry()


@pytest.mark.parametrize(
    ("key", "value"),
    [("INFINEQ_ENV", "production"), ("INFINEQ_LOG_LEVEL", "TRACE")],
)
def test_invalid_local_setting_has_a_normalized_configuration_error(key, value, tmp_path) -> None:
    with pytest.raises(ConfigurationError, match="Invalid Infineq configuration"):
        Settings.from_mapping({key: value}, project_root=tmp_path)
