"""AI Provider Configuration and Gateway for SAMANVAYA · SUTRA.

Supports four execution modes:
  Mode A  - Deterministic / No AI  (no credentials required)
  Mode B  - Hosted API providers   (OpenAI, Anthropic, Gemini, OpenRouter)
  Mode C  - Local inference        (Ollama, LM Studio, llama.cpp, vLLM)
  Mode D  - Custom / self-hosted   (any OpenAI-compatible base URL)

Security invariants:
  - API keys are NEVER logged, printed, or included in repr/str output.
  - Provider status messages use redacted placeholders.
  - No paid inference is ever triggered automatically.
"""
from __future__ import annotations

import enum
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Optional

log = logging.getLogger(__name__)


class AIMode(str, enum.Enum):
    NONE = "none"
    API = "api"
    LOCAL = "local"
    CUSTOM = "custom"


class AIProvider(str, enum.Enum):
    NONE = "none"
    OLLAMA = "ollama"
    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    GEMINI = "gemini"
    OPENROUTER = "openrouter"
    LMSTUDIO = "lmstudio"
    LLAMACPP = "llamacpp"
    VLLM = "vllm"
    CUSTOM = "custom"


class ProviderStatus(str, enum.Enum):
    CONFIGURED = "CONFIGURED"
    READY = "READY"
    CONNECTED = "CONNECTED"
    MODEL_AVAILABLE = "MODEL_AVAILABLE"
    TESTED = "TESTED"
    UNAVAILABLE = "UNAVAILABLE"
    UNVERIFIED = "UNVERIFIED"
    DISABLED = "DISABLED"
    ERROR = "ERROR"


@dataclass
class AgentModelConfig:
    provider: Optional[str] = None
    model: Optional[str] = None
    enabled: bool = True

    def is_inherited(self) -> bool:
        return self.provider is None and self.model is None


@dataclass
class AIConfig:
    mode: AIMode = AIMode.NONE
    provider: AIProvider = AIProvider.NONE
    model: str = ""
    base_url: str = ""
    timeout_seconds: float = 60.0
    max_retries: int = 2
    max_tokens: Optional[int] = None
    extra_params: dict[str, Any] = field(default_factory=dict)
    agent_overrides: dict[str, AgentModelConfig] = field(default_factory=dict)

    @classmethod
    def from_environment(cls) -> "AIConfig":
        raw_provider = os.environ.get("LLM_PROVIDER", "").strip().lower()
        if not raw_provider or raw_provider == "none":
            return cls(mode=AIMode.NONE, provider=AIProvider.NONE)
        provider_map = {
            "ollama": AIProvider.OLLAMA,
            "openai": AIProvider.OPENAI,
            "anthropic": AIProvider.ANTHROPIC,
            "gemini": AIProvider.GEMINI,
            "openrouter": AIProvider.OPENROUTER,
            "lmstudio": AIProvider.LMSTUDIO,
            "lm_studio": AIProvider.LMSTUDIO,
            "llamacpp": AIProvider.LLAMACPP,
            "llama.cpp": AIProvider.LLAMACPP,
            "vllm": AIProvider.VLLM,
            "custom": AIProvider.CUSTOM,
        }
        provider = provider_map.get(raw_provider, AIProvider.CUSTOM)
        mode, base_url, model = _resolve_provider_params(provider)
        timeout = float(os.environ.get("SUTRA_AI_TIMEOUT", "60") or "60")
        retries = int(os.environ.get("SUTRA_AI_MAX_RETRIES", "2") or "2")
        max_tokens_raw = os.environ.get("SUTRA_AI_MAX_TOKENS", "")
        max_tokens = int(max_tokens_raw) if max_tokens_raw.strip() else None
        return cls(
            mode=mode,
            provider=provider,
            model=model,
            base_url=base_url,
            timeout_seconds=max(1.0, timeout),
            max_retries=max(0, retries),
            max_tokens=max_tokens,
        )

    def effective_for_agent(self, agent_id: str) -> tuple[str, str]:
        override = self.agent_overrides.get(agent_id.casefold())
        if override and not override.is_inherited():
            p = override.provider or self.provider.value
            m = override.model or self.model
            return p, m
        return self.provider.value, self.model

    def redacted_summary(self) -> dict[str, str]:
        return {
            "mode": self.mode.value,
            "provider": self.provider.value,
            "model": self.model or "(none)",
            "base_url": self.base_url or "(default)",
            "timeout_seconds": str(self.timeout_seconds),
            "max_retries": str(self.max_retries),
        }


