"""TSK-279 Stage-C2 RED（真实 PostgreSQL）：排行榜核查与重投影存储接缝。

生产 ``PostgresRouletteStorage.inspect_leaderboard`` /
``komari_roulette.management_api`` 尚未落地，因此除 green 探针外本文件为**预期
RED**（``AttributeError`` / ``ModuleNotFoundError``）。这里验收：

* ``inspect_leaderboard(group)`` 用**一致快照**对照缓存投影与 completed 证明，
  精确报出缺行 / 多行 / 错 wins / 显示名错配 / 时间错配（闭集 code）；
* 损坏的 completed 证明必须抛 ``AggregateCorruptError``，绝不静默伪 0；
* ``rebuild_leaderboard`` 只从 completed 证明派生，且与终局投影共享同一
  ``pg_advisory_xact_lock``，两种加锁顺序下 wins 都不丢；
* HTTP ``inspect`` / ``rebuild`` 走真实存储默认缝，错误映射为固定 code。

只有 ``KOMARI_TEST_POSTGRES_URL`` 与 ``SQLALCHEMY_DATABASE_URL`` 指向同一库时
才执行；用例只清理自己创建的 app/group 作用域。
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy import text

from komari_bot.plugins.komari_roulette import (
    AggregateCorruptError,
    LeaderboardEntry,
    PostgresRouletteStorage,
)
from komari_bot.plugins.komari_roulette.observability import RouletteObservation

from .storage_support import (
    POSTGRES_URL,
    SQLALCHEMY_URL,
    clear_scope,
    group_for,
    open_session,
    reset_shared_orm_engine,
    same_database,
    scope,
)
from .test_storage_integration import (
    _backend_pid,
    _persist_completed,
    _wait_until_blocked,
)
from .tsk279_management_support import (
    DISCREPANCY_CODES,
    INSPECT_PATH,
    MANAGER_TOKEN,
    READER_TOKEN,
    REBUILD_PATH,
    asgi_client,
    assert_error_envelope,
    auth_headers,
    build_control_plane,
    inspect_body,
    rebuild_headers,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from komari_bot.plugins.komari_roulette.domain import GroupRef

pytestmark = [
    pytest.mark.skipif(
        not POSTGRES_URL,
        reason="未设置 KOMARI_TEST_POSTGRES_URL，不能执行真实 PostgreSQL 验收",
    ),
    pytest.mark.asyncio,
]

WINNER = "丙"
WINNERS = ("甲", "乙", WINNER)
SEATS = ("m-loser-1", "m-loser-2", "m-winner")
#: 在途投影的第二个终局证明：显示名与 ``WINNERS`` 相同、成员 key 全不同，
#: 使已提交快照 1 entry 在提交后真正变为 2 entry（同名冠军各自成行）。
UNCOMMITTED_SEATS = ("m-pending-loser-1", "m-pending-loser-2", "m-pending-winner")

#: 只改缓存投影（completed 证明不动），逐项验收差异 code。
CACHE_MUTATIONS: dict[str, tuple[str, str]] = {
    "delete": (
        "DELETE FROM komari_roulette_leaderboard "
        "WHERE app_id = :app_id AND group_openid = :group_openid",
        "missing_cached_row",
    ),
    "insert": (
        "INSERT INTO komari_roulette_leaderboard ("
        "app_id, group_openid, member_openid, display_name, wins, last_won_at) "
        "VALUES (:app_id, :group_openid, 'ghost-member', '幽灵玩家', 1, "
        "clock_timestamp())",
        "extra_cached_row",
    ),
    "wins": (
        "UPDATE komari_roulette_leaderboard SET wins = wins + 5 "
        "WHERE app_id = :app_id AND group_openid = :group_openid",
        "wins_mismatch",
    ),
    "display_name": (
        "UPDATE komari_roulette_leaderboard SET display_name = '改名玩家' "
        "WHERE app_id = :app_id AND group_openid = :group_openid",
        "display_name_mismatch",
    ),
    "last_won_at": (
        "UPDATE komari_roulette_leaderboard SET "
        "last_won_at = last_won_at + make_interval(hours => 1) "
        "WHERE app_id = :app_id AND group_openid = :group_openid",
        "last_won_at_mismatch",
    ),
}

CLEAR_CACHE_SQL = (
    "DELETE FROM komari_roulette_leaderboard "
    "WHERE app_id = :app_id AND group_openid = :group_openid"
)
CORRUPT_RESULT_SQL = (
    "UPDATE komari_roulette_results SET winner_display_name = NULL "
    "WHERE app_id = :app_id AND group_openid = :group_openid"
)


@pytest.fixture
async def db_scope() -> AsyncIterator[tuple[str, str, GroupRef]]:
    if not same_database(POSTGRES_URL, SQLALCHEMY_URL):
        pytest.skip("KOMARI_TEST_POSTGRES_URL 与 SQLALCHEMY_DATABASE_URL 不一致")
    app_id, group_openid = scope("management")
    group = group_for(app_id, group_openid)
    await reset_shared_orm_engine()
    try:
        yield app_id, group_openid, group
    finally:
        with suppress(Exception):
            await clear_scope(app_id, group_openid)
        await reset_shared_orm_engine()


def _params(app_id: str, group_openid: str) -> dict[str, str]:
    return {"app_id": app_id, "group_openid": group_openid}


async def _seed_completed(group: GroupRef) -> None:
    session = open_session()
    try:
        await _persist_completed(
            session,
            group,
            names=WINNERS,
            member_openids=SEATS,
        )
    finally:
        await session.close()


async def _inspect(group: GroupRef) -> Any:
    session = open_session()
    try:
        return await PostgresRouletteStorage(session).inspect_leaderboard(group)
    finally:
        await session.close()


async def _run(app_id: str, group_openid: str, *statements: str) -> None:
    session = open_session()
    try:
        for statement in statements:
            await session.execute(text(statement), _params(app_id, group_openid))
        await session.commit()
    finally:
        await session.close()


async def _mutation(app_id: str, group_openid: str, mutation: str) -> None:
    statement, _code = CACHE_MUTATIONS[mutation]
    await _run(app_id, group_openid, statement)


async def _assert_consistent_projection(
    group: GroupRef,
    *,
    expected_wins: int,
    expected_entries: int,
) -> None:
    session = open_session()
    try:
        storage = PostgresRouletteStorage(session)
        rows = await storage.list_leaderboard(group)
        assert len(rows) == expected_entries
        assert [row.wins for row in rows] == [expected_wins] * expected_entries
        inspection = await storage.inspect_leaderboard(group)
        assert inspection.consistent is True
        assert inspection.discrepancy_codes == ()
        assert inspection.cached_entry_count == expected_entries
        assert inspection.completed_entry_count == expected_entries
        assert inspection.cached_total_wins == expected_wins * expected_entries
        assert inspection.completed_total_wins == expected_wins * expected_entries
        for entry in inspection.entries:
            assert entry.last_won_at.tzinfo is not None
    finally:
        await session.close()


async def test_inspect_empty_scope_is_consistent_and_empty(
    db_scope: tuple[str, str, GroupRef],
) -> None:
    app_id, group_openid, group = db_scope
    inspection = await _inspect(group)
    assert inspection.app_id == app_id
    assert inspection.group_openid == group_openid
    assert inspection.consistent is True
    assert inspection.discrepancy_codes == ()
    assert inspection.entries == ()
    assert inspection.cached_entry_count == 0
    assert inspection.completed_entry_count == 0
    assert inspection.cached_total_wins == 0
    assert inspection.completed_total_wins == 0


async def test_inspect_matches_the_real_projection_after_a_completed_game(
    db_scope: tuple[str, str, GroupRef],
) -> None:
    _app_id, _group_openid, group = db_scope
    await _seed_completed(group)

    inspection = await _inspect(group)
    assert inspection.consistent is True
    assert inspection.discrepancy_codes == ()
    assert inspection.cached_entry_count == 1
    assert inspection.completed_entry_count == 1
    assert inspection.cached_total_wins == 1
    assert inspection.completed_total_wins == 1
    (entry,) = inspection.entries
    assert type(entry) is LeaderboardEntry
    assert entry.display_name == WINNER
    assert entry.wins == 1
    assert entry.last_won_at.tzinfo is not None


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [(name, code) for name, (_sql, code) in CACHE_MUTATIONS.items()],
)
async def test_inspect_reports_the_exact_cache_divergence(
    db_scope: tuple[str, str, GroupRef],
    mutation: str,
    expected_code: str,
) -> None:
    app_id, group_openid, group = db_scope
    assert expected_code in DISCREPANCY_CODES
    await _seed_completed(group)
    await _mutation(app_id, group_openid, mutation)

    inspection = await _inspect(group)
    assert inspection.consistent is False
    assert expected_code in inspection.discrepancy_codes
    assert set(inspection.discrepancy_codes) <= DISCREPANCY_CODES
    if mutation == "delete":
        assert inspection.cached_entry_count == 0
        assert inspection.completed_entry_count == 1
    if mutation == "insert":
        assert inspection.cached_entry_count == 2
        assert inspection.completed_entry_count == 1


async def test_rebuild_reproduces_the_cache_from_completed_proofs(
    db_scope: tuple[str, str, GroupRef],
) -> None:
    app_id, group_openid, group = db_scope
    await _seed_completed(group)
    await _seed_completed(group)
    await _run(app_id, group_openid, CLEAR_CACHE_SQL)

    session = open_session()
    try:
        storage = PostgresRouletteStorage(session)
        broken = await storage.inspect_leaderboard(group)
        assert broken.consistent is False
        assert "missing_cached_row" in broken.discrepancy_codes
        assert broken.completed_total_wins == 2

        await storage.rebuild_leaderboard(group)
        await session.commit()

        repaired = await storage.inspect_leaderboard(group)
        assert repaired.consistent is True
        assert repaired.discrepancy_codes == ()
        assert repaired.cached_entry_count == 1
        assert repaired.cached_total_wins == 2
        assert not hasattr(repaired.entries[0], "member_openid")
        assert repaired.entries[0].display_name == WINNER
    finally:
        await session.close()


async def test_inspect_and_rebuild_fail_safe_on_a_corrupt_proof(
    db_scope: tuple[str, str, GroupRef],
) -> None:
    """损坏的 completed 证明 ⇒ AggregateCorruptError，且缓存不被改写。"""

    app_id, group_openid, group = db_scope
    await _seed_completed(group)
    await _run(app_id, group_openid, CORRUPT_RESULT_SQL)

    session = open_session()
    try:
        storage = PostgresRouletteStorage(session)
        before = await storage.list_leaderboard(group)
        with pytest.raises(AggregateCorruptError):
            await storage.inspect_leaderboard(group)
        with pytest.raises(AggregateCorruptError):
            await storage.rebuild_leaderboard(group)
        after = await storage.list_leaderboard(group)
    finally:
        await session.close()
    assert after == before
    assert len(before) == 1
    assert before[0].wins == 1


async def test_http_inspect_and_rebuild_drive_the_real_storage(
    db_scope: tuple[str, str, GroupRef],
) -> None:
    app_id, group_openid, group = db_scope
    await _seed_completed(group)
    await _run(app_id, group_openid, CLEAR_CACHE_SQL)

    plane = build_control_plane(
        observation=RouletteObservation(
            runtime_status="ready",
            runtime_reason="policy_admitted",
        ),
        session_factory=open_session,
        real_storage=True,
    )
    async with asgi_client(plane.app) as client:
        broken = await client.post(
            INSPECT_PATH,
            headers=auth_headers(READER_TOKEN),
            json=inspect_body(app_id, group_openid),
        )
        assert broken.status_code == 200, broken.text
        broken_body = broken.json()
        assert broken_body["consistent"] is False
        assert "missing_cached_row" in broken_body["discrepancy_codes"]
        assert broken_body["completed_total_wins"] == 1

        rebuilt = await client.post(
            REBUILD_PATH,
            headers=rebuild_headers(MANAGER_TOKEN),
            json=inspect_body(app_id, group_openid),
        )
        assert rebuilt.status_code == 200, rebuilt.text
        rebuilt_body = rebuilt.json()
        assert rebuilt_body["app_id"] == app_id
        assert rebuilt_body["group_openid"] == group_openid
        assert rebuilt_body["consistent"] is True
        assert rebuilt_body["entry_count"] == 1
        assert rebuilt_body["total_wins"] == 1

        repaired = await client.post(
            INSPECT_PATH,
            headers=auth_headers(READER_TOKEN),
            json=inspect_body(app_id, group_openid),
        )
        assert repaired.status_code == 200, repaired.text
        repaired_body = repaired.json()
        assert repaired_body["consistent"] is True
        (entry,) = repaired_body["entries"]
        assert entry["display_name"] == WINNER
        assert entry["wins"] == 1
        assert "member_openid" not in repaired.text
        assert SEATS[2] not in repaired.text

    await _assert_consistent_projection(group, expected_wins=1, expected_entries=1)


async def test_http_rebuild_maps_a_corrupt_proof_to_the_fixed_code(
    db_scope: tuple[str, str, GroupRef],
) -> None:
    app_id, group_openid, group = db_scope
    await _seed_completed(group)
    await _run(app_id, group_openid, CORRUPT_RESULT_SQL)

    # 损坏证明 fail-safe：直接读缓存完整行（含成员 key）与证明行，不经 inspect 源。
    cache_before = await _cache_rows(app_id, group_openid)
    proofs_before = await _result_proof_rows(app_id, group_openid)
    assert len(cache_before) == 1
    assert cache_before[0][2] == 1
    assert len(proofs_before) == 1
    assert proofs_before[0][2] is None

    plane = build_control_plane(
        observation=RouletteObservation(runtime_status="ready"),
        session_factory=open_session,
        real_storage=True,
    )
    async with asgi_client(plane.app) as client:
        response = await client.post(
            REBUILD_PATH,
            headers=rebuild_headers(MANAGER_TOKEN),
            json=inspect_body(app_id, group_openid),
        )
        assert_error_envelope(
            response,
            503,
            "roulette_aggregate_corrupt",
            leaked=[app_id, group_openid],
        )

    # 缓存完整行（含成员 key）逐字节不变、仍 1 胜；损坏证明未被擅自修复/改写。
    assert await _cache_rows(app_id, group_openid) == cache_before
    assert await _result_proof_rows(app_id, group_openid) == proofs_before


async def test_rebuild_holding_the_scope_lock_keeps_the_next_terminal_win(
    db_scope: tuple[str, str, GroupRef],
) -> None:
    """rebuild 先持锁、终局投影后到：wins 不丢且核查一致。"""

    _app_id, _group_openid, group = db_scope
    await _seed_completed(group)

    rebuilder = open_session()
    projector = open_session()
    observer = open_session()
    projector_task: asyncio.Task[None] | None = None
    try:
        _snapshot, projection, _active = await _persist_completed(
            rebuilder,
            group,
            names=WINNERS,
            member_openids=SEATS,
            project=False,
        )
        rebuilder_pid = await _backend_pid(rebuilder)
        await PostgresRouletteStorage(rebuilder).rebuild_leaderboard(group)
        projector_pid = await _backend_pid(projector)
        projector_task = asyncio.create_task(
            PostgresRouletteStorage(projector).project_terminal(projection)
        )
        blockers = await _wait_until_blocked(observer, projector_pid)
        assert rebuilder_pid in blockers

        await asyncio.wait_for(rebuilder.commit(), timeout=5)
        await asyncio.wait_for(projector_task, timeout=5)
        await projector.commit()
    finally:
        if projector_task is not None and not projector_task.done():
            projector_task.cancel()
            with suppress(asyncio.CancelledError):
                await projector_task
        with suppress(Exception):
            await rebuilder.rollback()
        with suppress(Exception):
            await projector.rollback()
        await observer.close()
        await rebuilder.close()
        await projector.close()

    await _assert_consistent_projection(group, expected_wins=2, expected_entries=1)


async def test_terminal_projection_holding_the_scope_lock_keeps_the_win(
    db_scope: tuple[str, str, GroupRef],
) -> None:
    """终局投影先持锁、rebuild 后到：两种顺序都必须收敛到同一事实。"""

    _app_id, _group_openid, group = db_scope
    await _seed_completed(group)

    projector = open_session()
    rebuilder = open_session()
    observer = open_session()
    rebuild_task: asyncio.Task[None] | None = None
    try:
        _snapshot, projection, _active = await _persist_completed(
            projector,
            group,
            names=WINNERS,
            member_openids=SEATS,
            project=False,
        )
        projector_pid = await _backend_pid(projector)
        rebuild_pid = await _backend_pid(rebuilder)
        rebuild_task = asyncio.create_task(
            PostgresRouletteStorage(rebuilder).rebuild_leaderboard(group)
        )
        blockers = await _wait_until_blocked(observer, rebuild_pid)
        assert projector_pid in blockers

        await asyncio.wait_for(
            PostgresRouletteStorage(projector).project_terminal(projection),
            timeout=5,
        )
        await asyncio.wait_for(projector.commit(), timeout=5)
        await asyncio.wait_for(rebuild_task, timeout=5)
        await rebuilder.commit()
    finally:
        if rebuild_task is not None and not rebuild_task.done():
            rebuild_task.cancel()
            with suppress(asyncio.CancelledError):
                await rebuild_task
        with suppress(Exception):
            await projector.rollback()
        with suppress(Exception):
            await rebuilder.rollback()
        await observer.close()
        await projector.close()
        await rebuilder.close()

    await _assert_consistent_projection(group, expected_wins=2, expected_entries=1)


# ---------------------------------------------------------------------------
# C2 根审后补充：幂等重投影、同名成员级错配、在途投影下的一致快照
# ---------------------------------------------------------------------------

#: 两个游戏的冠军显示名相同、成员不同、总 wins 相同（=2）。
SAME_NAME = "同名玩家"
SAME_NAME_GAME_1 = ("甲", "乙", SAME_NAME)
SAME_NAME_GAME_2 = ("丙", "丁", SAME_NAME)
SAME_NAME_SEATS_1 = ("m-same-loser-1", "m-same-loser-2", "m-same-winner-1")
SAME_NAME_SEATS_2 = ("m-same-loser-3", "m-same-loser-4", "m-same-winner-2")


async def _result_proof_rows(
    app_id: str,
    group_openid: str,
) -> list[tuple[Any, ...]]:
    session = open_session()
    try:
        rows = (
            await session.execute(
                text(
                    "SELECT game_id, winner_member_openid, "
                    "winner_display_name, ended_at "
                    "FROM komari_roulette_results "
                    "WHERE app_id = :app_id AND group_openid = :group_openid "
                    "ORDER BY game_id"
                ),
                _params(app_id, group_openid),
            )
        ).all()
    finally:
        await session.close()
    return [tuple(row) for row in rows]


async def _cache_rows(app_id: str, group_openid: str) -> list[tuple[Any, ...]]:
    """直接读缓存投影完整行（含成员 key），不经过 ``inspect_leaderboard``。"""

    session = open_session()
    try:
        rows = (
            await session.execute(
                text(
                    "SELECT member_openid, display_name, wins, last_won_at "
                    "FROM komari_roulette_leaderboard "
                    "WHERE app_id = :app_id AND group_openid = :group_openid "
                    "ORDER BY member_openid"
                ),
                _params(app_id, group_openid),
            )
        ).all()
    finally:
        await session.close()
    return [tuple(row) for row in rows]


async def _swap_cached_last_won_at(
    app_id: str,
    group_openid: str,
    member_a: str,
    member_b: str,
) -> None:
    """只互换本 case 两个成员各自的 ``last_won_at``，不动 completed 证明。

    同事务先读后写：两个值必须真实存在且不相等，否则用例自身失真。
    """

    session = open_session()
    try:
        rows = (
            await session.execute(
                text(
                    "SELECT member_openid, last_won_at "
                    "FROM komari_roulette_leaderboard "
                    "WHERE app_id = :app_id AND group_openid = :group_openid "
                    "AND member_openid IN (:member_a, :member_b)"
                ),
                {
                    "app_id": app_id,
                    "group_openid": group_openid,
                    "member_a": member_a,
                    "member_b": member_b,
                },
            )
        ).all()
        times = {row[0]: row[1] for row in rows}
        assert set(times) == {member_a, member_b}
        assert times[member_a] != times[member_b]
        pairs = ((member_a, times[member_b]), (member_b, times[member_a]))
        for member, value in pairs:
            await session.execute(
                text(
                    "UPDATE komari_roulette_leaderboard SET last_won_at = :value "
                    "WHERE app_id = :app_id AND group_openid = :group_openid "
                    "AND member_openid = :member"
                ),
                {
                    "app_id": app_id,
                    "group_openid": group_openid,
                    "member": member,
                    "value": value,
                },
            )
        await session.commit()
    finally:
        await session.close()


async def _seed_named_completed(
    group: GroupRef,
    *,
    names: tuple[str, str, str],
    member_openids: tuple[str, str, str],
) -> None:
    session = open_session()
    try:
        await _persist_completed(
            session,
            group,
            names=names,
            member_openids=member_openids,
        )
    finally:
        await session.close()


async def test_rebuilt_projection_is_idempotent_and_never_recounts_wins(
    db_scope: tuple[str, str, GroupRef],
) -> None:
    """连续两次 rebuild 收敛到同一真实投影：不重复计分、不改 completed 证明。"""

    app_id, group_openid, group = db_scope
    await _seed_completed(group)
    await _run(app_id, group_openid, CLEAR_CACHE_SQL)
    proofs_before = await _result_proof_rows(app_id, group_openid)
    assert len(proofs_before) == 1

    session = open_session()
    try:
        storage = PostgresRouletteStorage(session)
        await storage.rebuild_leaderboard(group)
        await session.commit()
        first_rows = await storage.list_leaderboard(group)
        first = await storage.inspect_leaderboard(group)

        await storage.rebuild_leaderboard(group)
        await session.commit()
        second_rows = await storage.list_leaderboard(group)
        second = await storage.inspect_leaderboard(group)
    finally:
        await session.close()

    proofs_after = await _result_proof_rows(app_id, group_openid)
    assert proofs_after == proofs_before

    assert first.consistent is True, first.discrepancy_codes
    assert second.consistent is True, second.discrepancy_codes
    assert first.cached_total_wins == first.completed_total_wins == 1
    assert second.cached_total_wins == second.completed_total_wins == 1
    assert second.cached_entry_count == 1
    assert [row.wins for row in first_rows] == [1]
    assert second_rows == first_rows


async def test_inspect_flags_same_name_member_level_mismatch(
    db_scope: tuple[str, str, GroupRef],
) -> None:
    """同名不同成员、总 wins 相同时，只比 displayName / 总 sum 会漏判。

    两个 completed 证明的冠军显示名相同、成员不同、各 1 胜。互换这两名成员
    各自的 ``last_won_at`` 后，值集合与排序后的公共 entries 都不变，只有成员级
    比对才能报 ``consistent=False`` / ``last_won_at_mismatch``。
    """

    app_id, group_openid, group = db_scope
    await _seed_named_completed(
        group, names=SAME_NAME_GAME_1, member_openids=SAME_NAME_SEATS_1
    )
    await _seed_named_completed(
        group, names=SAME_NAME_GAME_2, member_openids=SAME_NAME_SEATS_2
    )

    baseline = await _inspect(group)
    assert baseline.consistent is True, baseline.discrepancy_codes
    assert baseline.cached_entry_count == baseline.completed_entry_count == 2
    assert baseline.cached_total_wins == baseline.completed_total_wins == 2
    assert sorted(entry.display_name for entry in baseline.entries) == [
        SAME_NAME,
        SAME_NAME,
    ]
    assert sorted(entry.wins for entry in baseline.entries) == [1, 1]

    winner_a = SAME_NAME_SEATS_1[2]
    winner_b = SAME_NAME_SEATS_2[2]
    cache_before = await _cache_rows(app_id, group_openid)
    times_before = {row[0]: row[3] for row in cache_before}
    assert set(times_before) == {winner_a, winner_b}
    # 先断言两名成员真实落库的时间不相等，交换才有可观测语义。
    assert times_before[winner_a] != times_before[winner_b]

    await _swap_cached_last_won_at(app_id, group_openid, winner_a, winner_b)

    cache_after = await _cache_rows(app_id, group_openid)
    times_after = {row[0]: row[3] for row in cache_after}
    assert times_after[winner_a] == times_before[winner_b]
    assert times_after[winner_b] == times_before[winner_a]
    # 只换成员映射：时间值集合、总 wins 与 completed 证明都不变。
    assert sorted(times_after.values()) == sorted(times_before.values())
    assert sum(row[2] for row in cache_after) == sum(row[2] for row in cache_before)
    assert sum(row[2] for row in cache_before) == 2

    corrupted = await _inspect(group)
    assert corrupted.consistent is False
    assert "last_won_at_mismatch" in corrupted.discrepancy_codes
    assert set(corrupted.discrepancy_codes) <= DISCREPANCY_CODES
    assert corrupted.cached_entry_count == corrupted.completed_entry_count == 2
    assert corrupted.cached_total_wins == corrupted.completed_total_wins == 2
    # 同名 + 值集合不变：排序后的公共 entries 与 baseline 完全相同。
    assert corrupted.entries == baseline.entries


async def test_inspect_reads_one_consistent_snapshot_while_a_projection_is_uncommitted(
    db_scope: tuple[str, str, GroupRef],
) -> None:
    """在途终局投影未提交时，``inspect`` 必须读到单一一致快照。

    复用真实 PG 暂停接缝（``_backend_pid`` / ``_wait_until_blocked``）：若
    ``inspect`` 与终局投影共享同一组锁则阻塞到提交后读取，否则只能读到已提交
    快照。两个分支都不允许 cache/proofs 计数或 wins 撕裂；这里不钉具体隔离级别，
    只冻结「读到的是一个一致快照」这一行为。
    """

    _app_id, _group_openid, group = db_scope
    await _seed_completed(group)

    projector = open_session()
    inspector = open_session()
    observer = open_session()
    inspect_task: asyncio.Task[Any] | None = None
    try:
        _snapshot, projection, _active = await _persist_completed(
            projector,
            group,
            names=WINNERS,
            member_openids=UNCOMMITTED_SEATS,
            project=False,
        )
        await PostgresRouletteStorage(projector).project_terminal(projection)
        projector_pid = await _backend_pid(projector)
        inspector_pid = await _backend_pid(inspector)

        inspect_task = asyncio.create_task(
            PostgresRouletteStorage(inspector).inspect_leaderboard(group)
        )
        done, _pending = await asyncio.wait({inspect_task}, timeout=0.5)
        if inspect_task in done:
            early = inspect_task.result()
            await projector.commit()
            late = await _inspect(group)
        else:
            blockers = await _wait_until_blocked(observer, inspector_pid)
            assert projector_pid in blockers
            await projector.commit()
            early = await asyncio.wait_for(inspect_task, timeout=5)
            late = await _inspect(group)
    finally:
        if inspect_task is not None and not inspect_task.done():
            inspect_task.cancel()
            with suppress(asyncio.CancelledError):
                await inspect_task
        with suppress(Exception):
            await projector.rollback()
        await observer.close()
        await inspector.close()
        await projector.close()

    for snapshot in (early, late):
        assert snapshot.consistent is True, snapshot.discrepancy_codes
        assert snapshot.discrepancy_codes == ()
        assert snapshot.cached_entry_count == snapshot.completed_entry_count
        assert snapshot.cached_total_wins == snapshot.completed_total_wins
    assert early.cached_entry_count in {1, 2}
    assert late.cached_entry_count == 2
    assert late.cached_total_wins == 2
