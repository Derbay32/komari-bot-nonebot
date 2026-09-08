"""TSK-277 真实 PostgreSQL：原子提交、并发、冲突、提交结果不确定与缓存发布。

只使用真实 engine/session factory 与真实 `CharacterBindingManager`；wizard 的
协调器端口按用例注入，事务与并发全部落在真实 PostgreSQL 上。
"""

from __future__ import annotations

import asyncio
import importlib
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

import pytest
from sqlalchemy import text

from komari_bot.db.group_transaction_locks import lock_group_scope
from komari_bot.plugins.character_binding import BindingTransaction
from komari_bot.plugins.character_binding.reply_evidence import ReplyEvidence
from tests.character_binding.test_reply_evidence import (
    _real_character_binding_package,
)
from tests.character_binding.tsk277_support import (
    BIND_SUCCESS,
    BINDING_CONFIRM,
    CANCELLED,
    EXISTING_BINDING,
    EXPIRED,
    GROUP_CONFLICT,
    LEGACY_CHOICE,
    MEMBER_CONFLICT,
    NAME_DUPLICATE_ERROR,
    NAME_INPUT,
    NO_CHARACTER_NAME,
    OFFICIAL_BOT_QQ,
    RENAME_BUTTON,
    RENAME_CONFIRM,
    RENAME_SUCCESS,
    UNBIND_BUTTON,
    UNBIND_CONFIRM,
    UNBIND_SUCCESS,
    FakeCoordinator,
    FrozenClock,
    buttons_of,
    freeze_qq_now,
    make_event,
    make_token,
    make_verified,
    markdown_content,
    require_wizard_contract,
)
from tests.group_admission.management_support import prepare_control_plane
from tests.group_admission.qq_admission_support import (
    QQProbeBot,
    dispatch_qq,
    event_gate_context,
    make_group_at,
)
from tests.group_admission.runtime_support import AdmissionStorageFake, stored_policy
from tests.komari_roulette.command_support import (
    PG_REQUIRED,
    backend_pid,
    create_engine_and_factory,
    wait_for_blocked,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

    from komari_bot.plugins.character_binding.manager import CharacterBindingManager

pytestmark = [pytest.mark.asyncio, PG_REQUIRED]

REPLY_EVIDENCE_MODULE = "komari_bot.plugins.character_binding.reply_evidence"
COORDINATOR_MODULE = "komari_bot.plugins.character_binding.qq_coordinator"


async def _empty_fetcher(_message_id: int) -> dict[str, object]:
    return {}


@dataclass(frozen=True, slots=True)
class Scope:
    app_id: str
    group_openid: str
    group_id: int
    member_openid: str
    member_qq: int
    session_code: str


def scope(tag: str) -> Scope:
    suffix = uuid4().hex[:10]
    return Scope(
        app_id=f"tsk277-app-{tag}-{suffix}",
        group_openid=f"tsk277-group-{tag}-{suffix}",
        group_id=277000 + (int(suffix[:4], 16) % 900),
        member_openid=f"tsk277-member-{tag}-{suffix}",
        member_qq=700000 + (int(suffix[4:8], 16) % 90000),
        session_code=f"tsk277-session-{tag}-{suffix}",
    )


def second_member(current: Scope) -> tuple[str, int, str]:
    return (
        f"{current.member_openid}-b",
        current.member_qq + 1,
        f"{current.session_code}-b",
    )


async def _cleanup(engine: AsyncEngine, current: Scope) -> None:
    params = {"app_id": current.app_id, "group_openid": current.group_openid}
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
        await connection.execute(
            text("DELETE FROM komari_character_bindings WHERE user_id = :member_qq"),
            {"member_qq": str(current.member_qq)},
        )


def _wizard(
    module: Any,
    *,
    factory: Any,
    clock: FrozenClock,
    coordinator: FakeCoordinator,
    manager: CharacterBindingManager | None,
) -> Any:
    return module.BindingWizard(
        coordinator=coordinator,
        session_factory=factory,
        clock=clock,
        manager=manager,
    )


def _binding_token(
    current: Scope,
    *,
    clock: FrozenClock,
    session_code: str | None = None,
    member_openid: str | None = None,
    member_qq: int | None = None,
    qq_message_id: str,
) -> Any:
    code = session_code or current.session_code
    verified = make_verified(
        clock=clock,
        session_code=code,
        app_id=current.app_id,
        group_openid=current.group_openid,
        member_openid=member_openid or current.member_openid,
        qq_message_id=qq_message_id,
        group_id=current.group_id,
        member_qq=member_qq or current.member_qq,
    )
    return make_token(
        scope="binding",
        app_id=current.app_id,
        group_openid=current.group_openid,
        member_openid=member_openid or current.member_openid,
        qq_message_id=qq_message_id,
        group_id=current.group_id,
        member_qq=member_qq or current.member_qq,
        verified_session=verified,
    )


def _business_token(
    current: Scope,
    *,
    member_openid: str,
    member_qq: int,
    qq_message_id: str,
) -> Any:
    return make_token(
        scope="business",
        app_id=current.app_id,
        group_openid=current.group_openid,
        member_openid=member_openid,
        qq_message_id=qq_message_id,
        group_id=current.group_id,
        member_qq=member_qq,
    )


async def _seed_binding(
    factory: async_sessionmaker[AsyncSession],
    current: Scope,
    *,
    member_openid: str,
    member_qq: int,
    name: str | None,
) -> None:
    async with factory() as session:
        transaction = BindingTransaction(session)
        await transaction.bind(
            app_id=current.app_id,
            group_id=str(current.group_id),
            group_openid=current.group_openid,
            member_qq=str(member_qq),
            member_openid=member_openid,
            character_name=name or "占位名",
        )
        if name is None:
            await transaction.clear(
                app_id=current.app_id,
                group_openid=current.group_openid,
                member_openid=member_openid,
            )
        await session.commit()


async def _member_row(
    factory: async_sessionmaker[AsyncSession],
    current: Scope,
    member_openid: str,
) -> dict[str, Any]:
    async with factory() as session:
        row = (
            (
                await session.execute(
                    text(
                        "SELECT member_qq, character_name, character_name_key, updated_at "
                        "FROM komari_character_binding_members "
                        "WHERE app_id = :app_id AND group_openid = :group_openid "
                        "AND member_openid = :member_openid"
                    ),
                    {
                        "app_id": current.app_id,
                        "group_openid": current.group_openid,
                        "member_openid": member_openid,
                    },
                )
            )
            .mappings()
            .one_or_none()
        )
    return dict(row) if row is not None else {}


async def _count_rows(
    factory: async_sessionmaker[AsyncSession],
    table: str,
    current: Scope,
) -> int:
    assert table in {
        "komari_character_binding_members",
        "komari_character_binding_groups",
    }
    async with factory() as session:
        return int(
            await session.scalar(
                text(
                    f"SELECT count(*) FROM {table} "
                    "WHERE app_id = :app_id AND group_openid = :group_openid"
                ),
                {
                    "app_id": current.app_id,
                    "group_openid": current.group_openid,
                },
            )
            or 0
        )


async def _session_code(
    wizard: Any,
    module: Any,
    current: Scope,
    *,
    member_openid: str | None = None,
) -> str:
    view = await wizard.get_session(
        module.WizardScope(
            app_id=current.app_id,
            group_openid=current.group_openid,
            member_openid=member_openid or current.member_openid,
        )
    )
    assert view is not None, "flow must expose its current session"
    return str(view.session_code)


async def _drive_to_confirm(
    wizard: Any,
    module: Any,
    current: Scope,
    *,
    token: Any,
    name: str,
    prefix: str,
) -> str:
    member_openid = token.member_openid
    reply = await wizard.handle_event(
        make_event(
            content="/bind",
            message_id=f"{prefix}-1",
            group_openid=current.group_openid,
            member_openid=member_openid,
        ),
        token,
    )
    assert reply is not None
    assert reply.body == NAME_INPUT
    session_code = await _session_code(
        wizard, module, current, member_openid=member_openid
    )
    confirm = await wizard.handle_event(
        make_event(
            content=f"/bind name {session_code} {name}",
            message_id=f"{prefix}-2",
            group_openid=current.group_openid,
            member_openid=member_openid,
        ),
        token,
    )
    assert confirm is not None
    assert confirm.body == BINDING_CONFIRM.format(name=name)
    return session_code


class _CommitHookSession:
    """在唯一 commit 边界注入提交结果不确定；其余调用委托真实 session。"""

    def __init__(self, inner: AsyncSession, hook: Callable[[AsyncSession], Awaitable[None]]) -> None:
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "_hook", hook)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def commit(self) -> None:
        await self._hook(self._inner)

    async def close(self) -> None:
        await self._inner.close()

    async def rollback(self) -> None:
        await self._inner.rollback()


