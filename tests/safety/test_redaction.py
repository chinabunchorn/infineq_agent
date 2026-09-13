import pytest

from infineq.security.redaction import REDACTED, redact_mapping, redact_text


@pytest.mark.parametrize(
    ("raw", "secret"),
    [
        ('api_key="sk-super-secret-value"', "sk-super-secret-value"),
        ('client_secret="client-secret-value"', "client-secret-value"),
        ('access_token="access-token-value"', "access-token-value"),
        ('refresh_token="refresh-token-value"', "refresh-token-value"),
        ("Authorization: Bearer eyJhbGciOi.secret.signature", "eyJhbGciOi.secret.signature"),
        (
            "AccountName=demo;AccountKey=base64secret==;EndpointSuffix=core.windows.net",
            "base64secret==",
        ),
        (
            "/subscriptions/12345678-1234-1234-1234-123456789abc/resourceGroups/demo",
            "12345678-1234-1234-1234-123456789abc",
        ),
        ("operator@example.com", "operator@example.com"),
        (
            '{"prompt":"private customer request","completion":"private answer"}',
            "private customer request",
        ),
    ],
)
def test_redact_text_removes_sensitive_values(raw: str, secret: str) -> None:
    redacted = redact_text(raw)

    assert secret not in redacted
    assert REDACTED in redacted


def test_redact_mapping_handles_sensitive_keys_and_nested_values() -> None:
    raw = {
        "api_key": "sk-secret-value",
        "message": "Contact operator@example.com",
        "nested": {"prompt": "customer payload", "status": "ok"},
    }

    redacted = redact_mapping(raw)

    assert redacted["api_key"] == REDACTED
    assert "operator@example.com" not in redacted["message"]
    assert redacted["nested"]["prompt"] == REDACTED
    assert redacted["nested"]["status"] == "ok"


def test_redact_mapping_handles_oauth_credential_keys() -> None:
    raw = {
        "client_secret": "client-secret-value",
        "access_token": "access-token-value",
        "refresh_token": "refresh-token-value",
    }

    redacted = redact_mapping(raw)

    assert redacted == {
        "client_secret": REDACTED,
        "access_token": REDACTED,
        "refresh_token": REDACTED,
    }
