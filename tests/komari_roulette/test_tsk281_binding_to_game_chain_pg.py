# ruff: noqa: RUF001, RUF003  # ｜ 是定稿排行榜文案字符
"""TSK-281 阶段 B1c：真实绑定链 → 真实弃权终局 + 排行榜 + 送达 UNKNOWN。

同一次用例、同一个 App、同一个物理群里贯通两个**真实**生产装配：

1. ``character_binding`` —— 真实包导入（真实 OneBot 证据 matcher + 真实
   ``/bind`` matcher）与真实 ``init_plugin``（``QQBindingCoordinator`` /
   ``BindingWizard`` / ``CharacterBindingManager``）；两名不同用户经真实 QQ
   向导完成绑定，OneBot 证据经真实 ``get_msg`` 采集与核验。
2. ``komari_roulette`` —— 真实组合根（``lifecycle`` 重载 + 真实 driver
   startup hook）在真实 binding / admission / PG 之上装配，并安装真实 QQ
   runtime。

随后两条 ``/轮盘 开局`` / ``/轮盘 加入`` / ``/轮盘 开始`` 经真实准入事件门
禁（``event_gate_context`` + ``prepare_control_plane`` 的受限白名单）与真实
QQ matcher 执行，断言真实 PG ``waiting → active``、房主 / 两名成员 / 冻结
角色名，以及三条命令各自唯一 receipt 与各自恰一次合法 QQ 发送。

链前强断言：群尚未 canonical 时同一条 ``/轮盘 开局`` 必须被门禁拒绝（无
receipt、无 game、无 QQ 发送）；而在真实向导绑定后同一条命令成立，证明拒绝
来自缺失群映射而不是缺失处理器。

QQ 向导自身会产生合法额外发送（每席位 4 次，共 8 次）；用例显式区分它们与
三条游戏命令的 3 次发送，绝不把整链总数当作 3。

替换缝（完整列出，不是只有前两类）：

* 平台传输：OneBot ``get_msg`` 受控载荷 + QQ 记录 bot；真实 matcher /
  delivery / adapter 消息构建仍在运行；
* 受限准入策略存储：``AdmissionStorageFake`` 只白名单本用例数字群，例行的
  recovery 扫描不会碰到外来 scope；
* ``user_ban``：``is_configured_superuser_id`` / ``is_user_banned`` 换成恒不
  封禁替身，真实 ``komari_user_bans`` 表与 revision 缓存**未被**本链执行；
* 调度器：``nonebot_plugin_apscheduler`` 单例换成 ``FakeScheduler``，真实
  maintenance job 被记录且可无睡眠调用；
* config manager 获取：顶层 ``get_config_manager`` getter 被包装以记录调用并
  按插件名复用同一个**真实** ``ConfigManager``（注册表形状的获取替身，不是替
  换配置存储）。

准入 runtime、canonical 绑定 manager / transaction、命令服务、maintenance、
roulette runtime 均为真实生产对象。环境里的 Redis URL 仅为配置齐备，本链不
声称覆盖任何 Redis 调用。
"""

from __future__ import annotations

import sys
from typing import Any

import pytest

from komari_bot.plugins.komari_roulette.domain import ChamberKind, ItemType
from tests.character_binding.conftest import require_postgres
from tests.character_binding.tsk277_support import CANCELLED, EXPIRED, markdown_content
from tests.character_binding.tsk281_native_chain_support import committed_rows
from tests.komari_roulette.command_support import PG_REQUIRED
from tests.komari_roulette.support import ScriptedRandomSource
from tests.komari_roulette.tsk278_support import (
    assert_body_has_markdown_structure,
    assert_no_member_openid,
    assert_single_mention_tag,
)
from tests.komari_roulette.tsk279_lifecycle_support import (
    LIFECYCLE_MODULE,
    QQ_MODULE,
)
from tests.komari_roulette.tsk281_chain_support import (
    CMD_BIND,
    CMD_CANCEL,
    CMD_CREATE,
    CMD_FORFEIT,
    CMD_JOIN,
    CMD_LEADERBOARD,
    CMD_START,
    Chain,
    chain_context,
)

pytestmark = [pytest.mark.asyncio, PG_REQUIRED]

_CHARACTER_NAMES = {1: "阿明", 2: "小夜"}

#: QQ 向导每席位的合法发送数（挑战 / 继续 / 确认 / 成功）。
_WIZARD_SENDS_PER_SEAT = 4

#: 弃权发起者（开局房主，开局后必定是当前玩家）与其唯一胜者。
_FORFEITER_SEAT = 1
_WINNER_SEAT = 2

#: 真实改名后的新角色名（与两个旧名互不为子串）。
_RENAMED_NAME = "阿凛"


async def _bind_two_and_start(chain: Chain) -> None:
    """共享真实前缀：两名用户真实绑定 → 真实开局 / 加入 / 开始。"""

    for seat, name in _CHARACTER_NAMES.items():
        await chain.bind(seat, name)
    for seat, command, tag in (
        (1, CMD_CREATE, "game-create"),
        (2, CMD_JOIN, "game-join"),
        (1, CMD_START, "game-start"),
    ):
        await chain.send_qq(
            command,
            member_openid=chain.member(seat).member_openid,
            message_id=chain.next_msg_id(tag),
        )
    games = await chain.game_rows()
    assert len(games) == 1 and games[0]["lifecycle"] == "active", games


async def _canonical_names(chain: Chain) -> dict[str, str | None]:
    """PG canonical 角色名（按 member_openid；解绑后对应值为 None）。"""

    _groups, members = await committed_rows(chain.harness.engine, chain.scope)
    return {row["member_openid"]: row["character_name"] for row in members}


async def _frozen_names(chain: Chain) -> dict[str, str]:
    """对局内冻结的角色名（按 member_openid）。"""

    players = await chain.player_rows()
    return {row["member_openid"]: row["display_name"] for row in players}


def _roulette_state() -> dict[str, Any]:
    """链前轮盘包 / 模块 / QQ runtime / 调度器的身份快照。"""

    import komari_bot.plugins.komari_roulette as package

    apscheduler_mod = sys.modules.get("nonebot_plugin_apscheduler")
    return {
        "package": package,
        "package_dict_names": sorted(package.__dict__),
        "lifecycle_attr": package.lifecycle,
        "qq_attr": package.qq,
        "get_roulette_observation": package.get_roulette_observation,
        "lifecycle_module": sys.modules[LIFECYCLE_MODULE],
        "qq_module": sys.modules[QQ_MODULE],
        "qq_runtime": package.qq.get_roulette_qq_runtime(),
        "scheduler_module": apscheduler_mod,
        "scheduler": getattr(apscheduler_mod, "scheduler", None),
    }


