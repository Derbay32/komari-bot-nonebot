"""TSK-271 群/成员/角色名关系的真实 PostgreSQL 验收。"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from contextlib import suppress
from contextvars import ContextVar
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from sqlalchemy import event
from sqlalchemy.orm import Session

from komari_bot.plugins.character_binding.manager import (
    BindingConflictError,
    BindingPersistenceError,
)
from tests.character_binding.conftest import (
    POSTGRES_URL,
    bind_member,
    list_group_bindings,
    lookup_name,
    require_postgres,
)

if TYPE_CHECKING:
    import asyncpg

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


def _sql_literal(value: str) -> str:
    """Quote a test-generated literal for the temporary trigger DDL."""
    return "'" + value.replace("'", "''") + "'"


def _asyncpg_url() -> str:
    return POSTGRES_URL.replace("postgresql+asyncpg://", "postgresql://", 1)


async def _open_control_connection() -> "asyncpg.Connection":
    import asyncpg

    return await asyncpg.connect(_asyncpg_url())


async def _install_cross_group_pause_trigger(
    *,
    app_id: str,
    group_id: str,
    group_openid: str,
) -> tuple[str, str, tuple[int, int], tuple[int, int]]:
    """Pause only the cross-mapped INSERT at PostgreSQL's trigger boundary.

    The trigger is deliberately test-local.  It proves that the X transaction
    has reached the public write's group INSERT before the two valid mappings
    commit, without depending on manager internals or a timing race.
    """
    suffix = uuid4().hex[:20]
    trigger_name = f"tsk271_pause_{suffix}"
    function_name = f"tsk271_pause_fn_{suffix}"
    signal_key = (uuid4().int % 2_000_000_000 + 1, uuid4().int % 2_000_000_000 + 1)
    gate_key = (uuid4().int % 2_000_000_000 + 1, uuid4().int % 2_000_000_000 + 1)
    function_sql = f"""
        CREATE FUNCTION "{function_name}"() RETURNS trigger
        LANGUAGE plpgsql AS $func$
        BEGIN
            IF NEW.app_id = TG_ARGV[0]
               AND NEW.group_id = TG_ARGV[1]
               AND NEW.group_openid = TG_ARGV[2] THEN
                PERFORM pg_advisory_xact_lock(
                    TG_ARGV[3]::integer, TG_ARGV[4]::integer
                );
                PERFORM pg_advisory_xact_lock(
                    TG_ARGV[5]::integer, TG_ARGV[6]::integer
                );
            END IF;
            RETURN NEW;
        END;
        $func$
    """
    trigger_sql = f"""
        CREATE TRIGGER "{trigger_name}"
        BEFORE INSERT ON komari_character_binding_groups
        FOR EACH ROW
        EXECUTE FUNCTION "{function_name}"(
            {_sql_literal(app_id)},
            {_sql_literal(group_id)},
            {_sql_literal(group_openid)},
            {_sql_literal(str(signal_key[0]))},
            {_sql_literal(str(signal_key[1]))},
            {_sql_literal(str(gate_key[0]))},
            {_sql_literal(str(gate_key[1]))}
        )
    """
    connection = await _open_control_connection()
    try:
        await connection.execute(function_sql)
        await connection.execute(trigger_sql)
    finally:
        await connection.close()
    return trigger_name, function_name, signal_key, gate_key


async def _drop_cross_group_pause_trigger(
    *,
    trigger_name: str,
    function_name: str,
) -> None:
    connection = await _open_control_connection()
    try:
        await connection.execute(
            f'DROP TRIGGER IF EXISTS "{trigger_name}" '
            "ON komari_character_binding_groups"
        )
        await connection.execute(f'DROP FUNCTION IF EXISTS "{function_name}"()')
    finally:
        await connection.close()


async def _wait_for_advisory_signal(key: tuple[int, int]) -> None:
    """Wait for the trigger's SQL-side signal, yielding through DB I/O."""
    statement = """
        SELECT EXISTS (
            SELECT 1
            FROM pg_locks
            WHERE locktype = 'advisory'
              AND granted
              AND classid = :class_id
              AND objid = :object_id
        )
        """.replace(":class_id", "$1").replace(":object_id", "$2")

    connection = await _open_control_connection()
    async def poll() -> None:
        while True:
            reached = bool(await connection.fetchval(statement, key[0], key[1]))
            if reached:
                return

    try:
        await asyncio.wait_for(poll(), timeout=10)
    finally:
        await connection.close()


