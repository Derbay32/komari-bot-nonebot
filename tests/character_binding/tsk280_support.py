"""TSK-280 独立测试辅助：作用域、REST 桩服务与真实 PG 门控。

本模块只依赖既有共享件（管理审计工具、SQLAlchemy、NoneBot ORM、TSK-276
轮盘公开 seam），**绝不 import 尚未实现的 ``repair`` 业务模块**，避免把
「缺失业务模块」的 RED 混进夹具。所有 SQL / 领域辅助都对照真实
``orm_models`` / ``storage_support`` / TSK-276 harness 核对过列名与语义，
并通过 ``test_tsk280_fixture_probe.py`` 独立验证。
"""

from __future__ import annotations

import asyncio
import os
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse
from uuid import uuid4

import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session as _SyncSession

from komari_bot.management.management_audit import hash_management_target
from komari_bot.plugins.komari_roulette import (
    CanonicalCommand,
    CommandRequest,
    GroupRef,
    PostgresRouletteStorage,
    ReplyProjection,
    RouletteCommandService,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Sequence

    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

    from komari_bot.plugins.komari_roulette.domain import RandomSource

POSTGRES_URL = os.getenv("KOMARI_TEST_POSTGRES_URL", "")
SQLALCHEMY_URL = os.getenv("SQLALCHEMY_DATABASE_URL", "")

REPAIR_TOKEN_TTL = timedelta(minutes=10)
REPAIR_API_PREFIX = "/api/v2/character-bindings/repair"

PG_REQUIRED = pytest.mark.skipif(
    not POSTGRES_URL,
    reason="未设置 KOMARI_TEST_POSTGRES_URL，不能执行 TSK-280 真实 PG 测试",
)


def _same_database(left: str, right: str) -> bool:
    left_parsed = urlparse(left.replace("postgresql+asyncpg://", "postgresql://"))
    right_parsed = urlparse(right.replace("postgresql+asyncpg://", "postgresql://"))
    return (
        left_parsed.hostname == right_parsed.hostname
        and (left_parsed.port or 5432) == (right_parsed.port or 5432)
        and left_parsed.path == right_parsed.path
    )


def require_postgres() -> None:
    """对每个真实库测试显式执行连接配置与同库守卫。"""
    if not POSTGRES_URL:
        pytest.skip("未配置真实 PostgreSQL 测试连接")
    if not _same_database(POSTGRES_URL, SQLALCHEMY_URL):
        pytest.skip(
            "KOMARI_TEST_POSTGRES_URL 与 SQLALCHEMY_DATABASE_URL 不一致"
        )


@dataclass(frozen=True, slots=True)
class Scope:
    """一次测试唯一的 app/group/member 作用域。"""

    app_id: str
    group_openid: str
    member_openid: str
    group_id: str
    member_qq: str

    def with_member(self, number: int) -> "Scope":
        return Scope(
            app_id=self.app_id,
            group_openid=self.group_openid,
            member_openid=f"{self.member_openid}-{number}",
            group_id=self.group_id,
            member_qq=f"qq-{self.member_openid}-{number}",
        )

    def sibling(
        self,
        *,
        other_app: bool = False,
        other_group: bool = False,
    ) -> "Scope":
        """同成员身份（openid/qq）在另一应用或另一群的作用域。"""
        return Scope(
            app_id=f"other-{self.app_id}" if other_app else self.app_id,
            group_openid=(
                f"other-{self.group_openid}" if other_group else self.group_openid
            ),
            member_openid=self.member_openid,
            group_id=(
                f"other-{self.group_id}" if other_group else self.group_id
            ),
            member_qq=self.member_qq,
        )


def scope(tag: str = "repair") -> Scope:
    """生成唯一的测试作用域。"""
    suffix = f"{tag}-{uuid4().hex}"
    return Scope(
        app_id=f"tsk280-app-{suffix}",
        group_openid=f"tsk280-group-{suffix}",
        member_openid=f"tsk280-member-{suffix}",
        group_id=f"qq-group-{suffix}",
        member_qq=f"qq-member-{suffix}",
    )


async def reset_shared_orm_engine() -> None:
    """归还 nonebot-plugin-orm 共享引擎连接。"""
    from nonebot import require

    require("nonebot_plugin_orm")
    import nonebot_plugin_orm as orm_module

    engines = getattr(orm_module, "_engines", None)
    if not engines:
        return
    for engine in list(engines.values()):
        with suppress(Exception):
            await engine.dispose()


async def create_engine_and_factory() -> AsyncIterator[
    tuple[AsyncEngine, async_sessionmaker[AsyncSession]]
]:
    """创建真实 PostgreSQL 引擎与会话工厂。"""
    require_postgres()
    await reset_shared_orm_engine()
    engine = create_async_engine(
        SQLALCHEMY_URL,
        pool_pre_ping=True,
        pool_size=4,
        max_overflow=4,
    )
    try:
        yield engine, async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


async def backend_pid(session: AsyncSession) -> int:
    return int(await session.scalar(text("SELECT pg_backend_pid()")))


async def wait_for_blocked(
    session_factory: async_sessionmaker[AsyncSession],
    blocker_pid: int,
) -> None:
    """等待真实 PostgreSQL 锁等待者出现（有界 5 秒）。"""
    await wait_for_blocked_count(session_factory, blocker_pid, min_count=1)


async def wait_for_blocked_count(
    session_factory: async_sessionmaker[AsyncSession],
    blocker_pid: int,
    min_count: int,
) -> None:
    """等待至少 ``min_count`` 个会话被 blocker 阻塞，带边界超时。"""
    async with asyncio.timeout(5):
        while True:
            async with session_factory() as session:
                blocked = await session.scalar(
                    text(
                        "SELECT count(*) FROM pg_stat_activity "
                        "WHERE :blocker = ANY(pg_blocking_pids(pid))"
                    ),
                    {"blocker": blocker_pid},
                )
            if int(blocked or 0) >= min_count:
                return
            await asyncio.sleep(0.02)


async def hold_group_lock(
    session: AsyncSession,
    current: Scope,
) -> None:
    """占用与绑定/轮盘相同的共享组锁。"""
    from komari_bot.db.group_transaction_locks import lock_group_scope

    await lock_group_scope(
        session,
        app_id=current.app_id,
        group_openid=current.group_openid,
    )


def make_game_state_reader() -> Callable[..., Awaitable[object | None]]:
    """真实 TSK-276 公共 seam 读取器（注入修复服务的 ``game_state_reader``）。

    契约要求修复服务不得直接反向 import 轮盘；对局状态经构造注入读取器。
    本辅助把真实 ``PostgresRouletteStorage.load_current`` 包成契约签名
    ``(session, *, app_id, group_openid, for_update=False)``，测试与生产
    装配共用同一公共 seam。
    """

    async def _read_game_state(
        session: AsyncSession,
        *,
        app_id: str,
        group_openid: str,
        for_update: bool = False,
    ) -> object | None:
        return await PostgresRouletteStorage(session).load_current(
            GroupRef(app_id=app_id, group_openid=group_openid),
            for_update=for_update,
        )

    return _read_game_state


# ─── 提交失败开关：真实 SQLAlchemy Session 事件包装 ─────────────────────

class CommitFailureSwitch:
    """可武装/解除的确定性提交失败开关。

    经 SQLAlchemy ``Session.before_commit`` 事件在真正 COMMIT 之前抛
    ``OperationalError``（DBAPIError 子类），与真实提交失败同型；解除后
    不影响任何正常提交。该开关**不**包装/替换 session，因此不会绕过
    实现方任何一种提交写法（``async with session:``、显式
    ``commit()``、``async with session.begin():`` 都会触发
    ``before_commit``）。
    """

    def __init__(self) -> None:
        self.armed = False
        self.raised = 0

    def arm(self) -> None:
        self.armed = True

    def disarm(self) -> None:
        self.armed = False


_ACTIVE_FAILURE_SWITCHES: list[CommitFailureSwitch] = []


def _fail_armed_commit(_session: _SyncSession) -> None:
    """在 ``before_commit`` 阶段拦截已武装开关下的提交。"""
    for switch in _ACTIVE_FAILURE_SWITCHES:
        if switch.armed:
            switch.raised += 1
            raise OperationalError(  # noqa: TRY003 - SQLAlchemy 异常以 message 为第一实参
                "injected commit failure",
                {},
                RuntimeError("boom"),
            )


@contextmanager
def install_commit_failure_switch(
    switch: CommitFailureSwitch,
) -> Iterator[CommitFailureSwitch]:
    """安装（并在退出时移除）``before_commit`` 失败监听器。"""
    event.listen(_SyncSession, "before_commit", _fail_armed_commit)
    _ACTIVE_FAILURE_SWITCHES.append(switch)
    try:
        yield switch
    finally:
        try:
            _ACTIVE_FAILURE_SWITCHES.remove(switch)
        finally:
            event.remove(_SyncSession, "before_commit", _fail_armed_commit)


async def health_check_commit_failure_switch(
    session_factory: async_sessionmaker[AsyncSession],
    switch: CommitFailureSwitch,
) -> None:
    """健康验证失败注入：未武装可提交、武装后必失败且不落库、可恢复。"""
    with install_commit_failure_switch(switch):
        def _table() -> str:
            # TEMP TABLE 按连接会话存活（非 ON COMMIT DROP），池化连接复用会
            # 保留上一段探针的表；每段探针必须用独立表名，避免同连接 duplicate。
            return f"tsk280_failure_probe_{uuid4().hex}"

        async def _probe_insert(value: int) -> int:
            table = _table()
            async with session_factory() as session:
                await session.execute(text(f"CREATE TEMP TABLE {table} (id integer)"))
                await session.execute(
                    text(f"INSERT INTO {table} (id) VALUES ({value})")
                )
                await session.commit()
                return int(
                    await session.scalar(text(f"SELECT count(*) FROM {table}"))
                )

        # 1) 未武装：提交成功并可见。
        assert await _probe_insert(1) == 1
        # 2) 武装：提交在真正 COMMIT 前失败，数据未落库。
        armed_table = _table()
        async with session_factory() as session:
            await session.execute(
                text(f"CREATE TEMP TABLE {armed_table} (id integer)")
            )
            await session.execute(
                text(f"INSERT INTO {armed_table} (id) VALUES (2)")
            )
            switch.arm()
            try:
                with pytest.raises(OperationalError):
                    await session.commit()
            finally:
                switch.disarm()
            await session.rollback()
            await session.close()
        post_failure_table = _table()
        async with session_factory() as session:
            await session.execute(
                text(f"CREATE TEMP TABLE {post_failure_table} (id integer)")
            )
            assert int(
                await session.scalar(
                    text(f"SELECT count(*) FROM {post_failure_table}")
                )
            ) == 0
        # 3) 已解除：可继续正常提交。
        assert await _probe_insert(3) == 1
        assert switch.raised == 1


class SessionCloseCounter:
    """统计进程中 AsyncSession 的关闭次数（monkeypatch ``close``）。"""

    def __init__(self) -> None:
        self.closed = 0


@contextmanager
def track_session_closes() -> Iterator[SessionCloseCounter]:
    """统计提交失败期间被关闭的 session（防 session 泄漏回归）。

    SQLAlchemy 2.0 没有 ``after_close`` 事件，改用对 ``AsyncSession.close``
    的计数 monkeypatch（测试辅助，不影响生产代码）。
    """
    from sqlalchemy.ext.asyncio import AsyncSession

    counter = SessionCloseCounter()
    original_close = AsyncSession.close

    async def _counting_close(self: AsyncSession) -> None:
        counter.closed += 1
        await original_close(self)

    AsyncSession.close = _counting_close  # type: ignore[assignment]
    try:
        yield counter
    finally:
        AsyncSession.close = original_close  # type: ignore[assignment]


# ─── 绑定/轮盘数据辅助（全部对照真实表结构） ─────────────────────────

async def seed_binding(
    manager: Any,
    current: Scope,
    number: int,
    *,
    name: str | None = None,
) -> str:
    """经既有 271 manager seam 建立一条测试绑定。"""
    member = current.with_member(number)
    await manager.bind_group_member(
        app_id=current.app_id,
        group_id=current.group_id,
        group_openid=current.group_openid,
        member_qq=member.member_qq,
        member_openid=member.member_openid,
        character_name=name or f"Seat {number}",
        bot_self_id="tsk280-test-bot",
    )
    return member.member_openid


async def bind_member(
    manager: Any,
    current: Scope,
    index: int,
    *,
    name: str | None = None,
) -> None:
    """本票统一的绑定辅助。"""
    await seed_binding(manager, current, index, name=name or f"角色{index}")


async def group_binding_rows(
    engine: AsyncEngine,
    current: Scope,
) -> int:
    """本作用域成员关联行数。"""
    async with engine.begin() as connection:
        rows = await connection.execute(
            text(
                """
                SELECT count(*) FROM komari_character_binding_members
                 WHERE app_id = :app_id AND group_openid = :group_openid
                """
            ),
            {"app_id": current.app_id, "group_openid": current.group_openid},
        )
        return int(rows.scalar_one())


async def group_mapping_rows(
    engine: AsyncEngine,
    current: Scope,
) -> int:
    """本作用域群映射行数（``komari_character_binding_groups``）。"""
    async with engine.begin() as connection:
        rows = await connection.execute(
            text(
                """
                SELECT count(*) FROM komari_character_binding_groups
                 WHERE app_id = :app_id AND group_openid = :group_openid
                """
            ),
            {"app_id": current.app_id, "group_openid": current.group_openid},
        )
        return int(rows.scalar_one())


async def member_rows(
    session: AsyncSession,
    current: Scope,
) -> list[tuple[str, str | None]]:
    """按 member_openid 排序返回 (member_openid, character_name)。"""
    rows = await session.execute(
        text(
            """
            SELECT member_openid, character_name
              FROM komari_character_binding_members
             WHERE app_id = :app_id AND group_openid = :group_openid
             ORDER BY member_openid
            """
        ),
        {"app_id": current.app_id, "group_openid": current.group_openid},
    )
    return [(str(row[0]), row[1]) for row in rows.all()]


async def clear_binding_scope(
    engine: AsyncEngine,
    current: Scope,
) -> None:
    """只清理本测试作用域的绑定表（真实表名/列名，失败必须浮出）。"""
    params = {
        "app_id": current.app_id,
        "group_openid": current.group_openid,
    }
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "DELETE FROM komari_character_binding_members "
                "WHERE app_id = :app_id AND group_openid = :group_openid"
            ),
            params,
        )
        await connection.execute(
            text(
                "DELETE FROM komari_character_binding_groups "
                "WHERE app_id = :app_id AND group_openid = :group_openid"
            ),
            params,
        )