async def test_two_users_bind_then_play_through_real_qq_roulette_matcher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真实双绑定 → 真实 QQ 轮盘开局链，两条链共享同一 App / 群。"""
    require_postgres()

    async with chain_context(monkeypatch, seat_names=_CHARACTER_NAMES) as chain:
        # ---- 链前强断言：未 canonical 的群不得开局 -------------------------
        await chain.assert_unbound_group_cannot_start()

        # ---- 真实 QQ 向导完成两名用户的绑定 -------------------------------
        for seat, name in _CHARACTER_NAMES.items():
            await chain.bind(seat, name)

        # 向导自身的合法发送（每席位 4 次）必须与三条游戏命令的 3 次发送分账。
        assert len(chain.qq.calls) == _WIZARD_SENDS_PER_SEAT * len(_CHARACTER_NAMES), (
            "两名用户的真实 /bind 向导应各产生 4 次合法 QQ 发送，"
            f"整链合计 {len(chain.qq.calls)}"
        )

        # ---- 真实 PG canonical 绑定行 -------------------------------------
        groups, members = await committed_rows(chain.harness.engine, chain.scope)
        assert len(groups) == 1
        assert groups[0]["group_id"] == str(chain.scope.group_id)
        assert len(members) == 2
        binding_names = {row["member_openid"]: row["character_name"] for row in members}
        assert binding_names == {
            chain.member(1).member_openid: _CHARACTER_NAMES[1],
            chain.member(2).member_openid: _CHARACTER_NAMES[2],
        }

        # ---- 三条真实 QQ 命令，各自唯一 inbound 消息 id --------------------
        command_specs = (
            (1, CMD_CREATE, "game-create"),
            (2, CMD_JOIN, "game-join"),
            (1, CMD_START, "game-start"),
        )
        game_message_ids: list[str] = []
        for index, (seat, command, tag) in enumerate(command_specs):
            message_id = chain.next_msg_id(tag)
            game_message_ids.append(message_id)
            before = len(chain.qq.calls)
            await chain.send_qq(
                command,
                member_openid=chain.member(seat).member_openid,
                message_id=message_id,
            )
            sent = chain.qq.calls[before:]
            # 每条游戏命令恰一次合法 QQ 发送（消息 id 即平台幂等键）。
            assert len(sent) == 1, f"{command} 必须恰一次 QQ 发送，实际 {sent!r}"
            api, payload = sent[0]
            assert api == "post_group_messages", api
            assert payload["msg_id"] == message_id
            assert payload["msg_seq"] == 1

            # 真实 PG 逐步推进：开局/加入后 waiting，开始后 active。
            receipts_so_far = await chain.receipt_rows()
            assert len(receipts_so_far) == index + 1, (
                f"{command} 后应恰有 {index + 1} 条 receipt，实际 {receipts_so_far!r}"
            )
            games_so_far = await chain.game_rows()
            assert len(games_so_far) == 1, "同一群同一时间只允许一场当前对局"
            if index < 2:
                assert games_so_far[0]["lifecycle"] == "waiting"
            players_so_far = await chain.player_rows()
            assert [row["join_seq"] for row in players_so_far] == list(
                range(1, len(players_so_far) + 1)
            )
            assert len(players_so_far) == (1 if index == 0 else 2)

        # ---- 真实 PG：waiting → active、房主、两成员、冻结角色名 -----------
        receipts = await chain.receipt_rows()
        assert [row["result_code"] for row in receipts] == [
            "created",
            "joined",
            "started",
        ]
        assert len({row["receipt_id"] for row in receipts}) == 3, "receipt 必须唯一"
        assert [row["inbound_msg_id"] for row in receipts] == game_message_ids, (
            "三条命令的 receipt 必须各自绑定本次唯一 inbound 消息 id"
        )

        games = await chain.game_rows()
        assert len(games) == 1, "同一群只允许一场当前对局"
        game = games[0]
        assert game["lifecycle"] == "active", "真实启动后 lifecycle 必须推进到 active"
        assert game["phase"] == "first_shot"
        assert game["host_seq"] == 1, "开局者必须是房主"

        players = await chain.player_rows()
        assert len(players) == 2, "房主 + 一名加入者"
        assert [row["join_seq"] for row in players] == [1, 2]
        frozen_names = {row["member_openid"]: row["display_name"] for row in players}
        assert frozen_names == binding_names, (
            "对局内冻结的角色名必须来自真实 canonical 绑定"
        )

        # ---- 整链：8 次向导发送 + 3 次游戏发送，绝不可混淆 -----------------
        assert len(chain.qq.calls) == _WIZARD_SENDS_PER_SEAT * len(_CHARACTER_NAMES) + 3
        game_apis = [api for api, _payload in chain.qq.calls[-3:]]
        assert game_apis == ["post_group_messages"] * 3


async def test_forfeit_reaches_endgame_and_leaderboard_same_fact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真实弃权终局：PG 结果 / 结果玩家 / 胜场账本 / 排行榜与终局载荷同一事实。"""
    require_postgres()

    async with chain_context(monkeypatch, seat_names=_CHARACTER_NAMES) as chain:
        await _bind_two_and_start(chain)

        forfeit_id = chain.next_msg_id("game-forfeit")
        sent = await chain.send_qq(
            CMD_FORFEIT,
            member_openid=chain.member(_FORFEITER_SEAT).member_openid,
            message_id=forfeit_id,
        )
        assert len(sent) == 1, sent
        api, payload = sent[0]
        assert api == "post_group_messages", api
        assert payload["msg_id"] == forfeit_id
        assert payload["msg_seq"] == 1
        assert payload["msg_type"] == 2, "终局正文必须是 markdown 载荷"

        # ---- 真实 PG：对局已终结，结果 / 结果玩家 / 胜场账本同一事实 ------
        games = await chain.game_rows()
        assert len(games) == 1
        assert games[0]["lifecycle"] == "completed"

        results = await chain.result_rows()
        assert len(results) == 1, results
        result = results[0]
        assert result["lifecycle"] == "completed"
        assert result["reason"] == "forfeit"
        assert result["winner_seq"] == _WINNER_SEAT
        assert (
            result["winner_member_openid"] == chain.member(_WINNER_SEAT).member_openid
        )
        assert result["winner_display_name"] == _CHARACTER_NAMES[_WINNER_SEAT]
        assert result["ended_at"] is not None
        assert (
            result["winner_member_openid"]
            != chain.member(_FORFEITER_SEAT).member_openid
        ), "触发弃权者绝不能被当作胜者"

        result_players = await chain.result_player_rows()
        assert [row["join_seq"] for row in result_players] == [1, 2]
        by_seq = {row["join_seq"]: row for row in result_players}
        assert by_seq[_FORFEITER_SEAT]["alive"] is False
        assert by_seq[_FORFEITER_SEAT]["eliminated_reason"] == "forfeit"
        assert by_seq[_WINNER_SEAT]["alive"] is True
        assert by_seq[_WINNER_SEAT]["eliminated_reason"] is None
        assert {row["display_name"] for row in result_players} == set(
            _CHARACTER_NAMES.values()
        ), "冻结名必须与真实绑定一致"

        ledger = await chain.leaderboard_rows()
        assert len(ledger) == 1, ledger
        assert ledger[0]["member_openid"] == chain.member(_WINNER_SEAT).member_openid
        assert ledger[0]["display_name"] == _CHARACTER_NAMES[_WINNER_SEAT]
        assert ledger[0]["wins"] == 1, "唯一胜者恰好 1 胜"

        # ---- 终局 SDK 载荷：单条完整正文 / 恰一次正确 winner 提及 -------
        body = markdown_content(payload)
        pg_body = await chain.receipt_body(forfeit_id)
        assert pg_body is not None
        assert body == pg_body, "终局载荷正文必须与冻结收据完全一致"
        # body == pg_body 只是投影自一致；独立验证它确实是单段完整文字：
        # 无换行 / 多段、无 Markdown 引用样式（>）、无粗体 / 分隔线 / 名册。
        assert "\n" not in body, f"终局正文必须是单段完整文字: {body!r}"
        assert body == body.strip(), f"终局正文不得含首尾空白: {body!r}"
        assert not body.lstrip().startswith(">"), (
            f"终局正文不得是 Markdown 引用（行首 '>' 引用样式）: {body!r}"
        )
        assert_body_has_markdown_structure(
            body, dividers=0, blockquote=False, bold=False, roster=False
        )
        assert body.startswith(f"{_CHARACTER_NAMES[_FORFEITER_SEAT]}弃权出局，")
        assert body.endswith("获胜，累计胜场 1。")
        assert _CHARACTER_NAMES[_FORFEITER_SEAT] in body
        assert _CHARACTER_NAMES[_WINNER_SEAT] in body
        assert "**" not in body, f"终局正文不得使用加粗: {body!r}"
        assert "message_reference" not in payload, "终局载荷不得引用其他消息"
        assert "keyboard" not in payload, "终局载荷不得携带按钮"
        assert_single_mention_tag(body, chain.member(_WINNER_SEAT).member_openid)
        assert_no_member_openid(
            body,
            chain.member(_FORFEITER_SEAT).member_openid,
            chain.member(_WINNER_SEAT).member_openid,
        )

        # ---- 真实排行榜查询：与胜场账本同一事实 ----------------------------
        leaderboard_id = chain.next_msg_id("game-leaderboard")
        sent_leaderboard = await chain.send_qq(
            CMD_LEADERBOARD,
            member_openid=chain.member(_FORFEITER_SEAT).member_openid,
            message_id=leaderboard_id,
        )
        assert len(sent_leaderboard) == 1, sent_leaderboard
        _lb_api, lb_payload = sent_leaderboard[0]
        lb_body = markdown_content(lb_payload)
        assert "**本群俄罗斯轮盘排行榜｜前 10 名**" in lb_body
        assert f"1. {_CHARACTER_NAMES[_WINNER_SEAT]}｜1 胜" in lb_body
        assert "共有 1 名玩家取得过胜利。" in lb_body
        assert await chain.receipt_body(leaderboard_id) == lb_body
        assert len(await chain.leaderboard_rows()) == 1