def _resolve_provider_params(provider: AIProvider) -> tuple[AIMode, str, str]:
    if provider == AIProvider.OLLAMA:
        return AIMode.LOCAL, os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434"), os.environ.get("OLLAMA_MODEL", "qwen2.5-coder:7b")
    if provider == AIProvider.LMSTUDIO:
        return AIMode.LOCAL, os.environ.get("LMSTUDIO_BASE_URL", "http://localhost:1234/v1"), os.environ.get("LMSTUDIO_MODEL", "")
    if provider == AIProvider.LLAMACPP:
        return AIMode.LOCAL, os.environ.get("LLAMACPP_BASE_URL", "http://localhost:8080/v1"), os.environ.get("LLAMACPP_MODEL", "")
    if provider == AIProvider.VLLM:
        return AIMode.LOCAL, os.environ.get("VLLM_BASE_URL", "http://localhost:8000/v1"), os.environ.get("VLLM_MODEL", "")
    if provider == AIProvider.OPENAI:
        return AIMode.API, os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"), os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
    if provider == AIProvider.ANTHROPIC:
        return AIMode.API, os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com"), os.environ.get("ANTHROPIC_MODEL", "claude-3-haiku-20240307")
    if provider == AIProvider.GEMINI:
        return AIMode.API, os.environ.get("GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta"), os.environ.get("GEMINI_MODEL", "gemini-1.5-flash")
    if provider == AIProvider.OPENROUTER:
        return AIMode.API, os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"), os.environ.get("OPENROUTER_MODEL", "")
    base_url = os.environ.get("SUTRA_AI_BASE_URL", "")
    model = os.environ.get("SUTRA_AI_MODEL", "")
    mode = AIMode.LOCAL if base_url.startswith(("http://localhost", "http://127.")) else AIMode.CUSTOM
    return mode, base_url, model


def credential_status(provider: AIProvider) -> tuple[bool, str]:
    cred_map = {
        AIProvider.OPENAI: ("OPENAI_API_KEY", ""),
        AIProvider.ANTHROPIC: ("ANTHROPIC_API_KEY", ""),
        AIProvider.GEMINI: ("GEMINI_API_KEY", ""),
        AIProvider.OPENROUTER: ("OPENROUTER_API_KEY", ""),
    }
    if provider not in cred_map:
        return True, ""
    var, fallback = cred_map[provider]
    val = os.environ.get(var, "") or (os.environ.get(fallback, "") if fallback else "")
    return bool(val and val.strip()), var


class ProviderProbeResult:
    def __init__(
        self,
        provider: AIProvider,
        status: ProviderStatus,
        message: str,
        latency_ms: Optional[float] = None,
        models: Optional[list[str]] = None,
    ) -> None:
        self.provider = provider
        self.status = status
        self.message = message
        self.latency_ms = latency_ms
        self.models: list[str] = models or []

    def __repr__(self) -> str:
        return (
            f"ProviderProbeResult(provider={self.provider.value!r}, "
            f"status={self.status.value!r}, message={self.message!r})"
        )


def probe_provider(config: AIConfig, timeout: float = 5.0) -> ProviderProbeResult:
    import time
    if config.mode == AIMode.NONE or config.provider == AIProvider.NONE:
        return ProviderProbeResult(config.provider, ProviderStatus.DISABLED, "AI mode is none - deterministic operation only.")
    has_cred, cred_var = credential_status(config.provider)
    if config.mode == AIMode.API:
        if not has_cred:
            return ProviderProbeResult(config.provider, ProviderStatus.ERROR, f"Missing required credential: {cred_var}")
        return ProviderProbeResult(config.provider, ProviderStatus.CONFIGURED, f"Credential for {config.provider.value} is present. Use --check-provider to run a live probe.")
    base = (config.base_url or "http://localhost:11434").rstrip("/")
    probe_paths = {AIProvider.OLLAMA: "/api/tags", AIProvider.LMSTUDIO: "/v1/models", AIProvider.LLAMACPP: "/health", AIProvider.VLLM: "/v1/models"}
    path = probe_paths.get(config.provider, "/v1/models")
    url = f"{base}{path}"
    try:
        import httpx
        t0 = time.monotonic()
        resp = httpx.get(url, timeout=timeout)
        latency_ms = (time.monotonic() - t0) * 1000
        if resp.status_code < 400:
            models: list[str] = []
            try:
                data = resp.json()
                if isinstance(data, dict):
                    if "models" in data:
                        models = [m.get("name", "") for m in data["models"] if isinstance(m, dict)]
                    elif "data" in data:
                        models = [m.get("id", "") for m in data["data"] if isinstance(m, dict)]
            except Exception:
                pass
            return ProviderProbeResult(config.provider, ProviderStatus.CONNECTED, f"Reachable at {url} (HTTP {resp.status_code}).", latency_ms=round(latency_ms, 1), models=[m for m in models if m])
        return ProviderProbeResult(config.provider, ProviderStatus.UNAVAILABLE, f"Server at {url} returned HTTP {resp.status_code}.", latency_ms=round((time.monotonic() - t0) * 1000, 1))
    except ImportError:
        return ProviderProbeResult(config.provider, ProviderStatus.UNVERIFIED, "httpx not available.")
    except Exception as exc:
        return ProviderProbeResult(config.provider, ProviderStatus.UNAVAILABLE, f"Connection to {url} failed: {type(exc).__name__}: {exc}")
