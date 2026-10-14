# Extending Agent Adapters

AgentBridge supports a wide variety of AI coding agents through its adapter layer located in [`sutra/agents/`](../../sutra/agents).

---

## 1. Adapter Contract

All adapters inherit from [`BaseAgentAdapter`](../../sutra/agents/models.py) and must implement the following core lifecycle methods:

```python
from sutra.agents.models import BaseAgentAdapter, AgentSessionStatus, AgentDescriptor

class CustomAgentAdapter(BaseAgentAdapter):
    @classmethod
    def descriptor(cls) -> AgentDescriptor:
        """Returns metadata about the agent, capabilities, and executable requirements."""
        ...

    async def is_available(self) -> bool:
        """Checks if the CLI tool or binary is installed and executable on the host."""
        ...

    async def start(self, project_path: str, environment: dict[str, str]) -> None:
        """Launches the agent subprocess with sanitized environment and bounds."""
        ...

    async def prompt(self, prompt_text: str) -> None:
        """Sends a user instruction to the running agent via stdin or API."""
        ...

    async def stop(self, timeout_seconds: float = 5.0) -> None:
        """Gracefully terminates the subprocess, ensuring process tree cleanup."""
        ...
```

---

## 2. Process Tree Teardown Safety

On Windows, child processes created by CLI tools often fail to exit on standard termination signals. AgentBridge adapters must implement robust process-tree teardown:

```python
import sys
import subprocess

async def _kill_process_tree(pid: int) -> None:
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True)
    else:
        import os, signal
        os.killpg(os.getpgid(pid), signal.SIGKILL)
```

---

## 3. Streaming and Backpressure

Adapters should stream stdout and stderr through debounced asynchronous queues to prevent buffer exhaustion and preserve monotonic sequence ordering:

```python
await self.event_sink.emit(
    event_type="AGENT_OUTPUT",
    session_id=self.session_id,
    generation=self.generation,
    data={"chunk": chunk_text}
)
```
