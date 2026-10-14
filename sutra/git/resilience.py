"""Resilience boundary, error normalization, rate limiting, circuit breaker,
bulkhead concurrency, cache, and retry mechanisms for Safe Git & GitHub Operations.

This module unifies all GitHub REST API interactions behind a single, consistent,
audited resilience boundary.
"""
from __future__ import annotations

import asyncio
import logging
import math
import os
import random
import time
from enum import Enum
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence, Tuple, TypeVar

import httpx

from ..cache import CacheStore
from ..metrics import Metrics, get_metrics
from ..redaction import redact_secrets
from ..resilience import (
    CircuitBreaker,
    CircuitOpenError,
    IdempotencyStore,
    RetryPolicy,
    SlidingWindowRateLimiter,
)

log = logging.getLogger(__name__)

T = TypeVar("T")


# ---------------------------------------------------------------------------
# Error Normalization & Error Categories
# ---------------------------------------------------------------------------

class GitHubErrorCode(str, Enum):
    """Normalized error categories for GitHub & Git operations."""
    AUTHENTICATION_ERROR = "AUTHENTICATION_ERROR"      # 401
    AUTHORIZATION_ERROR = "AUTHORIZATION_ERROR"        # 403 (permissions)
    RATE_LIMITED = "RATE_LIMITED"                      # 429 or rate bucket limit
    TIMEOUT = "TIMEOUT"                                # timeout (connect/read/op)
    NETWORK_ERROR = "NETWORK_ERROR"                    # network / socket / reset
    NOT_FOUND = "NOT_FOUND"                            # 404
    CONFLICT = "CONFLICT"                              # 409
    STALE_STATE = "STALE_STATE"                        # PR state or SHA changed
    VALIDATION_ERROR = "VALIDATION_ERROR"              # 422
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"      # 500, 502, 503, 504
    CIRCUIT_OPEN = "CIRCUIT_OPEN"                      # circuit breaker OPEN
    LOCAL_GIT_TIMEOUT = "LOCAL_GIT_TIMEOUT"            # subprocess timeout
    WORKSPACE_CONFLICT = "WORKSPACE_CONFLICT"          # workspace lock conflict
    UNKNOWN_ERROR = "UNKNOWN_ERROR"                    # generic catch-all


class GitHubResilienceError(Exception):
    """Normalized domain error for all GitHub REST interactions.
    
    Guarantees that secrets, tokens, authorization headers, and raw tracebacks
    are never leaked to caller displays or logs.
    """

    def __init__(
        self,
        error_code: GitHubErrorCode,
        message: str,
        user_friendly_message: Optional[str] = None,
        status_code: Optional[int] = None,
        retry_after: Optional[float] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.error_code = error_code
        self.message = redact_secrets(message)
        self.status_code = status_code
        self.retry_after = retry_after
        self.details = details or {}
        self.user_friendly_message = redact_secrets(user_friendly_message or message)
        super().__init__(self.message)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "error_code": self.error_code.value,
            "message": self.message,
            "user_friendly_message": self.user_friendly_message,
            "status_code": self.status_code,
            "retry_after": self.retry_after,
            "details": self.details,
        }


# ---------------------------------------------------------------------------
# HTTP Status & Error Classification
# ---------------------------------------------------------------------------

_RETRIABLE_HTTP_CODES = frozenset({429, 500, 502, 503, 504})
_NON_RETRIABLE_HTTP_CODES = frozenset({400, 401, 403, 404, 409, 422})

_TRANSIENT_EXCEPTIONS = (
    asyncio.TimeoutError,
    TimeoutError,
    ConnectionResetError,
    ConnectionRefusedError,
    httpx.TimeoutException,
    httpx.NetworkError,
    OSError,
)


def is_retriable_error(exc: BaseException) -> bool:
    """Classify whether a failure represents a transient problem worth retrying."""
    if isinstance(exc, GitHubResilienceError):
        return exc.error_code in {
            GitHubErrorCode.RATE_LIMITED,
            GitHubErrorCode.TIMEOUT,
            GitHubErrorCode.NETWORK_ERROR,
            GitHubErrorCode.PROVIDER_UNAVAILABLE,
        }
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in _RETRIABLE_HTTP_CODES
    return isinstance(exc, _TRANSIENT_EXCEPTIONS)


