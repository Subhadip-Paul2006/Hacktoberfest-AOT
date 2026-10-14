# Platform and Performance Matrix

| Dimension | Evidence from this audit | Status / limitation |
|---|---|---|
| Windows 11-class host | Pre-change baseline suite completed on Windows, Python 3.13; the post-change run was interrupted at approximately 51% with two visible failures and no tracebacks | Only this OS/runtime combination has baseline suite evidence; post-change status is unknown. |
| Linux | No Linux runner used | Unverified; run full suite and POSIX process cleanup regression. |
| macOS | No macOS runner used | Unverified; run full suite and POSIX process cleanup regression. |
| Python 3.11 | Not run in this environment | Supported by package metadata, not verified here. |
| Python 3.12 | Not run in this environment | Supported by package metadata, not verified here. |
| Python 3.13 | Pre-change baseline full suite passed; post-change run interrupted | Baseline verified on current Windows host; post-change outcome is unknown. |
| External API latency, throttling, provider outages | Mock/local test servers only | Not measured; no live services contacted. |
| Terminal output memory | Before fix, a 16 MiB child output produced 34,088,667 bytes traced peak with 1 KiB cap because all output was buffered. After fix, test asserts peak below 4 MiB while retaining no more than 1 KiB; focused regression passed. | Bounded in-process capture demonstrated by regression, not an OS-level RSS benchmark. |
| Large file previews/exports | Repository tests cover configured preview and export behavior | The manual plan must validate representative 50–120 MiB files on each target OS. |
| Process tree cleanup | Windows child-process tests; POSIX test is skipped on Windows | POSIX process-group behavior remains unverified. |
| Throughput/concurrency/load | No dedicated load benchmark performed | Establish workload-specific latency, memory, queue and cancellation targets before production use. |
| Package build/install | Editable import and CLI help worked; no release artifact build gate exists | Build wheel/sdist and validate clean installs on all supported OS/Python pairs. |

## Measurement notes

The memory regression uses Python `tracemalloc`, so it measures traced Python allocations and does not include all OS pipe buffers, native allocations, or subprocess RSS. The bound is meaningful for detecting the previous unbounded Python buffering behavior, but it is not a full process memory benchmark. No external service performance numbers are available from this run.
