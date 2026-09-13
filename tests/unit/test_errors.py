import pytest

from infineq.errors import (
    AgentTimeoutError,
    ApprovalMismatchError,
    ConfigurationError,
    DataMissingError,
    DataStaleError,
    ErrorCode,
    RecoveryNotVerifiedError,
    SchemaError,
    ToolPolicyDeniedError,
    ToolTransportError,
    VerificationBlockedError,
)


@pytest.mark.parametrize(
    ("error_type", "code"),
    [
        (ConfigurationError, ErrorCode.CONFIGURATION_ERROR),
        (SchemaError, ErrorCode.SCHEMA_ERROR),
        (DataMissingError, ErrorCode.DATA_MISSING),
        (DataStaleError, ErrorCode.DATA_STALE),
        (ToolTransportError, ErrorCode.TOOL_TRANSPORT_ERROR),
        (ToolPolicyDeniedError, ErrorCode.TOOL_POLICY_DENIED),
        (AgentTimeoutError, ErrorCode.AGENT_TIMEOUT),
        (VerificationBlockedError, ErrorCode.VERIFICATION_BLOCKED),
        (ApprovalMismatchError, ErrorCode.APPROVAL_MISMATCH),
        (RecoveryNotVerifiedError, ErrorCode.RECOVERY_NOT_VERIFIED),
    ],
)
def test_expected_failure_has_a_stable_machine_code(error_type, code) -> None:
    error = error_type("safe summary")

    assert error.code is code
    assert str(error) == "safe summary"