def normalize_github_exception(exc: BaseException) -> GitHubResilienceError:
    """Normalize any low-level exception into a clean GitHubResilienceError."""
    if isinstance(exc, GitHubResilienceError):
        return exc

    if isinstance(exc, CircuitOpenError):
        cooldown = max(1, int(round(exc.retry_after)))
        return GitHubResilienceError(
            error_code=GitHubErrorCode.CIRCUIT_OPEN,
            message=str(exc),
            user_friendly_message=(
                f"GitHub service is temporarily unavailable (circuit open). "
                f"Local Git and Codebase Explorer remain operational. Retry in {cooldown}s."
            ),
            retry_after=exc.retry_after,
        )

    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        retry_after_hdr = exc.response.headers.get("Retry-After") or exc.response.headers.get("retry-after")
        retry_after: Optional[float] = None
        if retry_after_hdr:
            try:
                retry_after = float(retry_after_hdr)
            except (ValueError, TypeError):
                pass

        if code == 401:
            return GitHubResilienceError(
                error_code=GitHubErrorCode.AUTHENTICATION_ERROR,
                message="GitHub API authentication failed. Verify token validity.",
                user_friendly_message="GitHub authentication failed. Please check your token or run 'gh auth login'.",
                status_code=401,
            )
        if code == 403:
            # Check rate limit headers
            remaining = exc.response.headers.get("x-ratelimit-remaining")
            if remaining == "0" or "rate limit" in exc.response.text.lower():
                reset_ts = exc.response.headers.get("x-ratelimit-reset")
                wait_sec = 60.0
                if reset_ts:
                    try:
                        wait_sec = max(1.0, float(reset_ts) - time.time())
                    except (ValueError, TypeError):
                        pass
                ra = retry_after or wait_sec
                return GitHubResilienceError(
                    error_code=GitHubErrorCode.RATE_LIMITED,
                    message=f"GitHub API rate limit exceeded (HTTP 403). Retry after {int(ra)}s.",
                    user_friendly_message=f"GitHub rate limit reached. Retry after {int(ra)} seconds.",
                    status_code=403,
                    retry_after=ra,
                )
            return GitHubResilienceError(
                error_code=GitHubErrorCode.AUTHORIZATION_ERROR,
                message="GitHub API permission denied. Token lacks repository write or workflow permissions.",
                user_friendly_message="GitHub permission denied. Please verify repository permissions.",
                status_code=403,
            )
        if code == 404:
            return GitHubResilienceError(
                error_code=GitHubErrorCode.NOT_FOUND,
                message="GitHub resource not found.",
                user_friendly_message="The requested GitHub resource was not found.",
                status_code=404,
            )
        if code == 409:
            return GitHubResilienceError(
                error_code=GitHubErrorCode.CONFLICT,
                message="GitHub conflict detected (state mismatch or merge conflict).",
                user_friendly_message="Operation conflict detected on GitHub. State may have changed.",
                status_code=409,
            )
        if code == 422:
            try:
                data = exc.response.json()
                msg = data.get("message", "Validation failed")
            except Exception:
                msg = exc.response.text[:200]
            return GitHubResilienceError(
                error_code=GitHubErrorCode.VALIDATION_ERROR,
                message=f"GitHub rejected request: {msg}",
                user_friendly_message=f"GitHub validation rejected request: {msg}",
                status_code=422,
            )
        if code == 429:
            ra = retry_after or 60.0
            return GitHubResilienceError(
                error_code=GitHubErrorCode.RATE_LIMITED,
                message=f"GitHub rate limit exceeded (HTTP 429). Retry after {int(ra)}s.",
                user_friendly_message=f"GitHub rate-limited you. Retry after {int(ra)} seconds.",
                status_code=429,
                retry_after=ra,
            )
        if code in {500, 502, 503, 504}:
            return GitHubResilienceError(
                error_code=GitHubErrorCode.PROVIDER_UNAVAILABLE,
                message=f"GitHub server error (HTTP {code}).",
                user_friendly_message=f"GitHub is temporarily experiencing server issues (HTTP {code}). Local Git remains available.",
                status_code=code,
            )

        return GitHubResilienceError(
            error_code=GitHubErrorCode.UNKNOWN_ERROR,
            message=f"GitHub API error (HTTP {code}): {exc.response.text[:200]}",
            user_friendly_message=f"GitHub error (HTTP {code}). Please try again later.",
            status_code=code,
        )

    if isinstance(exc, (httpx.TimeoutException, asyncio.TimeoutError, TimeoutError)):
        return GitHubResilienceError(
            error_code=GitHubErrorCode.TIMEOUT,
            message=f"GitHub request timed out: {str(exc)}",
            user_friendly_message="GitHub request timed out. Local Git remains operational.",
        )

    if isinstance(exc, (httpx.NetworkError, ConnectionResetError, ConnectionRefusedError, OSError)):
        return GitHubResilienceError(
            error_code=GitHubErrorCode.NETWORK_ERROR,
            message=f"Network error communicating with GitHub: {str(exc)}",
            user_friendly_message="Network error communicating with GitHub. Local Git remains operational.",
        )

    return GitHubResilienceError(
        error_code=GitHubErrorCode.UNKNOWN_ERROR,
        message=f"Unexpected error: {str(exc)}",
        user_friendly_message="An unexpected error occurred during GitHub operation.",
    )