def _hook_factory(
    base: async_sessionmaker[AsyncSession],
    hook: Callable[[AsyncSession], Awaitable[None]],
) -> Any:
    def factory() -> Any:
        return _CommitHookSession(base(), hook)

    return cast("Any", factory)


async def test_confirm_commits_atomically_and_publishes_cache_only_after_commit(
    binding_manager: CharacterBindingManager,
) -> None:
    """AC8：群映射+成员+名字单事务；双协议查询一致；提交后才发布缓存。"""
    module = require_wizard_contract()
    current = scope("confirm")
    clock = FrozenClock(datetime(2026, 9, 8, 12, 0, tzinfo=UTC))
    async for engine, factory in create_engine_and_factory():
        try:
            coordinator = FakeCoordinator()
            wizard = _wizard(
                module,
                factory=factory,
                clock=clock,
                coordinator=coordinator,
                manager=binding_manager,
            )
            token = _binding_token(
                current,
                clock=clock,
                qq_message_id="confirm-1",
            )
            session_code = await _drive_to_confirm(
                wizard,
                module,
                current,
                token=token,
                name="阿明",
                prefix="confirm",
            )

            reply = await wizard.handle_event(
                make_event(
                    content=f"/bind confirm {session_code}",
                    message_id="confirm-3",
                    group_openid=current.group_openid,
                    member_openid=current.member_openid,
                ),
                token,
            )

            assert reply is not None
            assert reply.body == BIND_SUCCESS.format(name="阿明")
            row = await _member_row(factory, current, current.member_openid)
            assert row["character_name"] == "阿明"
            assert row["character_name_key"] == "阿明"
            assert await _count_rows(
                factory, "komari_character_binding_groups", current
            ) == 1
            assert (
                binding_manager.get_qq_character_name(
                    app_id=current.app_id,
                    group_openid=current.group_openid,
                    member_openid=current.member_openid,
                    fallback_nickname="FALLBACK",
                )
                == "阿明"
            )
            assert (
                binding_manager.get_character_name(
                    group_id=str(current.group_id),
                    user_id=str(current.member_qq),
                    fallback_nickname="FALLBACK",
                )
                == "阿明"
            )
            assert coordinator.cancelled == [session_code]
            view = await wizard.get_session(
                module.WizardScope(
                    app_id=current.app_id,
                    group_openid=current.group_openid,
                    member_openid=current.member_openid,
                )
            )
            assert view is not None
            assert view.completed is True
            assert view.step == "completed"

            updated_before = row["updated_at"]
            repeat = await wizard.handle_event(
                make_event(
                    content=f"/bind confirm {session_code}",
                    message_id="confirm-4",
                    group_openid=current.group_openid,
                    member_openid=current.member_openid,
                ),
                token,
            )
            assert repeat is not None
            assert repeat.body == BIND_SUCCESS.format(name="阿明")
            row_after = await _member_row(factory, current, current.member_openid)
            assert row_after["updated_at"] == updated_before, "重复 confirm 不得二次写入"
            assert await _count_rows(
                factory, "komari_character_binding_members", current
            ) == 1

            existing = await wizard.handle_event(
                make_event(
                    content="/bind",
                    message_id="confirm-5",
                    group_openid=current.group_openid,
                    member_openid=current.member_openid,
                ),
                token,
            )
            assert existing is not None
            assert existing.body == EXISTING_BINDING.format(name="阿明")
            assert buttons_of(existing) == [
                (RENAME_BUTTON, "/bind rename", 2),
                (UNBIND_BUTTON, "/bind unbind", 2),
            ]
        finally:
            await _cleanup(engine, current)


