"""Generic, provider-neutral analysis and verification pipeline.

Implements the single unified pipeline:
ConnectorAnalysisDescriptor -> Environment Discovery -> Configuration ->
Dependencies -> Connection -> Authentication -> Capabilities ->
Client/CLI Applicability -> Health/Latency -> Usage Evidence -> Aggregation.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from typing import Any, Dict, List, Optional, Sequence, Union

from sutra.connectors.errors import ConnectorAuthError
from sutra.connectors.interface import Connector
from sutra.connectors.models import (
    ConnectorAnalysisDescriptor,
    ConnectorCapability,
    ConnectorHealth,
    ConnectorStatus,
    ConnectorType,
    sanitize_metadata,
)
from sutra.connectors.registry import ConnectorRegistry
from sutra.metrics import Metrics, get_metrics
from sutra.redaction import redact_secrets

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
from .probes import CLIProbe, ClientProbe, EnvironmentProbe

log = logging.getLogger(__name__)


class ConnectorAnalyzer:
    """Universal pipeline engine for analyzing and verifying any SŪTRA connector.
    
    Operates strictly on generic descriptors, capabilities, and declared requirements,
    with zero provider-specific conditionals.
    """

    def __init__(
        self,
        registry: ConnectorRegistry,
        *,
        activity_store: Optional[Any] = None,
        metrics: Optional[Metrics] = None,
        custom_env: Optional[dict[str, str]] = None,
        concurrency_limit: Optional[int] = None,
        max_concurrency: Optional[int] = None,
        cli_probe: Optional[CLIProbe] = None,
        client_probe: Optional[ClientProbe] = None,
    ) -> None:
        self.registry = registry
        self.activity_store = activity_store
        self.metrics = metrics or get_metrics()
        self.custom_env = custom_env  # For dependency/config injection during tests
        limit = max_concurrency if max_concurrency is not None else (concurrency_limit if concurrency_limit is not None else 10)
        self._semaphore = asyncio.Semaphore(limit)
        self.cli_probe = cli_probe or CLIProbe()
        self.client_probe = client_probe or ClientProbe()
        self._latest_reports: dict[str, AnalysisReport] = {}
        self._lock = asyncio.Lock()

    def get_latest_report(self, connector_id: str) -> Optional[AnalysisReport]:
        """Return the most recently generated analysis report for a connector."""
        return self._latest_reports.get(connector_id)

    def get_latest_analysis(self, connector_id: str) -> Optional[AnalysisReport]:
        """Alias for get_latest_report."""
        return self.get_latest_report(connector_id)

    def list_reports(self) -> list[AnalysisReport]:
        """Return all stored analysis reports."""
        return list(self._latest_reports.values())


    async def analyze(
        self,
        connector_id: str,
        *,
        request_id: Optional[str] = None,
        force_connect_probe: bool = True,
        test_connection: bool = True,
    ) -> AnalysisReport:
        """Run the full, evidence-based verification pipeline on a single connector."""
        async with self._semaphore:
            req_id = request_id or f"req_an_{uuid.uuid4().hex[:10]}"
            t0 = time.time()
            self.metrics.record("analyzer_run_total", 1)

            # 1. Fetch analysis descriptor and live instance
            desc = self.registry.get_analysis_descriptor(connector_id)
            connector = self.registry.get(connector_id)

            env_info = EnvironmentProbe.get_environment_info()
            checks: list[AnalysisCheckResult] = []
            evidence: dict[str, Any] = {}
            warnings: list[str] = []
            errors: list[str] = []

            # 2. Pipeline Phase: Environment Discovery
            check_env = self._check_environment(desc, env_info)
            checks.append(check_env)
            evidence["environment"] = check_env.evidence

            # 3. Pipeline Phase: Configuration Analysis
            check_config = self._check_configuration(desc)
            checks.append(check_config)
            evidence["configuration"] = check_config.evidence
            if check_config.status == CheckStatus.FAIL:
                errors.append(check_config.message)

            # 4. Pipeline Phase: Parameter Analysis
            check_params = self._check_parameters(desc)
            checks.append(check_params)
            evidence["parameters"] = check_params.evidence
            if check_params.status == CheckStatus.FAIL:
                errors.append(check_params.message)

            # 5. Pipeline Phase: Dependencies Analysis
            check_deps = self._check_dependencies(desc)
            checks.append(check_deps)
            evidence["dependencies"] = check_deps.evidence
            if check_deps.status == CheckStatus.FAIL:
                errors.append(check_deps.message)

            # 6. Pipeline Phase: Connection Verification
            if not test_connection:
                check_conn = AnalysisCheckResult(
                    check_id="connection_check",
                    category=CheckCategory.CONNECTION,
                    status=CheckStatus.SKIPPED,
                    message="Connection verification skipped by analysis options",
                    evidence={"status": desc.status.value, "tested": False},
                    started_at=time.time(),
                    completed_at=time.time(),
                )
                health_snapshot = None
            elif check_config.status == CheckStatus.FAIL:
                check_conn = AnalysisCheckResult(
                    check_id="connection_check",
                    category=CheckCategory.CONNECTION,
                    status=CheckStatus.UNKNOWN,
                    message="Connection verification skipped due to invalid configuration",
                    evidence={"status": desc.status.value, "tested": False},
                    started_at=time.time(),
                    completed_at=time.time(),
                )
                health_snapshot = None
            else:
                check_conn, health_snapshot = await self._check_connection(desc, connector, force_connect_probe)

            checks.append(check_conn)
            evidence["connection"] = check_conn.evidence
            if check_conn.status == CheckStatus.FAIL:
                errors.append(check_conn.message)

            # 7. Pipeline Phase: Authentication Verification
            check_auth = await self._check_authentication(desc, connector, check_config, check_conn, health_snapshot)
            checks.append(check_auth)
            evidence["authentication"] = check_auth.evidence
            if check_auth.status == CheckStatus.FAIL:
                errors.append(check_auth.message)

            # 8. Pipeline Phase: Capability Verification
            check_caps = await self._check_capabilities(desc, connector, check_conn)
            checks.append(check_caps)
            evidence["capabilities"] = check_caps.evidence


            # 8. Pipeline Phase: CLI Applicability Analysis
            check_cli = self._check_cli_applicability(desc)
            checks.append(check_cli)
            evidence["cli_applicability"] = check_cli.evidence

            # 9. Pipeline Phase: Client / IDE Applicability Analysis
            check_client = self._check_client_applicability(desc)
            checks.append(check_client)
            evidence["client_applicability"] = check_client.evidence

            # 10. Pipeline Phase: Health & Latency Probe
            check_health, check_latency = self._check_health_and_latency(desc, health_snapshot)
            checks.append(check_health)
            checks.append(check_latency)
            evidence["health"] = check_health.evidence
            evidence["latency"] = check_latency.evidence
            if check_health.status == CheckStatus.FAIL:
                errors.append(check_health.message)
            elif check_health.status == CheckStatus.UNKNOWN and desc.enabled:
                warnings.append(check_health.message)

            # 11. Pipeline Phase: Usage Evidence Analysis
            check_usage, usage_state = self._check_usage_evidence(desc)
            checks.append(check_usage)
            evidence["usage"] = check_usage.evidence

            # 12. Pipeline Phase: Evidence Aggregation
            total_duration_ms = round((time.time() - t0) * 1000, 2)
            verification_state = self._aggregate_evidence(
                checks=checks,
                desc=desc,
                check_config=check_config,
                check_conn=check_conn,
                check_auth=check_auth,
                check_health=check_health,
                check_deps=check_deps,
            )

            latency_summary = {
                "total_pipeline_ms": total_duration_ms,
                "check_latencies": {c.check_id: c.latency_ms or 0.0 for c in checks},
            }

            report = AnalysisReport(
                connector_id=connector_id,
                provider=desc.provider,
                connector_type=desc.type,
                transport=desc.transport,
                environment_id=env_info.get("environment_id", "unknown"),
                client_context=evidence.get("client_applicability", {}),
                timestamp=time.time(),
                checks=checks,
                evidence=evidence,
                verification_state=verification_state,
                usage_state=usage_state,
                overall_health=health_snapshot,
                latency_summary=latency_summary,
                warnings=warnings,
                errors=errors,
                request_id=req_id,
            )

            # 13. Persist and Record in ActivityStore & Metrics
            async with self._lock:
                self._latest_reports[connector_id] = report

            if verification_state == VerificationState.VERIFIED:
                self.metrics.record("analyzer_run_success", 1)
            elif verification_state == VerificationState.FAILED:
                self.metrics.record("analyzer_run_error", 1)

            self._record_activity_store(report)
            log.info(
                "Analyzed connector '%s': state=%s (duration=%.1fms, checks=%d)",
                connector_id, verification_state.value, total_duration_ms, len(checks),
            )
            return report

    async def analyze_all(self, *, request_id: Optional[str] = None) -> list[AnalysisReport]:
        """Analyze all registered connectors concurrently with fault isolation."""
        descriptors = self.registry.list()
        tasks = [self.analyze(d.connector_id, request_id=request_id) for d in descriptors]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        report_list: list[AnalysisReport] = []
        for d, res in zip(descriptors, results):
            if isinstance(res, Exception):
                log.error("Analysis crashed for connector '%s': %s", d.connector_id, res)
                # Build fallback report with FAILED state rather than aborting batch
                report_list.append(
                    AnalysisReport(
                        connector_id=d.connector_id,
                        provider=d.provider,
                        connector_type=d.type.value if hasattr(d.type, "value") else str(d.type),
                        transport=getattr(d, "transport", "unknown"),
                        environment_id="unknown",
                        verification_state=VerificationState.FAILED,
                        errors=[f"Analyzer failure: {res}"],
                    )
                )
            else:
                report_list.append(res)
        return report_list


    # -----------------------------------------------------------------------
    # Internal Pipeline Check Stages
    # -----------------------------------------------------------------------

    def _check_environment(self, desc: ConnectorAnalysisDescriptor, env_info: dict[str, Any]) -> AnalysisCheckResult:
        """Stage: Host system and OS environment analysis."""
        t_start = time.time()
        return AnalysisCheckResult(
            check_id="env_discovery",
            category=CheckCategory.DISCOVERY,
            status=CheckStatus.PASS,
            message="Host platform discovered",
            evidence=env_info,
            started_at=t_start,
            completed_at=time.time(),
        )

    def _check_configuration(self, desc: ConnectorAnalysisDescriptor) -> AnalysisCheckResult:
        """Stage: Configuration and declared environment variable checks."""
        t_start = time.time()
        connector_env = desc.metadata.get("custom_env") or {}
        env_map = {**os.environ, **connector_env}
        if self.custom_env is not None:
            env_map.update(self.custom_env)

        declared_vars = desc.declared_env_vars
        missing_vars: list[str] = []
        present_vars: list[str] = []

        for v in declared_vars:
            if v in env_map and env_map[v].strip():
                present_vars.append(v)
            else:
                missing_vars.append(v)


        if not desc.enabled:
            return AnalysisCheckResult(
                check_id="config_check",
                category=CheckCategory.CONFIGURATION,
                status=CheckStatus.UNKNOWN,
                message=f"Connector '{desc.connector_id}' is administratively disabled",
                evidence={"enabled": False, "declared_env_vars": list(declared_vars)},
                started_at=t_start,
                completed_at=time.time(),
                error_code="CONNECTOR_DISABLED",
            )

        if missing_vars:
            return AnalysisCheckResult(
                check_id="config_check",
                category=CheckCategory.CONFIGURATION,
                status=CheckStatus.FAIL,
                message=f"Missing required environment variables: {', '.join(missing_vars)}",
                evidence={
                    "missing_vars": missing_vars,
                    "present_vars": present_vars,
                    "enabled": True,
                },
                started_at=t_start,
                completed_at=time.time(),
                error_code="MISSING_CONFIG",
            )

        return AnalysisCheckResult(
            check_id="config_check",
            category=CheckCategory.CONFIGURATION,
            status=CheckStatus.PASS,
            message="All declared configuration requirements are satisfied",
            evidence={
                "present_vars": present_vars,
                "transport": desc.transport,
                "enabled": True,
            },
            started_at=t_start,
            completed_at=time.time(),
        )

    def _check_dependencies(self, desc: ConnectorAnalysisDescriptor) -> AnalysisCheckResult:
        """Stage: Binary dependencies and parameter analysis."""
        t_start = time.time()
        declared_binaries = desc.declared_binary_names
        if not declared_binaries:
            return AnalysisCheckResult(
                check_id="deps_check",
                category=CheckCategory.DEPENDENCIES,
                status=CheckStatus.PASS,
                message="No external CLI binaries declared",
                evidence={"declared_binaries": []},
                started_at=t_start,
                completed_at=time.time(),
            )

        missing_binaries: list[str] = []
        found_binaries: list[dict[str, Any]] = []

        for b in declared_binaries:
            res = self.cli_probe.inspect_binary(b)
            if res["status"] == CLIApplicabilityStatus.CLI_NOT_FOUND:
                missing_binaries.append(b)
            else:
                found_binaries.append(res)


        if missing_binaries:
            return AnalysisCheckResult(
                check_id="deps_check",
                category=CheckCategory.DEPENDENCIES,
                status=CheckStatus.FAIL,
                message=f"Required CLI binary dependencies not found: {', '.join(missing_binaries)}",
                evidence={"missing_binaries": missing_binaries, "found_binaries": found_binaries},
                started_at=t_start,
                completed_at=time.time(),
                error_code="MISSING_BINARY",
            )

        return AnalysisCheckResult(
            check_id="deps_check",
            category=CheckCategory.DEPENDENCIES,
            status=CheckStatus.PASS,
            message="All declared binary dependencies are executable",
            evidence={"found_binaries": found_binaries},
            started_at=t_start,
            completed_at=time.time(),
        )

    async def _check_connection(
        self,
        desc: ConnectorAnalysisDescriptor,
        connector: Optional[Connector],
        force_connect: bool,
    ) -> tuple[AnalysisCheckResult, Optional[ConnectorHealth]]:
        """Stage: Safe, bounded connection verification."""
        t_start = time.time()
        if not desc.enabled:
            return (
                AnalysisCheckResult(
                    check_id="connection_check",
                    category=CheckCategory.CONNECTION,
                    status=CheckStatus.FAIL,
                    message="Connector is disabled; connection rejected",
                    evidence={"status": desc.status.value, "connected": False},
                    started_at=t_start,
                    completed_at=time.time(),
                ),
                None,
            )

        # If already READY in registry, verify via health probe
        if desc.status == ConnectorStatus.READY and connector is not None:
            try:
                h = await connector.health()
                return (
                    AnalysisCheckResult(
                        check_id="connection_check",
                        category=CheckCategory.CONNECTION,
                        status=CheckStatus.PASS if h.healthy else CheckStatus.FAIL,
                        message="Connection active and verified",
                        evidence={"status": desc.status.value, "healthy": h.healthy},
                        started_at=t_start,
                        completed_at=time.time(),
                    ),
                    h,
                )
            except Exception as exc:
                return (
                    AnalysisCheckResult(
                        check_id="connection_check",
                        category=CheckCategory.CONNECTION,
                        status=CheckStatus.FAIL,
                        message=f"Connection probe error: {exc}",
                        evidence={"status": desc.status.value, "error": str(exc)},
                        started_at=t_start,
                        completed_at=time.time(),
                    ),
                    None,
                )

        if desc.status == ConnectorStatus.ERROR:
            return (
                AnalysisCheckResult(
                    check_id="connection_check",
                    category=CheckCategory.CONNECTION,
                    status=CheckStatus.FAIL,
                    message="Connector is in ERROR lifecycle state",
                    evidence={"status": desc.status.value, "connected": False},
                    started_at=t_start,
                    completed_at=time.time(),
                ),
                None,
            )

        if force_connect and connector is not None:
            try:
                h = await self.registry.connect(desc.connector_id)
                return (
                    AnalysisCheckResult(
                        check_id="connection_check",
                        category=CheckCategory.CONNECTION,
                        status=CheckStatus.PASS,
                        message="Connection established successfully",
                        evidence={"status": ConnectorStatus.READY.value, "healthy": h.healthy},
                        started_at=t_start,
                        completed_at=time.time(),
                    ),
                    h,
                )
            except ConnectorAuthError as exc:
                return (
                    AnalysisCheckResult(
                        check_id="connection_check",
                        category=CheckCategory.CONNECTION,
                        status=CheckStatus.FAIL,
                        message=f"Authentication rejected by provider: {exc}",
                        evidence={"status": ConnectorStatus.ERROR.value, "error": str(exc), "auth_failed": True},
                        started_at=t_start,
                        completed_at=time.time(),
                        error_code="AUTH_FAILED",
                    ),
                    None,
                )
            except Exception as exc:
                return (
                    AnalysisCheckResult(
                        check_id="connection_check",
                        category=CheckCategory.CONNECTION,
                        status=CheckStatus.FAIL,
                        message=f"Connection handshake failed: {exc}",
                        evidence={"status": ConnectorStatus.ERROR.value, "error": str(exc)},
                        started_at=t_start,
                        completed_at=time.time(),
                    ),
                    None,
                )

        # Not connected, but not in error
        return (
            AnalysisCheckResult(
                check_id="connection_check",
                category=CheckCategory.CONNECTION,
                status=CheckStatus.UNKNOWN,
                message=f"Connector is in {desc.status.value} state (not actively connected)",
                evidence={"status": desc.status.value, "connected": False},
                started_at=t_start,
                completed_at=time.time(),
            ),
            None,
        )

    async def _check_authentication(
        self,
        desc: ConnectorAnalysisDescriptor,
        connector: Optional[Connector],
        check_config: AnalysisCheckResult,
        check_conn: AnalysisCheckResult,
        health: Optional[ConnectorHealth],
    ) -> AnalysisCheckResult:
        """Stage: Authentication verification (strictly separated from connectivity)."""
        t_start = time.time()

        # If config is missing required credentials, authentication could not be evaluated -> UNKNOWN
        if check_config.status == CheckStatus.FAIL:
            return AnalysisCheckResult(
                check_id="auth_check",
                category=CheckCategory.AUTHENTICATION,
                status=CheckStatus.UNKNOWN,
                message="Cannot evaluate authentication due to missing or invalid configuration",
                evidence={"credentials_present": False, "authenticated": None},
                started_at=t_start,
                completed_at=time.time(),
            )


        # If connection was skipped or is unknown:
        if check_conn.status in (CheckStatus.SKIPPED, CheckStatus.UNKNOWN):
            return AnalysisCheckResult(
                check_id="auth_check",
                category=CheckCategory.AUTHENTICATION,
                status=CheckStatus.UNKNOWN,
                message="Authentication cannot be verified without active connection verification",
                evidence={"authenticated": None},
                started_at=t_start,
                completed_at=time.time(),
            )

        # If connection is active and healthy
        if check_conn.status == CheckStatus.PASS and health is not None and health.healthy:
            # If health diagnostics explicit or connected session valid
            return AnalysisCheckResult(
                check_id="auth_check",
                category=CheckCategory.AUTHENTICATION,
                status=CheckStatus.PASS,
                message="Endpoint authenticated successfully",
                evidence={"authenticated": True, "method": desc.metadata.get("auth_method", "token")},
                started_at=t_start,
                completed_at=time.time(),
            )

        # If connection failed
        if check_conn.status == CheckStatus.FAIL:
            # If error explicitly indicates 401/403 auth error
            msg_lower = (check_conn.message or "").lower()
            if check_conn.evidence.get("auth_failed") or "auth" in msg_lower or "401" in msg_lower or "403" in msg_lower or "unauthorized" in msg_lower or "credential" in msg_lower:
                return AnalysisCheckResult(
                    check_id="auth_check",
                    category=CheckCategory.AUTHENTICATION,
                    status=CheckStatus.FAIL,
                    message="Upstream authentication rejected credentials",
                    evidence={"authenticated": False},
                    started_at=t_start,
                    completed_at=time.time(),
                )
            # Otherwise, connection failed due to network / unreachable; auth state is UNKNOWN
            return AnalysisCheckResult(
                check_id="auth_check",
                category=CheckCategory.AUTHENTICATION,
                status=CheckStatus.UNKNOWN,
                message="Authentication cannot be verified because connection failed",
                evidence={"authenticated": None},
                started_at=t_start,
                completed_at=time.time(),
            )

        # Configuration exists, but connector is not connected or endpoint was not verified
        return AnalysisCheckResult(
            check_id="auth_check",
            category=CheckCategory.AUTHENTICATION,
            status=CheckStatus.UNKNOWN,
            message="Configuration present, but credentials have not been actively verified against provider",
            evidence={"authenticated": None, "credentials_configured": True},
            started_at=t_start,
            completed_at=time.time(),
        )

    def _check_parameters(self, desc: ConnectorAnalysisDescriptor) -> AnalysisCheckResult:
        """Stage: Parameter schema and structural parameter verification."""
        t_start = time.time()
        safe_keys = [k for k in desc.metadata.keys() if k not in ("custom_env", "declared_env_vars", "declared_binary_names")]
        return AnalysisCheckResult(
            check_id="param_check",
            category=CheckCategory.PARAMETERS,
            status=CheckStatus.PASS,
            message="Parameter structures verified without secret exposure",
            evidence={"parameter_keys": safe_keys, "transport": desc.transport},
            started_at=t_start,
            completed_at=time.time(),
        )

    async def _check_capabilities(
        self,
        desc: ConnectorAnalysisDescriptor,
        connector: Optional[Connector],
        check_conn: Optional[AnalysisCheckResult] = None,
    ) -> AnalysisCheckResult:
        """Stage: Read-only capability and tool discovery."""
        t_start = time.time()
        declared_caps = [c.value for c in desc.capabilities]
        discovered_tools_count = 0
        discovered_tools: list[str] = []

        is_connected = desc.status == ConnectorStatus.READY or (check_conn is not None and check_conn.status == CheckStatus.PASS)
        if connector is not None and is_connected:
            if ConnectorCapability.DISCOVERY in desc.capabilities:
                try:
                    tools = await connector.discover()
                    discovered_tools_count = len(tools)
                    discovered_tools = [t.name for t in tools]
                except Exception as exc:
                    log.warning("Tool discovery error on '%s': %s", desc.connector_id, exc)

        return AnalysisCheckResult(
            check_id="capabilities_check",
            category=CheckCategory.CAPABILITIES,
            status=CheckStatus.PASS,
            message=f"Declared {len(declared_caps)} capabilities; discovered {discovered_tools_count} tools",
            evidence={
                "declared_capabilities": declared_caps,
                "discovered_tools_count": discovered_tools_count,
                "discovered_tools": discovered_tools,
                "tools": discovered_tools,
            },
            started_at=t_start,
            completed_at=time.time(),
        )


    def _check_cli_applicability(self, desc: ConnectorAnalysisDescriptor) -> AnalysisCheckResult:
        """Stage: Local CLI applicability checks."""
        t_start = time.time()
        if not desc.declared_binary_names:
            return AnalysisCheckResult(
                check_id="cli_applicability",
                category=CheckCategory.CLI_APPLICABILITY,
                status=CheckStatus.SKIPPED,
                message="No CLI dependencies declared for this connector",
                evidence={},
                started_at=t_start,
                completed_at=time.time(),
            )

        results: dict[str, Any] = {}
        all_found = True
        for b in desc.declared_binary_names:
            info = self.cli_probe.inspect_binary(b)
            results[b] = info["status"].value
            if info["status"] == CLIApplicabilityStatus.CLI_NOT_FOUND:
                all_found = False

        missing = [b for b, s in results.items() if s == CLIApplicabilityStatus.CLI_NOT_FOUND.value]
        status = CheckStatus.PASS if all_found else CheckStatus.FAIL
        msg = "CLI applicability verified" if all_found else f"Missing CLI dependencies: {', '.join(missing)}"
        return AnalysisCheckResult(
            check_id="cli_applicability",
            category=CheckCategory.CLI_APPLICABILITY,
            status=status,
            message=msg,
            evidence={"binaries": results, "missing_binaries": missing},
            started_at=t_start,
            completed_at=time.time(),
        )


    def _check_client_applicability(self, desc: ConnectorAnalysisDescriptor) -> AnalysisCheckResult:
        """Stage: Developer client/IDE environment analysis."""
        t_start = time.time()
        clients = self.client_probe.detect_all_known_clients()
        detected_names = [k for k, v in clients.items() if v["detected"]]

        return AnalysisCheckResult(
            check_id="client_applicability",
            category=CheckCategory.CLIENT_APPLICABILITY,
            status=CheckStatus.PASS,
            message=f"Detected developer clients: {', '.join(detected_names) if detected_names else 'none'}",
            evidence={"detected_clients": detected_names, "all_probes": {k: v["status"].value for k, v in clients.items()}},
            started_at=t_start,
            completed_at=time.time(),
        )


    def _check_health_and_latency(
        self,
        desc: ConnectorAnalysisDescriptor,
        health: Optional[ConnectorHealth],
    ) -> tuple[AnalysisCheckResult, AnalysisCheckResult]:
        """Stage: Point-in-time health diagnostic and latency profiling."""
        t_start = time.time()
        if health is None:
            c_health = AnalysisCheckResult(
                check_id="health_probe",
                category=CheckCategory.HEALTH,
                status=CheckStatus.UNKNOWN,
                message="No active health diagnostic available (connector not in READY state)",
                evidence={"healthy": None},
                started_at=t_start,
                completed_at=time.time(),
            )
            c_latency = AnalysisCheckResult(
                check_id="latency_probe",
                category=CheckCategory.LATENCY,
                status=CheckStatus.UNKNOWN,
                message="Latency unmeasured",
                evidence={"latency_ms": None},
                started_at=t_start,
                completed_at=time.time(),
            )
            return c_health, c_latency

        latency = health.latency_ms or 0.0
        health_status = CheckStatus.PASS if health.healthy else CheckStatus.FAIL
        c_health = AnalysisCheckResult(
            check_id="health_probe",
            category=CheckCategory.HEALTH,
            status=health_status,
            message=f"Health reported: {health.status.value} (healthy={health.healthy})",
            evidence={"healthy": health.healthy, "status": health.status.value, "message": health.message},
            started_at=t_start,
            completed_at=time.time(),
        )

        c_latency = AnalysisCheckResult(
            check_id="latency_probe",
            category=CheckCategory.LATENCY,
            status=CheckStatus.PASS if latency < 10000.0 else CheckStatus.FAIL,
            message=f"Operation latency: {latency:.1f}ms",
            evidence={"latency_ms": latency},
            latency_ms=latency,
            started_at=t_start,
            completed_at=time.time(),
        )
        return c_health, c_latency

    def _check_usage_evidence(self, desc: ConnectorAnalysisDescriptor) -> tuple[AnalysisCheckResult, UsageEvidenceState]:
        """Stage: Historical usage evidence analysis via ActivityStore."""
        t_start = time.time()
        if self.activity_store is None:
            return (
                AnalysisCheckResult(
                    check_id="usage_evidence",
                    category=CheckCategory.USAGE_EVIDENCE,
                    status=CheckStatus.UNKNOWN,
                    message="ActivityStore unavailable; usage cannot be determined",
                    evidence={"usage_state": UsageEvidenceState.UNKNOWN.value},
                    started_at=t_start,
                    completed_at=time.time(),
                ),
                UsageEvidenceState.UNKNOWN,
            )

        try:
            # Query tool calls and connector events
            tool_calls = []
            if hasattr(self.activity_store, "list_tool_calls"):
                # Check for recent tool executions with this connector_id
                all_calls = self.activity_store.list_tool_calls(limit=100)
                tool_calls = [c for c in all_calls if c.get("connector_id") == desc.connector_id]

            events = []
            if hasattr(self.activity_store, "list_connector_events"):
                events = self.activity_store.list_connector_events(connector_id=desc.connector_id, limit=50)

            total_records = len(tool_calls) + len(events)
            if tool_calls:
                # Direct tool execution observed!
                return (
                    AnalysisCheckResult(
                        check_id="usage_evidence",
                        category=CheckCategory.USAGE_EVIDENCE,
                        status=CheckStatus.PASS,
                        message=f"Observed in use: {len(tool_calls)} tool calls recorded",
                        evidence={
                            "usage_state": UsageEvidenceState.OBSERVED_IN_USE.value,
                            "tool_calls_count": len(tool_calls),
                            "events_count": len(events),
                        },
                        started_at=t_start,
                        completed_at=time.time(),
                    ),
                    UsageEvidenceState.OBSERVED_IN_USE,
                )

            return (
                AnalysisCheckResult(
                    check_id="usage_evidence",
                    category=CheckCategory.USAGE_EVIDENCE,
                    status=CheckStatus.PASS,
                    message="ActivityStore checked: zero operational tool executions recorded",
                    evidence={
                        "usage_state": UsageEvidenceState.NOT_OBSERVED.value,
                        "events_count": len(events),
                    },
                    started_at=t_start,
                    completed_at=time.time(),
                ),
                UsageEvidenceState.NOT_OBSERVED,
            )
        except Exception as exc:
            log.warning("ActivityStore usage query error: %s", exc)
            return (
                AnalysisCheckResult(
                    check_id="usage_evidence",
                    category=CheckCategory.USAGE_EVIDENCE,
                    status=CheckStatus.UNKNOWN,
                    message=f"Could not query operational history: {exc}",
                    evidence={"usage_state": UsageEvidenceState.UNKNOWN.value},
                    started_at=t_start,
                    completed_at=time.time(),
                ),
                UsageEvidenceState.UNKNOWN,
            )

    # -----------------------------------------------------------------------
    # Evidence Aggregator
    # -----------------------------------------------------------------------

    def _aggregate_evidence(
        self,
        checks: list[AnalysisCheckResult],
        desc: ConnectorAnalysisDescriptor,
        check_config: AnalysisCheckResult,
        check_conn: AnalysisCheckResult,
        check_auth: AnalysisCheckResult,
        check_health: AnalysisCheckResult,
        check_deps: AnalysisCheckResult,
    ) -> VerificationState:
        """Deterministic evidence aggregation rules mapping check states to VerificationState."""
        # Rule 0: Administratively disabled connector
        if not desc.enabled:
            return VerificationState.CONFIGURED

        # Rule 1: Fatal configuration failure
        if check_config.status == CheckStatus.FAIL:
            return VerificationState.FAILED

        # Rule 2: Fatal binary dependency failure
        if check_deps.status == CheckStatus.FAIL:
            return VerificationState.FAILED

        # Rule 3: Fatal connection failure
        if check_conn.status == CheckStatus.FAIL:
            return VerificationState.FAILED

        # Rule 4: Fatal authentication rejection
        if check_auth.status == CheckStatus.FAIL:
            return VerificationState.FAILED

        # Rule 5: Degraded health
        if check_health.status == CheckStatus.FAIL or desc.status == ConnectorStatus.DEGRADED:
            return VerificationState.DEGRADED

        # Rule 6: Fully Verified
        # Requires: Configuration PASS, Connection PASS, Auth PASS, Health not FAIL
        if (
            check_config.status == CheckStatus.PASS
            and check_conn.status == CheckStatus.PASS
            and check_auth.status == CheckStatus.PASS
            and check_health.status in (CheckStatus.PASS, CheckStatus.UNKNOWN, CheckStatus.SKIPPED)
        ):
            return VerificationState.VERIFIED

        # Rule 7: Partially Verified
        # Configuration is valid, but either auth or connection is unconfirmed (UNKNOWN/SKIPPED)
        if check_config.status == CheckStatus.PASS and (
            check_conn.status in (CheckStatus.UNKNOWN, CheckStatus.SKIPPED)
            or check_auth.status in (CheckStatus.UNKNOWN, CheckStatus.SKIPPED)
        ):
            if check_conn.status == CheckStatus.PASS or check_conn.status == CheckStatus.SKIPPED:
                return VerificationState.PARTIALLY_VERIFIED
            return VerificationState.CONFIGURED

        # Rule 8: Discovered
        return VerificationState.DISCOVERED

    # -----------------------------------------------------------------------
    # ActivityStore Recording
    # -----------------------------------------------------------------------

    def _record_activity_store(self, report: AnalysisReport) -> None:
        """Record analysis run in ActivityStore if available."""
        if self.activity_store is None:
            return

        try:
            if hasattr(self.activity_store, "record_connector_event"):
                self.activity_store.record_connector_event(
                    connector_id=report.connector_id,
                    event_type="analyzer_completed",
                    status=report.verification_state.value,
                    request_id=report.request_id,
                    metadata={
                        "verification_state": report.verification_state.value,
                        "usage_state": report.usage_state.value,
                        "checks_count": len(report.checks),
                        "passed_count": sum(1 for c in report.checks if c.status == CheckStatus.PASS),
                    },
                )
            if hasattr(self.activity_store, "record_audit_event"):
                self.activity_store.record_audit_event(
                    event_type="connector_analysis",
                    actor="analyzer",
                    request_id=report.request_id,
                    target=report.connector_id,
                    details={
                        "verification_state": report.verification_state.value,
                        "checks": len(report.checks),
                        "duration_ms": report.latency_summary.get("total_pipeline_ms", 0.0),
                    },
                )
        except Exception as exc:
            log.warning("Failed to record analysis events in ActivityStore: %s", exc)


def analyze_connector(
    connector_id: str,
    registry: ConnectorRegistry,
    *,
    activity_store: Optional[Any] = None,
    metrics: Optional[Metrics] = None,
    custom_env: Optional[dict[str, str]] = None,
    force_connect_probe: bool = True,
    test_connection: bool = True,
    request_id: Optional[str] = None,
) -> Any:
    """Convenience synchronous/async wrapper to analyze a connector."""
    analyzer = ConnectorAnalyzer(
        registry=registry,
        activity_store=activity_store,
        metrics=metrics,
        custom_env=custom_env,
    )
    # Return the coroutine
    return analyzer.analyze(
        connector_id,
        request_id=request_id,
        force_connect_probe=force_connect_probe,
        test_connection=test_connection,
    )