# ---------------------------------------------------------------------------
# GitHub Resilience Boundary Configuration
# ---------------------------------------------------------------------------

class GitHubResilienceConfig:
    """Tunable resilience policy parameters with sensible environment defaults."""

    def __init__(
        self,
        rate_limit_user: int = 30,
        rate_limit_repo: int = 60,
        rate_limit_global: int = 300,
        rate_limit_window: float = 60.0,
        max_retries: int = 2,
        base_delay: float = 0.5,
        max_delay: float = 10.0,
        connect_timeout: float = 5.0,
        read_timeout: float = 15.0,
        circuit_threshold: int = 4,
        circuit_cooldown: float = 20.0,
        max_concurrency_github: int = 10,
        max_concurrency_local_git: int = 5,
        cache_ttl_seconds: float = 30.0,
        ci_poll_max_attempts: int = 10,
        ci_poll_interval: float = 3.0,
    ) -> None:
        self.rate_limit_user = int(os.environ.get("SUTRA_GITHUB_RATE_LIMIT_USER", str(rate_limit_user)))
        self.rate_limit_repo = int(os.environ.get("SUTRA_GITHUB_RATE_LIMIT_REPO", str(rate_limit_repo)))
        self.rate_limit_global = int(os.environ.get("SUTRA_GITHUB_RATE_LIMIT_GLOBAL", str(rate_limit_global)))
        self.rate_limit_window = float(os.environ.get("SUTRA_GITHUB_RATE_LIMIT_WINDOW", str(rate_limit_window)))
        self.max_retries = int(os.environ.get("SUTRA_GITHUB_MAX_RETRIES", str(max_retries)))
        self.base_delay = float(os.environ.get("SUTRA_GITHUB_RETRY_BASE_DELAY", str(base_delay)))
        self.max_delay = float(os.environ.get("SUTRA_GITHUB_RETRY_MAX_DELAY", str(max_delay)))
        self.connect_timeout = float(os.environ.get("SUTRA_GITHUB_CONNECT_TIMEOUT", str(connect_timeout)))
        self.read_timeout = float(os.environ.get("SUTRA_GITHUB_READ_TIMEOUT", str(read_timeout)))
        self.circuit_threshold = int(os.environ.get("SUTRA_GITHUB_CIRCUIT_THRESHOLD", str(circuit_threshold)))
        self.circuit_cooldown = float(os.environ.get("SUTRA_GITHUB_CIRCUIT_COOLDOWN", str(circuit_cooldown)))
        self.max_concurrency_github = int(os.environ.get("SUTRA_GITHUB_MAX_CONCURRENCY", str(max_concurrency_github)))
        self.max_concurrency_local_git = int(os.environ.get("SUTRA_GIT_MAX_CONCURRENCY", str(max_concurrency_local_git)))
        self.cache_ttl_seconds = float(os.environ.get("SUTRA_GITHUB_CACHE_TTL", str(cache_ttl_seconds)))
        self.ci_poll_max_attempts = int(os.environ.get("SUTRA_CI_POLL_MAX_ATTEMPTS", str(ci_poll_max_attempts)))
        self.ci_poll_interval = float(os.environ.get("SUTRA_CI_POLL_INTERVAL", str(ci_poll_interval)))