async def clear_roulette_scope(
    engine: AsyncEngine,
    current: Scope,
) -> None:
    """只清理本测试作用域的轮盘表（FK 顺序；列名对照 0019/0020 迁移）。"""
    params = {
        "app_id": current.app_id,
        "group_openid": current.group_openid,
    }
    statements = (
        "DELETE FROM komari_roulette_fulfillments "
        "WHERE receipt_id IN (SELECT receipt_id FROM komari_roulette_command_receipts "
        "WHERE app_id = :app_id AND group_openid = :group_openid)",
        "DELETE FROM komari_roulette_command_receipts "
        "WHERE app_id = :app_id AND group_openid = :group_openid",
        "DELETE FROM komari_roulette_result_players "
        "WHERE game_id IN (SELECT game_id FROM komari_roulette_games "
        "WHERE app_id = :app_id AND group_openid = :group_openid)",
        "DELETE FROM komari_roulette_results "
        "WHERE app_id = :app_id AND group_openid = :group_openid",
        "DELETE FROM komari_roulette_players "
        "WHERE game_id IN (SELECT game_id FROM komari_roulette_games "
        "WHERE app_id = :app_id AND group_openid = :group_openid)",
        "DELETE FROM komari_roulette_games "
        "WHERE app_id = :app_id AND group_openid = :group_openid",
        "DELETE FROM komari_roulette_leaderboard "
        "WHERE app_id = :app_id AND group_openid = :group_openid",
    )
    async with engine.begin() as connection:
        for statement in statements:
            await connection.execute(text(statement), params)


