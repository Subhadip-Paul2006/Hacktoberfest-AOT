"""Allowlisted Telegram ChatOps bot routed through the control plane."""
from __future__ import annotations

import asyncio
import contextvars
import logging
import math
import os
import re
import secrets
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import httpx
from fastapi import HTTPException

from ...adapters import AgentRegistry
from ...control_plane import ControlPlane
from ...resilience import (
    CircuitBreaker,
    CircuitOpenError,
    IdempotencyStore,
    RetryPolicy,
    SlidingWindowRateLimiter,
    with_timeout_and_retry,
)
from ...chunking import chunk_message
from ...redaction import redact_secrets
from ..common.base import BaseBotApp, AppStatus
from .keyboards import (
    NAVIGATION_KEYBOARD,
    allocate_target_button,
    build_button,
    build_nav_buttons,
)
from .formatting import (
    _CredentialRedactionFilter,
    _access_root_key,
    connectivity_label,
    format_devices,
    format_result,
    format_time,
    render_screen,
)
from .callbacks import handle_callback
from .handlers import (
    dispatch_command,
    do_process_update,
    internal_process_update,
    process_update,
    valid_project_context,
)

log = logging.getLogger("sutra.telegram_bot")
_TELEGRAM_WEBHOOK_SECRET = re.compile(r"^[A-Za-z0-9_-]{1,256}$")


@dataclass(frozen=True)
class MenuReply:
    text: str
    inline_keyboard: list[list[dict[str, str]]]
    chat_id: int
    user_id: int
    message_id: int | None = None
    callback_query_id: str | None = None
    show_navigation: bool = False
    document_name: str | None = None
    document_bytes: bytes | None = None


