# ruff: noqa: RUF003  # ｜ 是玩家行列分隔符（规范字符，非代码符号）
"""TSK-279 Stage-A support: real-PG harness, seam loaders, isolated copy RNG.

Design goals:

* RED tests never import a missing TSK-279 module at collection time; they load
  it lazily with a clear ``ModuleNotFoundError`` so a red run reports
  "missing seam" instead of a whole-file collection error.
* Every test tracks the *real* app/group scope it used and deletes its own
  roulette **and** character_binding rows in ``finally`` (the older TSK-276/278
  harness deleted an unrelated ``scope("fixture")`` and accumulated rows).
* ``Tsk279Harness`` owns bounded task release via :func:`cancel_and_join`.
"""

from __future__ import annotations

import asyncio
import importlib
from collections import deque
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from sqlalchemy import text

from komari_bot.plugins.character_binding.manager import CharacterBindingManager

from .command_support import (
    PG_REQUIRED,
    Scope,
    create_engine_and_factory,
    delete_scope,
    reset_shared_orm_engine,
    scope,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterable, Sequence

    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

CONFIG_SCHEMA_MODULE = "komari_bot.plugins.komari_roulette.config_schema"
COPY_POOL_MODULE = "komari_bot.plugins.komari_roulette.copy_pool"
RENDERER_MODULE = "komari_bot.plugins.komari_roulette.qq.renderer"

__all__ = [
    "CONFIG_SCHEMA_MODULE",
    "COPY_POOL_MODULE",
    "PG_REQUIRED",
    "RENDERER_MODULE",
    "ScriptedCopyRandom",
    "Tsk279Harness",
    "cancel_and_join",
    "delete_binding_scope",
    "harness_fixture_body",
    "load_module",
    "load_symbol",
]


def load_module(module_path: str) -> Any:
    """Import a TSK-279 seam module, lazily and with a clear error.

    A missing module is the expected Stage-A RED signal; keeping the import out
    of module scope means the green probe tests in the same file still run.
    """

    return importlib.import_module(module_path)


def load_symbol(module_path: str, symbol: str) -> Any:
    """Load one attribute from a seam module (missing module/symbol → clear)."""

    module = load_module(module_path)
    try:
        return getattr(module, symbol)
    except AttributeError as error:
        message = f"{module_path}.{symbol} is not implemented yet (TSK-279 RED)"
        raise AttributeError(message) from error


class ScriptedCopyRandom:
    """Deterministic, isolated copy-choice source for the S3 projector factory.

    Only implements ``choice(options)``; queued values are returned in order,
    otherwise the first offered template is returned (``fallback_first``) so a
    test can drive many result codes without enumerating every pick.  Every
    call is recorded so tests can prove the copy random source is independent
    from the domain ``RandomSource`` and that frozen receipts are not re-drawn.
    """

    def __init__(
        self,
        choices: Iterable[str] = (),
        *,
        fallback_first: bool = True,
    ) -> None:
        self._choices: deque[str] = deque(choices)
        self._fallback_first = fallback_first
        self.calls: list[tuple[str, ...]] = []
        self.returned: list[str] = []

    @property
    def draw_count(self) -> int:
        return len(self.returned)

    def choice(self, options: Sequence[str]) -> str:
        recorded = tuple(options)
        self.calls.append(recorded)
        if not recorded:
            message = "copy pool offered an empty option list (TSK-279 RED)"
            raise AssertionError(message)
        if self._choices:
            value = self._choices.popleft()
            if value not in recorded:
                message = (
                    f"scripted copy {value!r} not in offered options {recorded!r}"
                )
                raise AssertionError(message)
        elif self._fallback_first:
            value = recorded[0]
        else:
            message = "no scripted copy choice left (TSK-279 test fixture)"
            raise AssertionError(message)
        self.returned.append(value)
        return value


async def cancel_and_join(
    tasks: Sequence[asyncio.Task[Any]],
    *,
    deadline_seconds: float = 5.0,
) -> None:
    """Bounded ``finally`` task release: cancel, then wait with a deadline."""

    for task in tasks:
        task.cancel()
    if not tasks:
        return
    with suppress(Exception):
        await asyncio.wait(tasks, timeout=deadline_seconds)


async def delete_binding_scope(engine: AsyncEngine, current: Scope) -> None:
    """Delete this case's character_binding group/member rows (own rows only)."""

    params = {"app_id": current.app_id, "group_openid": current.group_openid}
    for statement in (
        "DELETE FROM komari_character_binding_members "
        "WHERE app_id = :app_id AND group_openid = :group_openid",
        "DELETE FROM komari_character_binding_groups "
        "WHERE app_id = :app_id AND group_openid = :group_openid",
    ):
        async with engine.begin() as connection:
            with suppress(Exception):
                await connection.execute(text(statement), params)


@dataclass(slots=True)
class Tsk279Harness:
    """Real PG harness that cleans up the exact scope each case created."""

    engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]
    binding_manager: CharacterBindingManager
    _scopes: list[Scope] = field(default_factory=list)

    @asynccontextmanager
    async def scope(self, tag: str) -> AsyncIterator[Scope]:
        """Yield a fresh unique scope and delete its own rows on exit."""

        current = scope(f"tsk279-{tag}")
        self._scopes.append(current)
        try:
            yield current
        finally:
            with suppress(ValueError):
                self._scopes.remove(current)
            with suppress(Exception):
                await delete_scope(self.engine, current)
            await delete_binding_scope(self.engine, current)


async def harness_fixture_body() -> AsyncIterator[Tsk279Harness]:
    """Shared fixture body: real engine, live binding manager, bounded teardown."""

    async for engine, session_factory in create_engine_and_factory():
        await reset_shared_orm_engine()
        manager = CharacterBindingManager()
        await manager.initialize()
        try:
            yield Tsk279Harness(engine, session_factory, manager)
        finally:
            with suppress(Exception):
                await manager.close()
            await reset_shared_orm_engine()