async def roulette_counts(
    engine: AsyncEngine,
    current: Scope,
) -> dict[str, int]:
    """本作用域轮盘表行数。

    对照真实表结构：``komari_roulette_players`` / ``result_players`` 没有
    ``app_id``/``group_openid`` 列，必须经 ``komari_roulette_games`` 关联；
    ``results``/``leaderboard``/``games`` 有作用域列，按 app+group 过滤。
    ``players`` 同时统计运行期座位与终局不可变快照：终局投影会把运行期
    玩家行搬入 ``komari_roulette_result_players`` 并清空
    ``komari_roulette_players``，两种状态互斥，行数相加不会重复计数。
    """
    async with engine.begin() as connection:
        rows = await connection.execute(
            text(
                """
                SELECT 'games', count(*)
                  FROM komari_roulette_games
                 WHERE app_id = :app_id AND group_openid = :group_openid
                UNION ALL
                SELECT 'results', count(*)
                  FROM komari_roulette_results
                 WHERE app_id = :app_id AND group_openid = :group_openid
                UNION ALL
                SELECT 'players', count(*)
                  FROM (
                        SELECT game_id FROM komari_roulette_players
                         WHERE game_id IN (
                               SELECT game_id FROM komari_roulette_games
                                WHERE app_id = :app_id
                                      AND group_openid = :group_openid
                         )
                        UNION ALL
                        SELECT game_id FROM komari_roulette_result_players
                         WHERE game_id IN (
                               SELECT game_id FROM komari_roulette_games
                                WHERE app_id = :app_id
                                      AND group_openid = :group_openid
                         )
                  ) AS scoped_participants
                UNION ALL
                SELECT 'wins', coalesce(sum(wins), 0)
                  FROM komari_roulette_leaderboard
                 WHERE app_id = :app_id AND group_openid = :group_openid
                """
            ),
            {"app_id": current.app_id, "group_openid": current.group_openid},
        )
        return {name: int(value) for name, value in rows.all()}