async def test_group_and_member_conflicts_preserve_original_records(
    binding_manager: CharacterBindingManager,
) -> None:
    """AC8：双向群/成员 identity 冲突保留原记录，且不写名字。"""
    module = require_wizard_contract()
    current = scope("conflict")
    member_scope = scope("member-conflict")
    clock = FrozenClock(datetime(2026, 9, 8, 12, 0, tzinfo=UTC))
    async for engine, factory in create_engine_and_factory():
        try:
            async with factory() as session:
                await session.execute(
                    text(
                        "INSERT INTO komari_character_binding_groups "
                        "(app_id, group_openid, group_id) VALUES (:a, :g, :other)"
                    ),
                    {
                        "a": current.app_id,
                        "g": current.group_openid,
                        "other": str(current.group_id + 1),
                    },
                )
                await session.commit()

            coordinator = FakeCoordinator()
            wizard = _wizard(
                module,
                factory=factory,
                clock=clock,
                coordinator=coordinator,
                manager=binding_manager,
            )
            token = _binding_token(current, clock=clock, qq_message_id="conflict-1")
            conflict_session = await _drive_to_confirm(
                wizard,
                module,
                current,
                token=token,
                name="阿明",
                prefix="conflict",
            )
            reply = await wizard.handle_event(
                make_event(
                    content=f"/bind confirm {conflict_session}",
                    message_id="conflict-3",
                    group_openid=current.group_openid,
                    member_openid=current.member_openid,
                ),
                token,
            )
            assert reply is not None
            assert reply.body == GROUP_CONFLICT
            assert await _count_rows(
                factory, "komari_character_binding_members", current
            ) == 0
            async with factory() as session:
                group_id = await session.scalar(
                    text(
                        "SELECT group_id FROM komari_character_binding_groups "
                        "WHERE app_id = :a AND group_openid = :g"
                    ),
                    {"a": current.app_id, "g": current.group_openid},
                )
            assert group_id == str(current.group_id + 1)

            # 成员 identity 冲突：member_qq 已关联另一 OpenID。
            async with factory() as session:
                await session.execute(
                    text(
                        "INSERT INTO komari_character_binding_groups "
                        "(app_id, group_openid, group_id) VALUES (:a, :g, :gid)"
                    ),
                    {
                        "a": member_scope.app_id,
                        "g": member_scope.group_openid,
                        "gid": str(member_scope.group_id),
                    },
                )
                await session.execute(
                    text(
                        "INSERT INTO komari_character_binding_members "
                        "(app_id, group_openid, member_openid, member_qq, "
                        "character_name, character_name_key) "
                        "VALUES (:a, :g, :other, :qq, '旧名', '旧名')"
                    ),
                    {
                        "a": member_scope.app_id,
                        "g": member_scope.group_openid,
                        "other": f"{member_scope.member_openid}-other",
                        "qq": str(member_scope.member_qq),
                    },
                )
                await session.commit()

            member_token = _binding_token(
                member_scope,
                clock=clock,
                qq_message_id="member-conflict-1",
            )
            member_session = await _drive_to_confirm(
                wizard,
                module,
                member_scope,
                token=member_token,
                name="阿明",
                prefix="member-conflict",
            )
            member_reply = await wizard.handle_event(
                make_event(
                    content=f"/bind confirm {member_session}",
                    message_id="member-conflict-3",
                    group_openid=member_scope.group_openid,
                    member_openid=member_scope.member_openid,
                ),
                member_token,
            )
            assert member_reply is not None
            assert member_reply.body == MEMBER_CONFLICT
            original = await _member_row(
                factory, member_scope, f"{member_scope.member_openid}-other"
            )
            assert original["character_name"] == "旧名"
        finally:
            await _cleanup(engine, current)
            await _cleanup(engine, member_scope)


async def test_same_group_concurrent_name_has_one_winner_and_loser_can_rename(
    binding_manager: CharacterBindingManager,
) -> None:
    """AC8：真实 PG 并发同名仅一方成功；失败方仍可改名。"""
    module = require_wizard_contract()
    current = scope("race")
    other_openid, other_qq, other_session = second_member(current)
    clock = FrozenClock(datetime(2026, 9, 8, 12, 0, tzinfo=UTC))
    async for engine, factory in create_engine_and_factory():
        try:
            coordinator = FakeCoordinator()
            wizard = _wizard(
                module,
                factory=factory,
                clock=clock,
                coordinator=coordinator,
                manager=binding_manager,
            )
            token_a = _binding_token(current, clock=clock, qq_message_id="race-a-1")
            token_b = _binding_token(
                current,
                clock=clock,
                session_code=other_session,
                member_openid=other_openid,
                member_qq=other_qq,
                qq_message_id="race-b-1",
            )
            session_a = await _drive_to_confirm(
                wizard,
                module,
                current,
                token=token_a,
                name="同名",
                prefix="race-a",
            )
            session_b = await _drive_to_confirm(
                wizard,
                module,
                current,
                token=token_b,
                name="同名",
                prefix="race-b",
            )

            blocker = factory()
            await lock_group_scope(
                blocker,
                app_id=current.app_id,
                group_openid=current.group_openid,
            )
            blocker_pid = await backend_pid(blocker)
            task_a = asyncio.create_task(
                wizard.handle_event(
                    make_event(
                        content=f"/bind confirm {session_a}",
                        message_id="race-a-3",
                        group_openid=current.group_openid,
                        member_openid=current.member_openid,
                    ),
                    token_a,
                )
            )
            task_b = asyncio.create_task(
                wizard.handle_event(
                    make_event(
                        content=f"/bind confirm {session_b}",
                        message_id="race-b-3",
                        group_openid=current.group_openid,
                        member_openid=other_openid,
                    ),
                    token_b,
                )
            )
            await wait_for_blocked(factory, blocker_pid)
            await blocker.rollback()
            await blocker.close()
            reply_a, reply_b = await asyncio.gather(task_a, task_b)

            bodies = {reply_a.body if reply_a else None, reply_b.body if reply_b else None}
            assert bodies == {BIND_SUCCESS.format(name="同名"), NAME_DUPLICATE_ERROR}
            assert (
                await _count_rows(
                    factory, "komari_character_binding_members", current
                )
                == 1
            )

            loser_token = token_a if reply_a and reply_a.body == NAME_DUPLICATE_ERROR else token_b
            loser_openid = (
                current.member_openid
                if loser_token is token_a
                else other_openid
            )
            loser_session = session_a if loser_token is token_a else session_b
            renamed = await wizard.handle_event(
                make_event(
                    content=f"/bind name {loser_session} 另一个名",
                    message_id="race-loser-4",
                    group_openid=current.group_openid,
                    member_openid=loser_openid,
                ),
                loser_token,
            )
            assert renamed is not None
            assert renamed.body == BINDING_CONFIRM.format(name="另一个名")
            confirmed = await wizard.handle_event(
                make_event(
                    content=f"/bind confirm {loser_session}",
                    message_id="race-loser-5",
                    group_openid=current.group_openid,
                    member_openid=loser_openid,
                ),
                loser_token,
            )
            assert confirmed is not None
            assert confirmed.body == BIND_SUCCESS.format(name="另一个名")
            assert (
                await _count_rows(
                    factory, "komari_character_binding_members", current
                )
                == 2
            )
        finally:
            await _cleanup(engine, current)


