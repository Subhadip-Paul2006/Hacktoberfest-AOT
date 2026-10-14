# AgentBridge — Resilience & Reliability API Reference

## 1. Overview

The Resilience API module ([`sutra/git/resilience.py`](../../sutra/git/resilience.py)) and core resilience primitives ([`sutra/resilience.py`](../../sutra/resilience.py)) provide unified, robust reliability tooling across all Git and GitHub interactions.

---

## 2. GitHubResilienceBoundary

The centralized execution boundary singleton that coordinates all rate limiting, retries, circuit breaking, caching, idempotency, and error normalization.

### Methods

#### `get_github_boundary() -> GitHubResilienceBoundary`
Retrieves or instantiates the global singleton instance.

#### `async execute(...) -> T`
Executes an arbitrary asynchronous coroutine function behind the full resilience perimeter.

```python
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
```

**Parameters:**
- `operation_name`: Descriptive identifier for logs and metrics (e.g. `"github.pr.get"`).
- `coro_fn`: Asynchronous function accepting an `httpx.AsyncClient` parameter and returning the operation result.
- `owner`: Optional repository owner for repo-scoped rate limiting.
- `repo`: Optional repository name for repo-scoped rate limiting.
- `user_id`: Optional Telegram user ID for user-scoped rate limiting.
- `cache_key`: Key for looking up or caching successful read responses.
- `cache_ttl`: Optional TTL override in seconds. Defaults to `config.cache_ttl_seconds`.
- `idempotency_key`: Unique deduplication key for mutations.
- `invalidate_prefixes`: Sequence of cache prefixes to invalidate upon successful mutation.
- `is_mutation`: Boolean flag. When `True`, retries are disabled by default and idempotency is enforced.
- `client`: Optional caller-injected `httpx.AsyncClient` for testing or custom session pooling.
- `allow_stale_on_error`: When `True`, allows serving cached data in degraded mode if downstream provider is down.

---

## 3. Configuration & Environment Variables

All settings in [`GitHubResilienceConfig`](../../sutra/git/resilience.py) are dynamically configurable via environment variables:

| Environment Variable | Python Config Attribute | Default Value | Description |
| :--- | :--- | :--- | :--- |
| `SUTRA_GITHUB_CONNECT_TIMEOUT` | `connect_timeout` | `5.0` s | HTTP connection establishment timeout |
| `SUTRA_GITHUB_READ_TIMEOUT` | `read_timeout` | `15.0` s | HTTP response read timeout |
| `SUTRA_GITHUB_MAX_RETRIES` | `max_retries` | `3` | Maximum retry attempts for transient read errors |
| `SUTRA_GITHUB_BASE_DELAY` | `base_delay` | `0.5` s | Initial base delay for exponential backoff |
| `SUTRA_GITHUB_MAX_DELAY` | `max_delay` | `8.0` s | Upper bound delay cap for backoff with jitter |
| `SUTRA_GITHUB_CIRCUIT_THRESHOLD` | `circuit_threshold` | `5` | Consecutive failures before circuit breaker trips `OPEN` |
| `SUTRA_GITHUB_CIRCUIT_COOLDOWN` | `circuit_cooldown` | `30.0` s | Cooldown period before transitioning to `HALF_OPEN` |
| `SUTRA_GITHUB_MAX_CONCURRENCY` | `max_concurrency` | `10` | Maximum simultaneous in-flight GitHub REST requests |
| `SUTRA_LOCAL_GIT_MAX_CONCURRENCY`| `local_git_max_concurrency` | `4` | Maximum simultaneous local Git subprocess executions |
| `SUTRA_GITHUB_RATE_LIMIT_USER` | `rate_limit_user` | `30` | Requests allowed per user within sliding window |
| `SUTRA_GITHUB_RATE_LIMIT_REPO` | `rate_limit_repo` | `60` | Requests allowed per repo within sliding window |
| `SUTRA_GITHUB_RATE_LIMIT_GLOBAL`| `rate_limit_global` | `120` | Requests allowed globally within sliding window |
| `SUTRA_GITHUB_RATE_LIMIT_WINDOW`| `rate_limit_window` | `60.0` s | Sliding window duration |
| `SUTRA_GITHUB_CACHE_TTL` | `cache_ttl_seconds` | `60.0` s | Default TTL for cached read operations |
| `SUTRA_GIT_TIMEOUT` | *(in `sutra/git/local.py`)* | `30.0` s | Maximum execution duration for local Git subprocesses |

---

## 4. Error Classes & Enums

### `GitHubErrorCode`
Enumeration of normalized error categories:
- `AUTHENTICATION_ERROR`
- `AUTHORIZATION_ERROR`
- `RATE_LIMITED`
- `TIMEOUT`
- `NETWORK_ERROR`
- `NOT_FOUND`
- `CONFLICT`
- `STALE_STATE`
- `VALIDATION_ERROR`
- `PROVIDER_UNAVAILABLE`
- `CIRCUIT_OPEN`
- `LOCAL_GIT_TIMEOUT`
- `WORKSPACE_CONFLICT`
- `UNKNOWN_ERROR`

### `GitHubResilienceError`
Standard normalized exception thrown by the boundary.
- `error_code: GitHubErrorCode`
- `message: str` (Sanitized and redacted)
- `user_friendly_message: str` (Safe for Telegram presentation)
- `status_code: Optional[int]` (HTTP status if applicable)
- `retry_after: Optional[float]` (Wait duration in seconds if rate limited)
- `details: Dict[str, Any]` (Additional non-sensitive metadata)