def make_roulette(
    factory: async_sessionmaker[AsyncSession],
    random_source: RandomSource | None = None,
) -> RouletteCommandService:
    """构造命令服务（ReplyProjection 假投影）。"""
    return RouletteCommandService(
        session_factory=factory,
        reply_projector=lambda _context: ReplyProjection(
            body="safe", metadata={"test": True}
        ),
        random_source=random_source,
    )


def request(
    current: Scope,
    message_id: str,
    command: CanonicalCommand,
    *,
    member_openid: str,
) -> CommandRequest:
    return CommandRequest(
        app_id=current.app_id,
        group_openid=current.group_openid,
        inbound_msg_id=message_id,
        member_openid=member_openid,
        command=command,
        target_mention_count=1,
    )


async def create_waiting(
    service: RouletteCommandService,
    current: Scope,
    member_openid: str,
    *,
    message_id: str | None = None,
) -> None:
    await service.execute_group_command(
        request(
            current,
            message_id or f"create-{uuid4().hex}",
            CanonicalCommand.create(),
            member_openid=member_openid,
        )
    )


async def create_active(
    service: RouletteCommandService,
    current: Scope,
    member_openids: tuple[str, str],
) -> None:
    """走完 create → join → start，进入 active 对局。"""
    await create_waiting(
        service, current, member_openids[0], message_id=f"create-{uuid4().hex}"
    )
    await service.execute_group_command(
        request(
            current,
            f"join-{uuid4().hex}",
            CanonicalCommand.join(),
            member_openid=member_openids[1],
        )
    )
    await service.execute_group_command(
        request(
            current,
            f"start-{uuid4().hex}",
            CanonicalCommand.start(),
            member_openid=member_openids[0],
        )
    )