async def test_rename_preview_confirm_conflict_and_cancel(
    binding_manager: CharacterBindingManager,
) -> None:
    """AC6/AC11：rename preview→confirm，冲突/取消保留原名，旧名释放。"""
    module = require_wizard_contract()
    current = scope("rename")
    other_openid, other_qq, _ = second_member(current)
    clock = FrozenClock(datetime(2026, 9, 8, 12, 0, tzinfo=UTC))
    async for engine, factory in create_engine_and_factory():
        try:
            await _seed_binding(
                factory,
                current,
                member_openid=current.member_openid,
                member_qq=current.member_qq,
                name="阿明",
            )
            await _seed_binding(
                factory,
                current,
                member_openid=other_openid,
                member_qq=other_qq,
                name="小明",
            )
            coordinator = FakeCoordinator()
            wizard = _wizard(
                module,
                factory=factory,
                clock=clock,
                coordinator=coordinator,
                manager=binding_manager,
            )
            token = _binding_token(current, clock=clock, qq_message_id="rename-1")
            existing = await wizard.handle_event(
                make_event(
                    content="/bind",
                    message_id="rename-1",
                    group_openid=current.group_openid,
                    member_openid=current.member_openid,
                ),
                token,
            )
            assert existing is not None
            assert existing.body == EXISTING_BINDING.format(name="阿明")
            assert coordinator.claim_calls == []
            assert coordinator.session_resolver_calls == []

            rename = await wizard.handle_event(
                make_event(
                    content="/bind rename",
                    message_id="rename-2",
                    group_openid=current.group_openid,
                    member_openid=current.member_openid,
                ),
                token,
            )
            assert rename is not None
            assert rename.body == NAME_INPUT
            view = await wizard.get_session(
                module.WizardScope(
                    app_id=current.app_id,
                    group_openid=current.group_openid,
                    member_openid=current.member_openid,
                )
            )
            assert view is not None
            assert view.operation == "rename"
            assert view.step == "name_input"
            assert coordinator.claim_calls == []
            rename_session = str(view.session_code)

            preview = await wizard.handle_event(
                make_event(
                    content=f"/bind name {rename_session} 小明",
                    message_id="rename-3",
                    group_openid=current.group_openid,
                    member_openid=current.member_openid,
                ),
                token,
            )
            assert preview is not None
            assert preview.body == RENAME_CONFIRM.format(old="阿明", new="小明")
            conflict = await wizard.handle_event(
                make_event(
                    content=f"/bind confirm {rename_session}",
                    message_id="rename-4",
                    group_openid=current.group_openid,
                    member_openid=current.member_openid,
                ),
                token,
            )
            assert conflict is not None
            assert conflict.body == NAME_DUPLICATE_ERROR
            assert (
                await _member_row(factory, current, current.member_openid)
            )["character_name"] == "阿明"

            cancel = await wizard.handle_event(
                make_event(
                    content=f"/bind cancel {rename_session}",
                    message_id="rename-5",
                    group_openid=current.group_openid,
                    member_openid=current.member_openid,
                ),
                token,
            )
            assert cancel is not None
            assert cancel.body == CANCELLED
            assert (
                await _member_row(factory, current, current.member_openid)
            )["character_name"] == "阿明"

            await wizard.handle_event(
                make_event(
                    content="/bind rename",
                    message_id="rename-6",
                    group_openid=current.group_openid,
                    member_openid=current.member_openid,
                ),
                token,
            )
            second_session = await _session_code(wizard, module, current)
            await wizard.handle_event(
                make_event(
                    content=f"/bind name {second_session} 小暗",
                    message_id="rename-7",
                    group_openid=current.group_openid,
                    member_openid=current.member_openid,
                ),
                token,
            )
            success = await wizard.handle_event(
                make_event(
                    content=f"/bind confirm {second_session}",
                    message_id="rename-8",
                    group_openid=current.group_openid,
                    member_openid=current.member_openid,
                ),
                token,
            )
            assert success is not None
            assert success.body == RENAME_SUCCESS.format(name="小暗")
            assert (
                await _member_row(factory, current, current.member_openid)
            )["character_name"] == "小暗"
            assert (
                binding_manager.get_qq_character_name(
                    app_id=current.app_id,
                    group_openid=current.group_openid,
                    member_openid=current.member_openid,
                )
                == "小暗"
            )
        finally:
            await _cleanup(engine, current)


async def test_rename_or_unbind_without_character_name_prompts_binding(
    binding_manager: CharacterBindingManager,
) -> None:
    """AC6：无角色名时 rename/unbind 不写入并给出定稿提示。"""
    module = require_wizard_contract()
    current = scope("noname")
    clock = FrozenClock(datetime(2026, 9, 8, 12, 0, tzinfo=UTC))
    async for engine, factory in create_engine_and_factory():
        try:
            await _seed_binding(
                factory,
                current,
                member_openid=current.member_openid,
                member_qq=current.member_qq,
                name=None,
            )
            coordinator = FakeCoordinator()
            wizard = _wizard(
                module,
                factory=factory,
                clock=clock,
                coordinator=coordinator,
                manager=binding_manager,
            )
            token = _binding_token(current, clock=clock, qq_message_id="noname-1")
            for index, command in enumerate(("/bind rename", "/bind unbind")):
                reply = await wizard.handle_event(
                    make_event(
                        content=command,
                        message_id=f"noname-{index + 1}",
                        group_openid=current.group_openid,
                        member_openid=current.member_openid,
                    ),
                    token,
                )
                assert reply is not None
                assert reply.body == NO_CHARACTER_NAME
            row = await _member_row(factory, current, current.member_openid)
            assert row["character_name"] is None
        finally:
            await _cleanup(engine, current)


async def test_unbind_clears_only_name_and_identity_stays_reusable(
    binding_manager: CharacterBindingManager,
) -> None:
    """AC6：解绑只清名字，保留 group/member identity；可复用同一身份重新绑定。"""
    module = require_wizard_contract()
    current = scope("unbind")
    clock = FrozenClock(datetime(2026, 9, 8, 12, 0, tzinfo=UTC))
    async for engine, factory in create_engine_and_factory():
        try:
            await _seed_binding(
                factory,
                current,
                member_openid=current.member_openid,
                member_qq=current.member_qq,
                name="阿明",
            )
            coordinator = FakeCoordinator()
            wizard = _wizard(
                module,
                factory=factory,
                clock=clock,
                coordinator=coordinator,
                manager=binding_manager,
            )
            token = _binding_token(current, clock=clock, qq_message_id="unbind-1")
            preview = await wizard.handle_event(
                make_event(
                    content="/bind unbind",
                    message_id="unbind-1",
                    group_openid=current.group_openid,
                    member_openid=current.member_openid,
                ),
                token,
            )
            assert preview is not None
            assert preview.body == UNBIND_CONFIRM.format(name="阿明")
            view = await wizard.get_session(
                module.WizardScope(
                    app_id=current.app_id,
                    group_openid=current.group_openid,
                    member_openid=current.member_openid,
                )
            )
            assert view is not None
            assert view.operation == "unbind"
            assert view.step == "unbind_confirm"
            unbind_session = str(view.session_code)

            success = await wizard.handle_event(
                make_event(
                    content=f"/bind confirm {unbind_session}",
                    message_id="unbind-2",
                    group_openid=current.group_openid,
                    member_openid=current.member_openid,
                ),
                token,
            )
            assert success is not None
            assert success.body == UNBIND_SUCCESS
            row = await _member_row(factory, current, current.member_openid)
            assert row["character_name"] is None
            assert str(row["member_qq"]) == str(current.member_qq)
            assert await _count_rows(
                factory, "komari_character_binding_groups", current
            ) == 1
            assert (
                binding_manager.get_qq_character_name(
                    app_id=current.app_id,
                    group_openid=current.group_openid,
                    member_openid=current.member_openid,
                    fallback_nickname="FALLBACK",
                )
                == "FALLBACK"
            )

            business = _business_token(
                current,
                member_openid=current.member_openid,
                member_qq=current.member_qq,
                qq_message_id="unbind-3",
            )
            rebind = await wizard.handle_event(
                make_event(
                    content="/bind",
                    message_id="unbind-3",
                    group_openid=current.group_openid,
                    member_openid=current.member_openid,
                ),
                business,
            )
            assert rebind is not None
            assert rebind.body == NAME_INPUT
            assert coordinator.claim_calls == [], "已有身份关联不得重新取证"
            rebind_session = await _session_code(wizard, module, current)
            await wizard.handle_event(
                make_event(
                    content=f"/bind name {rebind_session} 新名",
                    message_id="unbind-4",
                    group_openid=current.group_openid,
                    member_openid=current.member_openid,
                ),
                business,
            )
            rebound = await wizard.handle_event(
                make_event(
                    content=f"/bind confirm {rebind_session}",
                    message_id="unbind-5",
                    group_openid=current.group_openid,
                    member_openid=current.member_openid,
                ),
                business,
            )
            assert rebound is not None
            assert rebound.body == BIND_SUCCESS.format(name="新名")
            assert (
                await _count_rows(
                    factory, "komari_character_binding_members", current
                )
                == 1
            )
        finally:
            await _cleanup(engine, current)


