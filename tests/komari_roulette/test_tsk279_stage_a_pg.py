# ruff: noqa: RUF003  # ｜ ＝ × 等定稿文案字符
"""TSK-279 Stage-A runtime acceptance on real PostgreSQL.

These cases persist the Stage-A behaviours the earlier RED baseline pinned,
driving the **real** service / domain / renderer / typed config manager instead
of a hand-made stand-in context:

* the typed config manager initializes ``komari_roulette_config`` from Pydantic
  defaults (JSONB round-trip), persists a legal field update, and rejects an
  illegal pool without touching the persisted row;
* the three closed terminal keys (``shot`` / ``forfeit`` / ``timeout``) are
  selected from the *real* domain outcome, and the frozen receipt is replayed
  without re-drawing the copy;
* the legal action branches (created / joined / host transfer / shoot / item)
  draw from their closed keys, while the fixed waiting-end copies stay fixed
  when the configurable pool changes;
* a native mention / Markdown injection is rejected by the typed config, and a
  real markup display name is escaped.

Gated on ``KOMARI_TEST_POSTGRES_URL``; every case cleans its own rows.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, cast

import pytest
from pydantic import ValidationError
from sqlalchemy import text

from komari_bot.plugins.config_manager.manager import ConfigManager
from komari_bot.plugins.komari_roulette import RouletteCommandService
from komari_bot.plugins.komari_roulette.config_schema import DynamicConfigSchema
from komari_bot.plugins.komari_roulette.copy_pool import (
    DEFAULT_ACTION_COPY_POOL,
    DEFAULT_FINAL_COPY_POOL,
    compile_copy_pool,
)
from komari_bot.plugins.komari_roulette.domain import ChamberKind, ItemType
from komari_bot.plugins.komari_roulette.qq.renderer import build_reply_projector

from .command_support import (
    PG_REQUIRED,
    command_factory,
    member_id,
    observation,
    request,
    seed_binding,
)
from .test_command_service import (
    CountingRandom,
    create_waiting,
    current_game_row,
    join_player,
    seed_players,
    start_game,
)
from .tsk278_support import assert_single_mention_tag
from .tsk279_support import ScriptedCopyRandom, harness_fixture_body

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping

    from sqlalchemy.ext.asyncio import async_sessionmaker

    from .command_support import Scope

pytestmark = [pytest.mark.asyncio, PG_REQUIRED]

ROULETTE_CONFIG_TABLE = "komari_roulette_config"

#: Distinguishable, legal templates per closed action key.
ACTION_MARKERS: dict[str, str] = {
    "created": "[279-created]{name}",
    "joined": "[279-joined]{name}",
    "host_transferred": "[279-transferred]{name}",
    "shoot": "[279-shoot]{name}-{kind}",
    "lock_used": "[279-lock]{actor}对{target}{tag}",
}

#: Distinguishable, legal templates per closed terminal key.
FINAL_MARKERS: dict[str, str] = {
    "shot": "[279-final-shot]{event}{winner} 获胜，累计胜场 {wins}。",
    "forfeit": "[279-final-forfeit]{event}{winner} 获胜，累计胜场 {wins}。",
    "timeout": "[279-final-timeout]{event}{winner} 获胜，累计胜场 {wins}。",
}

#: Real terminal event copy (renderer ``_ELIMINATED_REASON_CN``).
EVENT_TEXT = {
    "shot": "打出实弹出局",
    "forfeit": "弃权出局",
    "timeout": "超时出局",
}


@pytest.fixture
async def harness() -> AsyncIterator[Any]:
    async for current in harness_fixture_body():
        yield current


def _snapshot(
    *,
    action_markers: Mapping[str, str] | None = None,
    final_markers: Mapping[str, str] | None = None,
) -> Any:
    """Compile a full, legal snapshot from the code defaults plus overrides."""

    action_pool = {
        key: [template]
        for key, template in (
            ACTION_MARKERS if action_markers is None else action_markers
        ).items()
    }
    for key, templates in DEFAULT_ACTION_COPY_POOL.items():
        action_pool.setdefault(key, list(templates))
    final_pool = {
        key: [template]
        for key, template in (
            FINAL_MARKERS if final_markers is None else final_markers
        ).items()
    }
    for key, templates in DEFAULT_FINAL_COPY_POOL.items():
        final_pool.setdefault(key, list(templates))
    return compile_copy_pool(action_copy_pool=action_pool, final_copy_pool=final_pool)


def _projector(snapshot: Any, rng: Any | None = None) -> Any:
    return build_reply_projector(
        snapshot=snapshot, random_source=rng or ScriptedCopyRandom()
    )


class LiveFirstRandom:
    """Deterministic domain random source whose first chamber round is live."""

    def __init__(self) -> None:
        self.chamber_calls: list[tuple[int, int]] = []
        self.item_calls: list[Mapping[ItemType, int]] = []

    def chamber_order(
        self,
        live_count: int,
        blank_count: int,
    ) -> tuple[ChamberKind, ...]:
        self.chamber_calls.append((live_count, blank_count))
        return (
            (ChamberKind.LIVE,)
            + (ChamberKind.BLANK,) * blank_count
            + (ChamberKind.LIVE,) * (live_count - 1)
        )

    def weighted_item(self, weights: Mapping[ItemType, int]) -> ItemType:
        self.item_calls.append(dict(weights))
        return ItemType.BEER


async def _game_observation(
    session_factory: async_sessionmaker[Any],
    current: Scope,
) -> Any:
    row = await current_game_row(session_factory, current)
    assert row is not None
    return observation(
        game_id=str(row["game_id"]),
        state_revision=int(row["state_revision"]),
        turn_seq=int(row["turn_seq"]),
    )


async def _expire_turn(
    session_factory: async_sessionmaker[Any],
    current: Scope,
) -> None:
    row = await current_game_row(session_factory, current)
    assert row is not None
    async with session_factory() as session:
        await session.execute(
            text(
                "UPDATE komari_roulette_games "
                "SET turn_deadline_at = NOW() - INTERVAL '1 minute' "
                "WHERE game_id = :game_id"
            ),
            {"game_id": str(row["game_id"])},
        )
        await session.commit()


async def _start_marked_game(
    harness: Any,
    current: Scope,
    members: tuple[str, ...],
    *,
    random_source: Any | None = None,
) -> RouletteCommandService:
    service = RouletteCommandService(
        session_factory=harness.session_factory,
        reply_projector=_projector(_snapshot()),
        random_source=random_source or CountingRandom(),
    )
    await create_waiting(service, current)
    for number, member in enumerate(members[1:], start=2):
        await join_player(service, current, member, f"mark-join-{number}")
    await start_game(service, current, members[0], "mark-start")
    return service


# ---------------------------------------------------------------------------
# Typed config manager (real PostgreSQL)
# ---------------------------------------------------------------------------


async def _delete_config_row(harness: Any) -> None:
    async with harness.engine.begin() as connection:
        await connection.execute(
            text(f"DELETE FROM {ROULETTE_CONFIG_TABLE} WHERE id = 1")
        )


async def _read_config_row(harness: Any) -> Mapping[str, Any] | None:
    async with harness.session_factory() as session:
        result = await session.execute(
            text(
                "SELECT revision, plugin_enable, item_weight_magnifier,"
                " item_weight_beer, item_weight_burst, item_weight_lock,"
                " action_copy_pool::text AS action_copy_pool,"
                " final_copy_pool::text AS final_copy_pool"
                f" FROM {ROULETTE_CONFIG_TABLE} WHERE id = 1"
            )
        )
        return result.mappings().first()


async def test_config_manager_initializes_jsonb_defaults_and_field_update(
    harness: Any,
) -> None:
    await _delete_config_row(harness)
    manager = ConfigManager("komari_roulette", DynamicConfigSchema)
    try:
        config = cast("DynamicConfigSchema", await manager.initialize_async())
        assert config.plugin_enable is False
        assert config.item_weights() == {
            ItemType.MAGNIFIER: 1,
            ItemType.BEER: 1,
            ItemType.BURST: 1,
            ItemType.LOCK: 1,
        }

        row = await _read_config_row(harness)
        assert row is not None
        assert int(row["revision"]) == 1
        assert row["plugin_enable"] is False
        # JSONB default writeback: the persisted pools equal the code defaults.
        assert json.loads(str(row["action_copy_pool"])) == {
            key: list(templates)
            for key, templates in DEFAULT_ACTION_COPY_POOL.items()
        }
        assert json.loads(str(row["final_copy_pool"])) == {
            key: list(templates)
            for key, templates in DEFAULT_FINAL_COPY_POOL.items()
        }

        updated = cast(
            "DynamicConfigSchema",
            await manager.update_field_async("item_weight_magnifier", 3),
        )
        assert updated.item_weights()[ItemType.MAGNIFIER] == 3
        after = await _read_config_row(harness)
        assert after is not None
        assert int(after["item_weight_magnifier"]) == 3
        assert int(after["revision"]) == 2
    finally:
        await _delete_config_row(harness)


async def test_config_manager_illegal_pool_leaves_persisted_row_unchanged(
    harness: Any,
) -> None:
    await _delete_config_row(harness)
    manager = ConfigManager("komari_roulette", DynamicConfigSchema)
    try:
        await manager.initialize_async()
        before = await _read_config_row(harness)
        assert before is not None

        illegal_mutators = (
            lambda pool: {**pool, "not_a_success_action": ["x"]},
            lambda pool: {**pool, "created": []},
            lambda pool: {**pool, "created": ['<qqbot-at-user id="m" />']},
        )
        for mutator in illegal_mutators:
            with pytest.raises((ValidationError, ValueError)):
                await manager.mutate_field_async("action_copy_pool", mutator)

        with pytest.raises((ValidationError, ValueError)):
            await manager.update_field_async("action_copy_pool", {"bad": ["x"]})

        after = await _read_config_row(harness)
        # CAS revision and the JSONB value must be byte-for-byte untouched.
        assert after == before
    finally:
        await _delete_config_row(harness)


# ---------------------------------------------------------------------------
# Three closed terminal keys, selected from the real domain outcome
# ---------------------------------------------------------------------------


async def test_final_branch_forfeit_uses_forfeit_key_and_frozen_replay(
    harness: Any,
) -> None:
    async with harness.scope("final-forfeit") as current:
        winner_openid = await seed_binding(
            harness.binding_manager, current, 2, name="小红"
        )
        await seed_binding(harness.binding_manager, current, 1, name="少年A")
        copy_rng = ScriptedCopyRandom()
        service = RouletteCommandService(
            session_factory=harness.session_factory,
            reply_projector=_projector(_snapshot(), copy_rng),
            random_source=CountingRandom(),
        )
        await create_waiting(service, current)
        await join_player(service, current, member_id(current, 2), "ff-join-2")
        await start_game(service, current, member_id(current, 1), "ff-start")

        ended = await service.execute_group_command(
            request(
                current,
                "ff-forfeit",
                command_factory("forfeit"),
                member_openid=member_id(current, 1),
            ),
            observation=await _game_observation(harness.session_factory, current),
        )
        assert ended.result_code == "forfeited"
        body = ended.reply.body
        assert body.startswith("[279-final-forfeit]"), body
        assert f"少年A{EVENT_TEXT['forfeit']}，" in body
        assert "小红" in body
        assert "累计胜场 1" in body
        assert "{" not in body and "}" not in body
        assert_single_mention_tag(body, winner_openid)
        draws_after = copy_rng.draw_count

        # Replay the same inbound message with a *different* snapshot/projector:
        # the frozen receipt body is returned and nothing is re-drawn.
        replay_rng = ScriptedCopyRandom()
        restarted = RouletteCommandService(
            session_factory=harness.session_factory,
            reply_projector=_projector(
                _snapshot(
                    final_markers={
                        "shot": "[alt-shot]{event}{winner} {wins}。",
                        "forfeit": "[alt-forfeit]{event}{winner} {wins}。",
                        "timeout": "[alt-timeout]{event}{winner} {wins}。",
                    }
                ),
                replay_rng,
            ),
            random_source=CountingRandom(),
        )
        replay = await restarted.execute_group_command(
            request(
                current,
                "ff-forfeit",
                command_factory("forfeit"),
                member_openid=member_id(current, 1),
            )
        )
        assert replay.reply.body == body
        assert copy_rng.draw_count == draws_after
        assert replay_rng.calls == []


async def test_final_branch_shot_uses_shot_key(harness: Any) -> None:
    async with harness.scope("final-shot") as current:
        winner_openid = await seed_binding(
            harness.binding_manager, current, 2, name="小红"
        )
        await seed_binding(harness.binding_manager, current, 1, name="少年A")
        service = await _start_marked_game(
            harness,
            current,
            (member_id(current, 1), member_id(current, 2)),
            random_source=LiveFirstRandom(),
        )
        ended = await service.execute_group_command(
            request(
                current,
                "fs-shoot",
                command_factory("shoot"),
                member_openid=member_id(current, 1),
            ),
            observation=await _game_observation(harness.session_factory, current),
        )
        assert ended.result_code == "shot"
        body = ended.reply.body
        assert body.startswith("[279-final-shot]"), body
        assert f"少年A{EVENT_TEXT['shot']}，" in body
        assert "小红" in body
        assert "累计胜场 1" in body
        assert "{" not in body and "}" not in body
        assert_single_mention_tag(body, winner_openid)


async def test_final_branch_timeout_uses_timeout_key(harness: Any) -> None:
    async with harness.scope("final-timeout") as current:
        winner_openid = await seed_binding(
            harness.binding_manager, current, 2, name="小红"
        )
        await seed_binding(harness.binding_manager, current, 1, name="少年A")
        service = await _start_marked_game(
            harness,
            current,
            (member_id(current, 1), member_id(current, 2)),
        )
        await _expire_turn(harness.session_factory, current)
        ended = await service.execute_group_command(
            request(
                current,
                "ft-end-turn",
                command_factory("end_turn"),
                member_openid=member_id(current, 1),
            ),
            observation=await _game_observation(harness.session_factory, current),
        )
        assert ended.result_code == "turn_expired"
        body = ended.reply.body
        assert body.startswith("你的行动时间已经结束，本次命令未执行。\n\n[279-final-timeout]"), (
            body
        )
        assert f"少年A{EVENT_TEXT['timeout']}，" in body
        assert "小红" in body
        assert "累计胜场 1" in body
        assert "{" not in body and "}" not in body
        assert_single_mention_tag(body, winner_openid)


# ---------------------------------------------------------------------------
# Legal action branches + fixed waiting-end copies
# ---------------------------------------------------------------------------


async def test_legal_action_branches_use_closed_keys(harness: Any) -> None:
    async with harness.scope("action-branches") as current:
        await seed_binding(harness.binding_manager, current, 1, name="甲")
        await seed_binding(harness.binding_manager, current, 2, name="乙")
        service = RouletteCommandService(
            session_factory=harness.session_factory,
            reply_projector=_projector(_snapshot()),
            random_source=CountingRandom(),
        )
        created = await create_waiting(service, current, message_id="ab-create")
        assert created.result_code == "created"
        assert "> [279-created]甲" in created.reply.body

        joined = await join_player(service, current, member_id(current, 2), "ab-join-2")
        assert joined.result_code == "joined"
        assert "> [279-joined]乙" in joined.reply.body

        transferred = await service.execute_group_command(
            request(
                current,
                "ab-transfer",
                command_factory("transfer", target_player_seq=2),
                member_openid=member_id(current, 1),
            )
        )
        assert transferred.result_code == "host_transferred"
        assert "> [279-transferred]乙" in transferred.reply.body

        # The transferred host (seat 2) starts; seat 1 still holds the first turn.
        started = await start_game(service, current, member_id(current, 2), "ab-start")
        assert started.result_code == "started"

        row = await current_game_row(harness.session_factory, current)
        assert row is not None
        async with harness.session_factory() as session:
            await session.execute(
                text(
                    "UPDATE komari_roulette_players SET lock_count = 1 "
                    "WHERE game_id = :game_id AND join_seq = 1"
                ),
                {"game_id": str(row["game_id"])},
            )
            await session.commit()

        locked = await service.execute_group_command(
            request(
                current,
                "ab-lock",
                command_factory("use_item", item=ItemType.LOCK, target_player_seq=2),
                member_openid=member_id(current, 1),
            ),
            observation=await _game_observation(harness.session_factory, current),
        )
        assert locked.result_code == "item_used"
        assert "[279-lock]甲对乙" in locked.reply.body
        assert_single_mention_tag(locked.reply.body, member_id(current, 2))

        shot = await service.execute_group_command(
            request(
                current,
                "ab-shoot",
                command_factory("shoot"),
                member_openid=member_id(current, 1),
            ),
            observation=await _game_observation(harness.session_factory, current),
        )
        assert shot.result_code == "shot"
        assert "> [279-shoot]甲-空弹" in shot.reply.body


async def test_fixed_waiting_end_copies_ignore_pool_changes(harness: Any) -> None:
    # Replacing every configurable action key with a marker must not change the
    # fixed host-cancel / last-leave / waiting-expiry copy.
    service_pool = _projector(_snapshot())

    async with harness.scope("fixed-cancel") as current:
        members = await seed_players(harness.binding_manager, current, 2)
        service = RouletteCommandService(
            session_factory=harness.session_factory,
            reply_projector=service_pool,
            random_source=CountingRandom(),
        )
        await create_waiting(service, current)
        await join_player(service, current, members[1], "fc-join-2")
        cancelled = await service.execute_group_command(
            request(
                current,
                "fc-cancel",
                command_factory("cancel"),
                member_openid=members[0],
            )
        )
        assert cancelled.result_code == "cancelled"
        assert cancelled.reply.body == "Seat 1取消了这局游戏，等候中的玩家已经全部离席。"
        assert "[279-" not in cancelled.reply.body

    async with harness.scope("fixed-lastleave") as current:
        members = await seed_players(harness.binding_manager, current, 1)
        service = RouletteCommandService(
            session_factory=harness.session_factory,
            reply_projector=service_pool,
            random_source=CountingRandom(),
        )
        await create_waiting(service, current)
        left = await service.execute_group_command(
            request(
                current,
                "fl-leave",
                command_factory("leave"),
                member_openid=members[0],
            )
        )
        assert left.result_code == "cancelled"
        assert left.reply.body == "Seat 1离开后，等候局已自动结束。"
        assert "[279-" not in left.reply.body

    async with harness.scope("fixed-expiry") as current:
        members = await seed_players(harness.binding_manager, current, 2)
        service = RouletteCommandService(
            session_factory=harness.session_factory,
            reply_projector=service_pool,
            random_source=CountingRandom(),
        )
        await create_waiting(service, current)
        row = await current_game_row(harness.session_factory, current)
        assert row is not None
        async with harness.session_factory() as session:
            await session.execute(
                text(
                    "UPDATE komari_roulette_games "
                    "SET waiting_expires_at = clock_timestamp() - interval '1 second' "
                    "WHERE game_id = :game_id"
                ),
                {"game_id": str(row["game_id"])},
            )
            await session.commit()
        expired = await service.execute_group_command(
            request(
                current,
                "fe-join-2",
                command_factory("join"),
                member_openid=members[1],
            )
        )
        assert expired.result_code == "waiting_game_expired"
        assert expired.reply.body == "这局游戏等待太久仍未开始，现已自动结束。"
        assert "[279-" not in expired.reply.body


async def test_action_sentence_escapes_markup_display_name(harness: Any) -> None:
    async with harness.scope("action-escape") as current:
        await seed_binding(harness.binding_manager, current, 1, name="红<&*>")
        markup_name = "红<&*>"
        escaped = "红&lt;&amp;\\*&gt;"
        service = RouletteCommandService(
            session_factory=harness.session_factory,
            reply_projector=_projector(_snapshot()),
            random_source=CountingRandom(),
        )
        created = await create_waiting(service, current, message_id="ae-create")
        assert created.result_code == "created"
        body = created.reply.body
        assert "[279-created]" in body
        assert escaped in body
        assert markup_name not in body