async def persist_completed_game(
    factory: async_sessionmaker[AsyncSession],
    current: Scope,
) -> str:
    """用公开命令 seam 走完一局并返回 game_id。

    语义对照真实 domain/command_service：active 后的 ``shoot`` 是
    ``_OBSERVED_ACTIVE_WRITES``，必须先 ``observe_current`` 取得观测
    （game_id/state_revision/turn_seq）再提交命令，否则返回
    ``state_conflict``。首发命中 live 弹仓后射手被淘汰，2 名玩家只剩
    1 名 → 直接 completed，胜者为第二位玩家（与 ``_eliminate_current``
    语义一致）。
    """
    from komari_bot.plugins.komari_roulette.domain import ChamberKind
    from tests.komari_roulette.storage_support import DeterministicRandom

    service = make_roulette(
        factory,
        random_source=DeterministicRandom(
            chambers=(
                (
                    ChamberKind.LIVE,
                    ChamberKind.LIVE,
                    ChamberKind.BLANK,
                    ChamberKind.BLANK,
                    ChamberKind.BLANK,
                    ChamberKind.BLANK,
                ),
            )
        ),
    )
    first = current.with_member(1)
    second = current.with_member(2)
    await create_waiting(
        service, current, first.member_openid, message_id="game-create"
    )
    await service.execute_group_command(
        request(
            current,
            "game-join",
            CanonicalCommand.join(),
            member_openid=second.member_openid,
        )
    )
    await service.execute_group_command(
        request(
            current,
            "game-start",
            CanonicalCommand.start(),
            member_openid=first.member_openid,
        )
    )
    observed = await service.observe_current(
        GroupRef(app_id=current.app_id, group_openid=current.group_openid)
    )
    assert observed is not None, "start 后必须存在 active 对局"
    shot = await service.execute_group_command(
        request(
            current,
            "game-shoot",
            CanonicalCommand.shoot(),
            member_openid=first.member_openid,
        ),
        observation=observed,
    )
    assert shot.result_code == "shot"
    assert shot.game_id is not None
    return shot.game_id


