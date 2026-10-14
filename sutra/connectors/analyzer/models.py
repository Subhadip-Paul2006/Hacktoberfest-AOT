"""Domain models and structured result types for Generic Environment & Connector Analyzer.

Defines provider-neutral verification states, check categories, check results,
and comprehensive analysis reports.
"""
from __future__ import annotations

import enum
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from sutra.connectors.models import (
    ConnectorHealth,
    ConnectorType,
    sanitize_metadata,
)
from sutra.redaction import redact_secrets


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class VerificationState(str, enum.Enum):
    """Overall verified state of a connector produced by evidence aggregation."""
    DISCOVERED = "DISCOVERED"                    # Registered, basic descriptor available
    CONFIGURED = "CONFIGURED"                    # Required config/env present, but not connected
    PARTIALLY_VERIFIED = "PARTIALLY_VERIFIED"    # Config present, some probes passed/unknown
    VERIFIED = "VERIFIED"                        # Config, connectivity, capabilities verified
    DEGRADED = "DEGRADED"                        # Functional with degraded health or warnings
    FAILED = "FAILED"                            # Critical check failed (missing config/unreachable)
    UNKNOWN = "UNKNOWN"                          # Insufficient evidence to determine state


class CheckStatus(str, enum.Enum):
    """Deterministic outcome of an individual diagnostic check."""
    PASS = "PASS"        # Check succeeded with positive evidence
    FAIL = "FAIL"        # Check conclusively failed
    UNKNOWN = "UNKNOWN"  # Insufficient evidence was available to verify
    SKIPPED = "SKIPPED"  # Check skipped due to configuration or prerequisites


class CheckCategory(str, enum.Enum):
    """Broad categories for pipeline diagnostic checks."""
    DISCOVERY = "DISCOVERY"
    CONFIGURATION = "CONFIGURATION"
    PARAMETERS = "PARAMETERS"
    DEPENDENCIES = "DEPENDENCIES"
    CONNECTION = "CONNECTION"
    AUTHENTICATION = "AUTHENTICATION"
    CAPABILITIES = "CAPABILITIES"
    CLIENT_APPLICABILITY = "CLIENT_APPLICABILITY"
    CLI_APPLICABILITY = "CLI_APPLICABILITY"
    HEALTH = "HEALTH"
    LATENCY = "LATENCY"
    USAGE_EVIDENCE = "USAGE_EVIDENCE"


class UsageEvidenceState(str, enum.Enum):
    """Observed runtime usage evidence from ActivityStore records."""
    OBSERVED_IN_USE = "OBSERVED_IN_USE"  # Recent tool calls or operations recorded
    NOT_OBSERVED = "NOT_OBSERVED"        # Verified store exists, but zero calls found
    UNKNOWN = "UNKNOWN"                  # No store or external activity cannot be verified


class CLIApplicabilityStatus(str, enum.Enum):
    """Evaluated state of a declared CLI binary dependency."""
    CLI_PRESENT = "CLI_PRESENT"
    CLI_EXECUTABLE = "CLI_EXECUTABLE"
    CLI_AUTH_UNKNOWN = "CLI_AUTH_UNKNOWN"
    CLI_AUTHENTICATED = "CLI_AUTHENTICATED"
    CLI_NOT_FOUND = "CLI_NOT_FOUND"


class ClientApplicabilityStatus(str, enum.Enum):
    """Evaluated state of a developer client/IDE environment."""
    CLIENT_DETECTED = "CLIENT_DETECTED"
    CONFIG_PRESENT = "CONFIG_PRESENT"
    CONNECTOR_CONFIGURED_IN_CLIENT = "CONNECTOR_CONFIGURED_IN_CLIENT"
    CONNECTOR_SUPPORTED_BY_CLIENT = "CONNECTOR_SUPPORTED_BY_CLIENT"
    NOT_DETECTED = "NOT_DETECTED"


# ---------------------------------------------------------------------------
# Check Result Model
# ---------------------------------------------------------------------------

@dataclass
class AnalysisCheckResult:
    """Individual structured result for a single verification check."""
    check_id: str
    category: CheckCategory
    status: CheckStatus
    message: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)
    latency_ms: Optional[float] = None
    started_at: float = field(default_factory=time.time)
    completed_at: float = field(default_factory=time.time)
    error_code: Optional[str] = None
    confidence: float = 1.0
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.message = redact_secrets(self.message) if self.message else ""
        self.evidence = sanitize_metadata(self.evidence)
        self.metadata = sanitize_metadata(self.metadata)
        if self.latency_ms is None and self.completed_at >= self.started_at:
            self.latency_ms = round((self.completed_at - self.started_at) * 1000, 2)


