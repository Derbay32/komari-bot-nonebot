"""TSK-280 真实 PostgreSQL 修复服务契约（KOMARI_TEST_POSTGRES_URL 门控）。

无服务阶段：本文件顶层 import 缺失业务模块即 RED（服务层）；门控未满足时
skip。实现落地后由 root 提供隔离库运行，验证：诊断直读 PG、成员/群范围
预览与确认、对局阻断与锁等待后槽位复核、依赖变化重预览、令牌单次/操作者/
10 分钟 TTL/重启失效、提交失败原子保留、终局历史与胜场不受修复影响、以及
与 bind/轮盘开局的并发锁串行化（复用 TSK-276 ``lock_group_scope``）。
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Self, cast
from uuid import uuid4

import pytest
from komari_bot.plugins.character_binding.repair import (
    BindingRepairService,
    RepairBlockedByGameError,
    RepairDependencyChangedError,
    RepairTokenError,
)
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from komari_bot.plugins.character_binding.manager import (
    BindingPersistenceError,
    CharacterBindingManager,
)
from komari_bot.plugins.komari_roulette import (
    CanonicalCommand,
    CommandRequest,
    GroupRef,
    PostgresRouletteStorage,
    ReplyProjection,
    RouletteCommandService,
)
from tests.character_binding.tsk280_support import (
    PG_REQUIRED,
    Scope,
    backend_pid,
    clear_roulette_scope,
    create_engine_and_factory,
    hold_group_lock,
    reset_shared_orm_engine,
    seed_binding,
    wait_for_blocked,
)
from tests.character_binding.tsk280_support import (
    scope as make_scope,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

    from komari_bot.plugins.komari_roulette.domain import RandomSource

pytestmark = [pytest.mark.asyncio, PG_REQUIRED]

START = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)
CHANGE_REASON = "运营核对错误关联"


@dataclass(frozen=True, slots=True)
class Harness:
    """TSK-276 同款：独立真实引擎 + 会话工厂 + 既有绑定管理器。"""

    engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]
    binding_manager: CharacterBindingManager


@pytest.fixture
async def harness() -> AsyncIterator[Harness]:
    async for engine, session_factory in create_engine_and_factory():
        manager = CharacterBindingManager()
        await manager.initialize()
        try:
            yield Harness(engine, session_factory, manager)
        finally:
            with suppress(Exception):
                await manager.close()
            await reset_shared_orm_engine()


class _Clock:
    """可推进时钟：令牌绝对 10 分钟 TTL 边界测试。"""

    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


def make_service(
    factory: async_sessionmaker[AsyncSession],
    clock: _Clock,
    manager: CharacterBindingManager | None = None,
) -> BindingRepairService:
    return BindingRepairService(session_factory=factory, clock=clock, manager=manager)


def make_roulette(
    factory: async_sessionmaker[AsyncSession],
    random_source: RandomSource | None = None,
) -> RouletteCommandService:
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


async def bind_member(
    manager: CharacterBindingManager,
    current: Scope,
    index: int,
    *,
    name: str | None = None,
) -> None:
    await seed_binding(
        manager,
        current,
        index,
        name=name or f"角色{index}",
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
    """用公开命令 seam 走完一局（2 名玩家、首发即命中终局），返回 game_id。"""
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
    shot = await service.execute_group_command(
        request(
            current,
            "game-shoot",
            CanonicalCommand.shoot(),
            member_openid=first.member_openid,
        )
    )
    assert shot.result_code == "shot"
    assert shot.game_id is not None
    return shot.game_id


async def roulette_counts(
    engine: AsyncEngine,
    current: Scope,
) -> dict[str, int]:
    """本作用域轮盘四表行数（终局历史/玩家/胜场/游戏）。"""
    group = current.group_openid
    async with engine.begin() as connection:
        rows = await connection.execute(
            text(
                """
                SELECT 'games', count(*) FROM komari_roulette_games
                 WHERE group_openid = :group
                UNION ALL
                SELECT 'results', count(*) FROM komari_roulette_results
                 WHERE group_openid = :group
                UNION ALL
                SELECT 'players', count(*) FROM komari_roulette_players
                 WHERE group_openid = :group
                UNION ALL
                SELECT 'wins', coalesce(sum(wins), 0) FROM komari_roulette_leaderboard
                 WHERE group_openid = :group
                """
            ),
            {"group": group},
        )
        return {name: int(value) for name, value in rows.all()}


async def group_binding_rows(
    engine: AsyncEngine,
    current: Scope,
) -> int:
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


class _FailingCommitSession:
    """把 commit 替换为确定性存储失败的窄代理（其余属性转发）。

    无论实现采用 ``async with session:``、显式 ``commit()`` 还是
    ``async with session.begin():``，代理都在成功提交路径抛出
    ``OperationalError``（DBAPIError 子类），保证失败确定性。
    """

    def __init__(self, inner: AsyncSession) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)

    async def __aenter__(self) -> Self:
        await self._inner.__aenter__()
        return self

    async def __aexit__(self, *args: object) -> None:
        if len(args) < 2 or args[1] is None:
            raise OperationalError("injected commit failure", {}, RuntimeError("boom"))  # noqa: TRY003
        await self._inner.__aexit__(*args)

    async def commit(self) -> None:
        raise OperationalError("injected commit failure", {}, RuntimeError("boom"))  # noqa: TRY003


class _FailingSessionFactory:
    """产出确定性 commit 失败 session 的 session_factory 注入点。"""

    def __init__(self, inner: async_sessionmaker[AsyncSession]) -> None:
        self._inner = inner

    def __call__(self) -> _FailingCommitSession:
        return _FailingCommitSession(self._inner())

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)


async def _blocking_confirm(
    service: BindingRepairService,
    current: Scope,
    token: str,
    *,
    operator_id: str = "tsk280-operator",
    request_id: str = "req-blocking",
) -> tuple[bool, BaseException | None]:
    """在独立任务里执行确认，返回 (是否成功, 异常)。"""
    try:
        await service.confirm(
            app_id=current.app_id,
            group_openid=current.group_openid,
            token=token,
            operator_id=operator_id,
            request_id=request_id,
            reason=CHANGE_REASON,
        )
    except BaseException as error:
        return False, error
    else:
        return True, None


async def _blocking_bind(
    manager: CharacterBindingManager,
    current: Scope,
    index: int,
    *,
    name: str,
) -> tuple[bool, BaseException | None]:
    try:
        await manager.bind_group_member(
            app_id=current.app_id,
            group_id=current.group_id,
            group_openid=current.group_openid,
            member_openid=current.with_member(index).member_openid,
            member_qq=current.with_member(index).member_qq,
            character_name=name,
        )
    except BaseException as error:
        return False, error
    else:
        return True, None


# ---------------------------------------------------------------- 诊断与范围


async def test_diagnose_reads_postgres_not_manager_cache(
    harness: Harness,
) -> None:
    """诊断必须直读 PG：管理器缓存外的直改（同一 scope）也能看到。"""
    current = make_scope("pg-diagnose-direct")
    await bind_member(harness.binding_manager, current, 1)
    service = make_service(
        harness.session_factory, _Clock(START), manager=harness.binding_manager
    )

    async with harness.session_factory() as session:
        await session.execute(
            text(
                """
                UPDATE komari_character_binding_members
                   SET character_name = '直改名字'
                 WHERE app_id = :app_id AND group_openid = :group_openid
                """
            ),
            {"app_id": current.app_id, "group_openid": current.group_openid},
        )
        await session.commit()

    diagnosis = await service.diagnose(
        app_id=current.app_id,
        group_openid=current.group_openid,
    )
    assert diagnosis.game_present is False
    assert len(diagnosis.members) == 1
    assert diagnosis.members[0].character_name == "直改名字"


async def test_member_scope_preview_and_confirm_with_isolation(
    harness: Harness,
) -> None:
    """成员范围只清除目标成员；同 scope 其他成员与群行不受影响。"""
    current = make_scope("pg-member-isolation")
    await bind_member(harness.binding_manager, current, 1, name="甲")
    await bind_member(harness.binding_manager, current, 2, name="乙")
    service = make_service(harness.session_factory, _Clock(START))
    member = current.with_member(1)

    preview = await service.preview(
        app_id=current.app_id,
        group_openid=current.group_openid,
        operator_id="tsk280-operator",
        member_openid=member.member_openid,
        reason=CHANGE_REASON,
    )
    assert preview.scope == "member"
    assert preview.member_openid == member.member_openid
    assert preview.affected_count == 1
    assert preview.cleared_names == ("甲",)
    assert preview.expires_at == START + timedelta(minutes=10)

    result = await service.confirm(
        app_id=current.app_id,
        group_openid=current.group_openid,
        token=preview.token,
        operator_id="tsk280-operator",
        request_id="req-member-isolation",
        reason=CHANGE_REASON,
    )
    assert result.scope == "member"
    assert result.cleared_count == 1
    assert result.cleared_names == ("甲",)

    async with harness.session_factory() as session:
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
        remaining = rows.all()
    assert remaining == [(current.with_member(2).member_openid, "乙")]


async def test_group_scope_preview_and_confirm_clear_all(
    harness: Harness,
) -> None:
    """群范围预览/确认清除全部成员并保留群行。"""
    current = make_scope("pg-group-clear")
    await bind_member(harness.binding_manager, current, 1, name="甲")
    await bind_member(harness.binding_manager, current, 2, name="乙")
    service = make_service(harness.session_factory, _Clock(START))

    preview = await service.preview(
        app_id=current.app_id,
        group_openid=current.group_openid,
        operator_id="tsk280-operator",
        reason=CHANGE_REASON,
    )
    assert preview.scope == "group"
    assert preview.member_openid is None
    assert preview.affected_count == 2
    assert set(preview.cleared_names) == {"甲", "乙"}

    result = await service.confirm(
        app_id=current.app_id,
        group_openid=current.group_openid,
        token=preview.token,
        operator_id="tsk280-operator",
        request_id="req-group-clear",
        reason=CHANGE_REASON,
    )
    assert result.scope == "group"
    assert result.cleared_count == 2
    assert set(result.cleared_names) == {"甲", "乙"}

    async with harness.session_factory() as session:
        group_rows = (
            await session.execute(
                text(
                    """
                    SELECT count(*) FROM komari_character_binding_groups
                     WHERE app_id = :app_id AND group_openid = :group_openid
                    """
                ),
                {"app_id": current.app_id, "group_openid": current.group_openid},
            )
        ).scalar_one()
        member_count = await group_binding_rows(harness.engine, current)
    assert int(group_rows) == 1  # 群行保留（空绑定），成员全部清除
    assert member_count == 0


async def test_repair_does_not_rebind_notify_or_change_admission(
    harness: Harness,
) -> None:
    """清除后不重绑、不触碰准入配置；服务构造无 sender，天然无通知路径。"""
    current = make_scope("pg-no-rebind")
    await bind_member(harness.binding_manager, current, 1, name="甲")
    service = make_service(harness.session_factory, _Clock(START))

    async with harness.engine.begin() as connection:
        before = (
            await connection.execute(
                text(
                    """
                    SELECT count(*), coalesce(max(revision), 0)
                      FROM komari_group_admission_config
                    """
                )
            )
        ).one()

    preview = await service.preview(
        app_id=current.app_id,
        group_openid=current.group_openid,
        operator_id="tsk280-operator",
        reason=CHANGE_REASON,
    )
    await service.confirm(
        app_id=current.app_id,
        group_openid=current.group_openid,
        token=preview.token,
        operator_id="tsk280-operator",
        request_id="req-no-rebind",
        reason=CHANGE_REASON,
    )

    async with harness.engine.begin() as connection:
        after = (
            await connection.execute(
                text(
                    """
                    SELECT count(*), coalesce(max(revision), 0)
                      FROM komari_group_admission_config
                    """
                )
            )
        ).one()
        bound_names = (
            await connection.execute(
                text(
                    """
                    SELECT count(*) FROM komari_character_binding_members
                     WHERE app_id = :app_id AND group_openid = :group_openid
                    """
                ),
                {"app_id": current.app_id, "group_openid": current.group_openid},
            )
        ).scalar_one()
    assert before == after  # 准入配置未变
    assert int(bound_names) == 0  # 未替用户重绑


# ---------------------------------------------------------------- 对局阻断


async def test_confirm_refused_while_waiting_game_exists(
    harness: Harness,
) -> None:
    """waiting 对局存在时确认被拒且成员原样保留。"""
    current = make_scope("pg-refuse-waiting")
    await bind_member(harness.binding_manager, current, 1, name="甲")
    await bind_member(harness.binding_manager, current, 2, name="乙")
    service = make_service(harness.session_factory, _Clock(START))
    roulette = make_roulette(harness.session_factory)

    preview = await service.preview(
        app_id=current.app_id,
        group_openid=current.group_openid,
        operator_id="tsk280-operator",
        reason=CHANGE_REASON,
    )
    assert preview.affected_count == 2

    await create_waiting(
        roulette, current, current.with_member(1).member_openid
    )

    with pytest.raises(RepairBlockedByGameError):
        await service.confirm(
            app_id=current.app_id,
            group_openid=current.group_openid,
            token=preview.token,
            operator_id="tsk280-operator",
            request_id="req-refuse-waiting",
            reason=CHANGE_REASON,
        )
    assert await group_binding_rows(harness.engine, current) == 2

    # 同一令牌再次确认已被消费（每次尝试都原子消耗令牌），游戏仍阻断修复
    with pytest.raises(RepairTokenError):
        await service.confirm(
            app_id=current.app_id,
            group_openid=current.group_openid,
            token=preview.token,
            operator_id="tsk280-operator",
            request_id="req-refuse-waiting-2",
            reason=CHANGE_REASON,
        )
    assert await group_binding_rows(harness.engine, current) == 2


async def test_confirm_refused_while_active_game_exists(
    harness: Harness,
) -> None:
    """active 对局存在时确认被拒，游戏不受影响。"""
    current = make_scope("pg-refuse-active")
    await bind_member(harness.binding_manager, current, 1, name="甲")
    await bind_member(harness.binding_manager, current, 2, name="乙")
    service = make_service(harness.session_factory, _Clock(START))
    roulette = make_roulette(harness.session_factory)

    preview = await service.preview(
        app_id=current.app_id,
        group_openid=current.group_openid,
        operator_id="tsk280-operator",
        reason=CHANGE_REASON,
    )
    assert preview.game_present is False

    await create_active(
        roulette,
        current,
        (
            current.with_member(1).member_openid,
            current.with_member(2).member_openid,
        ),
    )

    with pytest.raises(RepairBlockedByGameError):
        await service.confirm(
            app_id=current.app_id,
            group_openid=current.group_openid,
            token=preview.token,
            operator_id="tsk280-operator",
            request_id="req-refuse-active",
            reason=CHANGE_REASON,
        )
    assert await group_binding_rows(harness.engine, current) == 2


async def test_confirm_rechecks_slot_after_lock_wait(
    harness: Harness,
) -> None:
    """锁等待后必须复核槽位：等待期间出现的对局也会阻断确认。"""
    current = make_scope("pg-recheck-slot")
    await bind_member(harness.binding_manager, current, 1, name="甲")
    service = make_service(harness.session_factory, _Clock(START))
    preview = await service.preview(
        app_id=current.app_id,
        group_openid=current.group_openid,
        operator_id="tsk280-operator",
        reason=CHANGE_REASON,
    )

    async with harness.session_factory() as blocker:
        await blocker.begin()
        blocker_pid = await backend_pid(blocker)
        await hold_group_lock(blocker, current)
        confirm_task = asyncio.create_task(
            _blocking_confirm(service, current, preview.token)
        )
        await wait_for_blocked(harness.session_factory, blocker_pid)

        # 阻塞期间（同锁会话内）真实创建一个 waiting 对局
        from komari_bot.plugins.komari_roulette import game_state_to_snapshot
        from tests.komari_roulette.storage_support import group_for, waiting_state

        group = group_for(current.app_id, current.group_openid)
        member = current.with_member(1)
        snapshot = game_state_to_snapshot(
            waiting_state(
                group,
                player_count=1,
                names=("甲",),
                member_openids=(member.member_openid,),
            ),
            game_id=f"game-{uuid4().hex}",
        )
        storage = PostgresRouletteStorage(blocker)
        await storage.create_waiting(snapshot)
        await blocker.commit()

    ok, error = await confirm_task
    assert ok is False
    assert isinstance(error, RepairBlockedByGameError)
    assert await group_binding_rows(harness.engine, current) == 1

    # 令牌已因尝试而失效：重新预览才可修复（对局清除后）
    await clear_roulette_scope(harness.engine, current)
    with pytest.raises(RepairTokenError):
        await service.confirm(
            app_id=current.app_id,
            group_openid=current.group_openid,
            token=preview.token,
            operator_id="tsk280-operator",
            request_id="req-recheck-slot-replay",
            reason=CHANGE_REASON,
        )
    fresh = await service.preview(
        app_id=current.app_id,
        group_openid=current.group_openid,
        operator_id="tsk280-operator",
        reason=CHANGE_REASON,
    )
    assert fresh.affected_count == 1


# ---------------------------------------------------------------- 依赖变化


async def test_confirm_rejects_when_dependency_changed_between_preview_and_confirm(
    harness: Harness,
) -> None:
    """预览后依赖变化（成员被改名/增删）→ 确认 409 且原记录保留。"""
    current = make_scope("pg-dependency-changed")
    await bind_member(harness.binding_manager, current, 1, name="甲")
    service = make_service(harness.session_factory, _Clock(START))
    preview = await service.preview(
        app_id=current.app_id,
        group_openid=current.group_openid,
        operator_id="tsk280-operator",
        reason=CHANGE_REASON,
    )

    async with harness.session_factory() as session:
        await session.execute(
            text(
                """
                UPDATE komari_character_binding_members
                   SET character_name = '改名了'
                 WHERE app_id = :app_id AND group_openid = :group_openid
                """
            ),
            {"app_id": current.app_id, "group_openid": current.group_openid},
        )
        await session.commit()

    with pytest.raises(RepairDependencyChangedError):
        await service.confirm(
            app_id=current.app_id,
            group_openid=current.group_openid,
            token=preview.token,
            operator_id="tsk280-operator",
            request_id="req-dependency-changed",
            reason=CHANGE_REASON,
        )
    assert await group_binding_rows(harness.engine, current) == 1

    # 重新预览（新版本）→ 成功
    fresh = await service.preview(
        app_id=current.app_id,
        group_openid=current.group_openid,
        operator_id="tsk280-operator",
        reason=CHANGE_REASON,
    )
    assert fresh.version != preview.version
    result = await service.confirm(
        app_id=current.app_id,
        group_openid=current.group_openid,
        token=fresh.token,
        operator_id="tsk280-operator",
        request_id="req-dependency-repreview",
        reason=CHANGE_REASON,
    )
    assert result.cleared_count == 1


# ---------------------------------------------------------------- 令牌语义


async def test_confirm_rejects_operator_mismatch(
    harness: Harness,
) -> None:
    """令牌绑定预览操作者：他人确认被拒且不写库。"""
    current = make_scope("pg-token-operator")
    await bind_member(harness.binding_manager, current, 1, name="甲")
    service = make_service(harness.session_factory, _Clock(START))
    preview = await service.preview(
        app_id=current.app_id,
        group_openid=current.group_openid,
        operator_id="alice",
        reason=CHANGE_REASON,
    )
    with pytest.raises(RepairTokenError):
        await service.confirm(
            app_id=current.app_id,
            group_openid=current.group_openid,
            token=preview.token,
            operator_id="bob",
            request_id="req-operator-mismatch",
            reason=CHANGE_REASON,
        )
    assert await group_binding_rows(harness.engine, current) == 1


async def test_token_is_single_use(
    harness: Harness,
) -> None:
    """令牌一次性：成功确认后重放即 422。"""
    current = make_scope("pg-token-single-use")
    await bind_member(harness.binding_manager, current, 1, name="甲")
    service = make_service(harness.session_factory, _Clock(START))
    preview = await service.preview(
        app_id=current.app_id,
        group_openid=current.group_openid,
        operator_id="tsk280-operator",
        reason=CHANGE_REASON,
    )
    first = await service.confirm(
        app_id=current.app_id,
        group_openid=current.group_openid,
        token=preview.token,
        operator_id="tsk280-operator",
        request_id="req-single-use-1",
        reason=CHANGE_REASON,
    )
    assert first.cleared_count == 1
    with pytest.raises(RepairTokenError):
        await service.confirm(
            app_id=current.app_id,
            group_openid=current.group_openid,
            token=preview.token,
            operator_id="tsk280-operator",
            request_id="req-single-use-2",
            reason=CHANGE_REASON,
        )


async def test_token_ttl_boundary_at_nine_minutes_fifty_nine(
    harness: Harness,
) -> None:
    """9:59 仍在绝对 TTL 内，确认成功。"""
    current = make_scope("pg-token-ttl-959")
    await bind_member(harness.binding_manager, current, 1, name="甲")
    clock = _Clock(START)
    service = make_service(harness.session_factory, clock)
    preview = await service.preview(
        app_id=current.app_id,
        group_openid=current.group_openid,
        operator_id="tsk280-operator",
        reason=CHANGE_REASON,
    )
    clock.advance(timedelta(minutes=9, seconds=59))
    result = await service.confirm(
        app_id=current.app_id,
        group_openid=current.group_openid,
        token=preview.token,
        operator_id="tsk280-operator",
        request_id="req-ttl-959",
        reason=CHANGE_REASON,
    )
    assert result.cleared_count == 1


async def test_token_expires_at_exactly_ten_minutes(
    harness: Harness,
) -> None:
    """满 10:00 令牌绝对过期，确认 422 且不写库。"""
    current = make_scope("pg-token-ttl-1000")
    await bind_member(harness.binding_manager, current, 1, name="甲")
    clock = _Clock(START)
    service = make_service(harness.session_factory, clock)
    preview = await service.preview(
        app_id=current.app_id,
        group_openid=current.group_openid,
        operator_id="tsk280-operator",
        reason=CHANGE_REASON,
    )
    clock.advance(timedelta(minutes=10))
    with pytest.raises(RepairTokenError):
        await service.confirm(
            app_id=current.app_id,
            group_openid=current.group_openid,
            token=preview.token,
            operator_id="tsk280-operator",
            request_id="req-ttl-1000",
            reason=CHANGE_REASON,
        )
    assert await group_binding_rows(harness.engine, current) == 1


async def test_token_store_is_in_memory_and_restart_invalidates(
    harness: Harness,
) -> None:
    """令牌存进程内：新服务实例（重启）不认识旧令牌。"""
    current = make_scope("pg-token-restart")
    await bind_member(harness.binding_manager, current, 1, name="甲")
    first_service = make_service(harness.session_factory, _Clock(START))
    preview = await first_service.preview(
        app_id=current.app_id,
        group_openid=current.group_openid,
        operator_id="tsk280-operator",
        reason=CHANGE_REASON,
    )

    second_service = make_service(harness.session_factory, _Clock(START))
    with pytest.raises(RepairTokenError):
        await second_service.confirm(
            app_id=current.app_id,
            group_openid=current.group_openid,
            token=preview.token,
            operator_id="tsk280-operator",
            request_id="req-restart",
            reason=CHANGE_REASON,
        )
    assert await group_binding_rows(harness.engine, current) == 1


# ---------------------------------------------------------------- 原子性与历史


async def test_confirm_commit_failure_preserves_original_rows(
    harness: Harness,
) -> None:
    """提交失败 → BindingPersistenceError，删除不落库，原记录完整。"""
    current = make_scope("pg-commit-failure")
    await bind_member(harness.binding_manager, current, 1, name="甲")
    service = make_service(harness.session_factory, _Clock(START))
    preview = await service.preview(
        app_id=current.app_id,
        group_openid=current.group_openid,
        operator_id="tsk280-operator",
        reason=CHANGE_REASON,
    )

    failing_service = make_service(
        cast("Any", _FailingSessionFactory(harness.session_factory)), _Clock(START)
    )
    with pytest.raises(BindingPersistenceError):
        await failing_service.confirm(
            app_id=current.app_id,
            group_openid=current.group_openid,
            token=preview.token,
            operator_id="tsk280-operator",
            request_id="req-commit-failure",
            reason=CHANGE_REASON,
        )

    assert await group_binding_rows(harness.engine, current) == 1


async def test_completed_game_history_and_wins_preserved_after_repair(
    harness: Harness,
) -> None:
    """修复只清绑定，终局历史/玩家/胜场行原样保留。"""
    current = make_scope("pg-history-preserved")
    await bind_member(harness.binding_manager, current, 1, name="甲")
    await bind_member(harness.binding_manager, current, 2, name="乙")
    service = make_service(harness.session_factory, _Clock(START))

    game_id = await persist_completed_game(harness.session_factory, current)
    before = await roulette_counts(harness.engine, current)
    assert before["results"] >= 1
    assert before["wins"] >= 1

    preview = await service.preview(
        app_id=current.app_id,
        group_openid=current.group_openid,
        operator_id="tsk280-operator",
        reason=CHANGE_REASON,
    )
    assert preview.game_present is False  # 终局不阻断修复
    result = await service.confirm(
        app_id=current.app_id,
        group_openid=current.group_openid,
        token=preview.token,
        operator_id="tsk280-operator",
        request_id="req-history-preserved",
        reason=CHANGE_REASON,
    )
    assert result.cleared_count == 2

    after = await roulette_counts(harness.engine, current)
    assert after == before
    async with harness.session_factory() as session:
        winner = await session.execute(
            text(
                """
                SELECT winner_member_openid FROM komari_roulette_results
                 WHERE group_openid = :group AND game_id = :game_id
                """
            ),
            {"group": current.group_openid, "game_id": game_id},
        )
        assert winner.scalar_one() is not None
    assert await group_binding_rows(harness.engine, current) == 0


# ---------------------------------------------------------------- 并发锁


async def test_concurrent_confirm_and_bind_share_group_lock(
    harness: Harness,
) -> None:
    """确认与 /bind 共用 TSK-276 群锁：阻塞期间二者都在等同一把锁。"""
    current = make_scope("pg-concurrent-bind")
    await bind_member(harness.binding_manager, current, 1, name="甲")
    service = make_service(harness.session_factory, _Clock(START))
    preview = await service.preview(
        app_id=current.app_id,
        group_openid=current.group_openid,
        operator_id="tsk280-operator",
        reason=CHANGE_REASON,
    )

    async with harness.session_factory() as blocker:
        await blocker.begin()
        blocker_pid = await backend_pid(blocker)
        await hold_group_lock(blocker, current)
        confirm_task = asyncio.create_task(
            _blocking_confirm(service, current, preview.token)
        )
        bind_task = asyncio.create_task(
            _blocking_bind(harness.binding_manager, current, 3, name="并发绑定")
        )
        await wait_for_blocked(harness.session_factory, blocker_pid)
        await asyncio.sleep(0.5)
        async with harness.engine.connect() as probe:
            blocked = (
                await probe.execute(
                    text(
                        """
                        SELECT count(*) FROM pg_stat_activity
                         WHERE :blocker = ANY(pg_blocking_pids(pid))
                        """
                    ),
                    {"blocker": blocker_pid},
                )
            ).scalar_one()
        assert int(blocked) >= 2  # 确认与 bind 两条会话都阻塞在同一把锁
        await blocker.commit()

    confirm_ok, confirm_error = await confirm_task
    bind_ok, bind_error = await bind_task
    assert confirm_error is None
    assert bind_error is None
    assert confirm_ok is True
    assert bind_ok is True

    # 两种合法时序都产生一致终态：确认清掉旧成员，bind 后新增成员
    assert await group_binding_rows(harness.engine, current) == 1
    async with harness.session_factory() as session:
        rows = await session.execute(
            text(
                """
                SELECT member_openid, character_name
                  FROM komari_character_binding_members
                 WHERE app_id = :app_id AND group_openid = :group_openid
                """
            ),
            {"app_id": current.app_id, "group_openid": current.group_openid},
        )
        assert rows.all() == [(current.with_member(3).member_openid, "并发绑定")]


async def test_concurrent_confirm_and_roulette_open_share_group_lock(
    harness: Harness,
) -> None:
    """确认与轮盘开局共用群锁：无死锁，终态只有两种一致结果之一。"""
    current = make_scope("pg-concurrent-roulette")
    await bind_member(harness.binding_manager, current, 1, name="甲")
    await bind_member(harness.binding_manager, current, 2, name="乙")
    service = make_service(harness.session_factory, _Clock(START))
    roulette = make_roulette(harness.session_factory)
    preview = await service.preview(
        app_id=current.app_id,
        group_openid=current.group_openid,
        operator_id="tsk280-operator",
        reason=CHANGE_REASON,
    )

    async def _blocking_open() -> tuple[bool, BaseException | None]:
        try:
            await create_waiting(
                roulette, current, current.with_member(1).member_openid
            )
        except BaseException as error:
            return False, error
        else:
            return True, None

    async with harness.session_factory() as blocker:
        await blocker.begin()
        blocker_pid = await backend_pid(blocker)
        await hold_group_lock(blocker, current)
        confirm_task = asyncio.create_task(
            _blocking_confirm(service, current, preview.token)
        )
        open_task = asyncio.create_task(_blocking_open())
        await wait_for_blocked(harness.session_factory, blocker_pid)
        await asyncio.sleep(0.5)
        async with harness.engine.connect() as probe:
            blocked = (
                await probe.execute(
                    text(
                        """
                        SELECT count(*) FROM pg_stat_activity
                         WHERE :blocker = ANY(pg_blocking_pids(pid))
                        """
                    ),
                    {"blocker": blocker_pid},
                )
            ).scalar_one()
        assert int(blocked) >= 2
        await blocker.commit()

    confirm_ok, confirm_error = await confirm_task
    open_ok, open_error = await open_task
    assert confirm_task.done() and open_task.done()  # 无死锁

    async with harness.session_factory() as session:
        storage = PostgresRouletteStorage(session)
        current_game = await storage.load_current(
            GroupRef(app_id=current.app_id, group_openid=current.group_openid)
        )
        game_present = current_game is not None

    group_present = (await group_binding_rows(harness.engine, current)) > 0
    # 开局的先拿到锁 → 对局存在且确认被拒（绑定保留）；
    # 确认先拿到锁 → 群清空且开局因无绑定失败（无对局）。
    assert (game_present, group_present) in {(True, True), (False, False)}
    if game_present:
        assert confirm_ok is False
        assert isinstance(confirm_error, RepairBlockedByGameError)
        assert open_ok is True
    else:
        assert confirm_ok is True
        assert open_ok is False
        assert open_error is not None
