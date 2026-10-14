"""Validation tests for HTTP Connection Pooling in SŪTRA."""
from __future__ import annotations

import asyncio
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from sutra.control_plane import ControlPlane
from sutra.resilience import (
    CircuitBreaker,
    CircuitOpenError,
    RetryPolicy,
    with_timeout_and_retry,
)
from sutra.telegram_bot import TelegramBot


class ConnectionPoolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "cp.sqlite3"
        self.cp = ControlPlane(self.db_path, "test-operator-token-long-enough-32")

    async def asyncTearDown(self):
        self.cp.store.close()
        self.temp.cleanup()

    async def test_telegram_bot_configures_bounded_pool_limits(self):
        bot = TelegramBot(
            self.cp,
            "test-token",
            {42},
            max_connections=15,
            max_keepalive_connections=7,
            keepalive_timeout=25.0,
        )
        try:
            client = bot.get_http_client()
            self.assertIsInstance(client, httpx.AsyncClient)
            self.assertFalse(client.is_closed)

            # Reusing get_http_client returns the same client instance
            client2 = bot.get_http_client()
            self.assertIs(client, client2)

            # Verify configured pool limits
            limits = getattr(client, "_transport", None)
            pool = getattr(limits, "_pool", None)
            if pool is not None:
                # httpcore AsyncConnectionPool bounds
                self.assertEqual(pool._max_connections, 15)
                self.assertEqual(pool._max_keepalive_connections, 7)
                self.assertEqual(pool._keepalive_expiry, 25.0)
        finally:
            await bot.aclose()

    async def test_telegram_bot_environment_configuration(self):
        with patch.dict(os.environ, {
            "HTTP_POOL_MAX_CONNECTIONS": "30",
            "HTTP_POOL_MAX_KEEPALIVE": "12",
            "HTTP_POOL_KEEPALIVE_TIMEOUT": "45.0",
        }):
            bot = TelegramBot(self.cp, "test-token", {42})
            try:
                self.assertEqual(bot.max_connections, 30)
                self.assertEqual(bot.max_keepalive_connections, 12)
                self.assertEqual(bot.keepalive_timeout, 45.0)
            finally:
                await bot.aclose()

    async def test_connection_reuse_across_requests(self):
        """Verify that multiple requests reuse the same persistent connection pool."""
        requests_received = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests_received.append(request.url.path)
            return httpx.Response(200, json={"ok": True, "result": {"message_id": len(requests_received)}})

        transport = httpx.MockTransport(handler)
        limits = httpx.Limits(max_connections=10, max_keepalive_connections=5, keepalive_expiry=30.0)

        bot = TelegramBot(self.cp, "test-token", {42})
        mock_client = httpx.AsyncClient(transport=transport, limits=limits)
        bot._http_client = mock_client

        try:
            # Send 5 sequential requests through bot
            for i in range(5):
                res = await bot._send_text(None, 42, f"Hello {i}")
                self.assertIsNotNone(res)
                self.assertTrue(res.get("ok"))

            # All 5 requests reached the handler using the same mock_client
            self.assertEqual(len(requests_received), 5)
            self.assertFalse(mock_client.is_closed)
        finally:
            await bot.aclose()
            self.assertTrue(mock_client.is_closed)

    async def test_clean_lifecycle_shutdown(self):
        bot = TelegramBot(self.cp, "test-token", {42})
        client = bot.get_http_client()
        self.assertFalse(client.is_closed)

        await bot.aclose()
        self.assertTrue(client.is_closed)
        self.assertIsNone(bot._http_client)

        # Calling aclose again is safe and idempotent
        await bot.aclose()
        self.assertIsNone(bot._http_client)

    async def test_async_context_manager_lifecycle(self):
        bot = TelegramBot(self.cp, "test-token", {42})
        async with bot as b:
            client = b.get_http_client()
            self.assertFalse(client.is_closed)
            self.assertIs(b, bot)

        self.assertTrue(client.is_closed)
        self.assertIsNone(bot._http_client)

    async def test_timeout_and_retry_with_pooled_client(self):
        """Retries through the pooled client should execute through the resilience stack."""
        attempts = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                return httpx.Response(502, json={"ok": False, "description": "Bad Gateway"})
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 999}})

        transport = httpx.MockTransport(handler)
        bot = TelegramBot(self.cp, "test-token", {42})
        bot.retry_policy = RetryPolicy(max_retries=2, base_delay=0.01, max_delay=0.05)
        mock_client = httpx.AsyncClient(transport=transport)
        bot._http_client = mock_client

        try:
            res = await bot._send_text(None, 42, "Test retry message")
            self.assertIsNotNone(res)
            self.assertTrue(res.get("ok"))
            self.assertEqual(attempts, 2)
        finally:
            await bot.aclose()

    async def test_circuit_breaker_guards_pooled_client(self):
        """When circuit is OPEN, requests fail fast and do not touch the connection pool."""
        attempts = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            return httpx.Response(500, json={"ok": False, "description": "Internal Server Error"})

        transport = httpx.MockTransport(handler)
        bot = TelegramBot(self.cp, "test-token", {42})
        bot.retry_policy = RetryPolicy(max_retries=0, base_delay=0.01, max_delay=0.05)
        bot.circuit_breaker = CircuitBreaker("telegram_api", failure_threshold=2, cooldown_seconds=60.0)
        mock_client = httpx.AsyncClient(transport=transport)
        bot._http_client = mock_client

        try:
            # Trip circuit breaker
            await bot._send_text(None, 42, "Trip 1")
            await bot._send_text(None, 42, "Trip 2")
            self.assertEqual(bot.circuit_breaker.state, "OPEN")
            self.assertEqual(attempts, 2)

            # Third call is blocked by circuit breaker without reaching connection pool / transport
            res = await bot._send_text(None, 42, "Blocked by breaker")
            self.assertIsNone(res)
            self.assertEqual(attempts, 2)  # handler was not called!
        finally:
            await bot.aclose()


if __name__ == "__main__":
    unittest.main()
