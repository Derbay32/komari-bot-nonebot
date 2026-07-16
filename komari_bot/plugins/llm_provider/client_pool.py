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
        settings = self._settings_getter()
        fingerprint = settings.fingerprint
        close_after_swap: _ClientSlot | None = None

        async with self._lock:
            if self._shutting_down:
                raise RuntimeError("LLM 客户端连接池正在关闭")  # noqa: TRY003

            if self._current is not None and self._current.fingerprint == fingerprint:
                self._current.active_leases += 1
                return self._current

            candidate = self._client_factory(settings)
            if self._current is not None:
                try:
                    healthy = await candidate.test_connection(
                        settings.healthcheck_model
                    )
                except BaseException:
                    await self._close_client(candidate)
                    raise
                if not healthy:
                    await self._close_client(candidate)
                    raise RuntimeError(  # noqa: TRY003
                        "新的 LLM 连接配置健康检查失败，已继续保留旧连接"
                    )

                previous = self._current
                previous.retired = True
                self._retired.append(previous)
                if previous.active_leases == 0:
                    self._retired.remove(previous)
                    close_after_swap = previous

            slot = _ClientSlot(
                client=candidate,
                fingerprint=fingerprint,
                active_leases=1,
            )
            self._current = slot

        if close_after_swap is not None:
            await self._close_client(close_after_swap.client)
        return slot

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
            slots = [*(self._retired), *([self._current] if self._current else [])]
            self._retired = []
            self._current = None
            for slot in slots:
                slot.retired = True
                if slot.active_leases == 0:
                    to_close.append(slot)
                else:
                    self._retired.append(slot)

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

    async def generate_text(self, **kwargs: Any) -> Any: ...

    async def generate_text_with_messages(self, **kwargs: Any) -> Any: ...