async def _wait_for_group_scope_blocked(
    *,
    app_id: str,
    group_openid: str,
) -> None:
    """Prove a writer is waiting on the shared group scope advisory lock."""
    scope_key = f"komari-roulette:{app_id}:{group_openid}"
    statement = """
        SELECT EXISTS (
            SELECT 1
            FROM pg_locks
            WHERE locktype = 'advisory'
              AND NOT granted
              AND classid = (
                  (hashtextextended($1, 0) >> 32) & 4294967295
              )::oid
              AND objid = (
                  hashtextextended($1, 0) & 4294967295
              )::oid
        )
    """
    connection = await _open_control_connection()

    async def poll() -> None:
        while True:
            if await connection.fetchval(statement, scope_key):
                return
            await asyncio.sleep(0.02)

    try:
        await asyncio.wait_for(poll(), timeout=10)
    finally:
        await connection.close()


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
async def test_cross_group_mapping_race_never_attaches_member_to_wrong_group(
    app: object,
    app_id: str,
    binding_manager: CharacterBindingManager,
) -> None:
    """Crossed ``(group_id, group_openid)`` races fail closed atomically.

    X starts with ``(G1, O2)`` and is paused at its group INSERT.  S1 commits
    ``(G1, O1)`` and S2 commits ``(G2, O2)`` while X is waiting.  The public
    write must reject X after its conflict recheck rather than attaching X's
    member to S2's canonical group.
    """
    del app
    require_postgres()
    from komari_bot.plugins.character_binding.manager import CharacterBindingManager

    numeric_seed = int(app_id.removeprefix("tsk271-")[:12], 16)
    group_one_id = str(100_000_000 + numeric_seed % 800_000_000)
    group_one_openid = f"{app_id}-group-one"
    group_two_id = str(int(group_one_id) + 1)
    group_two_openid = f"{app_id}-group-two"
    member_one_qq = str(int(group_one_id) + 11)
    member_two_qq = str(int(group_one_id) + 12)
    cross_member_qq = str(int(group_one_id) + 13)
    cross_member_openid = f"{app_id}-member-cross"
    trigger_names: tuple[str, str] | None = None
    gate_connection = await _open_control_connection()
    gate_held = False
    cross_task: asyncio.Task[object] | None = None
    second_scope_task: asyncio.Task[object] | None = None
    managers = [
        binding_manager,
        CharacterBindingManager(),
        CharacterBindingManager(),
    ]
    await asyncio.gather(*(manager.initialize() for manager in managers[1:]))

    try:
        trigger_names_data = await _install_cross_group_pause_trigger(
            app_id=app_id,
            group_id=group_one_id,
            group_openid=group_two_openid,
        )
        trigger_names = trigger_names_data[:2]
        _trigger_name, _function_name, signal_key, gate_key = trigger_names_data

        await gate_connection.execute("BEGIN")
        await gate_connection.fetchval(
            "SELECT pg_advisory_xact_lock($1, $2)",
            gate_key[0],
            gate_key[1],
        )
        gate_held = True

        cross_task = asyncio.create_task(
            bind_member(
                managers[0],
                app_id=app_id,
                group_id=group_one_id,
                group_openid=group_two_openid,
                member_qq=cross_member_qq,
                member_openid=cross_member_openid,
                character_name="错误交叉",
            )
        )
        await _wait_for_advisory_signal(signal_key)

        second_scope_started = asyncio.Event()

        async def bind_second_scope() -> object:
            second_scope_started.set()
            return await bind_member(
                managers[2],
                app_id=app_id,
                group_id=group_two_id,
                group_openid=group_two_openid,
                member_qq=member_two_qq,
                member_openid=f"{app_id}-member-two",
                character_name="群二成员",
            )

        second_scope_task = asyncio.create_task(bind_second_scope())
        await second_scope_started.wait()
        await _wait_for_group_scope_blocked(
            app_id=app_id,
            group_openid=group_two_openid,
        )

        await bind_member(
            managers[1],
            app_id=app_id,
            group_id=group_one_id,
            group_openid=group_one_openid,
            member_qq=member_one_qq,
            member_openid=f"{app_id}-member-one",
            character_name="群一成员",
        )

        # The gate is transaction-scoped.  Rolling back this controller
        # session releases it even if the cross task is later cancelled.
        await gate_connection.execute("ROLLBACK")
        gate_held = False

        with pytest.raises(BindingConflictError):
            await asyncio.wait_for(cross_task, timeout=10)
        await asyncio.wait_for(second_scope_task, timeout=10)

        verifier = CharacterBindingManager()
        await verifier.initialize()
        try:
            group_one_rows = list_group_bindings(
                verifier,
                app_id=app_id,
                group_openid=group_one_openid,
            )
            group_two_rows = list_group_bindings(
                verifier,
                app_id=app_id,
                group_openid=group_two_openid,
            )
            assert _member_openids(group_one_rows) == {
                f"{app_id}-member-one"
            }
            assert _member_openids(group_two_rows) == {
                f"{app_id}-member-two"
            }
            assert (
                verifier.get_character_name(
                    group_id=group_one_id,
                    user_id=member_one_qq,
                    fallback_nickname="群一昵称",
                )
                == "群一成员"
            )
            assert (
                verifier.get_character_name(
                    group_id=group_two_id,
                    user_id=cross_member_qq,
                    fallback_nickname="交叉昵称",
                )
                == "交叉昵称"
            )
            assert (
                verifier.get_qq_character_name(
                    app_id=app_id,
                    group_openid=group_two_openid,
                    member_openid=cross_member_openid,
                    fallback_nickname="交叉昵称",
                )
                == "交叉昵称"
            )
        finally:
            await verifier.close()
    finally:
        if gate_held:
            with suppress(Exception):
                await gate_connection.execute("ROLLBACK")
        if cross_task is not None:
            with suppress(Exception):
                await asyncio.wait_for(cross_task, timeout=10)
        if second_scope_task is not None:
            with suppress(Exception):
                await asyncio.wait_for(second_scope_task, timeout=10)
        if trigger_names is not None:
            await _drop_cross_group_pause_trigger(
                trigger_name=trigger_names[0],
                function_name=trigger_names[1],
            )
        await gate_connection.close()
        await asyncio.gather(*(manager.close() for manager in managers[1:]))


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