# ---------------------------------------------------------------------------
# Centralized GitHub Resilience Boundary
# ---------------------------------------------------------------------------

class GitHubResilienceBoundary:
    """Single reusable resilience execution boundary wrapping all GitHub REST operations.
    
    Protects against downstream failures, rate limit exhaustion, cascading retry storms,
    and concurrent resource starvation.
    """

    def __init__(
        self,
        config: Optional[GitHubResilienceConfig] = None,
        rate_limiter: Optional[SlidingWindowRateLimiter] = None,
        circuit_breaker: Optional[CircuitBreaker] = None,
        cache_store: Optional[CacheStore] = None,
        idempotency_store: Optional[IdempotencyStore] = None,
        retry_policy: Optional[RetryPolicy] = None,
        metrics: Optional[Metrics] = None,
    ) -> None:
        self.config = config or GitHubResilienceConfig()
        self.metrics = metrics or get_metrics()

        # 1. Rate Limiting (per-user, per-repo, and global buckets)
        self.user_rate_limiter = SlidingWindowRateLimiter(
            limit=self.config.rate_limit_user,
            window_seconds=self.config.rate_limit_window,
        )
        self.repo_rate_limiter = SlidingWindowRateLimiter(
            limit=self.config.rate_limit_repo,
            window_seconds=self.config.rate_limit_window,
        )
        self.global_rate_limiter = rate_limiter or SlidingWindowRateLimiter(
            limit=self.config.rate_limit_global,
            window_seconds=self.config.rate_limit_window,
        )

        # 2. Circuit Breaker dedicated to GitHub REST
        self.circuit_breaker = circuit_breaker or CircuitBreaker(
            name="github_rest",
            failure_threshold=self.config.circuit_threshold,
            cooldown_seconds=self.config.circuit_cooldown,
            metrics=self.metrics,
        )

        # 3. Cache Store for safe idempotent queries
        self.cache_store = cache_store or CacheStore(max_entries=1000)

        # 4. Idempotency Store for side-effect mutations
        self.idempotency_store = idempotency_store or IdempotencyStore(
            max_entries=2048,
            ttl_seconds=300.0,
        )

        # 5. Retry Policy with full jitter exponential backoff
        self.retry_policy = retry_policy or RetryPolicy(
            max_retries=self.config.max_retries,
            base_delay=self.config.base_delay,
            max_delay=self.config.max_delay,
        )

        # 6. Bulkhead Concurrency Semaphores
        self.github_semaphore = asyncio.Semaphore(self.config.max_concurrency_github)
        self.local_git_semaphore = asyncio.Semaphore(self.config.max_concurrency_local_git)

        # Track degraded mode activations
        self.degraded_mode_active = False

    def reset(self) -> None:
        """Reset internal circuit breaker, rate limit, cache, and idempotency state (for testing)."""
        self.circuit_breaker.reset()
        self.user_rate_limiter.reset()
        self.repo_rate_limiter.reset()
        self.global_rate_limiter.reset()
        self.cache_store.clear()
        self.idempotency_store.clear()
        self.degraded_mode_active = False

    async def execute(
        self,
        operation_name: str,
        coro_fn: Callable[[httpx.AsyncClient], Awaitable[T]],
        *,
        owner: Optional[str] = None,
        repo: Optional[str] = None,
        user_id: Optional[Any] = None,
        device_id: Optional[str] = None,
        cache_key: Optional[str] = None,
        cache_ttl: Optional[float] = None,
        idempotency_key: Optional[str] = None,
        invalidate_prefixes: Optional[Sequence[str]] = None,
        is_mutation: bool = False,
        client: Optional[httpx.AsyncClient] = None,
        payload_hash: Optional[Any] = None,
        allow_stale_on_error: bool = False,
    ) -> T:
        """Execute a GitHub API call through the unified resilience boundary."""
        start_time = time.perf_counter_ns()
        rid = f"gh_{int(time.time() * 1000)}_{random.randint(1000, 9999)}"

        # 1. Idempotency Check for mutations
        if is_mutation and idempotency_key:
            action, value = await self.idempotency_store.check_and_start(idempotency_key, payload_hash)
            if action == "COMPLETED":
                log.info("github_idempotency_hit key=%s op=%s", idempotency_key, operation_name)
                return value
            if action == "PROCESSING":
                if isinstance(value, asyncio.Future):
                    log.info("github_idempotency_wait key=%s op=%s", idempotency_key, operation_name)
                    return await asyncio.wait_for(asyncio.shield(value), timeout=self.config.read_timeout)
                return value
            if action == "FAILED":
                raise GitHubResilienceError(
                    error_code=GitHubErrorCode.CONFLICT,
                    message=f"Duplicate mutation previously failed: {value}",
                    user_friendly_message="Duplicate operation was rejected.",
                )

        # 2. Circuit Breaker fast-fail check
        if self.circuit_breaker.is_open:
            retry_after = max(0.0, self.circuit_breaker.opened_at + self.circuit_breaker.cooldown_seconds - time.monotonic())
            if not (time.monotonic() - self.circuit_breaker.opened_at >= self.circuit_breaker.cooldown_seconds):
                self.metrics.record_circuit_breaker(opened=False, rejected=True, half_open=False, recovered=False)
                # Check for cached read fallback in degraded mode
                if not is_mutation and cache_key and allow_stale_on_error:
                    cached = self.cache_store.get(cache_key)
                    if cached is not None:
                        log.info("github_circuit_open_stale_cache_hit key=%s", cache_key)
                        self.metrics.record_degradation_activated(dependency="github_rest", feature=operation_name)
                        return cached
                raise GitHubResilienceError(
                    error_code=GitHubErrorCode.CIRCUIT_OPEN,
                    message=f"GitHub circuit breaker is OPEN (retry after {retry_after:.1f}s)",
                    user_friendly_message=(
                        f"GitHub service is temporarily unavailable (circuit open). "
                        f"Local Git and Codebase Explorer remain operational. Retry in {max(1, int(round(retry_after)))}s."
                    ),
                    retry_after=retry_after,
                )

        # 3. Rate Limiting Check
        # Check user bucket
        if user_id is not None:
            user_bucket = f"user:{user_id}"
            allowed, retry_after = self.user_rate_limiter.is_allowed(user_bucket)
            if not allowed:
                self.metrics.record_rate_limit(global_=False, identity=True)
                raise GitHubResilienceError(
                    error_code=GitHubErrorCode.RATE_LIMITED,
                    message=f"User rate limit reached for {user_bucket}",
                    user_friendly_message=f"GitHub rate-limited you. Retry after {max(1, int(round(retry_after)))} seconds.",
                    retry_after=retry_after,
                )

        # Check repository bucket
        if owner and repo:
            repo_bucket = f"repo:{owner}/{repo}"
            allowed, retry_after = self.repo_rate_limiter.is_allowed(repo_bucket)
            if not allowed:
                self.metrics.record_rate_limit(global_=False)
                raise GitHubResilienceError(
                    error_code=GitHubErrorCode.RATE_LIMITED,
                    message=f"Repository rate limit reached for {repo_bucket}",
                    user_friendly_message=f"GitHub rate limit reached for repository {owner}/{repo}. Retry after {max(1, int(round(retry_after)))} seconds.",
                    retry_after=retry_after,
                )

        # Check global bucket
        allowed, retry_after = self.global_rate_limiter.is_allowed("global")
        if not allowed:
            self.metrics.record_rate_limit(global_=True)
            raise GitHubResilienceError(
                error_code=GitHubErrorCode.RATE_LIMITED,
                message="Global GitHub API rate limit reached",
                user_friendly_message="GitHub rate limit reached globally. Retry after a few seconds.",
                retry_after=retry_after or 1.0,
            )

        # 4. Cache Check for read operations
        if not is_mutation and cache_key:
            cached_val = self.cache_store.get(cache_key)
            if cached_val is not None:
                self.metrics.record_cache(hit=True)
                log.debug("github_cache_hit key=%s op=%s", cache_key, operation_name)
                return cached_val
            self.metrics.record_cache(hit=False)

        # 5. Bulkhead Execution with Retries & Circuit Breaker
        own_client = False
        target_client = client
        if target_client is None:
            target_client = httpx.AsyncClient(
                timeout=httpx.Timeout(
                    timeout=self.config.read_timeout,
                    connect=self.config.connect_timeout,
                )
            )
            own_client = True

        max_attempts = 1 + (0 if is_mutation else self.config.max_retries)
        last_error: Optional[BaseException] = None

        try:
            async with self.github_semaphore:
                for attempt in range(1, max_attempts + 1):
                    try:
                        # Wrap execution under circuit breaker
                        async def _op() -> T:
                            res = await coro_fn(target_client)
                            if isinstance(res, httpx.Response) and res.status_code >= 400:
                                res.raise_for_status()
                            return res

                        result = await self.circuit_breaker.call(_op)


                        # Execution Succeeded!
                        duration_ms = (time.perf_counter_ns() - start_time) / 1_000_000
                        self.metrics.record_request(success=True, latency_ms=duration_ms)
                        log.debug(
                            "github_request_success op=%s request_id=%s attempt=%d duration_ms=%.1f",
                            operation_name, rid, attempt, duration_ms,
                        )

                        # Store in cache if eligible
                        if not is_mutation and cache_key:
                            ttl = cache_ttl or self.config.cache_ttl_seconds
                            self.cache_store.set(cache_key, result, ttl)

                        # Invalidate related cache prefixes on successful mutation
                        if is_mutation and invalidate_prefixes:
                            for prefix in invalidate_prefixes:
                                count = self.cache_store.invalidate_prefix(prefix)
                                log.debug("github_cache_invalidated prefix=%s count=%d", prefix, count)

                        # Complete or fail idempotency record based on domain result
                        if is_mutation and idempotency_key:
                            is_failure_obj = (
                                getattr(result, "success", None) is False
                                or (isinstance(result, dict) and result.get("success") is False)
                            )
                            if is_failure_obj:
                                err = getattr(result, "error", None) or (result.get("error") if isinstance(result, dict) else "Failed")
                                await self.idempotency_store.fail(idempotency_key, error=str(err))
                            else:
                                await self.idempotency_store.complete(idempotency_key, result)

                        return result

                    except BaseException as exc:
                        if isinstance(exc, asyncio.CancelledError):
                            if is_mutation and idempotency_key:
                                await self.idempotency_store.fail(idempotency_key, error="Cancelled", exc=exc)
                            raise

                        norm_err = normalize_github_exception(exc)
                        last_error = norm_err

                        # Non-retriable failure -> raise immediately
                        if not is_retriable_error(norm_err) or is_mutation:
                            duration_ms = (time.perf_counter_ns() - start_time) / 1_000_000
                            self.metrics.record_request(success=False, latency_ms=duration_ms)
                            if is_mutation and idempotency_key:
                                await self.idempotency_store.fail(idempotency_key, error=norm_err.message, exc=norm_err)
                            raise norm_err

                        # Retriable failure -> evaluate retry backoff
                        self.metrics.record_retry(success=False)
                        if attempt == max_attempts:
                            log.warning(
                                "github_retry_exhausted op=%s attempts=%d error=%s",
                                operation_name, attempt, norm_err.error_code,
                            )
                            break

                        # Extract retry_after or calculate exponential backoff with full jitter
                        retry_after = norm_err.retry_after
                        delay = self.retry_policy.backoff_seconds(attempt, retry_after)
                        log.info(
                            "github_retry_scheduled op=%s attempt=%d delay_s=%.2f error=%s",
                            operation_name, attempt, delay, norm_err.error_code,
                        )
                        await asyncio.sleep(delay)

            # All retries exhausted
            duration_ms = (time.perf_counter_ns() - start_time) / 1_000_000
            self.metrics.record_request(success=False, latency_ms=duration_ms)

            # Allow stale read fallback ONLY on downstream provider outage, timeout, or circuit open
            if (
                not is_mutation
                and cache_key
                and allow_stale_on_error
                and last_error is not None
                and getattr(last_error, "error_code", None) in {
                    GitHubErrorCode.PROVIDER_UNAVAILABLE,
                    GitHubErrorCode.CIRCUIT_OPEN,
                    GitHubErrorCode.TIMEOUT,
                }
            ):
                cached = self.cache_store.get(cache_key)
                if cached is not None:
                    log.info("github_degraded_stale_cache_served key=%s op=%s", cache_key, operation_name)
                    self.metrics.record_degradation_activated(dependency="github_rest", feature=operation_name)
                    return cached


            final_err = normalize_github_exception(last_error or RuntimeError("Retry exhausted"))
            if is_mutation and idempotency_key:
                await self.idempotency_store.fail(idempotency_key, error=final_err.message, exc=final_err)
            raise final_err

        finally:
            if own_client and target_client is not None:
                await target_client.aclose()


