# ruff: noqa: RUF003  # ｜ ＝ × 等定稿文案字符
"""TSK-279 Stage-A: real-PostgreSQL freeze probes + new-config RED.

Green probes below drive the *existing* TSK-276 ``RouletteCommandService`` with
the *existing* TSK-278 ``render_reply`` projector, proving the real seed path
and the real terminal (winner / event / cumulative wins) path are healthy and
already escape markup display names.  RED cases lazily load the proposed
TSK-279 config schema / copy-pool / projector-factory seams, so a red run here
reports the specific missing seam instead of turning the whole file green-less.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy import text

from komari_bot.plugins.komari_roulette import RouletteCommandService
from komari_bot.plugins.komari_roulette.domain import ItemType
from komari_bot.plugins.komari_roulette.qq.renderer import render_reply

from .command_support import (
    PG_REQUIRED,
    command_factory,
    member_id,
    observation,
    request,
    seed_binding,
)
from .test_command_service import (
    CountingProjector,
    CountingRandom,
    create_waiting,
    current_game_row,
    join_player,
    seed_players,
    start_game,
)
from .tsk278_support import (
    MENTION_TAG_RE,
    assert_no_mention_tag,
    assert_single_mention_tag,
)
from .tsk279_support import (
    CONFIG_SCHEMA_MODULE,
    COPY_POOL_MODULE,
    RENDERER_MODULE,
    ScriptedCopyRandom,
    harness_fixture_body,
    load_symbol,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping

    from sqlalchemy.ext.asyncio import async_sessionmaker

    from .command_support import Scope

pytestmark = [pytest.mark.asyncio, PG_REQUIRED]

#: The copy random source and the domain random source must be independent.
INDEPENDENT_COPY_SENTENCE = "【279 脚本化动作文案】"


@pytest.fixture
async def harness() -> AsyncIterator[Any]:
    async for current in harness_fixture_body():
        yield current


def _projector() -> CountingProjector:
    return CountingProjector(metadata={"keyboard": '{"rows": []}'})


async def _persisted_item_weights(
    session_factory: async_sessionmaker[Any],
    current: Scope,
) -> dict[str, int]:
    async with session_factory() as session:
        raw = await session.scalar(
            text(
                "SELECT item_weights::text FROM komari_roulette_games "
                "WHERE app_id = :app_id AND group_openid = :group_openid "
                "ORDER BY created_at DESC LIMIT 1"
            ),
            {"app_id": current.app_id, "group_openid": current.group_openid},
        )
    assert raw is not None, "no persisted roulette game row"
    weights = json.loads(raw) if isinstance(raw, str) else dict(raw)
    return {str(key): int(value) for key, value in weights.items()}


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


def _expected_default_weights() -> dict[str, int]:
    return {item.value: 1 for item in ItemType}


# ---------------------------------------------------------------------------
# GREEN old-API probe 1: real seed path persists the frozen default weights
# ---------------------------------------------------------------------------


async def test_old_api_seed_freezes_default_weights(harness: Any) -> None:
    async with harness.scope("seed") as current:
        await seed_players(harness.binding_manager, current, 2)
        service = RouletteCommandService(
            session_factory=harness.session_factory,
            reply_projector=_projector(),
        )
        created = await create_waiting(service, current)
        assert created.result_code == "created"
        joined = await join_player(
            service, current, member_id(current, 2), "seed-join-2"
        )
        assert joined.result_code == "joined"
        started = await start_game(service, current, member_id(current, 1))
        assert started.result_code == "started"

        weights = await _persisted_item_weights(
            harness.session_factory, current
        )
        assert weights == _expected_default_weights()


# ---------------------------------------------------------------------------
# GREEN old-API probe 2: real terminal winner / event / wins, markup escaping
# ---------------------------------------------------------------------------


async def test_old_api_terminal_expresses_winner_event_and_cumulative_wins(
    harness: Any,
) -> None:
    async with harness.scope("endgame") as current:
        winner_openid = await seed_binding(
            harness.binding_manager, current, 2, name="小红"
        )
        await seed_binding(harness.binding_manager, current, 1, name="少年A")
        service = RouletteCommandService(
            session_factory=harness.session_factory,
            reply_projector=render_reply,
        )
        await create_waiting(service, current)
        await join_player(service, current, member_id(current, 2), "end-join-2")
        await start_game(service, current, member_id(current, 1))

        ended = await service.execute_group_command(
            request(
                current,
                "end-forfeit",
                command_factory("forfeit"),
                member_openid=member_id(current, 1),
            ),
            observation=await _game_observation(
                harness.session_factory, current
            ),
        )
        assert ended.result_code in {"forfeited", "completed"}
        body = ended.reply.body

        # 终局是普通单段：无标题、无分隔线、无花名册、无弹仓/道具/锁/爆发，
        # 也不带按钮（TSK-266 1F）。提及标签本身包含 ``/>``，比较前先移除。
        assert "\n" not in body
        stripped = MENTION_TAG_RE.sub("", body)
        for forbidden in ("> ", "***", "**", "- ", "剩余", "弹仓", "道具", "锁", "爆发"):
            assert forbidden not in stripped, (
                f"terminal body leaked {forbidden!r}: {stripped!r}"
            )
        # 唯一胜者 + 终局事件 + 结算后累计胜场。
        assert "小红" in body
        assert "弃权出局" in body
        assert "获胜" in body
        assert "累计胜场 1" in body
        assert "{" not in body and "}" not in body
        # 真实提及由渲染层注入，且只有一条。
        assert_single_mention_tag(body, winner_openid)


async def test_old_api_terminal_escapes_markup_display_name(
    harness: Any,
) -> None:
    async with harness.scope("escape") as current:
        markup_name = "红<&*>"
        escaped = "红&lt;&amp;\\*&gt;"
        winner_openid = await seed_binding(
            harness.binding_manager, current, 2, name=markup_name
        )
        await seed_binding(harness.binding_manager, current, 1, name="少年A")
        service = RouletteCommandService(
            session_factory=harness.session_factory,
            reply_projector=render_reply,
        )
        await create_waiting(service, current)
        await join_player(service, current, member_id(current, 2), "esc-join-2")
        await start_game(service, current, member_id(current, 1))

        ended = await service.execute_group_command(
            request(
                current,
                "esc-forfeit",
                command_factory("forfeit"),
                member_openid=member_id(current, 1),
            ),
            observation=await _game_observation(
                harness.session_factory, current
            ),
        )
        assert ended.result_code in {"forfeited", "completed"}
        body = ended.reply.body
        assert escaped in body
        assert markup_name not in body
        assert_single_mention_tag(body, winner_openid)


async def test_old_api_non_terminal_reply_has_no_extra_mention(
    harness: Any,
) -> None:
    """Role separation probe: the actor's own non-terminal turn is not @-ed."""

    async with harness.scope("mention") as current:
        members = await seed_players(harness.binding_manager, current, 2)
        service = RouletteCommandService(
            session_factory=harness.session_factory,
            reply_projector=render_reply,
        )
        await create_waiting(service, current)
        await join_player(service, current, members[1], "men-join-2")
        started = await start_game(service, current, members[0])
        assert started.result_code == "started"
        # The host holds the first turn: continuing in place must not mention.
        assert_no_mention_tag(started.reply.body)