@pytest.mark.parametrize(
    "failure_mode",
    ["timeout", "no_id"],
    ids=["transport-raises", "transport-no-id"],
)
async def test_terminal_delivery_unknown_then_replay_keeps_single_win(
    monkeypatch: pytest.MonkeyPatch,
    failure_mode: str,
) -> None:
    """领域已提交但 QQ 传输结果不确定 → UNKNOWN/PENDING；同 msg_id 重投不重发、不加胜。

    ``timeout`` 让真实传输抛错，``no_id`` 让它返回无可用平台消息 id；两条路径
    都是“实际已发出但结果不确定”，必须收敛到同一 PENDING_CONFIRMATION（否则
    既有的 ``no_id`` latch 就是从未被验证的死代码）。
    """
    require_postgres()

    async with chain_context(monkeypatch, seat_names=_CHARACTER_NAMES) as chain:
        await _bind_two_and_start(chain)

        forfeit_id = chain.next_msg_id("game-forfeit")
        if failure_mode == "timeout":
            chain.fail_once_terminal_send(forfeit_id)
        else:
            chain.no_id_terminal_send(forfeit_id)
        attempted = await chain.send_qq(
            CMD_FORFEIT,
            member_openid=chain.member(_FORFEITER_SEAT).member_openid,
            message_id=forfeit_id,
        )
        # 传输先记录一次真实尝试，然后才给出不确定结果；领域提交不受影响。
        assert len(attempted) == 1, attempted
        assert attempted[0][0] == "post_group_messages"
        assert attempted[0][1]["msg_id"] == forfeit_id
        # 注入的 latch 必须真的被消费一次，否则用例是空转。
        assert forfeit_id not in chain.qq.fail_once_msg_ids
        assert forfeit_id not in chain.qq.no_id_msg_ids

        fulfillment = await chain.fulfillment_row(forfeit_id)
        assert fulfillment is not None
        assert fulfillment["state"] == "PENDING_CONFIRMATION"
        assert fulfillment["platform_message_id"] is None

        games = await chain.game_rows()
        assert len(games) == 1 and games[0]["lifecycle"] == "completed"
        results = await chain.result_rows()
        assert len(results) == 1 and results[0]["reason"] == "forfeit"
        ledger = await chain.leaderboard_rows()
        assert len(ledger) == 1 and ledger[0]["wins"] == 1

        # ---- 同一 msg_id 重投：不第二次发送、不重生成/恢复游戏、不加第二胜 --
        calls_before_replay = len(chain.qq.calls)
        replayed = await chain.send_qq(
            CMD_FORFEIT,
            member_openid=chain.member(_FORFEITER_SEAT).member_openid,
            message_id=forfeit_id,
        )
        assert replayed == [], "同一终局 msg_id 重投不得再次调用平台发送"
        assert len(chain.qq.calls) == calls_before_replay
        assert len(await chain.result_rows()) == 1
        games_after = await chain.game_rows()
        assert len(games_after) == 1 and games_after[0]["lifecycle"] == "completed"
        ledger_after = await chain.leaderboard_rows()
        assert len(ledger_after) == 1 and ledger_after[0]["wins"] == 1
        assert await chain.fulfillment_row(forfeit_id) == fulfillment, (
            "重投不得改变履约状态"
        )

        # ---- 新 msg_id 查排行榜：仍只见唯一已确认业务胜场 -------------------
        leaderboard_id = chain.next_msg_id("game-leaderboard")
        sent_leaderboard = await chain.send_qq(
            CMD_LEADERBOARD,
            member_openid=chain.member(_FORFEITER_SEAT).member_openid,
            message_id=leaderboard_id,
        )
        assert len(sent_leaderboard) == 1, sent_leaderboard
        lb_body = markdown_content(sent_leaderboard[0][1])
        assert f"1. {_CHARACTER_NAMES[_WINNER_SEAT]}｜1 胜" in lb_body
        assert len(await chain.leaderboard_rows()) == 1


