"""Tests for the SUTRA AI Provider Configuration module.

All tests are fully offline — no live API calls, no paid inference.
"""
from __future__ import annotations

import os
import pytest
from unittest.mock import patch, MagicMock


# ---------------------------------------------------------------------------
# AIConfig.from_environment
# ---------------------------------------------------------------------------

class TestAIConfigFromEnvironment:

    def test_no_provider_returns_none_mode(self):
        with patch.dict(os.environ, {"LLM_PROVIDER": ""}, clear=False):
            from sutra.ai_providers import AIConfig, AIMode, AIProvider
            cfg = AIConfig.from_environment()
            assert cfg.mode == AIMode.NONE
            assert cfg.provider == AIProvider.NONE

    def test_none_provider_explicit(self):
        with patch.dict(os.environ, {"LLM_PROVIDER": "none"}, clear=False):
            from sutra.ai_providers import AIConfig, AIMode
            cfg = AIConfig.from_environment()
            assert cfg.mode == AIMode.NONE

    def test_ollama_provider_local_mode(self):
        env = {"LLM_PROVIDER": "ollama", "OLLAMA_BASE_URL": "http://localhost:11434", "OLLAMA_MODEL": "llama3"}
        with patch.dict(os.environ, env, clear=False):
            from sutra.ai_providers import AIConfig, AIMode, AIProvider
            cfg = AIConfig.from_environment()
            assert cfg.mode == AIMode.LOCAL
            assert cfg.provider == AIProvider.OLLAMA
            assert cfg.model == "llama3"
            assert "11434" in cfg.base_url

    def test_openai_provider_api_mode(self):
        env = {"LLM_PROVIDER": "openai", "OPENAI_MODEL": "gpt-4o"}
        with patch.dict(os.environ, env, clear=False):
            from sutra.ai_providers import AIConfig, AIMode, AIProvider
            cfg = AIConfig.from_environment()
            assert cfg.mode == AIMode.API
            assert cfg.provider == AIProvider.OPENAI
            assert cfg.model == "gpt-4o"

    def test_anthropic_provider_api_mode(self):
        with patch.dict(os.environ, {"LLM_PROVIDER": "anthropic"}, clear=False):
            from sutra.ai_providers import AIConfig, AIMode, AIProvider
            cfg = AIConfig.from_environment()
            assert cfg.mode == AIMode.API
            assert cfg.provider == AIProvider.ANTHROPIC

    def test_gemini_provider_api_mode(self):
        with patch.dict(os.environ, {"LLM_PROVIDER": "gemini"}, clear=False):
            from sutra.ai_providers import AIConfig, AIMode, AIProvider
            cfg = AIConfig.from_environment()
            assert cfg.mode == AIMode.API
            assert cfg.provider == AIProvider.GEMINI

    def test_lmstudio_local_mode(self):
        with patch.dict(os.environ, {"LLM_PROVIDER": "lmstudio"}, clear=False):
            from sutra.ai_providers import AIConfig, AIMode, AIProvider
            cfg = AIConfig.from_environment()
            assert cfg.mode == AIMode.LOCAL
            assert cfg.provider == AIProvider.LMSTUDIO

    def test_timeout_from_env(self):
        with patch.dict(os.environ, {"LLM_PROVIDER": "ollama", "SUTRA_AI_TIMEOUT": "120"}, clear=False):
            from sutra.ai_providers import AIConfig
            cfg = AIConfig.from_environment()
            assert cfg.timeout_seconds == 120.0

    def test_retries_from_env(self):
        with patch.dict(os.environ, {"LLM_PROVIDER": "ollama", "SUTRA_AI_MAX_RETRIES": "5"}, clear=False):
            from sutra.ai_providers import AIConfig
            cfg = AIConfig.from_environment()
            assert cfg.max_retries == 5

    def test_max_tokens_from_env(self):
        with patch.dict(os.environ, {"LLM_PROVIDER": "ollama", "SUTRA_AI_MAX_TOKENS": "2048"}, clear=False):
            from sutra.ai_providers import AIConfig
            cfg = AIConfig.from_environment()
            assert cfg.max_tokens == 2048

    def test_unknown_provider_falls_back_to_custom(self):
        with patch.dict(os.environ, {"LLM_PROVIDER": "some_unknown_provider"}, clear=False):
            from sutra.ai_providers import AIConfig, AIProvider
            cfg = AIConfig.from_environment()
            assert cfg.provider == AIProvider.CUSTOM


# ---------------------------------------------------------------------------
# AIConfig.effective_for_agent
# ---------------------------------------------------------------------------

class TestEffectiveForAgent:

    def test_inherit_returns_global_defaults(self):
        from sutra.ai_providers import AIConfig, AIMode, AIProvider, AgentModelConfig
        cfg = AIConfig(
            mode=AIMode.LOCAL,
            provider=AIProvider.OLLAMA,
            model="llama3",
        )
        p, m = cfg.effective_for_agent("antigravity")
        assert p == "ollama"
        assert m == "llama3"

    def test_agent_override_is_applied(self):
        from sutra.ai_providers import AIConfig, AIMode, AIProvider, AgentModelConfig
        override = AgentModelConfig(provider="openai", model="gpt-4o")
        cfg = AIConfig(
            mode=AIMode.LOCAL,
            provider=AIProvider.OLLAMA,
            model="llama3",
            agent_overrides={"antigravity": override},
        )
        p, m = cfg.effective_for_agent("antigravity")
        assert p == "openai"
        assert m == "gpt-4o"

    def test_partial_override_uses_global_for_missing_field(self):
        from sutra.ai_providers import AIConfig, AIMode, AIProvider, AgentModelConfig
        override = AgentModelConfig(provider="openai", model=None)  # model is None → inherit
        cfg = AIConfig(
            mode=AIMode.API,
            provider=AIProvider.OPENAI,
            model="gpt-4o-mini",
            agent_overrides={"research": override},
        )
        p, m = cfg.effective_for_agent("research")
        assert p == "openai"
        assert m == "gpt-4o-mini"

    def test_case_insensitive_agent_id(self):
        from sutra.ai_providers import AIConfig, AIMode, AIProvider, AgentModelConfig
        override = AgentModelConfig(provider="anthropic", model="claude-3-haiku-20240307")
        cfg = AIConfig(
            mode=AIMode.API,
            provider=AIProvider.OPENAI,
            model="gpt-4o",
            agent_overrides={"Research": override},
        )
        p, m = cfg.effective_for_agent("Research")
        assert p == "anthropic"