class TelegramBot(BaseBotApp):
    """Interactive ChatOps control plane interface for Telegram."""

    def __init__(
        self,
        control_plane: ControlPlane,
        token: str,
        allowed_user_ids: Set[int],
        webhook_url: str = "",
        webhook_secret: str = "",
        *,
        max_connections: Optional[int] = None,
        max_keepalive_connections: Optional[int] = None,
        keepalive_timeout: Optional[float] = None,
        idempotency_store: Optional[IdempotencyStore] = None,
    ) -> None:
        super().__init__(
            name="telegram",
            status=AppStatus.ENABLED if token and allowed_user_ids else AppStatus.DISABLED,
        )
        self.control_plane = control_plane
        self.token = token
        self.allowed_user_ids = allowed_user_ids
        self.webhook_url = webhook_url
        self.webhook_secret = webhook_secret
        if webhook_url or webhook_secret:
            if not webhook_url.startswith("https://") or not _TELEGRAM_WEBHOOK_SECRET.fullmatch(webhook_secret):
                raise ValueError("Telegram webhook requires an https URL and a valid secret token")
        self.max_connections = max_connections if max_connections is not None else int(
            os.environ.get("HTTP_POOL_MAX_CONNECTIONS", os.environ.get("TELEGRAM_POOL_MAX_CONNECTIONS", "20")))
        self.max_keepalive_connections = max_keepalive_connections if max_keepalive_connections is not None else int(
            os.environ.get("HTTP_POOL_MAX_KEEPALIVE", os.environ.get("TELEGRAM_POOL_MAX_KEEPALIVE", "10")))
        self.keepalive_timeout = keepalive_timeout if keepalive_timeout is not None else float(
            os.environ.get("HTTP_POOL_KEEPALIVE_TIMEOUT", os.environ.get("TELEGRAM_POOL_KEEPALIVE_TIMEOUT", "30.0")))
        self.idempotency_store = idempotency_store or IdempotencyStore.from_environment("TELEGRAM_IDEMPOTENCY_")
        self._http_client: Optional[httpx.AsyncClient] = None
        self._active_message_id: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
            f"sutra_telegram_message_{id(self)}", default=None)
        self.api_url = f"https://api.telegram.org/bot{token}"

        redaction_filter = _CredentialRedactionFilter(token)
        log.addFilter(redaction_filter)
        logging.getLogger("sutra.telegram_bot").addFilter(redaction_filter)
        logging.getLogger("sutra.apps.telegram").addFilter(redaction_filter)
        for logger_name in ("httpx", "httpcore"):
            logger = logging.getLogger(logger_name)
            logger.setLevel(logging.WARNING)
            logger.addFilter(redaction_filter)

        self.agents = AgentRegistry()
        self.selected_devices: Dict[int, str] = {}
        self.screen_messages: Dict[Tuple[int, int], int] = {}
        self.callback_targets: Dict[str, Dict[str, Any]] = {}
        self.navigation_history: Dict[int, List[Tuple[str, Dict[str, Any]]]] = {}
        self.pending_permissions: Dict[str, Dict[str, Any]] = {}
        self.pending_agent_prompts: Dict[int, Dict[str, Any]] = {}
        self.agent_delivery_tasks: Dict[str, asyncio.Task[Any]] = {}
        self.agent_file_baselines: Dict[str, Dict[str, str] | None] = {}
        self.active_file_context: Dict[int, Dict[str, Any]] = {}
        self.active_git_context: Dict[int, Dict[str, Any]] = {}
        self.active_stage_selection: Dict[int, set[str]] = {}
        self.rate_limiter = getattr(control_plane, "rate_limiter", None) or SlidingWindowRateLimiter.from_environment()
        self.retry_policy = getattr(control_plane, "retry_policy", None) or RetryPolicy.from_environment()
        self.circuit_breaker = getattr(control_plane, "telegram_circuit_breaker", None) or CircuitBreaker.from_environment("telegram_api")
        self.max_concurrency = max(1, int(os.environ.get("MAX_TELEGRAM_CONCURRENCY", 10)))
        self._update_semaphore = asyncio.Semaphore(self.max_concurrency)

    def is_enabled(self) -> bool:
        """Check if Telegram ChatOps is properly configured."""
        return bool(self.token and self.allowed_user_ids)

    def get_http_client(self) -> httpx.AsyncClient:
        """Return the shared persistent pooled HTTP client, creating it if needed."""
        if self._http_client is None or self._http_client.is_closed:
            limits = httpx.Limits(
                max_connections=self.max_connections,
                max_keepalive_connections=self.max_keepalive_connections,
                keepalive_expiry=self.keepalive_timeout,
            )
            self._http_client = httpx.AsyncClient(
                limits=limits,
                timeout=httpx.Timeout(30.0, connect=10.0),
            )
        return self._http_client

    async def aclose(self) -> None:
        """Cleanly close the persistent HTTP client and release connection pool sockets."""
        tasks = list(self.agent_delivery_tasks.values())
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.agent_delivery_tasks.clear()
        client = self._http_client
        self._http_client = None
        if client is not None and not client.is_closed:
            await client.aclose()

    async def __aenter__(self) -> TelegramBot:
        self.get_http_client()
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.aclose()

    async def _telegram_request(
        self,
        client: Optional[httpx.AsyncClient],
        method: str,
        payload: Dict[str, Any],
        *,
        idempotency_key: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        started_ns = time.perf_counter_ns()
        http_client = client if client is not None else self.get_http_client()
        try:
            async def _post() -> Optional[Dict[str, Any]]:
                response = await http_client.post(self.api_url + "/" + method, json=payload)
                response.raise_for_status()
                result = response.json()
                if isinstance(result, dict) and result.get("ok"):
                    return result
                return {"ok": True} if isinstance(result, dict) and result.get("result") is True else None

            res = await with_timeout_and_retry(
                _post,
                timeout_seconds=15.0,
                policy=self.retry_policy,
                circuit_breaker=self.circuit_breaker,
                idempotency_store=self.idempotency_store if idempotency_key else None,
                idempotency_key=idempotency_key,
                payload=payload,
                operation_name=f"telegram_{method}",
            )
            self.control_plane.metrics.record("telegram_api", (time.perf_counter_ns() - started_ns) / 1_000_000)
            return res
        except CircuitOpenError as exc:
            log.warning("Telegram request skipped; circuit is OPEN (retry after %.1fs)", exc.retry_after)
            return None
        except (httpx.HTTPError, ValueError, TypeError, TimeoutError, asyncio.TimeoutError) as exc:
            log.warning("Telegram request failed (%s)", type(exc).__name__)
            return None

    async def _send_text(self, client: Optional[httpx.AsyncClient], chat_id: int, text: str) -> Optional[Dict[str, Any]]:
        sanitized = redact_secrets(text)
        chunks = chunk_message(sanitized, max_chunk_size=3800)
        res = None
        for chunk in chunks:
            res = await self._telegram_request(client, "sendMessage", {
                "chat_id": chat_id, "text": chunk, "reply_markup": NAVIGATION_KEYBOARD})
        return res

    async def _send_reply(self, client: Optional[httpx.AsyncClient], reply: MenuReply) -> None:
        http_client = client if client is not None else self.get_http_client()
        if reply.callback_query_id:
            await self._telegram_request(http_client, "answerCallbackQuery", {
                "callback_query_id": reply.callback_query_id})
        if reply.show_navigation:
            await self._send_text(http_client, reply.chat_id, "SŪTRA navigation")

        sanitized_text = redact_secrets(reply.text)
        chunks = chunk_message(sanitized_text, max_chunk_size=3800)
        first_chunk = chunks[0] if chunks else ""

        if reply.message_id is not None:
            edit_started_ns = time.perf_counter_ns()
            result = await self._telegram_request(http_client, "editMessageText", {
                "chat_id": reply.chat_id, "message_id": reply.message_id,
                "text": first_chunk, "reply_markup": {"inline_keyboard": reply.inline_keyboard}})
            if result is None:
                result = await self._telegram_request(http_client, "sendMessage", {
                    "chat_id": reply.chat_id, "text": first_chunk,
                    "reply_markup": {"inline_keyboard": reply.inline_keyboard}})
            send_ms = (time.perf_counter_ns() - edit_started_ns) / 1_000_000
            self.control_plane.metrics.record("telegram_message_edit", send_ms)
            self.control_plane.metrics.record("telegram_send_ms", send_ms)
        else:
            send_started_ns = time.perf_counter_ns()
            result = await self._telegram_request(http_client, "sendMessage", {
                "chat_id": reply.chat_id, "text": first_chunk,
                "reply_markup": {"inline_keyboard": reply.inline_keyboard}})
            send_ms = (time.perf_counter_ns() - send_started_ns) / 1_000_000
            self.control_plane.metrics.record("telegram_send_ms", send_ms)

        if len(chunks) > 1:
            for extra_chunk in chunks[1:]:
                await self._telegram_request(http_client, "sendMessage", {
                    "chat_id": reply.chat_id, "text": extra_chunk})
        message = (result or {}).get("result") or {}
        if isinstance(message, dict) and isinstance(message.get("message_id"), int):
            self.screen_messages[(reply.chat_id, reply.user_id)] = message["message_id"]
        if reply.document_name and reply.document_bytes is not None:
            try:
                await http_client.post(self.api_url + "/sendDocument",
                    data={"chat_id": str(reply.chat_id), "caption": "Secret-patterns redacted text export"},
                    files={"document": (reply.document_name, reply.document_bytes, "text/plain")})
            except httpx.HTTPError as exc:
                log.warning("Telegram document response failed (%s)", type(exc).__name__)

    def _select_device(self, device_id: Optional[str] = None, user_id: Optional[int] = None) -> str:
        devices = self.control_plane.store.list_devices(set(self.control_plane.sessions))
        requested = device_id or (self.selected_devices.get(user_id) if user_id is not None else None)
        if requested:
            selected = next((item for item in devices if item["device_id"] == requested
                             and item["connection_status"] != "REVOKED"), None)
            if not selected:
                raise HTTPException(404, "device not found")
            return requested
        connected = [item["device_id"] for item in devices if item["connection_status"] == "CONNECTED"]
        if len(connected) == 1:
            return connected[0]
        if len(connected) > 1:
            raise HTTPException(409, "select a device first")
        paired = [item["device_id"] for item in devices if item["connection_status"] != "REVOKED"]
        if len(paired) == 1:
            return paired[0]
        raise HTTPException(409, "select a paired device first")

    def _devices(self) -> List[Dict[str, Any]]:
        return self.control_plane.store.list_devices(set(self.control_plane.sessions))

    def _target_button(
        self,
        label: str,
        user_id: int,
        chat_id: int,
        action: str,
        context: Dict[str, Any],
    ) -> Dict[str, str]:
        return allocate_target_button(
            self.callback_targets,
            label,
            user_id,
            chat_id,
            action,
            context,
            self.control_plane,
        )

    @staticmethod
    def _button(label: str, action: str) -> Dict[str, str]:
        return build_button(label, action)

    def _nav_buttons(self, *, back: str = "nav:home") -> List[List[Dict[str, str]]]:
        return build_nav_buttons(back=back)

    def _reply(
        self,
        text: str,
        chat_id: int,
        user_id: int,
        keyboard: Optional[List[List[Dict[str, str]]]] = None,
        *,
        message_id: Optional[int] = None,
        callback_query_id: Optional[str] = None,
        show_navigation: bool = False,
        document_name: Optional[str] = None,
        document_bytes: Optional[bytes] = None,
    ) -> MenuReply:
        return MenuReply(
            text,
            keyboard or [],
            chat_id,
            user_id,
            message_id,
            callback_query_id,
            show_navigation,
            document_name,
            document_bytes,
        )

    async def _call_device(
        self,
        user_id: int,
        command: str,
        payload: Optional[Dict[str, Any]] = None,
        *,
        message_id: Optional[int] = None,
        chat_id: Optional[int] = None,
        warning_title: str = "",
    ) -> Tuple[str, Dict[str, Any]]:
        device_id = self._select_device(user_id=user_id)

        async def show_waiting(request_id: str) -> None:
            client = self._http_client
            if not client or message_id is None or chat_id is None or not warning_title:
                return
            retry_action = "projects_retry" if command == "project.list" else "status_retry"
            retry = self._target_button("🔄 Retry", user_id, chat_id, retry_action,
                                        {"device_id": device_id, "command": command})
            await self._telegram_request(client, "editMessageText", {
                "chat_id": chat_id, "message_id": message_id,
                "text": f"Still waiting for {warning_title.lower()} from the runtime…",
                "reply_markup": {"inline_keyboard": [[retry], [self._button("🏠 Home", "nav:home")]]}})

        result = await self.control_plane.dispatch_command(
            device_id,
            command,
            payload or {},
            message_id=str(message_id) if message_id is not None else self._active_message_id.get(),
            warning_callback=show_waiting if warning_title else None,
        )
        body = result.get("payload", {})
        if result.get("status") != "success" or not isinstance(body, dict):
            raise HTTPException(502, "runtime response unavailable")
        return device_id, body

    @staticmethod
    def _connectivity_label(guard: Any) -> str:
        return connectivity_label(guard)

    async def _screen(
        self,
        screen: str,
        context: Dict[str, Any],
        chat_id: int,
        user_id: int,
        message_id: Optional[int] = None,
        callback_query_id: Optional[str] = None,
        show_navigation: bool = False,
    ) -> MenuReply:
        return await render_screen(
            self,
            screen,
            context,
            chat_id,
            user_id,
            message_id=message_id,
            callback_query_id=callback_query_id,
            show_navigation=show_navigation,
        )

    async def _handle_callback(self, update: Dict[str, Any], user_id: int, chat_id: int) -> MenuReply:
        return await handle_callback(self, update, user_id, chat_id)

    async def _valid_project_context(self, user_id: int, context: Dict[str, Any]) -> bool:
        return await valid_project_context(self, user_id, context)

    async def _dispatch_command(
        self,
        device_id: str,
        command: str,
        payload: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        return await dispatch_command(self, device_id, command, payload)

    async def process_update(
        self,
        update: Dict[str, Any],
        client: Optional[httpx.AsyncClient] = None,
    ) -> Any:
        return await process_update(self, update, client)

    async def _process_update(
        self,
        update: Dict[str, Any],
        client: Optional[httpx.AsyncClient] = None,
    ) -> Any:
        return await internal_process_update(self, update, client)

    async def _do_process_update(
        self,
        update: Dict[str, Any],
        client: Optional[httpx.AsyncClient] = None,
    ) -> Any:
        return await do_process_update(self, update, client)

    def _format_devices(self) -> str:
        return format_devices(self._devices())

    @staticmethod
    def _time(timestamp: Any) -> str:
        return format_time(timestamp)

    @staticmethod
    def _format_result(command: str, payload: Any) -> str:
        return format_result(command, payload)

    async def run(self) -> None:
        """Run the Telegram bot long-polling loop or hold webhook state."""
        if not self.token or not self.allowed_user_ids:
            log.info("Telegram polling is disabled; configure bot token and allowlisted user IDs")
            return
        offset = self.control_plane.store.get_telegram_offset()
        client = self.get_http_client()
        try:
            if self.webhook_url:
                if not self.webhook_url.startswith("https://") or not self.webhook_secret:
                    raise ValueError("Telegram webhook requires an https URL and a secret token")
                configured = await self._telegram_request(client, "setWebhook", {
                    "url": self.webhook_url, "secret_token": self.webhook_secret,
                    "allowed_updates": ["message", "callback_query"]})
                if configured is None:
                    raise RuntimeError("Telegram webhook configuration failed")
                await asyncio.Event().wait()
                return
            while True:
                try:
                    response = await client.post(self.api_url + "/getUpdates", json={
                        "offset": offset, "timeout": 20, "allowed_updates": ["message", "callback_query"]})
                    response.raise_for_status()
                    body = response.json()
                    if not body.get("ok"):
                        raise ValueError("Telegram returned an unsuccessful response")
                    updates = body.get("result", [])
                    if updates:
                        async def _process_single(up: Dict[str, Any]) -> None:
                            async with self._update_semaphore:
                                callback = up.get("callback_query") if isinstance(up, dict) else None
                                callback_id = callback.get("id") if isinstance(callback, dict) else None
                                if isinstance(callback_id, str):
                                    ack_started_ns = time.perf_counter_ns()
                                    await self._telegram_request(client, "answerCallbackQuery", {
                                        "callback_query_id": callback_id})
                                    self.control_plane.metrics.record("telegram_callback_ack",
                                        (time.perf_counter_ns() - ack_started_ns) / 1_000_000)
                                interaction_started_ns = time.perf_counter_ns()
                                result = await self.process_update(up, client=client)
                                if isinstance(result, MenuReply):
                                    await self._send_reply(client, replace(result, callback_query_id=None))
                                elif result:
                                    message = up.get("message") or {}
                                    callback = up.get("callback_query") or {}
                                    chat_id = (message.get("chat") or {}).get("id") or ((callback.get("message") or {}).get("chat") or {}).get("id")
                                    if isinstance(chat_id, int):
                                        await self._send_text(client, chat_id, result)
                                if isinstance(callback_id, str):
                                    self.control_plane.metrics.record("telegram_callback_to_response",
                                        (time.perf_counter_ns() - interaction_started_ns) / 1_000_000)

                        tasks = [asyncio.create_task(_process_single(u)) for u in updates]
                        await asyncio.gather(*tasks, return_exceptions=True)
                        for u in updates:
                            update_id = u.get("update_id") if isinstance(u, dict) else None
                            if isinstance(update_id, int):
                                offset = max(offset, update_id + 1)
                except asyncio.CancelledError:
                    raise
                except (httpx.HTTPError, ValueError, TypeError) as exc:
                    log.warning("Telegram polling failed (%s)", type(exc).__name__)
                    await asyncio.sleep(2)
        finally:
            await self.aclose()

    async def handle_webhook_update(self, update: Dict[str, Any]) -> None:
        """Handle incoming webhook update payload."""
        client = self.get_http_client()
        callback = update.get("callback_query")
        callback_id = callback.get("id") if isinstance(callback, dict) else None
        if isinstance(callback_id, str):
            await self._telegram_request(client, "answerCallbackQuery", {"callback_query_id": callback_id})
        result = await self.process_update(update, client=client)
        if isinstance(result, MenuReply):
            await self._send_reply(client, replace(result, callback_query_id=None))
        elif isinstance(result, str) and result:
            message = update.get("message") or {}
            callback_message = (update.get("callback_query") or {}).get("message") or {}
            chat_id = (message.get("chat") or {}).get("id") or (callback_message.get("chat") or {}).get("id")
            if isinstance(chat_id, int):
                await self._send_text(client, chat_id, result)
