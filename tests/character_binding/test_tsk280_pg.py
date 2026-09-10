"""TSK-280 真实 PostgreSQL 修复服务契约（KOMARI_TEST_POSTGRES_URL 门控）。

无服务阶段：本文件顶层 import 缺失业务模块即 RED（服务层）；门控未满足时
skip。实现落地后由 root 提供隔离库运行，验证：诊断直读 PG、成员/群范围
预览与确认（群级同时清除群映射，TSK-269 第六节）、对局阻断与锁等待后槽位
复核、依赖增删/改名/解绑变化、令牌绑定目标/单次/操作者/10 分钟 TTL/重启
失效、锁等待跨 TTL 后失效、同令牌并发仅一次清除、提交失败原子保留（同一
实例 + 真实 before_commit 事件注入）、终局历史与胜场不受修复影响、普通用户
解绑保留身份关系，以及与 bind/轮盘开局的并发锁串行化。

新增正确 RED（当前生产明确缺失）：close 生命周期（等待群锁的 confirm 不得
删除 / preview 不得发令牌 / 旧引用拒绝操作）与版本指纹 ABA（改名改回、删重
建同值不得用旧令牌清除）。

夹具正确性由 ``test_tsk280_fixture_probe.py`` 独立验证（不依赖本模块缺失
的 repair 模块）。
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from sqlalchemy import text

from komari_bot.plugins.character_binding.manager import (
    BindingPersistenceError,
    CharacterBindingManager,
)
from komari_bot.plugins.character_binding.repair import (
    BindingRepairService,
    RepairBlockedByGameError,
    RepairDependencyChangedError,
    RepairTokenError,
)
from komari_bot.plugins.character_binding.transaction import BindingTransaction
from komari_bot.plugins.komari_roulette import (
    CanonicalCommand,
    GroupRef,
    PostgresRouletteStorage,
)
from tests.character_binding.tsk280_support import (
    PG_REQUIRED,
    CommitFailureSwitch,
    Scope,
    backend_pid,
    bind_member,
    clear_roulette_scope,
    create_active,
    create_engine_and_factory,
    create_waiting,
    create_waiting_in_session,
    group_binding_rows,
    group_mapping_rows,
    health_check_commit_failure_switch,
    hold_group_lock,
    install_commit_failure_switch,
    make_game_state_reader,
    make_roulette,
    member_rows,
    persist_completed_game,
    request,
    reset_shared_orm_engine,
    roulette_counts,
    seed_binding,
    track_session_closes,
    wait_for_blocked,
    wait_for_blocked_count,
)
from tests.character_binding.tsk280_support import scope as make_scope

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

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
    return BindingRepairService(
        session_factory=factory,
        clock=clock,
        game_state_reader=make_game_state_reader(),
        manager=manager,
    )


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


async def _blocking_preview(
    service: BindingRepairService,
    current: Scope,
    *,
    operator_id: str = "tsk280-operator",
) -> tuple[object | None, BaseException | None]:
    """在独立任务里执行预览，返回 (预览结果, 异常)。"""
    try:
        result = await service.preview(
            app_id=current.app_id,
            group_openid=current.group_openid,
            operator_id=operator_id,
            reason=CHANGE_REASON,
        )
    except BaseException as error:
        return None, error
    else:
        return result, None


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
    """成员范围只清除目标成员；跨应用同 openid 与同应用其他群完全不变。"""
    current = make_scope("pg-member-isolation")
    await bind_member(harness.binding_manager, current, 1, name="甲")
    await bind_member(harness.binding_manager, current, 2, name="乙")
    cross_app = current.sibling(other_app=True)
    cross_group = current.sibling(other_group=True)
    await seed_binding(harness.binding_manager, cross_app, 1, name="跨应用甲")
    await seed_binding(harness.binding_manager, cross_group, 1, name="跨群甲")
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
        assert await member_rows(session, current) == [
            (current.with_member(2).member_openid, "乙")
        ]
        # 跨应用同 openid、同应用其他群：成员行与群映射原样保留。
        assert await member_rows(session, cross_app) == [
            (member.member_openid, "跨应用甲")
        ]
        assert await member_rows(session, cross_group) == [
            (member.member_openid, "跨群甲")
        ]
    assert await group_mapping_rows(harness.engine, current) == 1
    assert await group_mapping_rows(harness.engine, cross_app) == 1
    assert await group_mapping_rows(harness.engine, cross_group) == 1


async def test_group_scope_preview_and_confirm_clear_all(
    harness: Harness,
) -> None:
    """群范围清除所选应用/群映射及全部成员；其他应用/群完全不变（TSK-269）。"""
    current = make_scope("pg-group-clear")
    await bind_member(harness.binding_manager, current, 1, name="甲")
    await bind_member(harness.binding_manager, current, 2, name="乙")
    cross_app = current.sibling(other_app=True)
    cross_group = current.sibling(other_group=True)
    await seed_binding(harness.binding_manager, cross_app, 1, name="跨应用甲")
    await seed_binding(harness.binding_manager, cross_app, 2, name="跨应用乙")
    await seed_binding(harness.binding_manager, cross_group, 1, name="跨群甲")
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

    # TSK-269 第六节：群级清除该群映射及依赖它的全部成员关联。
    assert await group_mapping_rows(harness.engine, current) == 0
    assert await group_binding_rows(harness.engine, current) == 0

    # 独立验证：同 openid 的其他应用、同应用的其他群完全不变。
    async with harness.session_factory() as session:
        assert await member_rows(session, cross_app) == [
            (current.with_member(1).member_openid, "跨应用甲"),
            (current.with_member(2).member_openid, "跨应用乙"),
        ]
        assert await member_rows(session, cross_group) == [
            (current.with_member(1).member_openid, "跨群甲"),
        ]
    assert await group_mapping_rows(harness.engine, cross_app) == 1
    assert await group_mapping_rows(harness.engine, cross_group) == 1


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


async def test_preview_refused_while_waiting_game_exists(
    harness: Harness,
) -> None:
    """waiting 对局已存在时预览即被拒（无需先签令牌）。"""
    current = make_scope("pg-preview-refuse-waiting")
    await bind_member(harness.binding_manager, current, 1, name="甲")
    await bind_member(harness.binding_manager, current, 2, name="乙")
    service = make_service(harness.session_factory, _Clock(START))
    roulette = make_roulette(harness.session_factory)

    await create_waiting(
        roulette, current, current.with_member(1).member_openid
    )
    with pytest.raises(RepairBlockedByGameError):
        await service.preview(
            app_id=current.app_id,
            group_openid=current.group_openid,
            operator_id="tsk280-operator",
            reason=CHANGE_REASON,
        )
    assert await group_binding_rows(harness.engine, current) == 2


async def test_preview_refused_while_active_game_exists(
    harness: Harness,
) -> None:
    """active 对局已存在时预览即被拒。"""
    current = make_scope("pg-preview-refuse-active")
    await bind_member(harness.binding_manager, current, 1, name="甲")
    await bind_member(harness.binding_manager, current, 2, name="乙")
    service = make_service(harness.session_factory, _Clock(START))
    roulette = make_roulette(harness.session_factory)

    await create_active(
        roulette,
        current,
        (
            current.with_member(1).member_openid,
            current.with_member(2).member_openid,
        ),
    )
    with pytest.raises(RepairBlockedByGameError):
        await service.preview(
            app_id=current.app_id,
            group_openid=current.group_openid,
            operator_id="tsk280-operator",
            reason=CHANGE_REASON,
        )
    assert await group_binding_rows(harness.engine, current) == 2


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
        try:
            await wait_for_blocked(harness.session_factory, blocker_pid)

            # 阻塞期间（同锁会话内）真实创建一个 waiting 对局
            await create_waiting_in_session(
                blocker,
                current,
                current.with_member(1).member_openid,
                name="甲",
            )
        finally:
            await blocker.commit()

    ok, error = await asyncio.wait_for(confirm_task, timeout=10)
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


async def test_confirm_rejects_when_member_added_after_preview(
    harness: Harness,
) -> None:
    """群范围预览后新增成员 → 依赖集合变化，确认被拒且不误删。"""
    current = make_scope("pg-dep-add")
    await bind_member(harness.binding_manager, current, 1, name="甲")
    await bind_member(harness.binding_manager, current, 2, name="乙")
    service = make_service(harness.session_factory, _Clock(START))
    preview = await service.preview(
        app_id=current.app_id,
        group_openid=current.group_openid,
        operator_id="tsk280-operator",
        reason=CHANGE_REASON,
    )
    assert preview.affected_count == 2

    await bind_member(harness.binding_manager, current, 3, name="丙")

    with pytest.raises(RepairDependencyChangedError):
        await service.confirm(
            app_id=current.app_id,
            group_openid=current.group_openid,
            token=preview.token,
            operator_id="tsk280-operator",
            request_id="req-dep-add",
            reason=CHANGE_REASON,
        )
    assert await group_binding_rows(harness.engine, current) == 3

    # 重新预览后才可清除全部 3 名成员
    fresh = await service.preview(
        app_id=current.app_id,
        group_openid=current.group_openid,
        operator_id="tsk280-operator",
        reason=CHANGE_REASON,
    )
    assert fresh.affected_count == 3
    result = await service.confirm(
        app_id=current.app_id,
        group_openid=current.group_openid,
        token=fresh.token,
        operator_id="tsk280-operator",
        request_id="req-dep-add-repreview",
        reason=CHANGE_REASON,
    )
    assert result.cleared_count == 3
    assert await group_binding_rows(harness.engine, current) == 0


async def test_confirm_rejects_when_member_deleted_after_preview(
    harness: Harness,
) -> None:
    """群范围预览后成员被删除 → 依赖集合变化，确认被拒且不误删。"""
    current = make_scope("pg-dep-delete")
    await bind_member(harness.binding_manager, current, 1, name="甲")
    await bind_member(harness.binding_manager, current, 2, name="乙")
    service = make_service(harness.session_factory, _Clock(START))
    preview = await service.preview(
        app_id=current.app_id,
        group_openid=current.group_openid,
        operator_id="tsk280-operator",
        reason=CHANGE_REASON,
    )
    assert preview.affected_count == 2

    async with harness.session_factory() as session:
        await session.execute(
            text(
                """
                DELETE FROM komari_character_binding_members
                 WHERE app_id = :app_id AND group_openid = :group_openid
                   AND member_openid = :member_openid
                """
            ),
            {
                "app_id": current.app_id,
                "group_openid": current.group_openid,
                "member_openid": current.with_member(2).member_openid,
            },
        )
        await session.commit()

    with pytest.raises(RepairDependencyChangedError):
        await service.confirm(
            app_id=current.app_id,
            group_openid=current.group_openid,
            token=preview.token,
            operator_id="tsk280-operator",
            request_id="req-dep-delete",
            reason=CHANGE_REASON,
        )
    assert await group_binding_rows(harness.engine, current) == 1


async def test_confirm_rejects_when_target_unbound_after_preview(
    harness: Harness,
) -> None:
    """成员范围预览后目标被普通解绑（清角色名）→ 依赖变化，关系保留。"""
    current = make_scope("pg-dep-unbind")
    await bind_member(harness.binding_manager, current, 1, name="甲")
    service = make_service(harness.session_factory, _Clock(START))
    member = current.with_member(1)
    preview = await service.preview(
        app_id=current.app_id,
        group_openid=current.group_openid,
        operator_id="tsk280-operator",
        member_openid=member.member_openid,
        reason=CHANGE_REASON,
    )
    assert preview.affected_count == 1

    await harness.binding_manager.clear_character_name(
        app_id=current.app_id,
        group_openid=current.group_openid,
        member_openid=member.member_openid,
    )

    with pytest.raises(RepairDependencyChangedError):
        await service.confirm(
            app_id=current.app_id,
            group_openid=current.group_openid,
            token=preview.token,
            operator_id="tsk280-operator",
            request_id="req-dep-unbind",
            reason=CHANGE_REASON,
        )
    # 普通解绑不清除身份关系：成员行仍在、角色名被清空。
    async with harness.session_factory() as session:
        assert await member_rows(session, current) == [(member.member_openid, None)]
    assert await group_mapping_rows(harness.engine, current) == 1


# ---------------------------------------------------------------- 令牌语义


async def test_confirm_rejects_tampered_app_or_group_target(
    harness: Harness,
) -> None:
    """令牌绑定预览时的 app/group 目标：篡改任一目标都不能清除任何行。"""
    current = make_scope("pg-token-tamper")
    await bind_member(harness.binding_manager, current, 1, name="甲")
    service = make_service(harness.session_factory, _Clock(START))
    preview = await service.preview(
        app_id=current.app_id,
        group_openid=current.group_openid,
        operator_id="tsk280-operator",
        reason=CHANGE_REASON,
    )

    other_app = f"{current.app_id}-other"
    other_group = f"{current.group_openid}-other"
    for tampered_app, tampered_group in (
        (other_app, current.group_openid),
        (current.app_id, other_group),
    ):
        with pytest.raises(RepairTokenError):
            await service.confirm(
                app_id=tampered_app,
                group_openid=tampered_group,
                token=preview.token,
                operator_id="tsk280-operator",
                request_id="req-tamper",
                reason=CHANGE_REASON,
            )
    assert await group_binding_rows(harness.engine, current) == 1
    assert await group_mapping_rows(harness.engine, current) == 1

    # 真实目标重新预览后仍可正常清除。
    fresh = await service.preview(
        app_id=current.app_id,
        group_openid=current.group_openid,
        operator_id="tsk280-operator",
        reason=CHANGE_REASON,
    )
    result = await service.confirm(
        app_id=current.app_id,
        group_openid=current.group_openid,
        token=fresh.token,
        operator_id="tsk280-operator",
        request_id="req-tamper-fresh",
        reason=CHANGE_REASON,
    )
    assert result.cleared_count == 1
    assert await group_binding_rows(harness.engine, current) == 0


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


async def test_same_token_concurrent_confirm_clears_exactly_once(
    harness: Harness,
) -> None:
    """同一令牌并发确认：有界并发下恰好一次清除，数据库不变量成立。"""
    current = make_scope("pg-token-concurrent")
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

    results = await asyncio.wait_for(
        asyncio.gather(
            _blocking_confirm(
                service, current, preview.token, request_id="race-a"
            ),
            _blocking_confirm(
                service, current, preview.token, request_id="race-b"
            ),
        ),
        timeout=10,
    )
    ok_flags = [ok for ok, _ in results]
    errors = [error for _, error in results]
    assert ok_flags.count(True) == 1  # 恰好一次清除
    assert sum(isinstance(error, RepairTokenError) for error in errors) == 1

    # 数据库不变量：群映射与成员关联都只被清除一次。
    assert await group_mapping_rows(harness.engine, current) == 0
    assert await group_binding_rows(harness.engine, current) == 0
    async with harness.session_factory() as session:
        assert await member_rows(session, current) == []


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
    """提交失败 → BindingPersistenceError，删除不落库，原记录完整。

    使用与 preview **同一实例** 的会话工厂：先健康验证提交失败注入，再
    武装开关执行确认。失败必须发生在真正 COMMIT 之前（``before_commit``
    事件），不能等 ``__aexit__`` 已提交后抛，也不能漏掉 session 关闭。
    """
    current = make_scope("pg-commit-failure")
    await bind_member(harness.binding_manager, current, 1, name="甲")
    await bind_member(harness.binding_manager, current, 2, name="乙")

    switch = CommitFailureSwitch()
    await health_check_commit_failure_switch(harness.session_factory, switch)

    service = make_service(harness.session_factory, _Clock(START))
    preview = await service.preview(
        app_id=current.app_id,
        group_openid=current.group_openid,
        operator_id="tsk280-operator",
        reason=CHANGE_REASON,
    )
    assert preview.affected_count == 2

    with install_commit_failure_switch(switch):
        raised_before = switch.raised
        switch.arm()
        try:
            with pytest.raises(BindingPersistenceError):
                await service.confirm(
                    app_id=current.app_id,
                    group_openid=current.group_openid,
                    token=preview.token,
                    operator_id="tsk280-operator",
                    request_id="req-commit-failure",
                    reason=CHANGE_REASON,
                )
        finally:
            switch.disarm()

    # 注入确实在真正 COMMIT 前拦截了一次提交。
    assert switch.raised == raised_before + 1
    # 原数据未提交：群行与全部成员行完整保留。
    assert await group_mapping_rows(harness.engine, current) == 1
    assert await group_binding_rows(harness.engine, current) == 2
    async with harness.session_factory() as session:
        assert await member_rows(session, current) == [
            (current.with_member(1).member_openid, "甲"),
            (current.with_member(2).member_openid, "乙"),
        ]

    # 失败后服务仍可用：重新预览并成功清除（证明没有卡死/半提交状态）。
    fresh = await service.preview(
        app_id=current.app_id,
        group_openid=current.group_openid,
        operator_id="tsk280-operator",
        reason=CHANGE_REASON,
    )
    result = await service.confirm(
        app_id=current.app_id,
        group_openid=current.group_openid,
        token=fresh.token,
        operator_id="tsk280-operator",
        request_id="req-commit-failure-after",
        reason=CHANGE_REASON,
    )
    assert result.cleared_count == 2
    assert await group_binding_rows(harness.engine, current) == 0


async def test_confirm_commit_failure_closes_failed_session(
    harness: Harness,
) -> None:
    """提交失败路径不能泄漏 session：after_close 计数必须覆盖失败确认。"""
    current = make_scope("pg-commit-failure-close")
    await bind_member(harness.binding_manager, current, 1, name="甲")
    service = make_service(harness.session_factory, _Clock(START))
    preview = await service.preview(
        app_id=current.app_id,
        group_openid=current.group_openid,
        operator_id="tsk280-operator",
        reason=CHANGE_REASON,
    )

    switch = CommitFailureSwitch()
    await health_check_commit_failure_switch(harness.session_factory, switch)
    with install_commit_failure_switch(switch), track_session_closes() as closes:
        before = closes.closed
        switch.arm()
        try:
            with pytest.raises(BindingPersistenceError):
                await service.confirm(
                    app_id=current.app_id,
                    group_openid=current.group_openid,
                    token=preview.token,
                    operator_id="tsk280-operator",
                    request_id="req-commit-failure-close",
                    reason=CHANGE_REASON,
                )
        finally:
            switch.disarm()
        assert closes.closed > before  # 失败确认的 session 已被关闭
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

    # 终局（completed）不阻断修复：预览与确认都正常完成。
    preview = await service.preview(
        app_id=current.app_id,
        group_openid=current.group_openid,
        operator_id="tsk280-operator",
        reason=CHANGE_REASON,
    )
    assert preview.affected_count == 2
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


# ---------------------------------------------------------------- 普通用户解绑


async def test_normal_user_unbind_keeps_identity_relationship(
    harness: Harness,
) -> None:
    """普通解绑只清角色名、保留身份关系；修复才是删除关系的唯一入口。"""
    current = make_scope("pg-unbind-keeps")
    await bind_member(harness.binding_manager, current, 1, name="甲")
    member = current.with_member(1)

    cleared = await harness.binding_manager.clear_character_name(
        app_id=current.app_id,
        group_openid=current.group_openid,
        member_openid=member.member_openid,
    )
    assert cleared is True

    # 身份关系保留：成员行仍在（角色名清空）、群映射仍在。
    async with harness.session_factory() as session:
        assert await member_rows(session, current) == [(member.member_openid, None)]
    assert await group_mapping_rows(harness.engine, current) == 1

    # 诊断仍列出该成员（关系未被删除）。
    service = make_service(harness.session_factory, _Clock(START))
    diagnosis = await service.diagnose(
        app_id=current.app_id,
        group_openid=current.group_openid,
    )
    assert len(diagnosis.members) == 1
    assert diagnosis.members[0].member_openid == member.member_openid
    assert diagnosis.members[0].character_name is None

    # 成员级修复可清除该关联（仅修复入口删除身份关系）。
    preview = await service.preview(
        app_id=current.app_id,
        group_openid=current.group_openid,
        operator_id="tsk280-operator",
        member_openid=member.member_openid,
        reason=CHANGE_REASON,
    )
    assert preview.affected_count == 1
    result = await service.confirm(
        app_id=current.app_id,
        group_openid=current.group_openid,
        token=preview.token,
        operator_id="tsk280-operator",
        request_id="req-unbind-repair",
        reason=CHANGE_REASON,
    )
    assert result.cleared_count == 1
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
        try:
            await wait_for_blocked_count(
                harness.session_factory, blocker_pid, min_count=2
            )
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
        finally:
            await blocker.rollback()

    # 锁已释放：有界等待两个任务自然完成；finally 只兜底清理仍未完成的任务，
    # 绝不在释放锁后立即 cancel 尚未完成任务（那会把正常完成变成 CancelledError）。
    pending = (confirm_task, bind_task)
    try:
        confirm_ok, confirm_error = await asyncio.wait_for(confirm_task, timeout=10)
        bind_ok, bind_error = await asyncio.wait_for(bind_task, timeout=10)
    finally:
        for task in pending:
            if not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
    assert bind_error is None
    assert bind_ok is True

    # 两种合法时序都成立（共享同一把群锁、无死锁）：
    # - 确认先拿到锁 → 清掉旧成员，bind 随后新增成员 → 只剩新成员；
    # - bind 先拿到锁 → 依赖集合已变化（预览后新增成员），确认按契约被拒
    #   （RepairDependencyChangedError）→ 原成员保留并新增成员。
    async with harness.session_factory() as session:
        final_members = await member_rows(session, current)
    if confirm_ok:
        assert confirm_error is None
        assert final_members == [
            (current.with_member(3).member_openid, "并发绑定")
        ]
    else:
        assert isinstance(confirm_error, RepairDependencyChangedError)
        assert final_members == [
            (current.with_member(1).member_openid, "甲"),
            (current.with_member(3).member_openid, "并发绑定"),
        ]
    assert await group_mapping_rows(harness.engine, current) == 1


async def test_concurrent_confirm_and_roulette_open_share_group_lock(
    harness: Harness,
) -> None:
    """确认与轮盘开局共用群锁：无死锁，终态只有两种一致结果之一。"""
    current = make_scope("pg-concurrent-roulette")
    await bind_member(harness.binding_manager, current, 1, name="甲")
    await bind_member(harness.binding_manager, current, 2, name="乙")
    service = make_service(harness.session_factory, _Clock(START))
    preview = await service.preview(
        app_id=current.app_id,
        group_openid=current.group_openid,
        operator_id="tsk280-operator",
        reason=CHANGE_REASON,
    )
    # 并发创建必须走真实轮盘命令服务；BindingRepairService 绝不是
    # RouletteCommandService 的转发入口（严禁 execute_group_command 越界 API）。
    roulette_service = make_roulette(harness.session_factory)

    async def _blocking_open() -> tuple[str | None, BaseException | None]:
        try:
            receipt = await roulette_service.execute_group_command(
                request(
                    current,
                    f"open-{uuid4().hex}",
                    CanonicalCommand.create(),
                    member_openid=current.with_member(1).member_openid,
                )
            )
        except BaseException as error:
            return None, error
        else:
            return receipt.result_code, None

    async with harness.session_factory() as blocker:
        await blocker.begin()
        blocker_pid = await backend_pid(blocker)
        await hold_group_lock(blocker, current)
        confirm_task = asyncio.create_task(
            _blocking_confirm(service, current, preview.token)
        )
        open_task = asyncio.create_task(_blocking_open())
        try:
            await wait_for_blocked_count(
                harness.session_factory, blocker_pid, min_count=2
            )
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
        finally:
            await blocker.rollback()

    # 锁已释放：有界等待两个任务自然完成；finally 只兜底清理仍未完成的任务。
    pending = (confirm_task, open_task)
    try:
        confirm_ok, confirm_error = await asyncio.wait_for(confirm_task, timeout=10)
        open_code, open_error = await asyncio.wait_for(open_task, timeout=10)
    finally:
        for task in pending:
            if not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
    assert confirm_task.done() and open_task.done()  # 无死锁

    async with harness.session_factory() as session:
        storage = PostgresRouletteStorage(session)
        current_game = await storage.load_current(
            GroupRef(app_id=current.app_id, group_openid=current.group_openid)
        )
        game_present = current_game is not None

    group_present = (await group_binding_rows(harness.engine, current)) > 0
    # 开局的先拿到锁 → 对局存在且确认被拒（绑定保留）；
    # 确认先拿到锁 → 群清空且开局因无绑定返回 binding_required（无对局）。
    assert (game_present, group_present) in {(True, True), (False, False)}
    if game_present:
        assert confirm_ok is False
        assert isinstance(confirm_error, RepairBlockedByGameError)
        assert open_code == "created"
        assert open_error is None
    else:
        assert confirm_ok is True
        assert open_code == "binding_required"
        assert open_error is None


# ---------------------------------------------------------------- 锁等待跨 TTL


async def test_confirm_rechecks_token_expiry_after_lock_wait(
    harness: Harness,
) -> None:
    """锁等待期间令牌过期：提交前必须复核，过期则拒绝且不写库。"""
    current = make_scope("pg-ttl-lock-wait")
    await bind_member(harness.binding_manager, current, 1, name="甲")
    clock = _Clock(START)
    service = make_service(harness.session_factory, clock)
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
        try:
            await wait_for_blocked(harness.session_factory, blocker_pid)
            # 确认已通过初始令牌校验并阻塞在群锁上；等待期间令牌绝对过期。
            clock.advance(timedelta(minutes=10, seconds=1))
        finally:
            await blocker.rollback()

    # 锁已释放：有界等待确认任务自然完成（锁释放后确认继续执行并复核 TTL）。
    try:
        ok, error = await asyncio.wait_for(confirm_task, timeout=10)
    finally:
        if not confirm_task.done():
            confirm_task.cancel()
            await asyncio.gather(confirm_task, return_exceptions=True)
    assert ok is False
    assert isinstance(error, RepairTokenError)
    assert await group_binding_rows(harness.engine, current) == 1
    assert await group_mapping_rows(harness.engine, current) == 1


# ---------------------------------------------------------------- close 生命周期


async def test_close_while_confirm_waits_on_group_lock_does_not_delete(
    harness: Harness,
) -> None:
    """close 期间已阻塞在群锁上的 confirm 不得删除任何行。

    close() 不得等待在途任务（会与锁等待死锁）；锁释放后 confirm 必须
    复核已关闭状态（或令牌已失效）并失败，数据库不变量保持。
    """
    current = make_scope("pg-close-confirm")
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
        try:
            await wait_for_blocked(harness.session_factory, blocker_pid)
            # 确认已通过初始令牌校验并阻塞在群锁上；此时关闭服务。
            # close 必须立即返回（不等待在途任务），否则与锁等待互相死锁。
            await asyncio.wait_for(service.close(), timeout=5)
        finally:
            await blocker.rollback()

    try:
        ok, error = await asyncio.wait_for(confirm_task, timeout=10)
    finally:
        if not confirm_task.done():
            confirm_task.cancel()
            await asyncio.gather(confirm_task, return_exceptions=True)
    assert ok is False
    assert error is not None
    # 业务不变量：close 期间等待锁的 confirm 不得清除任何行。
    assert await group_binding_rows(harness.engine, current) == 1
    assert await group_mapping_rows(harness.engine, current) == 1


async def test_close_while_preview_waits_on_group_lock_does_not_issue_token(
    harness: Harness,
) -> None:
    """close 期间已阻塞在群锁上的 preview 不得签发确认令牌。"""
    current = make_scope("pg-close-preview")
    await bind_member(harness.binding_manager, current, 1, name="甲")
    service = make_service(harness.session_factory, _Clock(START))

    async with harness.session_factory() as blocker:
        await blocker.begin()
        blocker_pid = await backend_pid(blocker)
        await hold_group_lock(blocker, current)
        preview_task = asyncio.create_task(_blocking_preview(service, current))
        try:
            await wait_for_blocked(harness.session_factory, blocker_pid)
            await asyncio.wait_for(service.close(), timeout=5)
        finally:
            await blocker.rollback()

    try:
        result, error = await asyncio.wait_for(preview_task, timeout=10)
    finally:
        if not preview_task.done():
            preview_task.cancel()
            await asyncio.gather(preview_task, return_exceptions=True)
    assert result is None
    assert error is not None
    # 未签发令牌：修复入口没有产生任何可用的确认凭证。
    assert await group_binding_rows(harness.engine, current) == 1
    assert await group_mapping_rows(harness.engine, current) == 1


async def test_closed_service_reference_refuses_operations_and_tokens_invalidated(
    harness: Harness,
) -> None:
    """close 后旧引用拒绝一切操作；close 使已签发令牌失效，重启亦不识别。"""
    current = make_scope("pg-close-refuse")
    await bind_member(harness.binding_manager, current, 1, name="甲")
    service = make_service(harness.session_factory, _Clock(START))
    preview = await service.preview(
        app_id=current.app_id,
        group_openid=current.group_openid,
        operator_id="tsk280-operator",
        reason=CHANGE_REASON,
    )
    token = preview.token

    await service.close()

    # 旧引用拒绝诊断/预览（服务已关闭）。
    with pytest.raises(RuntimeError):
        await service.diagnose(
            app_id=current.app_id,
            group_openid=current.group_openid,
        )
    with pytest.raises(RuntimeError):
        await service.preview(
            app_id=current.app_id,
            group_openid=current.group_openid,
            operator_id="tsk280-operator",
            reason=CHANGE_REASON,
        )
    # 旧引用拒绝确认（令牌已被 close 失效或服务已关闭）。
    with pytest.raises((RuntimeError, RepairTokenError)):
        await service.confirm(
            app_id=current.app_id,
            group_openid=current.group_openid,
            token=token,
            operator_id="tsk280-operator",
            request_id="req-close-refuse",
            reason=CHANGE_REASON,
        )
    # 重启后的新实例同样不认识旧令牌。
    restarted = make_service(harness.session_factory, _Clock(START))
    with pytest.raises(RepairTokenError):
        await restarted.confirm(
            app_id=current.app_id,
            group_openid=current.group_openid,
            token=token,
            operator_id="tsk280-operator",
            request_id="req-close-restart",
            reason=CHANGE_REASON,
        )
    assert await group_binding_rows(harness.engine, current) == 1
    assert await group_mapping_rows(harness.engine, current) == 1


# ---------------------------------------------------------------- 版本指纹 ABA


async def test_rename_rename_back_does_not_clear_with_old_token(
    harness: Harness,
) -> None:
    """指纹必须含行版本（updated_at 或等价 generation）：改名后改回原值（ABA）
    不得让旧令牌继续清除。"""
    current = make_scope("pg-aba-rename")
    await bind_member(harness.binding_manager, current, 1, name="甲")
    service = make_service(harness.session_factory, _Clock(START))
    member = current.with_member(1)
    preview = await service.preview(
        app_id=current.app_id,
        group_openid=current.group_openid,
        operator_id="tsk280-operator",
        member_openid=member.member_openid,
        reason=CHANGE_REASON,
    )

    async with harness.session_factory() as session:
        transaction = BindingTransaction(session)
        await transaction.rename(
            app_id=current.app_id,
            group_openid=current.group_openid,
            member_openid=member.member_openid,
            character_name="乙",
        )
        await session.commit()
    async with harness.session_factory() as session:
        transaction = BindingTransaction(session)
        await transaction.rename(
            app_id=current.app_id,
            group_openid=current.group_openid,
            member_openid=member.member_openid,
            character_name="甲",
        )
        await session.commit()

    # 值已回到预览时状态，但行版本已前进：旧令牌必须失效，绝不能清除。
    with pytest.raises(RepairDependencyChangedError):
        await service.confirm(
            app_id=current.app_id,
            group_openid=current.group_openid,
            token=preview.token,
            operator_id="tsk280-operator",
            request_id="req-aba-rename",
            reason=CHANGE_REASON,
        )
    assert await group_binding_rows(harness.engine, current) == 1
    assert await group_mapping_rows(harness.engine, current) == 1


async def test_delete_recreate_same_value_does_not_clear_with_old_token(
    harness: Harness,
) -> None:
    """指纹必须含行版本：删后重建同值（ABA）不得让旧令牌继续清除。"""
    current = make_scope("pg-aba-recreate")
    await bind_member(harness.binding_manager, current, 1, name="甲")
    service = make_service(harness.session_factory, _Clock(START))
    member = current.with_member(1)
    preview = await service.preview(
        app_id=current.app_id,
        group_openid=current.group_openid,
        operator_id="tsk280-operator",
        member_openid=member.member_openid,
        reason=CHANGE_REASON,
    )

    async with harness.session_factory() as session:
        await session.execute(
            text(
                """
                DELETE FROM komari_character_binding_members
                 WHERE app_id = :app_id AND group_openid = :group_openid
                   AND member_openid = :member_openid
                """
            ),
            {
                "app_id": current.app_id,
                "group_openid": current.group_openid,
                "member_openid": member.member_openid,
            },
        )
        await session.commit()
    async with harness.session_factory() as session:
        transaction = BindingTransaction(session)
        await transaction.bind(
            app_id=current.app_id,
            group_id=current.group_id,
            group_openid=current.group_openid,
            member_qq=member.member_qq,
            member_openid=member.member_openid,
            character_name="甲",
        )
        await session.commit()

    # 同值重建后行版本已变化：旧令牌必须失效，绝不能清除。
    with pytest.raises(RepairDependencyChangedError):
        await service.confirm(
            app_id=current.app_id,
            group_openid=current.group_openid,
            token=preview.token,
            operator_id="tsk280-operator",
            request_id="req-aba-recreate",
            reason=CHANGE_REASON,
        )
    assert await group_binding_rows(harness.engine, current) == 1
    assert await group_mapping_rows(harness.engine, current) == 1