# ---------------------------------------------------------------------------
# Credential presence check
# ---------------------------------------------------------------------------

class TestCredentialStatus:

    def test_openai_credential_present(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test-value"}, clear=False):
            from sutra.ai_providers import AIProvider, credential_status
            has, var = credential_status(AIProvider.OPENAI)
            assert has is True
            assert "OPENAI_API_KEY" in var

    def test_openai_credential_missing(self):
        env = dict(os.environ)
        env.pop("OPENAI_API_KEY", None)
        with patch.dict(os.environ, env, clear=True):
            from sutra.ai_providers import AIProvider, credential_status
            has, var = credential_status(AIProvider.OPENAI)
            assert has is False

    def test_local_provider_no_credential_needed(self):
        from sutra.ai_providers import AIProvider, credential_status
        has, var = credential_status(AIProvider.OLLAMA)
        assert has is True
        assert var == ""

    def test_credential_not_logged(self, caplog):
        """Credential values must not appear in any log output."""
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-SUPER_SECRET_KEY_12345"}, clear=False):
            import logging
            with caplog.at_level(logging.DEBUG, logger="sutra.ai_providers"):
                from sutra.ai_providers import AIProvider, credential_status
                credential_status(AIProvider.OPENAI)
        assert "SUPER_SECRET_KEY_12345" not in caplog.text


# ---------------------------------------------------------------------------
# Redacted summary
# ---------------------------------------------------------------------------

class TestRedactedSummary:

    def test_summary_excludes_api_key(self):
        with patch.dict(os.environ, {"LLM_PROVIDER": "openai", "OPENAI_API_KEY": "sk-secret-123"}, clear=False):
            from sutra.ai_providers import AIConfig
            cfg = AIConfig.from_environment()
            summary = cfg.redacted_summary()
            for v in summary.values():
                assert "sk-secret-123" not in v

    def test_summary_contains_expected_fields(self):
        with patch.dict(os.environ, {"LLM_PROVIDER": "ollama"}, clear=False):
            from sutra.ai_providers import AIConfig
            cfg = AIConfig.from_environment()
            s = cfg.redacted_summary()
            assert "mode" in s
            assert "provider" in s
            assert "model" in s
            assert "base_url" in s


# ---------------------------------------------------------------------------
# Provider probe (mocked — no network)
# ---------------------------------------------------------------------------

class TestProviderProbe:

    def test_none_mode_returns_disabled(self):
        from sutra.ai_providers import AIConfig, AIMode, AIProvider, probe_provider, ProviderStatus
        cfg = AIConfig(mode=AIMode.NONE, provider=AIProvider.NONE)
        result = probe_provider(cfg)
        assert result.status == ProviderStatus.DISABLED

    def test_api_mode_missing_credential(self):
        env = dict(os.environ)
        env.pop("OPENAI_API_KEY", None)
        with patch.dict(os.environ, env, clear=True):
            from sutra.ai_providers import AIConfig, AIMode, AIProvider, probe_provider, ProviderStatus
            cfg = AIConfig(mode=AIMode.API, provider=AIProvider.OPENAI, model="gpt-4o", base_url="https://api.openai.com/v1")
            result = probe_provider(cfg)
            assert result.status == ProviderStatus.ERROR
            assert "OPENAI_API_KEY" in result.message

    def test_api_mode_credential_present_returns_configured(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}, clear=False):
            from sutra.ai_providers import AIConfig, AIMode, AIProvider, probe_provider, ProviderStatus
            cfg = AIConfig(mode=AIMode.API, provider=AIProvider.OPENAI, model="gpt-4o", base_url="https://api.openai.com/v1")
            result = probe_provider(cfg)
            assert result.status == ProviderStatus.CONFIGURED
            # Must not contain any credential values
            assert "sk-test" not in result.message

    def test_local_server_unreachable_returns_unavailable(self):
        from sutra.ai_providers import AIConfig, AIMode, AIProvider, probe_provider, ProviderStatus
        cfg = AIConfig(mode=AIMode.LOCAL, provider=AIProvider.OLLAMA, model="llama3",
                       base_url="http://127.0.0.1:19999")  # port no one listens on
        result = probe_provider(cfg, timeout=1.0)
        assert result.status == ProviderStatus.UNAVAILABLE

    def test_local_server_connected(self):
        """Mock a successful Ollama /api/tags response."""
        from sutra.ai_providers import AIConfig, AIMode, AIProvider, probe_provider, ProviderStatus
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"models": [{"name": "llama3"}, {"name": "codellama"}]}
        with patch("httpx.get", return_value=mock_resp):
            cfg = AIConfig(mode=AIMode.LOCAL, provider=AIProvider.OLLAMA, model="llama3",
                           base_url="http://localhost:11434")
            result = probe_provider(cfg, timeout=2.0)
        assert result.status == ProviderStatus.CONNECTED
        assert "llama3" in result.models
        assert result.latency_ms is not None