@pytest.mark.parametrize(
    "unbound_seat",
    [_FORFEITER_SEAT, _WINNER_SEAT],
    ids=["unbound-forfeits", "unbound-wins"],
)
async def test_rename_then_unbind_keeps_frozen_seat_and_gates_new_games(
    monkeypatch: pytest.MonkeyPatch,
    unbound_seat: int,
) -> None:
    """真实改名 / 解绑链：canonical 变、冻结名不变、在席资格保留、新局被拒。

    两个参数各证一条：``unbound-forfeits``（被维护者在座 1，亲自弃权）证明解绑
    者仍能执行允许的对局动作并达成已验证终局；``unbound-wins``（被维护者在座
    2，房主弃权）证明解绑者仍可获胜。两次运行复用同一条真实 /bind 向导与真实
    QQ 装配，不复制长链。

    ``/bind rename`` / ``/bind unbind`` 的会话码一律从真实公开键盘按钮 data
    提取；绝不猜码，也绝不直接 SQL 改名 / 改状态。
    """
    require_postgres()

    async with chain_context(monkeypatch, seat_names=_CHARACTER_NAMES) as chain:
        await _bind_two_and_start(chain)

        unbound = chain.member(unbound_seat)
        other_seat = (
            _WINNER_SEAT if unbound_seat == _FORFEITER_SEAT else _FORFEITER_SEAT
        )
        other = chain.member(other_seat)
        old_name = _CHARACTER_NAMES[unbound_seat]
        other_name = _CHARACTER_NAMES[other_seat]
        assert await _canonical_names(chain) == {
            unbound.member_openid: old_name,
            other.member_openid: other_name,
        }
        assert await _frozen_names(chain) == {
            unbound.member_openid: old_name,
            other.member_openid: other_name,
        }

        # ---- 真实 /bind rename：会话码只来自公开键盘按钮 data --------------
        rename_code = await chain.rename_start(unbound_seat)
        # 确认前 canonical 仍是旧名
        assert (await _canonical_names(chain))[unbound.member_openid] == old_name
        await chain.rename_name(
            unbound_seat,
            rename_code,
            old_name=old_name,
            new_name=_RENAMED_NAME,
        )
        # 仅“填写名字”只推进向导会话，确认前 canonical 必定仍是旧名
        assert (await _canonical_names(chain))[unbound.member_openid] == old_name
        await chain.rename_confirm(unbound_seat, rename_code, new_name=_RENAMED_NAME)
        # 确认后 canonical 新名，而在席冻结名仍旧
        assert (await _canonical_names(chain))[unbound.member_openid] == _RENAMED_NAME
        assert await _frozen_names(chain) == {
            unbound.member_openid: old_name,
            other.member_openid: other_name,
        }
        games = await chain.game_rows()
        assert len(games) == 1 and games[0]["lifecycle"] == "active", games
        assert {p["member_openid"] for p in await chain.player_rows()} == {
            unbound.member_openid,
            other.member_openid,
        }, "改名不得撤销在席资格"

        # ---- 真实 /bind unbind：二次确认只清当前角色名 ---------------------
        unbind_code = await chain.unbind_start(
            unbound_seat, expected_name=_RENAMED_NAME
        )
        # 确认前 canonical 仍是新名
        assert (await _canonical_names(chain))[unbound.member_openid] == _RENAMED_NAME
        await chain.unbind_confirm(unbound_seat, unbind_code)
        groups_after, _members = await committed_rows(chain.harness.engine, chain.scope)
        assert len(groups_after) == 1, "ordinary unbind 必须保留群身份关系"
        canonical_after = await _canonical_names(chain)
        assert unbound.member_openid in canonical_after, "成员 identity 行必须保留"
        assert canonical_after[unbound.member_openid] is None
        assert canonical_after[other.member_openid] == other_name
        # 对局仍 active、冻结名仍旧、原 player 仍在席
        assert await _frozen_names(chain) == {
            unbound.member_openid: old_name,
            other.member_openid: other_name,
        }
        games = await chain.game_rows()
        assert len(games) == 1 and games[0]["lifecycle"] == "active", games
        assert len(await chain.player_rows()) == 2

        # ---- 解绑者仍能执行允许的对局动作：弃权 → 已验证终局 ---------------
        forfeit_id = chain.next_msg_id("game-forfeit")
        sent = await chain.send_qq(
            CMD_FORFEIT,
            member_openid=chain.member(_FORFEITER_SEAT).member_openid,
            message_id=forfeit_id,
        )
        assert len(sent) == 1, sent
        api, payload = sent[0]
        assert api == "post_group_messages", api
        assert payload["msg_type"] == 2, "终局正文必须是 markdown 载荷"

        results = await chain.result_rows()
        assert len(results) == 1, results
        result = results[0]
        assert result["lifecycle"] == "completed"
        assert result["reason"] == "forfeit"
        assert result["winner_seq"] == _WINNER_SEAT
        assert (
            result["winner_member_openid"] == chain.member(_WINNER_SEAT).member_openid
        )
        assert result["winner_display_name"] == _CHARACTER_NAMES[_WINNER_SEAT]
        assert (
            result["winner_member_openid"]
            != chain.member(_FORFEITER_SEAT).member_openid
        ), "触发弃权者绝不能被当作胜者"

        # 终局 / 榜单仍用冻结名：改名新名绝不出现，解绑者冻结旧名出现
        body = markdown_content(payload)
        assert _RENAMED_NAME not in body, "终局不得使用解绑后的新名"
        assert old_name in body, "终局必须使用解绑者的冻结旧名"
        assert body.startswith(f"{_CHARACTER_NAMES[_FORFEITER_SEAT]}弃权出局，")
        assert "**" not in body, f"终局正文不得使用加粗: {body!r}"
        assert "message_reference" not in payload, "终局载荷不得引用其他消息"
        assert "keyboard" not in payload, "终局载荷不得携带按钮"
        assert "\n" not in body, f"终局正文必须是单段完整文字: {body!r}"
        assert not body.lstrip().startswith(">"), (
            f"终局正文不得是 Markdown 引用（行首 '>' 引用样式）: {body!r}"
        )
        assert_body_has_markdown_structure(
            body, dividers=0, blockquote=False, bold=False, roster=False
        )
        assert_single_mention_tag(body, chain.member(_WINNER_SEAT).member_openid)
        assert_no_member_openid(body, unbound.member_openid, other.member_openid)

        if unbound_seat == _FORFEITER_SEAT:
            # 解绑者亲自弃权：证明其在席资格仍允许该动作
            assert result["winner_member_openid"] == other.member_openid
        else:
            # 解绑者获胜：证明其仍可赢下终局
            assert result["winner_member_openid"] == unbound.member_openid
            assert result["winner_display_name"] == old_name

        ledger = await chain.leaderboard_rows()
        assert len(ledger) == 1, ledger
        assert ledger[0]["member_openid"] == chain.member(_WINNER_SEAT).member_openid
        assert ledger[0]["display_name"] == _CHARACTER_NAMES[_WINNER_SEAT]
        assert ledger[0]["wins"] == 1
        if unbound_seat == _WINNER_SEAT:
            assert ledger[0]["display_name"] == old_name != _RENAMED_NAME

        # ---- 解绑后：同一 App / 群内不能新开局、不能重新加入 ----------------
        games_before = await chain.game_rows()
        blocked_id = chain.next_msg_id("post-unbind-create")
        blocked = await chain.send_qq(
            CMD_CREATE,
            member_openid=unbound.member_openid,
            message_id=blocked_id,
        )
        assert len(blocked) == 1, blocked
        assert await chain.game_rows() == games_before, "解绑者开局不得新增对局"
        receipts = {
            row["inbound_msg_id"]: row["result_code"]
            for row in await chain.receipt_rows()
        }
        assert receipts[blocked_id] == "binding_required"

        # 另一名仍有角色名的用户开新 waiting
        create_id = chain.next_msg_id("post-unbind-open")
        created = await chain.send_qq(
            CMD_CREATE,
            member_openid=other.member_openid,
            message_id=create_id,
        )
        assert len(created) == 1, created
        games_after_open = await chain.game_rows()
        assert len(games_after_open) == len(games_before) + 1
        new_game = games_after_open[-1]
        assert new_game["lifecycle"] == "waiting"
        assert new_game["next_join_seq"] == 2

        # 解绑者加入 → binding_required，不新增 player
        join_id = chain.next_msg_id("post-unbind-join")
        joined = await chain.send_qq(
            CMD_JOIN,
            member_openid=unbound.member_openid,
            message_id=join_id,
        )
        assert len(joined) == 1, joined
        after_join = await chain.game_rows()
        assert after_join[-1]["game_id"] == new_game["game_id"]
        assert after_join[-1]["next_join_seq"] == 2
        assert await chain.player_count_for_game(new_game["game_id"]) == 1
        receipts = {
            row["inbound_msg_id"]: row["result_code"]
            for row in await chain.receipt_rows()
        }
        assert receipts[join_id] == "binding_required"