# ---------------------------------------------------------------------------
# Analysis Report Model
# ---------------------------------------------------------------------------

@dataclass
class AnalysisReport:
    """Comprehensive, provider-neutral verification report for a connector."""
    connector_id: str
    provider: str
    connector_type: ConnectorType
    transport: str
    environment_id: str
    client_context: dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)
    checks: list[AnalysisCheckResult] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)
    verification_state: VerificationState = VerificationState.UNKNOWN
    usage_state: UsageEvidenceState = UsageEvidenceState.UNKNOWN
    overall_health: Optional[ConnectorHealth] = None
    latency_summary: dict[str, float] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    request_id: str = field(default_factory=lambda: f"req_an_{uuid.uuid4().hex[:10]}")

    def __post_init__(self) -> None:
        self.client_context = sanitize_metadata(self.client_context)
        self.evidence = sanitize_metadata(self.evidence)
        self.warnings = [redact_secrets(w) for w in self.warnings]
        self.errors = [redact_secrets(e) for e in self.errors]

    @property
    def total_checks(self) -> int:
        """Total number of checks executed."""
        return len(self.checks)

    @property
    def checks_passed(self) -> int:
        """Total number of checks with PASS status."""
        return sum(1 for c in self.checks if c.status == CheckStatus.PASS)

    @property
    def checks_failed(self) -> int:
        """Total number of checks with FAIL status."""
        return sum(1 for c in self.checks if c.status == CheckStatus.FAIL)

    @property
    def checks_unknown(self) -> int:
        """Total number of checks with UNKNOWN status."""
        return sum(1 for c in self.checks if c.status == CheckStatus.UNKNOWN)

    @property
    def checks_skipped(self) -> int:
        """Total number of checks with SKIPPED status."""
        return sum(1 for c in self.checks if c.status == CheckStatus.SKIPPED)

    @property
    def total_latency_ms(self) -> float:
        """Total execution latency in milliseconds."""
        return self.latency_summary.get("total_pipeline_ms", 0.0)

    @property
    def is_healthy(self) -> bool:
        """Whether the overall health probe succeeded."""
        return bool(self.overall_health.healthy) if self.overall_health is not None else False

    def get_check(self, check_id: str) -> Optional[AnalysisCheckResult]:
        """Find a check result by its check_id."""
        for c in self.checks:
            if c.check_id == check_id:
                return c
        return None

    def get_checks_by_category(self, category: CheckCategory) -> list[AnalysisCheckResult]:
        """Filter check results belonging to a category."""
        return [c for c in self.checks if c.category == category]

    def has_check_status(self, category: CheckCategory, status: CheckStatus) -> bool:
        """Check if any check in a category has a given status."""
        return any(c.status == status for c in self.checks if c.category == category)

    def to_dict(self) -> dict[str, Any]:
        """Return a sanitized, JSON-serializable representation of the report."""
        return {
            "connector_id": self.connector_id,
            "provider": self.provider,
            "connector_type": self.connector_type.value if hasattr(self.connector_type, "value") else str(self.connector_type),
            "transport": self.transport,
            "environment_id": self.environment_id,
            "timestamp": self.timestamp,
            "request_id": self.request_id,
            "verification_state": self.verification_state.value,
            "usage_state": self.usage_state.value,
            "warnings": list(self.warnings),
            "errors": list(self.errors),
            "latency_summary": dict(self.latency_summary),
            "evidence": dict(self.evidence),
            "client_context": dict(self.client_context),
            "overall_health": {
                "healthy": self.overall_health.healthy,
                "status": self.overall_health.status.value,
                "message": self.overall_health.message,
                "latency_ms": self.overall_health.latency_ms,
            } if self.overall_health else None,
            "checks": [
                {
                    "check_id": c.check_id,
                    "category": c.category.value,
                    "status": c.status.value,
                    "message": c.message,
                    "latency_ms": c.latency_ms,
                    "evidence": c.evidence,
                    "error_code": c.error_code,
                    "confidence": c.confidence,
                }
                for c in self.checks
            ],
        }
