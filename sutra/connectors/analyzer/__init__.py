"""Generic Environment & Connector Analyzer for SAMANVAYA · SŪTRA (Phase 7.4).

Provides a unified, provider-neutral verification pipeline for any registered
connector without provider-specific branching.
"""
from __future__ import annotations

from .models import (
    AnalysisCheckResult,
    AnalysisReport,
    CheckCategory,
    CheckStatus,
    CLIApplicabilityStatus,
    ClientApplicabilityStatus,
    UsageEvidenceState,
    VerificationState,
)
from .pipeline import ConnectorAnalyzer, analyze_connector
from .probes import CLIProbe, ClientProbe, EnvironmentProbe

__all__ = [
    # Core Pipeline & Entry Points
    "ConnectorAnalyzer",
    "analyze_connector",
    # Domain Models
    "AnalysisCheckResult",
    "AnalysisReport",
    "VerificationState",
    "CheckStatus",
    "CheckCategory",
    "UsageEvidenceState",
    "CLIApplicabilityStatus",
    "ClientApplicabilityStatus",
    # Probes
    "EnvironmentProbe",
    "CLIProbe",
    "ClientProbe",
]