# ---------------------------------------------------------------------------
# RED S4: weights are read once at waiting -> active from the config provider
# ---------------------------------------------------------------------------


async def test_config_weights_frozen_at_start_then_reloaded_for_next_game(
    harness: Any,
) -> None:
    schema_cls = load_symbol(CONFIG_SCHEMA_MODULE, "DynamicConfigSchema")
    first_config = schema_cls(
        item_weight_magnifier=2,
        item_weight_beer=1,
        item_weight_burst=1,
        item_weight_lock=1,
    )
    async with harness.scope("weights") as current:
        await seed_players(harness.binding_manager, current, 2)
        service = RouletteCommandService(
            session_factory=harness.session_factory,
            reply_projector=_projector(),
            item_weights_provider=first_config.item_weights,
        )
        await create_waiting(service, current)
        await join_player(service, current, member_id(current, 2), "w-join-2")
        started = await start_game(service, current, member_id(current, 1))
        assert started.result_code == "started"
        first = await _persisted_item_weights(harness.session_factory, current)
        assert first["magnifier"] == 2
        assert first["beer"] == 1

        # An updated config must not rewrite the already-active game's freeze.
        updated_config = schema_cls(
            item_weight_magnifier=5,
            item_weight_beer=1,
            item_weight_burst=1,
            item_weight_lock=1,
        )
        still_frozen = await _persisted_item_weights(
            harness.session_factory, current
        )
        assert still_frozen["magnifier"] == 2

        # End the first game, then a fresh game with the updated provider uses
        # the new weights.
        ended = await service.execute_group_command(
            request(
                current,
                "w-forfeit",
                command_factory("forfeit"),
                member_openid=member_id(current, 1),
            ),
            observation=await _game_observation(
                harness.session_factory, current
            ),
        )
        assert ended.result_code in {"forfeited", "completed"}

        refreshed = RouletteCommandService(
            session_factory=harness.session_factory,
            reply_projector=_projector(),
            item_weights_provider=updated_config.item_weights,
        )
        await create_waiting(refreshed, current, message_id="w2-create")
        await join_player(
            refreshed, current, member_id(current, 2), "w2-join-2"
        )
        await start_game(refreshed, current, member_id(current, 1), "w2-start")
        second = await _persisted_item_weights(harness.session_factory, current)
        assert second["magnifier"] == 5