async def test_legacy_candidate_reuse_and_conflict(
    binding_manager: CharacterBindingManager,
) -> None:
    """AC7：旧名只在主动 /bind + 已验证身份 + 当前群未绑定时候选；reuse 重新校验。"""
    module = require_wizard_contract()
    current = scope("legacy")
    conflict_scope = scope("legacy-conflict")
    other_openid, other_qq, _ = second_member(current)
    clock = FrozenClock(datetime(2026, 9, 8, 12, 0, tzinfo=UTC))
    async for engine, factory in create_engine_and_factory():
        try:
            async with factory() as session:
                await session.execute(
                    text(
                        "INSERT INTO komari_character_bindings "
                        "(user_id, character_name) VALUES (:user_id, '小明')"
                    ),
                    {"user_id": str(current.member_qq)},
                )
                await session.commit()

            coordinator = FakeCoordinator()
            wizard = _wizard(
                module,
                factory=factory,
                clock=clock,
                coordinator=coordinator,
                manager=binding_manager,
            )
            assert (
                binding_manager.get_character_name(
                    group_id=str(current.group_id),
                    user_id=str(current.member_qq),
                    fallback_nickname="FALLBACK",
                )
                == "FALLBACK"
            )
            token = _binding_token(current, clock=clock, qq_message_id="legacy-1")
            candidate = await wizard.handle_event(
                make_event(
                    content="/bind",
                    message_id="legacy-1",
                    group_openid=current.group_openid,
                    member_openid=current.member_openid,
                ),
                token,
            )
            assert candidate is not None
            assert candidate.body == LEGACY_CHOICE.format(name="小明")
            legacy_session = await _session_code(wizard, module, current)
            reuse = await wizard.handle_event(
                make_event(
                    content=f"/bind reuse {legacy_session}",
                    message_id="legacy-2",
                    group_openid=current.group_openid,
                    member_openid=current.member_openid,
                ),
                token,
            )
            assert reuse is not None
            assert reuse.body == BINDING_CONFIRM.format(name="小明")
            committed = await wizard.handle_event(
                make_event(
                    content=f"/bind confirm {legacy_session}",
                    message_id="legacy-3",
                    group_openid=current.group_openid,
                    member_openid=current.member_openid,
                ),
                token,
            )
            assert committed is not None
            assert committed.body == BIND_SUCCESS.format(name="小明")
            async with factory() as session:
                legacy = await session.scalar(
                    text(
                        "SELECT character_name FROM komari_character_bindings "
                        "WHERE user_id = :user_id"
                    ),
                    {"user_id": str(current.member_qq)},
                )
            assert legacy == "小明", "迁移不得改写旧全局表"

            # 旧名与群内现有名字冲突：reuse 必须在最终提交重新校验。
            await _seed_binding(
                factory,
                conflict_scope,
                member_openid=other_openid,
                member_qq=other_qq,
                name="小明",
            )
            async with factory() as session:
                await session.execute(
                    text(
                        "INSERT INTO komari_character_bindings "
                        "(user_id, character_name) VALUES (:user_id, '小明')"
                    ),
                    {"user_id": str(conflict_scope.member_qq)},
                )
                await session.commit()
            conflict_token = _binding_token(
                conflict_scope,
                clock=clock,
                qq_message_id="legacy-conflict-1",
            )
            conflict_candidate = await wizard.handle_event(
                make_event(
                    content="/bind",
                    message_id="legacy-conflict-1",
                    group_openid=conflict_scope.group_openid,
                    member_openid=conflict_scope.member_openid,
                ),
                conflict_token,
            )
            assert conflict_candidate is not None
            assert conflict_candidate.body == LEGACY_CHOICE.format(name="小明")
            conflict_session = await _session_code(
                wizard, module, conflict_scope
            )
            await wizard.handle_event(
                make_event(
                    content=f"/bind reuse {conflict_session}",
                    message_id="legacy-conflict-2",
                    group_openid=conflict_scope.group_openid,
                    member_openid=conflict_scope.member_openid,
                ),
                conflict_token,
            )
            conflict = await wizard.handle_event(
                make_event(
                    content=f"/bind confirm {conflict_session}",
                    message_id="legacy-conflict-3",
                    group_openid=conflict_scope.group_openid,
                    member_openid=conflict_scope.member_openid,
                ),
                conflict_token,
            )
            assert conflict is not None
            assert conflict.body == NAME_DUPLICATE_ERROR
            assert (
                await _member_row(
                    factory, conflict_scope, conflict_scope.member_openid
                )
            ) == {}
        finally:
            await _cleanup(engine, current)
            await _cleanup(engine, conflict_scope)
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "DELETE FROM komari_character_bindings "
                        "WHERE user_id = :user_id"
                    ),
                    {"user_id": str(other_qq)},
                )