async def test_partial_roulette_assembly_restores_pre_entry_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """组合根装配中途失败 → 仍回滚模块树 / 包重导出 / QQ runtime / 调度器。"""
    require_postgres()

    import komari_bot.plugins.komari_roulette.qq as qq_module
    import tests.komari_roulette.tsk281_chain_support as support

    # 链前已存在的 QQ runtime 是一个真实的不透明对象：合法修复必须保留它，
    # 而旧实现会在恢复旧模块时无条件 clear 掉。
    sentinel_runtime = object()
    monkeypatch.setattr(
        qq_module._state,
        "runtime",
        sentinel_runtime,
    )
    before = _roulette_state()
    original_application_api = support.application_api
    calls = 0

    def flaky_application_api() -> dict[str, Any]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("tsk281 injected roulette assembly failure")  # noqa: TRY003
        return original_application_api()

    monkeypatch.setattr(support, "application_api", flaky_application_api)

    with pytest.raises(RuntimeError, match="injected"):
        async with chain_context(monkeypatch, seat_names=_CHARACTER_NAMES):
            pytest.fail("组合根装配失败时不得进入用例正文")

    assert calls == 2, "失败注入恰一次，善后仍须重新解析真实 lifecycle API"
    after = _roulette_state()
    assert after["package"] is before["package"]
    assert after["package_dict_names"] == before["package_dict_names"], (
        "包 __dict__ 的重导出名集必须逐字还原"
    )
    assert after["lifecycle_attr"] is before["lifecycle_attr"]
    assert after["qq_attr"] is before["qq_attr"]
    assert after["get_roulette_observation"] is before["get_roulette_observation"], (
        "包级重导出（reload 在就地 __dict__ 中改写）必须逐字还原"
    )
    assert after["lifecycle_module"] is before["lifecycle_module"]
    assert after["qq_module"] is before["qq_module"]
    assert after["qq_runtime"] is before["qq_runtime"], (
        "链前已存在的 QQ runtime 不得被无条件清除"
    )
    assert after["qq_runtime"] is sentinel_runtime
    assert after["scheduler_module"] is before["scheduler_module"]
    assert after["scheduler"] is before["scheduler"]