# ---------------------------------------------------------------------------
# RED S2/S3: copy pool snapshot frozen into the receipt, isolated random
# ---------------------------------------------------------------------------


def _pool_with(
    pool_dump: Mapping[str, Any],
    *,
    key: str,
    templates: list[str],
) -> dict[str, Any]:
    rebuilt = {name: list(values) for name, values in pool_dump.items()}
    rebuilt[key] = templates
    return rebuilt


def _snapshot_with_scripted_created() -> Any:
    """Compile a legal snapshot whose ``created`` key offers the script copy.

    ``ScriptedCopyRandom`` forces a queued value and rejects any value outside
    the offered candidates, so the scripted sentence has to be a real member of
    a closed-key list.  Making it the *second* candidate also proves the pick
    is not merely ``options[0]``.
    """

    compile_copy_pool = load_symbol(COPY_POOL_MODULE, "compile_copy_pool")
    defaults = load_symbol(COPY_POOL_MODULE, "default_copy_snapshot")()
    action_pool = {
        name: list(values) for name, values in defaults.action_templates.items()
    }
    action_pool["created"] = ["默认创建文案：{name}。", INDEPENDENT_COPY_SENTENCE]
    return compile_copy_pool(
        action_copy_pool=action_pool,
        final_copy_pool={
            name: list(values) for name, values in defaults.final_templates.items()
        },
    )


