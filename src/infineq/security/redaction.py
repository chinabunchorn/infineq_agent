"""Conservative redaction for logs, errors, and trace attributes."""

from __future__ import annotations

import re
from collections.abc import Mapping

REDACTED = "[REDACTED]"

_SENSITIVE_KEYS = frozenset(
    {
        "access_token",
        "api_key",
        "apikey",
        "authorization",
        "client_secret",
        "completion",
        "connection_string",
        "password",
        "prompt",
        "secret",
        "tenant_id",
        "token",
        "refresh_token",
    }
)
_QUOTED_VALUE = re.compile(
    r"(?i)([\"']?(?:access[_-]?token|api[_-]?key|authorization|client[_-]?secret|completion|"
    r"connection[_-]?string|password|prompt|refresh[_-]?token|secret|tenant[_-]?id|token)"
    r"[\"']?\s*[:=]\s*)([\"'])(.*?)(\2)"
)
_BEARER = re.compile(r"(?i)(\b(?:authorization\s*:\s*)?bearer\s+)[A-Za-z0-9._~+/=-]+")
_ACCOUNT_KEY = re.compile(r"(?i)(\bAccountKey\s*=\s*)[^;\s]+")
_UNQUOTED_VALUE = re.compile(
    r"(?i)(\b(?:access[_-]?token|api[_-]?key|client[_-]?secret|connection[_-]?string|"
    r"password|refresh[_-]?token|secret|tenant[_-]?id|token)\s*=\s*)[^\s;,]+"
)
_AZURE_SCOPE_ID = re.compile(
    r"(?i)(/(?:subscriptions|tenants)/)[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12}"
)
_EMAIL = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")


def redact_text(value: str) -> str:
    """Remove common credentials, account identifiers, and content bodies."""

    redacted = _QUOTED_VALUE.sub(
        lambda match: f"{match.group(1)}{match.group(2)}{REDACTED}{match.group(4)}", value
    )
    redacted = _BEARER.sub(lambda match: f"{match.group(1)}{REDACTED}", redacted)
    redacted = _ACCOUNT_KEY.sub(lambda match: f"{match.group(1)}{REDACTED}", redacted)
    redacted = _UNQUOTED_VALUE.sub(lambda match: f"{match.group(1)}{REDACTED}", redacted)
    redacted = _AZURE_SCOPE_ID.sub(lambda match: f"{match.group(1)}{REDACTED}", redacted)
    return _EMAIL.sub(REDACTED, redacted)


def _redact_value(value: object, *, sensitive: bool = False) -> object:
    if sensitive:
        return REDACTED
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, Mapping):
        return {
            str(key): _redact_value(item, sensitive=str(key).lower() in _SENSITIVE_KEYS)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_value(item) for item in value)
    return value


def redact_mapping(value: Mapping[str, object]) -> dict[str, object]:
    """Recursively redact a mapping while preserving nonsensitive structure."""

    return {
        str(key): _redact_value(item, sensitive=str(key).lower() in _SENSITIVE_KEYS)
        for key, item in value.items()
    }