# Global Singleton Instance
_GLOBAL_GITHUB_RESILIENCE_BOUNDARY: Optional[GitHubResilienceBoundary] = None


def get_github_boundary() -> GitHubResilienceBoundary:
    """Retrieve or initialize the centralized GitHub resilience boundary singleton."""
    global _GLOBAL_GITHUB_RESILIENCE_BOUNDARY
    if _GLOBAL_GITHUB_RESILIENCE_BOUNDARY is None:
        _GLOBAL_GITHUB_RESILIENCE_BOUNDARY = GitHubResilienceBoundary()
    return _GLOBAL_GITHUB_RESILIENCE_BOUNDARY


def set_github_boundary(boundary: GitHubResilienceBoundary) -> None:
    """Explicitly inject a resilience boundary (useful for testing)."""
    global _GLOBAL_GITHUB_RESILIENCE_BOUNDARY
    _GLOBAL_GITHUB_RESILIENCE_BOUNDARY = boundary


async def poll_workflow_run_until_completed(
    poll_fn: Callable[[], Awaitable[Any]],
    max_attempts: Optional[int] = None,
    interval_seconds: Optional[float] = None,
    boundary: Optional[GitHubResilienceBoundary] = None,
) -> Tuple[Any, bool]:
    """Safely poll a CI workflow run until completion or bounded deadline.
    
    Returns:
        (last_known_run, is_degraded)
    """
    b = boundary or get_github_boundary()
    limit = max_attempts or b.config.ci_poll_max_attempts
    interval = interval_seconds or b.config.ci_poll_interval

    last_run = None
    for attempt in range(1, limit + 1):
        # Stop immediately if circuit breaker is tripped
        if b.circuit_breaker.is_open:
            log.warning("ci_polling_aborted_circuit_open attempt=%d/%d", attempt, limit)
            return last_run, True

        try:
            run = await poll_fn()
            last_run = run
            if run is not None and getattr(run, "status", None) == "completed":
                return run, False
        except GitHubResilienceError as err:
            if err.error_code in {GitHubErrorCode.CIRCUIT_OPEN, GitHubErrorCode.RATE_LIMITED, GitHubErrorCode.PROVIDER_UNAVAILABLE}:
                log.warning("ci_polling_stopped_error attempt=%d/%d code=%s", attempt, limit, err.error_code)
                return last_run, True
            raise

        if attempt < limit:
            await asyncio.sleep(interval)

    log.info("ci_polling_deadline_reached attempts=%d", limit)
    return last_run, False