async def test_commit_outcome_unknown_after_commit_does_not_report_and_converges(
    binding_manager: CharacterBindingManager,
) -> None:
    """AC8/AC10：已提交但响应丢失不虚报；重复 confirm 依据 canonical 记录收敛。"""
    module = require_wizard_contract()
    current = scope("unknown-after")
    clock = FrozenClock(datetime(2026, 9, 8, 12, 0, tzinfo=UTC))
    failed = {"once": False}
    async for engine, factory in create_engine_and_factory():
        try:
            coordinator = FakeCoordinator()

            async def commit_then_lose(inner: AsyncSession) -> None:
                await inner.commit()
                if not failed["once"]:
                    failed["once"] = True
                    raise RuntimeError("commit outcome unknown")  # noqa: TRY003

            wizard = _wizard(
                module,
                factory=_hook_factory(factory, commit_then_lose),
                clock=clock,
                coordinator=coordinator,
                manager=binding_manager,
            )
            token = _binding_token(current, clock=clock, qq_message_id="unknown-1")
            session_code = await _drive_to_confirm(
                wizard,
                module,
                current,
                token=token,
                name="阿明",
                prefix="unknown",
            )

            with pytest.raises(module.BindingCommitOutcomeUnknownError):
                await wizard.handle_event(
                    make_event(
                        content=f"/bind confirm {session_code}",
                        message_id="unknown-3",
                        group_openid=current.group_openid,
                        member_openid=current.member_openid,
                    ),
                    token,
                )

            row = await _member_row(factory, current, current.member_openid)
            assert row["character_name"] == "阿明", "提交已发生"
            assert (
                binding_manager.get_qq_character_name(
                    app_id=current.app_id,
                    group_openid=current.group_openid,
                    member_openid=current.member_openid,
                    fallback_nickname="FALLBACK",
                )
                == "FALLBACK"
            ), "无法确认提交时不得发布缓存"
            updated_before = row["updated_at"]

            repeat = await wizard.handle_event(
                make_event(
                    content=f"/bind confirm {session_code}",
                    message_id="unknown-4",
                    group_openid=current.group_openid,
                    member_openid=current.member_openid,
                ),
                token,
            )
            assert repeat is not None
            assert repeat.body == BIND_SUCCESS.format(name="阿明")
            row_after = await _member_row(factory, current, current.member_openid)
            assert row_after["updated_at"] == updated_before, "收敛不得改写已提交记录"
            assert (
                binding_manager.get_qq_character_name(
                    app_id=current.app_id,
                    group_openid=current.group_openid,
                    member_openid=current.member_openid,
                )
                == "阿明"
            )
        finally:
            await _cleanup(engine, current)


async def test_commit_outcome_unknown_before_commit_leaves_no_record_and_repeat_writes_once(
    binding_manager: CharacterBindingManager,
) -> None:
    """AC8：未提交的不确定结果不虚报；重复 confirm 只补写一次。"""
    module = require_wizard_contract()
    current = scope("unknown-before")
    clock = FrozenClock(datetime(2026, 9, 8, 12, 0, tzinfo=UTC))
    failed = {"once": False}
    async for engine, factory in create_engine_and_factory():
        try:
            coordinator = FakeCoordinator()

            async def lose_before_commit(inner: AsyncSession) -> None:
                if not failed["once"]:
                    failed["once"] = True
                    raise RuntimeError("commit outcome unknown")  # noqa: TRY003
                await inner.commit()

            wizard = _wizard(
                module,
                factory=_hook_factory(factory, lose_before_commit),
                clock=clock,
                coordinator=coordinator,
                manager=binding_manager,
            )
            token = _binding_token(current, clock=clock, qq_message_id="before-1")
            session_code = await _drive_to_confirm(
                wizard,
                module,
                current,
                token=token,
                name="阿明",
                prefix="before",
            )

            with pytest.raises(module.BindingCommitOutcomeUnknownError):
                await wizard.handle_event(
                    make_event(
                        content=f"/bind confirm {session_code}",
                        message_id="before-3",
                        group_openid=current.group_openid,
                        member_openid=current.member_openid,
                    ),
                    token,
                )

            assert (
                await _count_rows(
                    factory, "komari_character_binding_members", current
                )
                == 0
            )
            assert (
                binding_manager.get_qq_character_name(
                    app_id=current.app_id,
                    group_openid=current.group_openid,
                    member_openid=current.member_openid,
                    fallback_nickname="FALLBACK",
                )
                == "FALLBACK"
            )

            repeat = await wizard.handle_event(
                make_event(
                    content=f"/bind confirm {session_code}",
                    message_id="before-4",
                    group_openid=current.group_openid,
                    member_openid=current.member_openid,
                ),
                token,
            )
            assert repeat is not None
            assert repeat.body == BIND_SUCCESS.format(name="阿明")
            assert (
                await _count_rows(
                    factory, "komari_character_binding_members", current
                )
                == 1
            )
            assert (
                binding_manager.get_qq_character_name(
                    app_id=current.app_id,
                    group_openid=current.group_openid,
                    member_openid=current.member_openid,
                )
                == "阿明"
            )
        finally:
            await _cleanup(engine, current)


async def test_commit_unknown_after_commit_never_overwrites_later_maintenance_rename(
    binding_manager: CharacterBindingManager,
) -> None:
    """AC8：after-commit 不确定后另一合法维护流程改名，重复旧 confirm 绝不覆写。"""
    module = require_wizard_contract()
    current = scope("unknown-overwrite")
    clock = FrozenClock(datetime(2026, 9, 8, 12, 0, tzinfo=UTC))
    failed = {"once": False}

    async def commit_then_lose(inner: AsyncSession) -> None:
        await inner.commit()
        if not failed["once"]:
            failed["once"] = True
            raise RuntimeError("commit outcome unknown")  # noqa: TRY003

    async for engine, factory in create_engine_and_factory():
        try:
            coordinator = FakeCoordinator()
            wizard = _wizard(
                module,
                factory=_hook_factory(factory, commit_then_lose),
                clock=clock,
                coordinator=coordinator,
                manager=binding_manager,
            )
            token = _binding_token(current, clock=clock, qq_message_id="ow-1")
            session_code = await _drive_to_confirm(
                wizard,
                module,
                current,
                token=token,
                name="阿明",
                prefix="ow",
            )
            with pytest.raises(module.BindingCommitOutcomeUnknownError):
                await wizard.handle_event(
                    make_event(
                        content=f"/bind confirm {session_code}",
                        message_id="ow-3",
                        group_openid=current.group_openid,
                        member_openid=current.member_openid,
                    ),
                    token,
                )
            assert (
                await _member_row(factory, current, current.member_openid)
            )["character_name"] == "阿明"

            # 另一合法维护流程（共享 271 facade）把同一成员改名为“小暗”。
            async with factory() as session:
                await BindingTransaction(session).rename(
                    app_id=current.app_id,
                    group_openid=current.group_openid,
                    member_openid=current.member_openid,
                    character_name="小暗",
                )
                await session.commit()

            repeat = await wizard.handle_event(
                make_event(
                    content=f"/bind confirm {session_code}",
                    message_id="ow-4",
                    group_openid=current.group_openid,
                    member_openid=current.member_openid,
                ),
                token,
            )

            assert repeat is not None
            assert repeat.body == EXPIRED, "不确定提交后的旧 confirm 不得虚报成功"
            assert (
                await _member_row(factory, current, current.member_openid)
            )["character_name"] == "小暗", "绝不覆写后续合法正式值"
            assert (
                binding_manager.get_qq_character_name(
                    app_id=current.app_id,
                    group_openid=current.group_openid,
                    member_openid=current.member_openid,
                    fallback_nickname="FALLBACK",
                )
                != "阿明"
            )
        finally:
            await _cleanup(engine, current)


