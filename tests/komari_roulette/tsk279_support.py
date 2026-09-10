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
import json
from collections import deque
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4

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

#: TSK-279 Stage-B proposed production seams (module paths are the proposal).
RUNTIME_MODULE = "komari_bot.plugins.komari_roulette.runtime"
MAINTENANCE_MODULE = "komari_bot.plugins.komari_roulette.maintenance"
OBSERVABILITY_MODULE = "komari_bot.plugins.komari_roulette.observability"

#: Real roulette tables keyed by app/group (cleanup + retention assertions).
_SCOPE_TABLES: tuple[str, ...] = (
    "komari_roulette_command_receipts",
    "komari_roulette_games",
    "komari_roulette_results",
    "komari_roulette_leaderboard",
)

__all__ = [
    "CONFIG_SCHEMA_MODULE",
    "COPY_POOL_MODULE",
    "MAINTENANCE_MODULE",
    "OBSERVABILITY_MODULE",
    "PG_REQUIRED",
    "RENDERER_MODULE",
    "RUNTIME_MODULE",
    "ScriptedCopyRandom",
    "Tsk279Harness",
    "age_game",
    "cancel_and_join",
    "delete_app",
    "delete_binding_scope",
    "harness_fixture_body",
    "insert_waiting_game",
    "load_module",
    "load_symbol",
    "pg_now",
    "scope_counts",
    "seed_aged_receipt",
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

    @asynccontextmanager
    async def app(self, tag: str) -> AsyncIterator[str]:
        """Yield a fresh unique app id and delete every roulette row it made.

        Multi-group cases (pagination) need many ``group_openid`` values under
        one app; exact-scope cleanup cannot track them, so cleanup is keyed by
        the single app id instead of lowering the gate database head or using
        an unscoped ``DELETE``.
        """

        app_id = f"tsk279-app-{tag}-{uuid4().hex}"
        try:
            yield app_id
        finally:
            with suppress(Exception):
                await delete_app(self.engine, app_id)


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


# ---------------------------------------------------------------------------
# TSK-279 Stage-B real-PG probes: reading PG time, seeding retention fixtures
# and observing scope locks.  These helpers use the **real** schema and the
# real command service so a cleanup/recovery RED never fails for "wrong
# fixture": only for the still-missing production seam.
# ---------------------------------------------------------------------------


async def pg_now(session: AsyncSession) -> datetime:
    """Read the authoritative PostgreSQL clock (not the host wall clock)."""

    value = await session.scalar(text("SELECT clock_timestamp()"))
    if not isinstance(value, datetime):
        message = "clock_timestamp() did not return a datetime"
        raise TypeError(message)
    return value


async def scope_counts(
    session_factory: async_sessionmaker[AsyncSession],
    current: Scope,
) -> dict[str, int]:
    """Count every real roulette table for one exact app/group scope."""

    params = {"app_id": current.app_id, "group_openid": current.group_openid}
    joins = {
        "fulfillments": (
            "SELECT count(*) FROM komari_roulette_fulfillments AS f "
            "JOIN komari_roulette_command_receipts AS r "
            "ON r.receipt_id = f.receipt_id "
            "WHERE r.app_id = :app_id AND r.group_openid = :group_openid"
        ),
        "players": (
            "SELECT count(*) FROM komari_roulette_players AS p "
            "JOIN komari_roulette_games AS g ON g.game_id = p.game_id "
            "WHERE g.app_id = :app_id AND g.group_openid = :group_openid"
        ),
        "result_players": (
            "SELECT count(*) FROM komari_roulette_result_players AS rp "
            "JOIN komari_roulette_results AS r ON r.game_id = rp.game_id "
            "WHERE r.app_id = :app_id AND r.group_openid = :group_openid"
        ),
    }
    counts: dict[str, int] = {}
    async with session_factory() as session:
        for table in _SCOPE_TABLES:
            statement = joins.get(
                table,
                f"SELECT count(*) FROM {table} "
                "WHERE app_id = :app_id AND group_openid = :group_openid",
            )
            counts[table] = int(await session.scalar(text(statement), params) or 0)
        for table, statement in joins.items():
            counts[table] = int(await session.scalar(text(statement), params) or 0)
    return counts


async def seed_aged_receipt(
    session_factory: async_sessionmaker[AsyncSession],
    current: Scope,
    *,
    inbound_msg_id: str,
    age_seconds: int,
    result_code: str = "created",
    game_id: str | None = None,
    state: str = "NOT_STARTED",
) -> str:
    """Insert one real receipt + fulfillment with a controlled PG age.

    Uses the real ``komari_roulette_command_receipts`` /
    ``komari_roulette_fulfillments`` schema and the PostgreSQL clock, so the
    7-day retention boundary is measured in PG time, never host time.
    """

    receipt_id = f"tsk279-receipt-{uuid4().hex}"
    fingerprint = json.dumps({"message_id": inbound_msg_id})
    projection = json.dumps({"body": "冻结安全回复", "metadata": {}})
    async with session_factory() as session:
        await session.execute(
            text(
                "INSERT INTO komari_roulette_command_receipts ("
                "receipt_id, app_id, group_openid, inbound_msg_id, fingerprint, "
                "result_code, game_id, state_revision, turn_seq, reply_projection, "
                "created_at) VALUES ("
                ":receipt_id, :app_id, :group_openid, :inbound_msg_id, "
                "CAST(:fingerprint AS jsonb), :result_code, :game_id, NULL, NULL, "
                "CAST(:projection AS jsonb), "
                "clock_timestamp() - make_interval(secs => :age))"
            ),
            {
                "receipt_id": receipt_id,
                "app_id": current.app_id,
                "group_openid": current.group_openid,
                "inbound_msg_id": inbound_msg_id,
                "fingerprint": fingerprint,
                "result_code": result_code,
                "game_id": game_id,
                "projection": projection,
                "age": age_seconds,
            },
        )
        await session.execute(
            text(
                "INSERT INTO komari_roulette_fulfillments ("
                "receipt_id, state, platform_message_id, updated_at) "
                "VALUES (:receipt_id, :state, NULL, clock_timestamp())"
            ),
            {"receipt_id": receipt_id, "state": state},
        )
        await session.commit()
    return receipt_id


async def age_game(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    game_id: str,
    age_seconds: int,
) -> None:
    """Push one game and its result back by ``age_seconds`` in PG time."""

    params = {"game_id": game_id, "age": age_seconds}
    async with session_factory() as session:
        await session.execute(
            text(
                "UPDATE komari_roulette_games SET "
                "created_at = created_at - make_interval(secs => :age), "
                "ended_at = CASE WHEN ended_at IS NULL THEN NULL "
                "ELSE ended_at - make_interval(secs => :age) END "
                "WHERE game_id = :game_id"
            ),
            params,
        )
        await session.execute(
            text(
                "UPDATE komari_roulette_results SET "
                "created_at = created_at - make_interval(secs => :age), "
                "ended_at = ended_at - make_interval(secs => :age) "
                "WHERE game_id = :game_id"
            ),
            params,
        )
        await session.commit()


_WAITING_GAME_SQL = (
    "INSERT INTO komari_roulette_games ("
    "game_id, app_id, group_openid, lifecycle, host_seq, created_at, started_at, "
    "ended_at, waiting_expires_at, turn_deadline_at, state_revision, "
    "chamber_revision, turn_seq, current_player_seq, phase, ordered_chamber, "
    "pending_rewards, pending_burst, pending_locks, item_weights, next_join_seq, "
    "updated_at) VALUES ("
    ":game_id, :app_id, :group_openid, 'waiting', 1, "
    "clock_timestamp() - make_interval(secs => :age), NULL, NULL, "
    "clock_timestamp() - make_interval(secs => :deadline_age), NULL, 1, 0, 0, "
    "NULL, NULL, '{}'::text[], '{}'::text[], false, '{}'::integer[], "
    "CAST('{\"magnifier\": 1, \"beer\": 1, \"burst\": 1, \"lock\": 1}' AS json), "
    "2, clock_timestamp())"
)

_WAITING_PLAYER_SQL = (
    "INSERT INTO komari_roulette_players ("
    "game_id, join_seq, member_openid, display_name, alive, magnifier_count, "
    "beer_count, burst_count, lock_count, eliminated_order, eliminated_reason, "
    "eliminated_at) VALUES ("
    ":game_id, 1, :member_openid, 'Seat 1', true, 0, 0, 0, 0, NULL, NULL, NULL)"
)


async def insert_waiting_game(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    game_id: str,
    app_id: str,
    group_openid: str,
    member_openid: str,
    deadline_age_seconds: int = 300,
) -> None:
    """Insert one schema-valid waiting game whose deadline already passed.

    The row satisfies the real CHECK constraints and the storage aggregate
    validation, so ``advance_expired`` accepts it as a real due game.
    """

    params = {
        "game_id": game_id,
        "app_id": app_id,
        "group_openid": group_openid,
        "member_openid": member_openid,
        "age": deadline_age_seconds + 60,
        "deadline_age": deadline_age_seconds,
    }
    async with session_factory() as session:
        await session.execute(text(_WAITING_GAME_SQL), params)
        await session.execute(text(_WAITING_PLAYER_SQL), params)
        await session.commit()


async def delete_app(engine: AsyncEngine, app_id: str) -> None:
    """Delete every roulette row for one app id (multi-group cleanup)."""

    params = {"app_id": app_id}
    statements = (
        "DELETE FROM komari_roulette_fulfillments WHERE receipt_id IN "
        "(SELECT receipt_id FROM komari_roulette_command_receipts "
        "WHERE app_id = :app_id)",
        "DELETE FROM komari_roulette_command_receipts WHERE app_id = :app_id",
        "DELETE FROM komari_roulette_result_players WHERE game_id IN "
        "(SELECT game_id FROM komari_roulette_games WHERE app_id = :app_id)",
        "DELETE FROM komari_roulette_results WHERE app_id = :app_id",
        "DELETE FROM komari_roulette_players WHERE game_id IN "
        "(SELECT game_id FROM komari_roulette_games WHERE app_id = :app_id)",
        "DELETE FROM komari_roulette_games WHERE app_id = :app_id",
        "DELETE FROM komari_roulette_leaderboard WHERE app_id = :app_id",
    )
    for statement in statements:
        async with engine.begin() as connection:
            with suppress(Exception):
                await connection.execute(text(statement), params)
