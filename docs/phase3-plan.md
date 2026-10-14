# Phase 3.3 Implementation Plan

## Current State Assessment

**Phase 3.2 has implemented:**
- Real vendor adapters: `CursorAdapter`, `CodexAdapter`, `JulesAdapter`
- `GenericCLIAdapter` with subprocess management, stream readers, secret redaction, event emission
- New modules: `resilience.py`, `degradation.py`, `metrics.py`, `latency.py`
- `runtime.py` with all 5 agent commands (list, status, start, stop, prompt)
- `control_plane.py` with dispatch_command, caching, single-flight
- `manager.py` with basic session lifecycle (create, start, stop, prompt, subscribe, cleanup)
- Comprehensive test suites for adapters

**Gaps Phase 3.3 must fill:**
1. No state transition validation (invalid transitions silently accepted)
2. No generation tracking/incrementing for stale-session protection
3. No stale-session rejection mechanism
4. Event queue backpressure with coalescing (silent drops only)
5. No timeout on stop_session
6. No process failure detection integration in manager
7. Incomplete session cleanup (no task cancellation)
8. Missing comprehensive tests for lifecycle, concurrency, failure recovery

## Implementation Plan

### Step 1: Add state transition validation to manager.py
- Add `_validate_transition(current, desired)` method
- Define valid transitions per Phase 3.3 spec
- Raise `InvalidStateTransitionError` on invalid transitions
- Update start_session, stop_session to use validation

### Step 2: Add session generation management
- Auto-increment generation on session restart
- Track generation in session metadata
- Add stale-session check in send_prompt, stop_session

### Step 3: Add bounded output/event coalescing
- Add `MAX_EVENT_PAYLOAD_SIZE` constant
- Add event coalescing for rapid repeated events
- Add batching logic for output events
- Ensure lifecycle events (STARTED, STOPPED, FAILED) are never dropped

### Step 4: Add timeout to stop_session
- Wrap adapter.stop() in asyncio.wait_for with configurable timeout
- Force cleanup on timeout

### Step 5: Add session cleanup improvements
- Cancel pending reader/watchdog tasks on cleanup
- Close event queues properly
- Remove all per-session state

### Step 6: Add session status reconciliation
- Add `_reconcile_status(session)` that checks adapter status and updates session
- Handle process exit detection for CLI adapters
- Return UNKNOWN when actual state cannot be determined

### Step 7: Write comprehensive Phase 3.3 tests
- Test invalid state transitions
- Test session generation/stale protection
- Test concurrent sessions (3+ simultaneous)
- Test event sequencing per session
- Test event backpressure/coalescing
- Test process failure detection
- Test graceful vs forced stop
- Test timeout handling
- Test cancellation safety
- Test session cleanup (no leaks)
- Test prompt routing to correct session
- Test status sync with actual adapter state

### Files to modify:
- `sutra/agents/manager.py` — state validation, generation, timeouts, cleanup
- `sutra/agents/errors.py` — add InvalidStateTransitionError
- `tests/test_session_lifecycle.py` — new test file for Phase 3.3

### Files NOT to modify:
- Any adapter (cursor, codex, jules, generic_cli)
- models.py
- registry.py
- security.py
- runtime.py
- control_plane.py
- Any Phase 2/2.5/2.6 files
