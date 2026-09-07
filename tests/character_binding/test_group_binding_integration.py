"""TSK-271 群/成员/角色名关系的真实 PostgreSQL 验收。"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from contextvars import ContextVar
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import event
from sqlalchemy.orm import Session

from komari_bot.plugins.character_binding.manager import (
    BindingConflictError,
    BindingPersistenceError,
)
from tests.character_binding.conftest import (
    bind_member,
    list_group_bindings,
    lookup_name,
    require_postgres,
)

if TYPE_CHECKING:
    from komari_bot.plugins.character_binding.manager import CharacterBindingManager


_GROUP_NUMBERS = {
    "g-unique": "100101",
    "g-member": "100102",
    "g-member-other": "100103",
    "g-name": "100104",
    "g-name-other": "100105",
    "g-concurrent": "100106",
    "g-atomic": "100107",
    "g-write-failure": "100111",
    "g-app-isolation": "100108",
    "g-clear": "100110",
}

_FAIL_NEXT_COMMIT: ContextVar[bool] = ContextVar(
    "tsk271_fail_next_character_binding_commit",
    default=False,
)


class _InjectedCommitFailureError(RuntimeError):
    """测试用事务提交故障。"""


def _raise_once_before_commit(_session: Session) -> None:
    if _FAIL_NEXT_COMMIT.get():
        _FAIL_NEXT_COMMIT.set(False)
        raise _InjectedCommitFailureError


def _group(label: str) -> tuple[str, str]:
    return _GROUP_NUMBERS[label], f"openid-{label}"


def _member_openids(rows: object) -> set[str]:
    if isinstance(rows, Mapping):
        direct_member_openid = rows.get("member_openid")
        if isinstance(direct_member_openid, str):
            return {direct_member_openid}
        return {
            member_openid
            for row in rows.values()
            for member_openid in _member_openids(row)
        }
    if not isinstance(rows, (list, tuple, set, frozenset)):
        value = getattr(rows, "member_openid", None)
        if isinstance(value, str):
            return {value}
        raise TypeError from None
    result: set[str] = set()
    for row in rows:
        result.update(_member_openids(row))
    return result


@pytest.mark.asyncio
async def test_group_id_and_openid_are_bidirectionally_unique_and_ignore_self_id(
    binding_manager: CharacterBindingManager,
    app_id: str,
) -> None:
    group_id, group_openid = _group("g-unique")

    await bind_member(
        binding_manager,
        app_id=app_id,
        group_id=group_id,
        group_openid=group_openid,
        member_qq="10001",
        member_openid="member-openid-1",
        character_name="花火",
        bot_self_id="onebot-a",
    )
    await bind_member(
        binding_manager,
        app_id=app_id,
        group_id=group_id,
        group_openid=group_openid,
        member_qq="10002",
        member_openid="member-openid-2",
        character_name="黑塔",
        bot_self_id="onebot-b",
    )
    assert (
        lookup_name(
            binding_manager,
            app_id=app_id,
            group_openid=group_openid,
            member_openid="member-openid-2",
        )
        == "黑塔"
    )

    with pytest.raises(BindingConflictError):
        await bind_member(
            binding_manager,
            app_id=app_id,
            group_id=group_id,
            group_openid="openid-conflicting",
            member_qq="10003",
            member_openid="member-openid-3",
            character_name="冲突群",
        )
    with pytest.raises(BindingConflictError):
        await bind_member(
            binding_manager,
            app_id=app_id,
            group_id="100109",
            group_openid=group_openid,
            member_qq="10004",
            member_openid="member-openid-4",
            character_name="冲突群",
        )

    assert (
        lookup_name(
            binding_manager,
            app_id=app_id,
            group_openid="openid-conflicting",
            member_openid="member-openid-3",
        )
        is None
    )
    assert (
        lookup_name(
            binding_manager,
            app_id=app_id,
            group_openid=group_openid,
            member_openid="member-openid-4",
        )
        is None
    )


@pytest.mark.asyncio
async def test_member_qq_and_openid_are_bidirectionally_unique_per_group(
    binding_manager: CharacterBindingManager,
    app_id: str,
) -> None:
    group_id, group_openid = _group("g-member")
    await bind_member(
        binding_manager,
        app_id=app_id,
        group_id=group_id,
        group_openid=group_openid,
        member_qq="10011",
        member_openid="openid-1",
        character_name="成员一",
    )

    with pytest.raises(BindingConflictError):
        await bind_member(
            binding_manager,
            app_id=app_id,
            group_id=group_id,
            group_openid=group_openid,
            member_qq="10011",
            member_openid="openid-other",
            character_name="覆盖 QQ",
        )
    with pytest.raises(BindingConflictError):
        await bind_member(
            binding_manager,
            app_id=app_id,
            group_id=group_id,
            group_openid=group_openid,
            member_qq="10012",
            member_openid="openid-1",
            character_name="覆盖 OpenID",
        )

    other_group_id, other_group_openid = _group("g-member-other")
    await bind_member(
        binding_manager,
        app_id=app_id,
        group_id=other_group_id,
        group_openid=other_group_openid,
        member_qq="10011",
        member_openid="openid-1",
        character_name="另一群成员",
    )
    assert (
        lookup_name(
            binding_manager,
            app_id=app_id,
            group_openid=other_group_openid,
            member_openid="openid-1",
        )
        == "另一群成员"
    )


@pytest.mark.asyncio
async def test_same_group_name_uses_nfkc_casefold_and_cross_group_allows_same_name(
    binding_manager: CharacterBindingManager,
    app_id: str,
) -> None:
    group_id, group_openid = _group("g-name")
    await bind_member(
        binding_manager,
        app_id=app_id,
        group_id=group_id,
        group_openid=group_openid,
        member_qq="10021",
        member_openid="openid-name-1",
        character_name="  \uff21lice   Smith  ",
    )

    with pytest.raises(BindingConflictError):
        await bind_member(
            binding_manager,
            app_id=app_id,
            group_id=group_id,
            group_openid=group_openid,
            member_qq="10022",
            member_openid="openid-name-2",
            character_name="alice smith",
        )

    other_group_id, other_group_openid = _group("g-name-other")
    await bind_member(
        binding_manager,
        app_id=app_id,
        group_id=other_group_id,
        group_openid=other_group_openid,
        member_qq="10022",
        member_openid="openid-name-2",
        character_name="alice smith",
    )
    assert (
        lookup_name(
            binding_manager,
            app_id=app_id,
            group_openid=other_group_openid,
            member_openid="openid-name-2",
        )
        == "alice smith"
    )


@pytest.mark.asyncio
async def test_same_group_name_race_has_one_conflict_and_one_committed_row(
    app_id: str,
) -> None:
    require_postgres()
    from komari_bot.plugins.character_binding.manager import CharacterBindingManager

    group_id, group_openid = _group("g-concurrent")
    managers = [CharacterBindingManager(), CharacterBindingManager()]
    await asyncio.gather(*(manager.initialize() for manager in managers))
    try:
        results = await asyncio.gather(
            *(
                bind_member(
                    manager,
                    app_id=app_id,
                    group_id=group_id,
                    group_openid=group_openid,
                    member_qq=str(10031 + index),
                    member_openid=f"openid-race-{index}",
                    character_name="  Straße  ",
                )
                for index, manager in enumerate(managers)
            ),
            return_exceptions=True,
        )
        successful_indices = [
            index
            for index, result in enumerate(results)
            if not isinstance(result, Exception)
        ]
        assert len(successful_indices) == 1
        for index, manager in enumerate(managers):
            assert lookup_name(
                manager,
                app_id=app_id,
                group_openid=group_openid,
                member_openid=f"openid-race-{index}",
            ) == ("Straße" if index in successful_indices else None)
    finally:
        await asyncio.gather(*(manager.close() for manager in managers))

    assert sum(not isinstance(result, Exception) for result in results) == 1
    failures = [result for result in results if isinstance(result, Exception)]
    assert len(failures) == 1
    assert isinstance(failures[0], BindingConflictError)

    verifier = CharacterBindingManager()
    await verifier.initialize()
    try:
        names = [
            lookup_name(
                verifier,
                app_id=app_id,
                group_openid=group_openid,
                member_openid=f"openid-race-{index}",
            )
            for index in range(2)
        ]
        assert names.count("Straße") == 1
        assert names.count(None) == 1
        rows = list_group_bindings(
            verifier,
            app_id=app_id,
            group_openid=group_openid,
        )
        assert _member_openids(rows) == {f"openid-race-{successful_indices[0]}"}
    finally:
        await verifier.close()


@pytest.mark.asyncio
async def test_conflicting_name_rolls_back_group_member_and_name_atomically(
    binding_manager: CharacterBindingManager,
    app_id: str,
) -> None:
    group_id, group_openid = _group("g-atomic")
    await bind_member(
        binding_manager,
        app_id=app_id,
        group_id=group_id,
        group_openid=group_openid,
        member_qq="10041",
        member_openid="openid-existing",
        character_name="已存在",
    )

    with pytest.raises(BindingConflictError):
        await bind_member(
            binding_manager,
            app_id=app_id,
            group_id=group_id,
            group_openid=group_openid,
            member_qq="10042",
            member_openid="openid-new",
            character_name=" 已存在 ",
        )

    assert (
        lookup_name(
            binding_manager,
            app_id=app_id,
            group_openid=group_openid,
            member_openid="openid-new",
        )
        is None
    )
    assert "openid-new" not in _member_openids(
        list_group_bindings(
            binding_manager,
            app_id=app_id,
            group_openid=group_openid,
        )
    )
    assert (
        lookup_name(
            binding_manager,
            app_id=app_id,
            group_openid=group_openid,
            member_openid="openid-existing",
        )
        == "已存在"
    )


@pytest.mark.asyncio
async def test_new_group_write_failure_rolls_back_and_keeps_snapshot_unchanged(
    binding_manager: CharacterBindingManager,
    app_id: str,
) -> None:
    """提交阶段故障也不能留下新群、成员或进程内名字。"""
    group_id, group_openid = _group("g-write-failure")
    event.listen(Session, "before_commit", _raise_once_before_commit)
    token = _FAIL_NEXT_COMMIT.set(True)
    try:
        with pytest.raises(BindingPersistenceError):
            await bind_member(
                binding_manager,
                app_id=app_id,
                group_id=group_id,
                group_openid=group_openid,
                member_qq="10071",
                member_openid="openid-write-failure",
                character_name="提交失败名字",
            )
    finally:
        _FAIL_NEXT_COMMIT.reset(token)
        event.remove(Session, "before_commit", _raise_once_before_commit)

    assert (
        lookup_name(
            binding_manager,
            app_id=app_id,
            group_openid=group_openid,
            member_openid="openid-write-failure",
            fallback_nickname="失败后昵称",
        )
        == "失败后昵称"
    )
    assert (
        _member_openids(
            list_group_bindings(
                binding_manager,
                app_id=app_id,
                group_openid=group_openid,
            )
        )
        == set()
    )

    await bind_member(
        binding_manager,
        app_id=app_id,
        group_id=group_id,
        group_openid=group_openid,
        member_qq="10071",
        member_openid="openid-write-failure",
        character_name="提交成功名字",
    )
    assert (
        lookup_name(
            binding_manager,
            app_id=app_id,
            group_openid=group_openid,
            member_openid="openid-write-failure",
        )
        == "提交成功名字"
    )


@pytest.mark.asyncio
async def test_app_isolation_allows_same_group_and_member_aliases(
    binding_manager: CharacterBindingManager,
    app_id: str,
) -> None:
    group_id, group_openid = _group("g-app-isolation")
    other_app = f"{app_id}-other"
    await bind_member(
        binding_manager,
        app_id=app_id,
        group_id=group_id,
        group_openid=group_openid,
        member_qq="10061",
        member_openid="openid-shared",
        character_name="应用一",
    )
    await bind_member(
        binding_manager,
        app_id=other_app,
        group_id=group_id,
        group_openid=group_openid,
        member_qq="10061",
        member_openid="openid-shared",
        character_name="应用二",
    )
    assert (
        lookup_name(
            binding_manager,
            app_id=app_id,
            group_openid=group_openid,
            member_openid="openid-shared",
        )
        == "应用一"
    )
    assert (
        lookup_name(
            binding_manager,
            app_id=other_app,
            group_openid=group_openid,
            member_openid="openid-shared",
        )
        == "应用二"
    )


@pytest.mark.asyncio
async def test_clear_name_retains_verified_group_and_member_identity(
    binding_manager: CharacterBindingManager,
    app_id: str,
) -> None:
    group_id, group_openid = _group("g-clear")
    await bind_member(
        binding_manager,
        app_id=app_id,
        group_id=group_id,
        group_openid=group_openid,
        member_qq="10051",
        member_openid="openid-clear",
        character_name="待清除",
    )

    assert (
        await binding_manager.clear_character_name(
            app_id=app_id,
            group_openid=group_openid,
            member_openid="openid-clear",
        )
        is True
    )
    assert (
        lookup_name(
            binding_manager,
            app_id=app_id,
            group_openid=group_openid,
            member_openid="openid-clear",
        )
        is None
    )

    with pytest.raises(BindingConflictError):
        await bind_member(
            binding_manager,
            app_id=app_id,
            group_id=group_id,
            group_openid=group_openid,
            member_qq="10051",
            member_openid="openid-replaced",
            character_name="改绑绕过",
        )
    await bind_member(
        binding_manager,
        app_id=app_id,
        group_id=group_id,
        group_openid=group_openid,
        member_qq="10051",
        member_openid="openid-clear",
        character_name="重新绑定",
    )
    assert (
        lookup_name(
            binding_manager,
            app_id=app_id,
            group_openid=group_openid,
            member_openid="openid-clear",
        )
        == "重新绑定"
    )