async def test_copy_pool_and_receipt_freeze_independent_of_domain_random(
    harness: Any,
) -> None:
    build_projector = load_symbol(RENDERER_MODULE, "build_reply_projector")
    action_keys = set(load_symbol(COPY_POOL_MODULE, "ACTION_COPY_KEYS"))
    assert "created" in action_keys
    assert "started" in action_keys
    snapshot = _snapshot_with_scripted_created()
    created_candidates = tuple(snapshot.action_templates["created"])
    assert INDEPENDENT_COPY_SENTENCE in created_candidates
    assert created_candidates[0] != INDEPENDENT_COPY_SENTENCE

    copy_rng = ScriptedCopyRandom([INDEPENDENT_COPY_SENTENCE])
    projector = build_projector(snapshot=snapshot, random_source=copy_rng)
    domain_rng = CountingRandom()

    async with harness.scope("copy-freeze") as current:
        await seed_players(harness.binding_manager, current, 2)
        service = RouletteCommandService(
            session_factory=harness.session_factory,
            reply_projector=projector,
            random_source=domain_rng,
        )
        created = await create_waiting(service, current, message_id="copy-1")
        assert created.result_code == "created"
        # The copy source was offered exactly the compiled ``created`` list.
        assert copy_rng.calls[-1] == tuple(snapshot.action_templates["created"])
        assert copy_rng.returned[-1] == INDEPENDENT_COPY_SENTENCE
        first_body = created.reply.body
        draws_after_first = copy_rng.draw_count

        # The domain random source is untouched by copy rendering.
        assert domain_rng.chamber_calls == 0
        assert domain_rng.item_calls == 0

        # Update the pool + restart the service: replaying the same inbound
        # message must return the frozen receipt body without re-drawing.
        updated_action_pool = _pool_with(
            dict(snapshot.action_templates),
            key="created",
            templates=["更新后的创建文案：{name}。"],
        )
        compile_copy_pool = load_symbol(COPY_POOL_MODULE, "compile_copy_pool")
        final_defaults = {
            k: list(v) for k, v in snapshot.final_templates.items()
        }
        updated_snapshot = compile_copy_pool(
            action_copy_pool=updated_action_pool,
            final_copy_pool=final_defaults,
        )
        other_rng = ScriptedCopyRandom(["更新后的创建文案：{name}。"])
        restarted = RouletteCommandService(
            session_factory=harness.session_factory,
            reply_projector=build_projector(
                snapshot=updated_snapshot, random_source=other_rng
            ),
            random_source=CountingRandom(),
        )
        replay = await restarted.execute_group_command(
            request(
                current,
                "copy-1",
                command_factory("create"),
            )
        )
        assert replay.reply.body == first_body
        assert copy_rng.draw_count == draws_after_first
        assert other_rng.calls == []


async def test_copy_random_source_is_not_the_domain_random_source(
    harness: Any,
) -> None:
    """A projector built with the copy source must never fall back to domain.

    ``CountingRandom`` has no ``choice`` method, so a domain-source fallback
    would raise ``AttributeError``; the copy source is the only option.
    """

    build_projector = load_symbol(RENDERER_MODULE, "build_reply_projector")
    snapshot = _snapshot_with_scripted_created()
    copy_rng = ScriptedCopyRandom([INDEPENDENT_COPY_SENTENCE])
    domain_rng = CountingRandom()
    assert not hasattr(domain_rng, "choice")
    assert (
        INDEPENDENT_COPY_SENTENCE in snapshot.action_templates["created"]
    ), "scripted copy must be a legal closed-key candidate"

    async with harness.scope("copy-source") as current:
        await seed_players(harness.binding_manager, current, 2)
        service = RouletteCommandService(
            session_factory=harness.session_factory,
            reply_projector=build_projector(
                snapshot=snapshot, random_source=copy_rng
            ),
            random_source=domain_rng,
        )
        created = await create_waiting(service, current)
        assert created.result_code == "created"
        joined = await join_player(
            service, current, member_id(current, 2), "copy-source-join-2"
        )
        assert joined.result_code == "joined"
        started = await start_game(service, current, member_id(current, 1))
        assert started.result_code == "started"
        # start needed the domain chamber draw AND a copy pick; the two
        # sources advanced independently.
        assert domain_rng.chamber_calls == 1
        assert copy_rng.draw_count >= 3
        assert copy_rng.calls[-1] == tuple(snapshot.action_templates["started"])
