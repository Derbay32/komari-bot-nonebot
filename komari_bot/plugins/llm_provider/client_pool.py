"""LLM 客户端生命周期与连接复用。"""

from __future__ import annotations

import asyncio
import hashlib
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from nonebot import logger

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from .config_schema import DynamicConfigSchema


class ManagedLLMClient(Protocol):
    """连接池所需的最小客户端协议。"""

    async def generate_text(self, **kwargs: Any) -> Any: ...

    async def generate_text_with_messages(self, **kwargs: Any) -> Any: ...

    async def test_connection(self, model: str | None = None) -> bool: ...

    async def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class ClientSettings:
    """会影响底层 HTTP 连接的配置快照。"""

    api_token: str = field(repr=False)
    base_url: str
    timeout_seconds: float
    healthcheck_model: str

    @classmethod
    def from_config(cls, config: DynamicConfigSchema) -> ClientSettings:
        token = config.api_token.strip()
        if not token:
            raise ValueError("API Token 未配置，请在配置中设置 api_token")  # noqa: TRY003
        return cls(
            api_token=token,
            base_url=str(config.api_base),
            timeout_seconds=float(config.timeout_seconds),
            healthcheck_model=config.model,
        )

    @property
    def fingerprint(self) -> tuple[str, str, float]:
        """返回不含明文令牌的连接配置指纹。"""
        token_hash = hashlib.sha256(self.api_token.encode()).hexdigest()
        return token_hash, self.base_url, self.timeout_seconds


@dataclass(slots=True)
class _ClientSlot:
    client: ManagedLLMClient
    fingerprint: tuple[str, str, float]
    active_leases: int = 0
    retired: bool = False


class LLMClientPool:
    """以租约保护正在使用的客户端，并在配置变化时原子替换。"""

    def __init__(
        self,
        *,
        settings_getter: Callable[[], ClientSettings],
        client_factory: Callable[[ClientSettings], ManagedLLMClient],
    ) -> None:
        self._settings_getter = settings_getter
        self._client_factory = client_factory
        self._lock = asyncio.Lock()
        self._current: _ClientSlot | None = None
        self._retired: list[_ClientSlot] = []
        self._refresh_tasks: dict[
            tuple[str, str, float], asyncio.Task[None]
        ] = {}
        self._shutting_down = False

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[ManagedLLMClient]:
        """获取共享客户端租约；退出上下文不会关闭当前连接。"""
        slot = await self._acquire_slot()
        try:
            yield slot.client
        finally:
            await self._release_slot(slot)

    async def _acquire_slot(self) -> _ClientSlot:
        while True:
            settings = self._settings_getter()
            fingerprint = settings.fingerprint

            async with self._lock:
                if self._shutting_down:
                    raise RuntimeError("LLM 客户端连接池正在关闭")  # noqa: TRY003

                if (
                    self._current is not None
                    and self._current.fingerprint == fingerprint
                ):
                    self._current.active_leases += 1
                    return self._current

                refresh_task = self._refresh_tasks.get(fingerprint)
                if refresh_task is None:
                    validate_candidate = self._current is not None
                    refresh_task = asyncio.create_task(
                        self._refresh_client(
                            settings,
                            validate_candidate=validate_candidate,
                        ),
                        name="llm-provider-client-refresh",
                    )
                    self._refresh_tasks[fingerprint] = refresh_task

            try:
                await asyncio.shield(refresh_task)
            except BaseException:
                if self._shutting_down:
                    raise RuntimeError(  # noqa: TRY003
                        "LLM 客户端连接池正在关闭"
                    ) from None
                latest_fingerprint = self._settings_getter().fingerprint
                if latest_fingerprint != fingerprint:
                    continue
                raise

    async def _refresh_client(
        self,
        settings: ClientSettings,
        *,
        validate_candidate: bool,
    ) -> None:
        """在锁外验证候选连接，并在锁内进行最小化的原子交换。"""
        fingerprint = settings.fingerprint
        candidate: ManagedLLMClient | None = None
        published = False
        close_after_swap: _ClientSlot | None = None

        try:
            candidate = self._client_factory(settings)
            if validate_candidate:
                healthy = await candidate.test_connection(
                    settings.healthcheck_model
                )
                if not healthy:
                    raise RuntimeError(  # noqa: TRY003
                        "新的 LLM 连接配置健康检查失败，已继续保留旧连接"
                    )

            latest_fingerprint = self._settings_getter().fingerprint
            if latest_fingerprint != fingerprint:
                return

            async with self._lock:
                if self._shutting_down:
                    raise RuntimeError("LLM 客户端连接池正在关闭")  # noqa: TRY003
                if (
                    self._current is not None
                    and self._current.fingerprint == fingerprint
                ):
                    return

                previous = self._current
                if previous is not None:
                    previous.retired = True
                    self._retired.append(previous)
                    if previous.active_leases == 0:
                        self._retired.remove(previous)
                        close_after_swap = previous

                self._current = _ClientSlot(
                    client=candidate,
                    fingerprint=fingerprint,
                )
                published = True
        finally:
            if candidate is not None and not published:
                await self._close_client(candidate)
            if close_after_swap is not None:
                await self._close_client(close_after_swap.client)
            current_task = asyncio.current_task()
            async with self._lock:
                if self._refresh_tasks.get(fingerprint) is current_task:
                    self._refresh_tasks.pop(fingerprint, None)

    async def _release_slot(self, slot: _ClientSlot) -> None:
        should_close = False
        async with self._lock:
            slot.active_leases = max(0, slot.active_leases - 1)
            if slot.retired and slot.active_leases == 0:
                if slot in self._retired:
                    self._retired.remove(slot)
                should_close = True

        if should_close:
            await self._close_client(slot.client)

    async def close(self) -> None:
        """停止接受新租约，并关闭所有没有在途请求的客户端。"""
        to_close: list[_ClientSlot] = []
        async with self._lock:
            self._shutting_down = True
            refresh_tasks = list(self._refresh_tasks.values())
            slots = [*(self._retired), *([self._current] if self._current else [])]
            self._retired = []
            self._current = None
            for slot in slots:
                slot.retired = True
                if slot.active_leases == 0:
                    to_close.append(slot)
                else:
                    self._retired.append(slot)

        for task in refresh_tasks:
            task.cancel()
        if refresh_tasks:
            await asyncio.gather(*refresh_tasks, return_exceptions=True)

        for slot in to_close:
            await self._close_client(slot.client)

    @staticmethod
    async def _close_client(client: ManagedLLMClient) -> None:
        try:
            await client.close()
        except Exception as exc:
            logger.warning(
                "[LLM Provider] 关闭旧客户端失败: error_type={}",
                type(exc).__name__,
            )
