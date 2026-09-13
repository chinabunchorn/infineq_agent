"""Infineq's normalized exception hierarchy."""

from enum import StrEnum


class ErrorCode(StrEnum):
    """Stable machine-readable error codes used across component boundaries."""

    CONFIGURATION_ERROR = "configuration_error"
    SCHEMA_ERROR = "schema_error"
    DATA_MISSING = "data_missing"
    DATA_STALE = "data_stale"
    TOOL_TRANSPORT_ERROR = "tool_transport_error"
    TOOL_POLICY_DENIED = "tool_policy_denied"
    AGENT_TIMEOUT = "agent_timeout"
    VERIFICATION_BLOCKED = "verification_blocked"
    APPROVAL_MISMATCH = "approval_mismatch"
    RECOVERY_NOT_VERIFIED = "recovery_not_verified"


class InfineqError(Exception):
    """Base class for expected Infineq failures."""

    code: ErrorCode


class ConfigurationError(InfineqError):
    code = ErrorCode.CONFIGURATION_ERROR


class SchemaError(InfineqError):
    code = ErrorCode.SCHEMA_ERROR


class DataMissingError(InfineqError):
    code = ErrorCode.DATA_MISSING


class DataStaleError(InfineqError):
    code = ErrorCode.DATA_STALE


class ToolTransportError(InfineqError):
    code = ErrorCode.TOOL_TRANSPORT_ERROR


class ToolPolicyDeniedError(InfineqError):
    code = ErrorCode.TOOL_POLICY_DENIED


class AgentTimeoutError(InfineqError):
    code = ErrorCode.AGENT_TIMEOUT


class VerificationBlockedError(InfineqError):
    code = ErrorCode.VERIFICATION_BLOCKED


class ApprovalMismatchError(InfineqError):
    code = ErrorCode.APPROVAL_MISMATCH


class RecoveryNotVerifiedError(InfineqError):
    code = ErrorCode.RECOVERY_NOT_VERIFIED