# ---------------------------------------------------------------------------
# TSK-281 返工：取消组合（真实等待局取消 / 未确认草稿取消）
# ---------------------------------------------------------------------------


async def test_host_cancel_waiting_game_keeps_binding_and_allows_reopen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真实等待局由房主 ``/轮盘 取消`` → cancelled / 无胜场 / 绑定不动 / 可新开。

    取消是等候局的真实生产终局：``lifecycle=cancelled``、无当前局且排行榜为空
    （无胜场）；生产仍写入一条 winner 为空的 cancelled 终局结果。canonical 绑定
    不被触碰，同群随后可再开一局真实 waiting。
    """
    require_postgres()

    async with chain_context(
        monkeypatch, seat_names={_FORFEITER_SEAT: _CHARACTER_NAMES[_FORFEITER_SEAT]}
    ) as chain:
        host = chain.member(_FORFEITER_SEAT)
        await chain.bind(_FORFEITER_SEAT, _CHARACTER_NAMES[_FORFEITER_SEAT])

        create_id = chain.next_msg_id("cancel-create")
        created = await chain.send_qq(
            CMD_CREATE, member_openid=host.member_openid, message_id=create_id
        )
        assert len(created) == 1, created
        games_before = await chain.game_rows()
        assert len(games_before) == 1 and games_before[0]["lifecycle"] == "waiting"
        binding_before = await _canonical_names(chain)
        assert binding_before == {host.member_openid: _CHARACTER_NAMES[_FORFEITER_SEAT]}
        assert [row["result_code"] for row in await chain.receipt_rows()] == ["created"]

        cancel_id = chain.next_msg_id("cancel-waiting")
        sent = await chain.send_qq(
            CMD_CANCEL, member_openid=host.member_openid, message_id=cancel_id
        )
        assert len(sent) == 1, sent
        api, payload = sent[0]
        assert api == "post_group_messages", api
        assert payload["msg_id"] == cancel_id
        assert markdown_content(payload) == (
            f"{_CHARACTER_NAMES[_FORFEITER_SEAT]}取消了这局游戏，"
            "等候中的玩家已经全部离席。"
        )

        # 取消就地把同一条 waiting 局推进为 cancelled（不新增历史行）。
        assert [row["lifecycle"] for row in await chain.game_rows()] == ["cancelled"]
        assert await chain.current_snapshot() is None
        assert await chain.leaderboard_rows() == []
        results = await chain.result_rows()
        assert len(results) == 1
        assert results[0]["lifecycle"] == "cancelled"
        assert results[0]["reason"] == "cancelled"
        assert results[0]["winner_seq"] is None
        assert results[0]["winner_member_openid"] is None
        assert results[0]["winner_display_name"] is None
        assert [row["result_code"] for row in await chain.receipt_rows()] == [
            "created",
            "cancelled",
        ]

        assert await _canonical_names(chain) == binding_before

        reopen_id = chain.next_msg_id("cancel-reopen")
        reopened = await chain.send_qq(
            CMD_CREATE, member_openid=host.member_openid, message_id=reopen_id
        )
        assert len(reopened) == 1, reopened
        games_reopened = await chain.game_rows()
        assert [row["lifecycle"] for row in games_reopened] == [
            "cancelled",
            "waiting",
        ]
        assert games_reopened[-1]["next_join_seq"] == 2
        snapshot = await chain.current_snapshot()
        assert snapshot is not None and snapshot.lifecycle == "waiting"


async def test_rename_draft_cancel_during_active_game_preserves_everything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """active 期间打开 ``/bind rename`` 草稿，经公开取消命令撤销。

    canonical 原名、入席冻结名、游戏状态 / 期限与 roulette receipts 全不变；
    迟到的 ``/bind confirm`` 不得应用已取消的草稿。
    """
    require_postgres()

    async with chain_context(monkeypatch, seat_names=_CHARACTER_NAMES) as chain:
        await _bind_two_and_start(chain)
        member = chain.member(_FORFEITER_SEAT)
        other = chain.member(_WINNER_SEAT)
        old_name = _CHARACTER_NAMES[_FORFEITER_SEAT]
        names_before = await _canonical_names(chain)
        frozen_before = await _frozen_names(chain)

        snapshot_before = await chain.current_snapshot()
        assert snapshot_before is not None and snapshot_before.lifecycle == "active"
        deadline_before = snapshot_before.deadline
        revision_before = snapshot_before.state_revision
        games_before = await chain.game_rows()
        receipts_before = await chain.receipt_rows()

        session_code, cancel_command = await chain.rename_cancel_draft(_FORFEITER_SEAT)

        # 把真实草稿推进到 RENAME_CONFIRM（仍未提交）：若取消未生效，迟到的
        # confirm 会真的应用改名，从而让本用例的取消断言具备区分性。
        await chain.rename_name(
            _FORFEITER_SEAT, session_code, old_name=old_name, new_name=_RENAMED_NAME
        )

        # 草稿未确认前：canonical 原名与在席冻结名均不变。
        assert (await _canonical_names(chain))[member.member_openid] == old_name
        assert await _frozen_names(chain) == frozen_before

        # 通过真实公开“取消”按钮命令撤销草稿。
        cancelled = await chain.send_command(
            _FORFEITER_SEAT, cancel_command, tag="rename-cancel"
        )
        assert len(cancelled) == 1, cancelled
        assert markdown_content(cancelled[0][1]) == CANCELLED

        # 迟到 confirm 不得应用草稿。
        late = await chain.send_command(
            _FORFEITER_SEAT,
            f"{CMD_BIND} confirm {session_code}",
            tag="rename-late-confirm",
        )
        assert len(late) == 1, late
        assert markdown_content(late[0][1]) == EXPIRED

        # canonical 原名 / 入席冻结名不变。
        assert await _canonical_names(chain) == names_before
        assert await _frozen_names(chain) == frozen_before
        assert names_before == {
            member.member_openid: old_name,
            other.member_openid: _CHARACTER_NAMES[_WINNER_SEAT],
        }

        # 游戏状态 / 期限 / receipts 不变。
        snapshot_after = await chain.current_snapshot()
        assert snapshot_after is not None
        assert snapshot_after.lifecycle == "active"
        assert snapshot_after.state_revision == revision_before
        assert snapshot_after.deadline == deadline_before
        assert await chain.game_rows() == games_before
        assert await chain.receipt_rows() == receipts_before


# ---------------------------------------------------------------------------
# TSK-281 B1e: 真实道具面板 → 满仓奖励 → item_choice → 真实弃权终局
# ---------------------------------------------------------------------------

#: 领域初始 / 重载弹仓（2 实 4 空）；真实领域仍会校验 live/blank 精确计数。
_DUAL_BLANK_CHAMBER: tuple[ChamberKind, ...] = (
    ChamberKind.BLANK,
    ChamberKind.BLANK,
    ChamberKind.BLANK,
    ChamberKind.BLANK,
    ChamberKind.LIVE,
    ChamberKind.LIVE,
)

#: 主链实际采样的道具抽取序列（连发器一次消耗两发 ⇒ 双奖励）。
_CHAIN_ITEM_DRAWS: tuple[ItemType, ...] = (
    ItemType.BEER,  # 第 2 发空弹（跟进）
    ItemType.BEER,  # 第 3 发空弹
    ItemType.BURST,  # 第 4 发空弹 → 库存 3
    ItemType.MAGNIFIER,  # 连发器消耗两发：奖励 1
    ItemType.LOCK,  # 连发器消耗两发：奖励 2
    ItemType.BEER,  # 第 9 发空弹
    ItemType.BEER,  # 第 10 发空弹 → 库存满 4
    ItemType.BEER,  # 两次啤酒重载后的第 13 发
    ItemType.BURST,  # 第 14 发空弹 → 库存满 4
    ItemType.LOCK,  # 连发器消耗两发：奖励 1（满仓后入待选）
    ItemType.BEER,  # 连发器消耗两发：奖励 2（满仓 → item_choice）
)


def _inventory_of(snapshot: Any, join_seq: int) -> dict[str, int]:
    """公开快照中某一冻结席位的库存（省略零计数）。"""

    seats = {seat.join_seq: seat for seat in snapshot.players}
    assert join_seq in seats, f"玩家 {join_seq} 不在当前对局 roster"
    return {
        item.value: count
        for item, count in seats[join_seq].inventory.items()
        if count > 0
    }


async def _run_tracked_command(
    chain: Chain,
    seat: int,
    command: str,
    *,
    tag: str,
) -> tuple[str, dict[str, Any]]:
    """发送一条真实 QQ 命令，并要求恰好一次合法平台发送。"""

    message_id = chain.next_msg_id(tag)
    before = len(chain.qq.calls)
    await chain.send_qq(
        command,
        member_openid=chain.member(seat).member_openid,
        message_id=message_id,
    )
    sent = list(chain.qq.calls[before:])
    assert len(sent) == 1, f"{command} 必须恰一次 QQ 发送，实际 {sent!r}"
    api, payload = sent[0]
    assert api == "post_group_messages", api
    assert payload["msg_id"] == message_id, payload
    assert payload["msg_seq"] == 1, payload
    return message_id, payload


async def test_item_panel_full_inventory_reward_to_endgame_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真实道具面板 + 合法射击/道具累积满仓 → item_choice → 真实弃权终局。

    随机端口是唯一替身：真实组合根仍构建真实 ``RouletteCommandService``，
    只把领域 ``RandomSource`` 换成符合弹仓/抽取契约的确定性源；准入、matcher、
    命令服务、存储与送达全部真实。items 只使用真实可抽取道具，弹仓只使用满足
    精确 live/blank 计数的合法序列。
    """

    require_postgres()
    entropy = ScriptedRandomSource(
        chambers=(
            _DUAL_BLANK_CHAMBER,
            _DUAL_BLANK_CHAMBER,
            _DUAL_BLANK_CHAMBER,
        ),
        items=_CHAIN_ITEM_DRAWS,
    )

    async with chain_context(
        monkeypatch,
        seat_names=_CHARACTER_NAMES,
        random_source=entropy,
    ) as chain:
        await _bind_two_and_start(chain)
        seat = _FORFEITER_SEAT
        other = _WINNER_SEAT
        expected: list[tuple[str, str]] = []

        async def run(command: str, tag: str, code: str) -> dict[str, Any]:
            message_id, payload = await _run_tracked_command(
                chain, seat, command, tag=tag
            )
            expected.append((message_id, code))
            return payload

        # ---- 4 发真实跟进射击，逐步累积第一批库存 -------------------------
        for index in range(1, 5):
            await run("/轮盘 开枪", f"b1e-shot-{index}", "shot")
        snap = await chain.current_snapshot()
        assert snap is not None
        assert snap.phase == "follow_up"
        assert snap.current_player_seq == seat
        assert _inventory_of(snap, seat) == {"beer": 2, "burst": 1}

        # ---- 真实道具面板：展示真实库存，且不改变任何游戏事实 --------------
        before_panel = await chain.current_snapshot()
        assert before_panel is not None
        panel_payload = await run("/轮盘 道具", "b1e-panel", "panel_opened")
        after_panel = await chain.current_snapshot()
        assert after_panel is not None
        assert after_panel.state_revision == before_panel.state_revision
        assert after_panel.phase == before_panel.phase
        assert after_panel.current_player_seq == before_panel.current_player_seq
        assert _inventory_of(after_panel, seat) == _inventory_of(before_panel, seat)
        panel_body = markdown_content(panel_payload)
        assert panel_body.startswith(f"**{_CHARACTER_NAMES[seat]}的道具**")
        assert "- B｜啤酒 ×2" in panel_body
        assert "- C｜连发器 ×1" in panel_body

        # ---- 用掉两瓶真实啤酒（各丢弃一轮）后重载，再连发双奖励 ----------
        await run("/轮盘 道具 使用 B", "b1e-beer-1", "item_used")
        await run("/轮盘 道具 使用 B", "b1e-beer-2", "item_used")
        await run("/轮盘 道具 使用 C", "b1e-burst-1", "item_used")
        await run("/轮盘 开枪", "b1e-burst-shot-1", "shot")
        await run("/轮盘 开枪", "b1e-shot-5", "shot")
        await run("/轮盘 开枪", "b1e-shot-6", "shot")
        snap = await chain.current_snapshot()
        assert snap is not None
        assert _inventory_of(snap, seat) == {
            "magnifier": 1,
            "lock": 1,
            "beer": 2,
        }, "两轮连发双奖励后库存应恰好 4 件"

        # ---- 再次啤酒重载 + 射击 + 连发：满仓后再得奖励 → item_choice -----
        await run("/轮盘 道具 使用 B", "b1e-beer-3", "item_used")
        await run("/轮盘 道具 使用 B", "b1e-beer-4", "item_used")
        await run("/轮盘 开枪", "b1e-shot-7", "shot")
        await run("/轮盘 开枪", "b1e-shot-8", "shot")
        snap = await chain.current_snapshot()
        assert snap is not None
        assert _inventory_of(snap, seat) == {
            "magnifier": 1,
            "lock": 1,
            "beer": 1,
            "burst": 1,
        }
        await run("/轮盘 道具 使用 C", "b1e-burst-2", "item_used")
        reward_payload = await run(
            "/轮盘 开枪", "b1e-burst-shot-2", "item_choice_pending"
        )
        snap = await chain.current_snapshot()
        assert snap is not None
        assert snap.phase == "item_choice"
        assert snap.current_player_seq == seat
        assert [item.value for item in snap.pending_rewards] == ["beer"]
        assert _inventory_of(snap, seat) == {"magnifier": 1, "lock": 2, "beer": 1}
        reward_body = markdown_content(reward_payload)
        assert "道具列表已满，选择一项来替换。" in reward_body
        assert "当前新道具：**啤酒**" in reward_body

        # ---- item_choice 中所有非奖励选择动作被拒且不改变任何事实 ----------
        locked_snapshot = await chain.current_snapshot()
        assert locked_snapshot is not None
        for command, tag in (
            ("/轮盘 开枪", "b1e-reject-shoot"),
            ("/轮盘 道具 使用 A", "b1e-reject-magnifier"),
            ("/轮盘 道具 丢弃 B", "b1e-reject-discard"),
            ("/轮盘 装填", "b1e-reject-reload"),
            ("/轮盘 道具", "b1e-reject-panel"),
            ("/轮盘 结束", "b1e-reject-end-turn"),
        ):
            rejected = await run(command, tag, "action_not_allowed_in_phase")
            assert "请先处理当前新道具" in markdown_content(rejected)
        after_rejections = await chain.current_snapshot()
        assert after_rejections is not None
        assert after_rejections.state_revision == locked_snapshot.state_revision
        assert after_rejections.phase == "item_choice"
        assert [item.value for item in after_rejections.pending_rewards] == ["beer"]
        assert _inventory_of(after_rejections, seat) == _inventory_of(
            locked_snapshot, seat
        )

        # ---- 真实奖励选择：替换指定合法槽（放大镜）→ 恢复 follow_up --------
        await run(
            "/轮盘 奖励 替换 放大镜", "b1e-reward-replace", "item_choice_updated"
        )
        restored = await chain.current_snapshot()
        assert restored is not None
        assert restored.phase == "follow_up"
        assert restored.current_player_seq == seat
        assert restored.pending_rewards == ()
        assert _inventory_of(restored, seat) == {"lock": 2, "beer": 2}, (
            "替换必须消耗一件放大镜并纳入新啤酒"
        )

        # ---- 零隐式补发：奖励已处理，再发奖励命令必须被拒且不改变事实 ------
        before_second_reject = await chain.current_snapshot()
        assert before_second_reject is not None
        second_reject = await run(
            "/轮盘 奖励 丢弃",
            "b1e-reward-after-restore",
            "action_not_allowed_in_phase",
        )
        assert "当前阶段不能执行这个操作。" in markdown_content(second_reject)
        after_second_reject = await chain.current_snapshot()
        assert after_second_reject is not None
        assert after_second_reject.state_revision == before_second_reject.state_revision
        assert after_second_reject.phase == "follow_up"

        # ---- 真实弃权终局 → 唯一胜场 -------------------------------------
        forfeit_payload = await run("/轮盘 弃权", "b1e-forfeit", "forfeited")
        forfeit_body = markdown_content(forfeit_payload)
        assert forfeit_body.startswith(f"{_CHARACTER_NAMES[seat]}弃权出局，")
        games = await chain.game_rows()
        assert len(games) == 1 and games[0]["lifecycle"] == "completed"
        results = await chain.result_rows()
        assert len(results) == 1
        result = results[0]
        assert result["reason"] == "forfeit"
        assert result["winner_seq"] == other
        assert result["winner_member_openid"] == chain.member(other).member_openid
        assert result["winner_display_name"] == _CHARACTER_NAMES[other]
        assert result["winner_member_openid"] != chain.member(seat).member_openid
        ledger = await chain.leaderboard_rows()
        assert len(ledger) == 1
        assert ledger[0]["member_openid"] == chain.member(other).member_openid
        assert ledger[0]["wins"] == 1, "唯一胜者恰好 1 胜"

        # ---- 真实排行榜命令与胜场账本同一事实 -----------------------------
        leaderboard_payload = await run(
            "/轮盘 排行榜", "b1e-leaderboard", "leaderboard"
        )
        leaderboard_body = markdown_content(leaderboard_payload)
        assert f"1. {_CHARACTER_NAMES[other]}｜1 胜" in leaderboard_body
        assert len(await chain.leaderboard_rows()) == 1

        # ---- 随机端口采样与 receipt / 发送逐条对应 -------------------------
        assert len(entropy.chamber_calls) == 3, entropy.chamber_calls
        assert len(entropy.item_calls) == len(_CHAIN_ITEM_DRAWS), entropy.item_calls
        receipts = await chain.receipt_rows()
        assert [row["result_code"] for row in receipts[:3]] == [
            "created",
            "joined",
            "started",
        ]
        tracked = receipts[3:]
        assert [row["result_code"] for row in tracked] == [
            code for _message_id, code in expected
        ]
        assert [row["inbound_msg_id"] for row in tracked] == [
            message_id for message_id, _code in expected
        ]
        assert len({row["receipt_id"] for row in receipts}) == len(receipts)
        assert len(receipts) == 3 + len(expected)
        assert len(chain.qq.calls) == _WIZARD_SENDS_PER_SEAT * 2 + 3 + len(expected)