async def create_waiting_in_session(
    session: AsyncSession,
    current: Scope,
    member_openid: str,
    *,
    name: str,
    game_id: str | None = None,
) -> str:
    """在调用方持锁的同一 session 内直接落一条 waiting 对局。

    复用 TSK-275 storage_support 的公开域 seam（``waiting_state`` +
    ``game_state_to_snapshot`` + ``PostgresRouletteStorage.create_waiting``）。
    """
    from komari_bot.plugins.komari_roulette import game_state_to_snapshot
    from tests.komari_roulette.storage_support import group_for, waiting_state

    resolved = game_id or f"game-{uuid4().hex}"
    group = group_for(current.app_id, current.group_openid)
    snapshot = game_state_to_snapshot(
        waiting_state(
            group,
            player_count=1,
            names=(name,),
            member_openids=(member_openid,),
        ),
        game_id=resolved,
    )
    storage = PostgresRouletteStorage(session)
    await storage.create_waiting(snapshot)
    return resolved


# ─── REST 桩服务（duck-typed，只经 API 路由 seam 观察） ───────────────────

READ_CREDENTIALS = [
    {
        "credential_id": "binding-reader",
        "token": "reader-token-00000000",
        "permissions": ["character_binding:read"],
    }
]
MANAGE_CREDENTIALS = [
    {
        "credential_id": "binding-operator",
        "token": "operator-token-000000",
        "permissions": ["character_binding:manage"],
    }
]
WILDCARD_CREDENTIALS = [
    {
        "credential_id": "binding-wildcard",
        "token": "wildcard-token-00000",
        "permissions": ["*"],
    }
]
REVOKED_MANAGE_CREDENTIALS = [
    {
        "credential_id": "binding-revoked",
        "token": "revoked-token-000000",
        "permissions": ["character_binding:manage"],
        "revoked_at": "2020-01-01T00:00:00+00:00",
    }
]


class StubBindingRepairService:
    """REST 路由测试使用的桩服务端口实现。

    桩服务只复现协议（diagnose/preview/confirm 三个关键字方法）与
    预先配置的失败异常；返回值是普通 dict，由路由层的 response_model
    负责定型，避免在夹具里 import 尚未实现的业务模块。
    """

    def __init__(self) -> None:
        self.diagnose_calls: list[dict[str, str]] = []
        self.preview_calls: list[dict[str, object]] = []
        self.confirm_calls: list[dict[str, object]] = []
        self.diagnosis: dict[str, object] | None = None
        self.preview_result: dict[str, object] | None = None
        self.confirm_result: dict[str, object] | None = None
        self.error: BaseException | None = None

    async def diagnose(self, **kwargs: object) -> dict[str, object]:
        self.diagnose_calls.append({key: str(value) for key, value in kwargs.items()})
        if self.error is not None:
            raise self.error
        if self.diagnosis is None:
            raise AssertionError("stub diagnosis is not configured")  # noqa: TRY003
        return self.diagnosis

    async def preview(self, **kwargs: object) -> dict[str, object]:
        self.preview_calls.append(dict(kwargs))
        if self.error is not None:
            raise self.error
        if self.preview_result is None:
            raise AssertionError("stub preview result is not configured")  # noqa: TRY003
        return self.preview_result

    async def confirm(self, **kwargs: object) -> dict[str, object]:
        self.confirm_calls.append(dict(kwargs))
        if self.error is not None:
            raise self.error
        if self.confirm_result is None:
            raise AssertionError("stub confirm result is not configured")  # noqa: TRY003
        return self.confirm_result