async def test_commit_unknown_before_commit_never_overwrites_existing_new_formal_state(
    binding_manager: CharacterBindingManager,
) -> None:
    """AC8：before-commit 不确定后已有新正式状态，重复旧 confirm 不得盲目覆盖。"""
    module = require_wizard_contract()
    current = scope("unknown-before-overwrite")
    clock = FrozenClock(datetime(2026, 9, 8, 12, 0, tzinfo=UTC))
    failed = {"once": False}

    async def lose_before_commit(inner: AsyncSession) -> None:
        if not failed["once"]:
            failed["once"] = True
            raise RuntimeError("commit outcome unknown")  # noqa: TRY003
        await inner.commit()

    async for engine, factory in create_engine_and_factory():
        try:
            coordinator = FakeCoordinator()
            wizard = _wizard(
                module,
                factory=_hook_factory(factory, lose_before_commit),
                clock=clock,
                coordinator=coordinator,
                manager=binding_manager,
            )
            token = _binding_token(current, clock=clock, qq_message_id="bo-1")
            session_code = await _drive_to_confirm(
                wizard,
                module,
                current,
                token=token,
                name="阿明",
                prefix="bo",
            )
            with pytest.raises(module.BindingCommitOutcomeUnknownError):
                await wizard.handle_event(
                    make_event(
                        content=f"/bind confirm {session_code}",
                        message_id="bo-3",
                        group_openid=current.group_openid,
                        member_openid=current.member_openid,
                    ),
                    token,
                )
            assert (
                await _count_rows(
                    factory, "komari_character_binding_members", current
                )
                == 0
            )

            # 另一合法维护流程为同一成员建立正式绑定“小暗”。
            await _seed_binding(
                factory,
                current,
                member_openid=current.member_openid,
                member_qq=current.member_qq,
                name="小暗",
            )

            repeat = await wizard.handle_event(
                make_event(
                    content=f"/bind confirm {session_code}",
                    message_id="bo-4",
                    group_openid=current.group_openid,
                    member_openid=current.member_openid,
                ),
                token,
            )

            assert repeat is not None
            assert repeat.body == EXPIRED, "不确定提交后的旧 confirm 不得虚报成功"
            row = await _member_row(factory, current, current.member_openid)
            assert row["character_name"] == "小暗", "绝不盲目覆盖已有新正式状态"
            assert (
                await _count_rows(
                    factory, "komari_character_binding_members", current
                )
                == 1
            )
        finally:
            await _cleanup(engine, current)