def diagnosis_payload(
    current: Scope,
    *,
    group_id: str | None = None,
    members: Sequence[dict[str, object]] = (),
    game_present: bool = False,
    game_lifecycle: str | None = None,
) -> dict[str, object]:
    return {
        "app_id": current.app_id,
        "group_openid": current.group_openid,
        "group_id": current.group_id if group_id is None else group_id,
        "members": list(members),
        "game_present": game_present,
        "game_lifecycle": game_lifecycle,
    }


def member_view(
    current: Scope,
    number: int = 1,
    *,
    name: str | None = "花火",
) -> dict[str, object]:
    member = current.with_member(number)
    return {
        "app_id": current.app_id,
        "group_openid": current.group_openid,
        "member_openid": member.member_openid,
        "member_qq": member.member_qq,
        "character_name": name,
    }


def preview_payload(
    current: Scope,
    *,
    token: str = "preview-token-1",
    scope: str = "member",
    member_openid: str | None = None,
    affected_count: int = 1,
    cleared_names: Sequence[str] = ("花火",),
    version: str = "deadbeef",
    expires_at: datetime | None = None,
) -> dict[str, object]:
    return {
        "token": token,
        "scope": scope,
        "app_id": current.app_id,
        "group_openid": current.group_openid,
        "member_openid": member_openid or current.member_openid,
        "affected_count": affected_count,
        "cleared_names": list(cleared_names),
        "version": version,
        "expires_at": (expires_at or datetime(2026, 9, 8, 12, 0, tzinfo=UTC)).isoformat(),
    }


def confirm_result_payload(
    current: Scope,
    *,
    scope: str = "member",
    member_openid: str | None = None,
    cleared_count: int = 1,
    cleared_names: Sequence[str] = ("花火",),
    version: str = "deadbeef",
    expected_count: int = 1,
) -> dict[str, object]:
    return {
        "scope": scope,
        "app_id": current.app_id,
        "group_openid": current.group_openid,
        "member_openid": member_openid or current.member_openid,
        "cleared_count": cleared_count,
        "cleared_names": list(cleared_names),
        "version": version,
        "expected_count": expected_count,
    }


def safe_target_hash(current: Scope, *, member_openid: str | None = None) -> str:
    """审计期望使用的安全目标哈希（与路由约定一致）。"""
    return hash_management_target(
        current.app_id,
        current.group_openid,
        member_openid or "<group>",
    )


def write_headers(
    *,
    request_id: str,
    token: str = "wildcard-token-00000",
    reason: str = "运营核对错误关联",
) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "X-Komari-Change-Reason": reason,
        "X-Request-ID": request_id,
    }


def read_headers(token: str = "reader-token-00000000") -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def now_utc() -> datetime:
    return datetime.now(UTC)


__all__ = [
    "MANAGE_CREDENTIALS",
    "PG_REQUIRED",
    "READ_CREDENTIALS",
    "REPAIR_API_PREFIX",
    "REPAIR_TOKEN_TTL",
    "REVOKED_MANAGE_CREDENTIALS",
    "WILDCARD_CREDENTIALS",
    "CommitFailureSwitch",
    "Scope",
    "SessionCloseCounter",
    "StubBindingRepairService",
    "backend_pid",
    "bind_member",
    "clear_binding_scope",
    "clear_roulette_scope",
    "confirm_result_payload",
    "create_active",
    "create_engine_and_factory",
    "create_waiting",
    "create_waiting_in_session",
    "diagnosis_payload",
    "group_binding_rows",
    "group_mapping_rows",
    "health_check_commit_failure_switch",
    "hold_group_lock",
    "install_commit_failure_switch",
    "make_game_state_reader",
    "make_roulette",
    "member_rows",
    "member_view",
    "now_utc",
    "persist_completed_game",
    "preview_payload",
    "read_headers",
    "request",
    "require_postgres",
    "reset_shared_orm_engine",
    "roulette_counts",
    "safe_target_hash",
    "scope",
    "seed_binding",
    "track_session_closes",
    "wait_for_blocked",
    "wait_for_blocked_count",
    "write_headers",
]