@pytest.mark.group_admission_acceptance
async def test_real_handler_evidence_progression_and_success_send_survive_cancel(
    binding_manager: CharacterBindingManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC4/AC9/AC10：真实 binding token 链证据推进→成功发送不被 coordinator.cancel 破坏。"""
    current = scope("chain")
    clock = FrozenClock(datetime(2026, 9, 8, 12, 0, tzinfo=UTC))
    storage = AdmissionStorageFake(
        stored_policy(1, {"mode": "blacklist", "group_ids": []})
    )
    await prepare_control_plane(monkeypatch, storage)

    async def resolve_group(_app_id: str, _group_openid: str) -> int | None:
        return None

    async def never_banned(_member_qq: int, _scope: str) -> bool:
        return False

    async for engine, factory in create_engine_and_factory():
        try:
            async with event_gate_context():
                with _real_character_binding_package():
                    module = require_wizard_contract()
                    reply_evidence = importlib.import_module(REPLY_EVIDENCE_MODULE)
                    coordinator_module = importlib.import_module(COORDINATOR_MODULE)
                    freeze_qq_now(monkeypatch, clock)
                    collector = reply_evidence.ReplyEvidenceCollector(
                        app_id=current.app_id,
                        official_bot_qq=OFFICIAL_BOT_QQ,
                        message_fetcher=_empty_fetcher,
                        clock=clock,
                    )
                    coordinator = coordinator_module.QQBindingCoordinator(
                        collectors=(collector,),
                        group_resolver=resolve_group,
                        ban_checker=never_banned,
                        clock=clock,
                    )
                    await coordinator.start()
                    wizard = module.BindingWizard(
                        coordinator=coordinator,
                        session_factory=factory,
                        clock=clock,
                        manager=binding_manager,
                    )
                    module.set_binding_wizard(wizard)
                    try:
                        bot = QQProbeBot(current.app_id)
                        await dispatch_qq(
                            bot,
                            make_group_at(
                                content="/bind",
                                message_id="chain-1",
                                group_openid=current.group_openid,
                                member_openid=current.member_openid,
                            ),
                        )
                        assert len(bot.calls) == 1
                        challenge = markdown_content(bot.calls[0][1])
                        assert challenge.startswith("正在确认你的本群身份。\n会话码：")
                        session_code = challenge.split("会话码：", 1)[1].split("\n", 1)[0]

                        evidence = ReplyEvidence(
                            app_id=current.app_id,
                            session_code=session_code,
                            group_openid=current.group_openid,
                            member_openid=current.member_openid,
                            group_id=str(current.group_id),
                            member_qq=str(current.member_qq),
                            original_command="/bind",
                            qq_message_id="chain-1",
                            onebot_original_message_id=91001,
                            challenge_message_id=91002,
                            connection_generation=0,
                        )
                        token = await coordinator.accept_reply_evidence(evidence)
                        assert token is not None
                        assert token.scope == "binding"
                        assert len(bot.calls) == 1, "证据到达不得自动追加 QQ 消息"

                        await dispatch_qq(
                            bot,
                            make_group_at(
                                content="/bind",
                                message_id="chain-2",
                                group_openid=current.group_openid,
                                member_openid=current.member_openid,
                            ),
                        )
                        assert len(bot.calls) == 2
                        assert markdown_content(bot.calls[1][1]) == NAME_INPUT

                        await dispatch_qq(
                            bot,
                            make_group_at(
                                content=f"/bind name {session_code} 阿明",
                                message_id="chain-3",
                                group_openid=current.group_openid,
                                member_openid=current.member_openid,
                            ),
                        )
                        assert len(bot.calls) == 3
                        assert markdown_content(bot.calls[2][1]) == BINDING_CONFIRM.format(
                            name="阿明"
                        )

                        await dispatch_qq(
                            bot,
                            make_group_at(
                                content=f"/bind confirm {session_code}",
                                message_id="chain-4",
                                group_openid=current.group_openid,
                                member_openid=current.member_openid,
                            ),
                        )
                        assert len(bot.calls) == 4, (
                            f"提交成功后成功文案必须实际发送: {bot.calls}"
                        )
                        assert markdown_content(bot.calls[3][1]) == BIND_SUCCESS.format(
                            name="阿明"
                        )
                        assert (
                            await _member_row(factory, current, current.member_openid)
                        )["character_name"] == "阿明"
                    finally:
                        module.set_binding_wizard(None)
                        await coordinator.close()
                        reply_evidence.set_runtime_collectors(())
        finally:
            await _cleanup(engine, current)


async def _wait_for_draft_name(
    wizard: Any,
    module: Any,
    current: Scope,
    expected: str,
) -> None:
    """等待同 scope 草稿变为 expected；串行实现下会超时，由调用方吞掉。"""
    async with asyncio.timeout(1.0):
        while True:
            view = await wizard.get_session(
                module.WizardScope(
                    app_id=current.app_id,
                    group_openid=current.group_openid,
                    member_openid=current.member_openid,
                )
            )
            if view is not None and view.character_name == expected:
                return
            await asyncio.sleep(0.01)


async def test_authorize_send_denies_completed_reply_after_recheck_revocation(
    binding_manager: CharacterBindingManager,
) -> None:
    """AC9：成功回复生成后撤销准入/封禁，authorize_send 必须拒绝，即使 canonical 一致。"""
    module = require_wizard_contract()
    current = scope("authz-revoked")
    clock = FrozenClock(datetime(2026, 9, 8, 12, 0, tzinfo=UTC))
    async for engine, factory in create_engine_and_factory():
        try:
            coordinator = FakeCoordinator()
            wizard = _wizard(
                module,
                factory=factory,
                clock=clock,
                coordinator=coordinator,
                manager=binding_manager,
            )
            token = _binding_token(current, clock=clock, qq_message_id="authz-1")
            session_code = await _drive_to_confirm(
                wizard,
                module,
                current,
                token=token,
                name="阿明",
                prefix="authz",
            )
            success = await wizard.handle_event(
                make_event(
                    content=f"/bind confirm {session_code}",
                    message_id="authz-3",
                    group_openid=current.group_openid,
                    member_openid=current.member_openid,
                ),
                token,
            )
            assert success is not None
            assert success.body == BIND_SUCCESS.format(name="阿明")
            assert (
                await _member_row(factory, current, current.member_openid)
            )["character_name"] == "阿明"

            # 阳性：准入仍允许时，已完成回复可以发送。
            assert await wizard.authorize_send(token, success) is True

            # 撤销准入/封禁后：canonical 仍等于目标，也不得放行。
            coordinator.recheck_allowed = False
            assert await wizard.authorize_send(token, success) is False, (
                "completed canonical 一致不得绕过发送前重审"
            )
        finally:
            await _cleanup(engine, current)


async def test_confirm_rechecks_after_group_lock_and_writes_nothing_when_revoked(
    binding_manager: CharacterBindingManager,
) -> None:
    """AC9：confirm 等待组锁期间撤销准入，获锁后必须重审且零写入。"""
    module = require_wizard_contract()
    current = scope("lock-recheck")
    clock = FrozenClock(datetime(2026, 9, 8, 12, 0, tzinfo=UTC))
    async for engine, factory in create_engine_and_factory():
        try:
            coordinator = FakeCoordinator()
            wizard = _wizard(
                module,
                factory=factory,
                clock=clock,
                coordinator=coordinator,
                manager=binding_manager,
            )
            token = _binding_token(current, clock=clock, qq_message_id="lock-1")
            session_code = await _drive_to_confirm(
                wizard,
                module,
                current,
                token=token,
                name="阿明",
                prefix="lock",
            )

            blocker = factory()
            await lock_group_scope(
                blocker,
                app_id=current.app_id,
                group_openid=current.group_openid,
            )
            blocker_pid = await backend_pid(blocker)
            task = asyncio.create_task(
                wizard.handle_event(
                    make_event(
                        content=f"/bind confirm {session_code}",
                        message_id="lock-3",
                        group_openid=current.group_openid,
                        member_openid=current.member_openid,
                    ),
                    token,
                )
            )
            await wait_for_blocked(factory, blocker_pid)

            # 确认已停在组锁等待上；此刻撤销准入，再放锁。
            coordinator.recheck_allowed = False
            await blocker.rollback()
            await blocker.close()
            result = await task

            assert result is None, "获锁后重审失败必须静默"
            assert (
                await _count_rows(
                    factory, "komari_character_binding_members", current
                )
                == 0
            ), "获锁后重审失败不得写入成员/名字"
            assert (
                await _count_rows(
                    factory, "komari_character_binding_groups", current
                )
                == 0
            ), "获锁后重审失败不得写入群映射"
        finally:
            await _cleanup(engine, current)


async def test_concurrent_name_change_during_confirm_never_writes_unconfirmed_name(
    binding_manager: CharacterBindingManager,
) -> None:
    """AC8/AC10：同 scope confirm 等待期间改草稿，不得写入未确认的新名。"""
    module = require_wizard_contract()
    current = scope("draft-race")
    clock = FrozenClock(datetime(2026, 9, 8, 12, 0, tzinfo=UTC))
    async for engine, factory in create_engine_and_factory():
        try:
            coordinator = FakeCoordinator()
            wizard = _wizard(
                module,
                factory=factory,
                clock=clock,
                coordinator=coordinator,
                manager=binding_manager,
            )
            token = _binding_token(current, clock=clock, qq_message_id="draft-1")
            session_code = await _drive_to_confirm(
                wizard,
                module,
                current,
                token=token,
                name="阿明",
                prefix="draft",
            )

            blocker = factory()
            await lock_group_scope(
                blocker,
                app_id=current.app_id,
                group_openid=current.group_openid,
            )
            blocker_pid = await backend_pid(blocker)
            confirm_task = asyncio.create_task(
                wizard.handle_event(
                    make_event(
                        content=f"/bind confirm {session_code}",
                        message_id="draft-3",
                        group_openid=current.group_openid,
                        member_openid=current.member_openid,
                    ),
                    token,
                )
            )
            await wait_for_blocked(factory, blocker_pid)

            name_task = asyncio.create_task(
                wizard.handle_event(
                    make_event(
                        content=f"/bind name {session_code} 小暗",
                        message_id="draft-4",
                        group_openid=current.group_openid,
                        member_openid=current.member_openid,
                    ),
                    token,
                )
            )
            # 无串行/快照时草稿会被改成“小暗”；合理实现下这里会超时。
            with suppress(TimeoutError):
                await _wait_for_draft_name(wizard, module, current, "小暗")

            await blocker.rollback()
            await blocker.close()
            await asyncio.gather(confirm_task, name_task, return_exceptions=True)

            row = await _member_row(factory, current, current.member_openid)
            assert row.get("character_name") == "阿明", (
                f"不得写入未确认的新名: {row}"
            )
        finally:
            await _cleanup(engine, current)
