"""TSK-281 阶段 B2：真实受权修复 → 独立用户重绑 → 真实轮盘读新名。

本用例在**同一个**真实物理群、同一条真实 PostgreSQL 上贯通三段彼此独立的
流程：

1. 真实双绑定 + 一场真实弃权终局（历史 / 结果玩家 / 胜场账本真实存在）；
2. 真实管理装配（``start_binding_repair_service``）+ 真实 REST 路由
   （``register_character_binding_repair_api``）经 ``nonebug`` 的
   ``app.test_server`` 走**真实 HTTP** 完成 diagnose → preview → confirm；
3. 修复后由目标用户**稍后独立**发起真实 ``/bind`` 重绑，再由真实
   ``/轮盘 开局`` 读取**新**角色名。

业务不变量：

* 运营清除的终态就是“未绑定”——修复**绝不**自动重绑、绝不改写下标历史/
  结果/胜场、绝不向用户发送 QQ 或 OneBot 通知；
* 缓存按真实作用域定向失效（目标 canonical 关系与角色名立即可见地缺失）；
* 重绑是随后独立的用户流程，成功后新对局读到新名，而既有完成对局的历史不被
  修复或重绑改写。

替换缝（完整列出）：

* 平台传输：OneBot ``get_msg`` 受控载荷 + QQ 记录 bot（真实 matcher /
  delivery / adapter 消息构建仍在运行）；
* 受限准入策略存储、``user_ban`` 恒不封禁替身、``FakeScheduler``、
  config-manager 获取替身——与 B1 链完全一致（见 ``tsk281_chain_support``
  模块文档），本文件不新增任何业务替身；
* **本文件唯一新增接缝**：``install_repair_lifecycle_require_seam`` 让真实
  ``start_binding_repair_service`` 能解析 ``require("komari_roulette")``
  （该组合根由 ``importlib.reload`` 驱动，未注册进 NoneBot 插件管理器）；
  它只把已导入的真实模块返回，其余插件名委派给进入接缝前的真实
  ``nonebot.require``；
* 凭据 ``MANAGE_CREDENTIALS`` / ``READ_CREDENTIALS`` 是测试内常量（配置来源
  替身），但 Bearer 鉴权、``character_binding:read`` / ``:manage`` 权限依赖
  与一次性确认令牌语义全部真实执行，未跳过任何权限依赖。

未运行完整管理插件与 Driver 生命周期：只装配真实路由器 + 真实服务；
``app.test_server`` 提供真实 ASGI 请求-响应边界。环境 Redis 仅为配置齐备，
本链不声称覆盖任何 Redis 调用。
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING, Any, cast

import pytest
from fastapi import FastAPI

from tests.character_binding.conftest import require_postgres
from tests.character_binding.tsk280_support import (
    MANAGE_CREDENTIALS,
    READ_CREDENTIALS,
    read_headers,
    write_headers,
)
from tests.character_binding.tsk281_native_chain_support import committed_rows
from tests.komari_roulette.command_support import PG_REQUIRED
from tests.komari_roulette.support import ScriptedRandomSource
from tests.komari_roulette.tsk281_chain_support import (
    CMD_CREATE,
    CMD_FORFEIT,
    CMD_JOIN,
    CMD_START,
    COMMAND_SERVICE_MODULE,
    chain_context,
    install_repair_lifecycle_require_seam,
)

if TYPE_CHECKING:
    from nonebug import App

    from komari_bot.management.management_audit import ManagementAuditEvent
    from tests.komari_roulette.tsk281_chain_support import Chain

pytestmark = [pytest.mark.asyncio, PG_REQUIRED]

_CHARACTER_NAMES = {1: "阿明", 2: "小夜"}
_REBIND_NAME = "阿凛"

_TARGET_SEAT = 1
_OTHER_SEAT = 2

#: 便宜地覆盖“成员级”与“群级”两种清除范围；两者共用同一真实删除路径，
#: 群级依赖 ``komari_character_binding_members`` 的外键 ``ON DELETE CASCADE``。
_REPAIR_SCOPES = ("member", "group")


async def _canonical_names(chain: Chain) -> dict[str, str | None]:
    """PG canonical 角色名（按 member_openid；清除后对应值缺失）。"""

    _groups, members = await committed_rows(chain.harness.engine, chain.scope)
    return {row["member_openid"]: row["character_name"] for row in members}


async def _history_snapshot(
    chain: Chain,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """完成对局的历史事实：结果 / 结果玩家 / 胜场账本。"""

    return (
        [dict(row) for row in await chain.result_rows()],
        [dict(row) for row in await chain.result_player_rows()],
        [dict(row) for row in await chain.leaderboard_rows()],
    )


async def _completed_game_ids(chain: Chain) -> list[str]:
    """仍存在的已完成对局 id（历史行不得被修复 / 重绑删除）。"""

    return [
        str(row["game_id"])
        for row in await chain.game_rows()
        if row["lifecycle"] == "completed"
    ]


async def _run_repair_http(
    nonebug_app: App,
    *,
    app_id: str,
    group_openid: str,
    member_openid: str | None,
    audit_events: list[ManagementAuditEvent],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """真实路由器 + 真实服务的 REST 边界：diagnose(read) → preview/confirm。

    ``member_openid`` 为 ``None`` 时请求群级范围。返回三份真实响应体（状态码
    已在内部断言为 200，失败时携带 ``response.text`` 供定位）。
    """

    # 必须在窗口内惰性导入：此处解析到的是本窗口真实重载后的
    # ``character_binding`` 包，其 ``repair`` 全局注册表与
    # ``start_binding_repair_service`` 写入的是同一模块对象。
    from komari_bot.plugins.character_binding import management_api as live_api
    from komari_bot.plugins.character_binding.repair import (
        get_binding_repair_service as live_service_getter,
    )

    repair_app = FastAPI()

    async def _record(event: ManagementAuditEvent) -> None:
        audit_events.append(event)

    live_api.register_character_binding_repair_api(
        repair_app,
        api_token=[*READ_CREDENTIALS, *MANAGE_CREDENTIALS],
        allowed_origins=[],
        service_getter=live_service_getter,
        audit_recorder=_record,
    )
    preview_payload: dict[str, Any] = {
        "app_id": app_id,
        "group_openid": group_openid,
    }
    if member_openid is not None:
        preview_payload["member_openid"] = member_openid

    async with nonebug_app.test_server(asgi=cast("Any", repair_app)) as ctx:
        client = ctx.get_client()
        prefix = live_api.API_PREFIX
        diagnose = await client.get(
            f"{prefix}/diagnose?app_id={app_id}&group_openid={group_openid}",
            headers=read_headers(READ_CREDENTIALS[0]["token"]),
        )
        preview = await client.post(
            f"{prefix}/preview",
            headers=write_headers(
                request_id="tsk281-b2-preview",
                token=MANAGE_CREDENTIALS[0]["token"],
            ),
            json=preview_payload,
        )
        assert preview.status_code == 200, preview.text
        preview_body = preview.json()
        confirm = await client.post(
            f"{prefix}/confirm",
            headers=write_headers(
                request_id="tsk281-b2-confirm",
                token=MANAGE_CREDENTIALS[0]["token"],
            ),
            json={
                "app_id": app_id,
                "group_openid": group_openid,
                "token": preview_body["token"],
            },
        )

    assert diagnose.status_code == 200, diagnose.text
    assert confirm.status_code == 200, confirm.text
    return diagnose.json(), preview_body, confirm.json()


async def _bind_two_and_forfeit(chain: Chain) -> None:
    """真实双绑定 → 真实弃权终局（唯一胜场）。"""

    for seat, name in _CHARACTER_NAMES.items():
        await chain.bind(seat, name)
    for seat, command, tag in (
        (1, CMD_CREATE, "b2-create"),
        (2, CMD_JOIN, "b2-join"),
        (1, CMD_START, "b2-start"),
    ):
        sent = await chain.send_command(seat, command, tag=tag)
        assert len(sent) == 1, f"{command} 必须恰一次 QQ 发送，实际 {sent!r}"
    forfeit_id = chain.next_msg_id("b2-forfeit")
    sent = await chain.send_qq(
        CMD_FORFEIT,
        member_openid=chain.member(_TARGET_SEAT).member_openid,
        message_id=forfeit_id,
    )
    assert len(sent) == 1, sent


@pytest.mark.parametrize("repair_scope", _REPAIR_SCOPES)
async def test_authorized_repair_then_independent_rebind_keeps_history(
    monkeypatch: pytest.MonkeyPatch,
    app: App,
    repair_scope: str,
) -> None:
    """真实修复 → 独立重绑 → 新对局读新名，历史/胜场全程不变。"""
    require_postgres()
    install_repair_lifecycle_require_seam(monkeypatch)

    async with chain_context(monkeypatch, seat_names=_CHARACTER_NAMES) as chain:
        target = chain.member(_TARGET_SEAT)
        other = chain.member(_OTHER_SEAT)

        # ---- 前置：真实双绑定 + 真实弃权终局 ------------------------------
        await _bind_two_and_forfeit(chain)
        completed_ids = await _completed_game_ids(chain)
        assert len(completed_ids) == 1, completed_ids
        history_before = await _history_snapshot(chain)
        assert len(history_before[0]) == 1, history_before[0]
        assert len(history_before[1]) == 2, history_before[1]
        assert len(history_before[2]) == 1 and history_before[2][0]["wins"] == 1
        assert await _canonical_names(chain) == {
            target.member_openid: _CHARACTER_NAMES[_TARGET_SEAT],
            other.member_openid: _CHARACTER_NAMES[_OTHER_SEAT],
        }

        from komari_bot.plugins.character_binding import get_binding_manager

        manager = get_binding_manager()
        assert (
            manager.get_qq_character_name(
                app_id=chain.scope.app_id,
                group_openid=chain.scope.group_openid,
                member_openid=target.member_openid,
            )
            == _CHARACTER_NAMES[_TARGET_SEAT]
        )
        assert (
            len(
                manager.list_group_bindings(
                    app_id=chain.scope.app_id,
                    group_openid=chain.scope.group_openid,
                )
            )
            == 2
        )

        qq_calls_before = len(chain.qq.calls)
        onebot_calls_before = len(chain.onebot.calls)

        # ---- 真实管理装配 + 真实 HTTP 修复 --------------------------------
        from komari_bot.plugins.komari_management.binding_repair_lifecycle import (
            start_binding_repair_service,
            stop_binding_repair_service,
        )

        start_binding_repair_service()
        try:
            audit_events: list[ManagementAuditEvent] = []
            diagnose_body, preview_body, confirm_body = await _run_repair_http(
                app,
                app_id=chain.scope.app_id,
                group_openid=chain.scope.group_openid,
                member_openid=(
                    target.member_openid if repair_scope == "member" else None
                ),
                audit_events=audit_events,
            )
        finally:
            await stop_binding_repair_service()

        # 真实 diagnose：两名成员 + 已完成对局不阻断修复。
        assert len(diagnose_body["members"]) == 2, diagnose_body
        assert diagnose_body["game_present"] is False, diagnose_body
        # 真实 preview/confirm：范围、数量与一次性令牌版本严格绑定。
        assert preview_body["scope"] == repair_scope, preview_body
        assert preview_body["affected_count"] == (
            1 if repair_scope == "member" else 2
        ), preview_body
        assert confirm_body["scope"] == repair_scope, confirm_body
        assert confirm_body["version"] == preview_body["version"], confirm_body
        assert confirm_body["expected_count"] == preview_body["affected_count"]
        assert confirm_body["cleared_count"] == preview_body["affected_count"]
        # 真实审计：确认成功事件携带同一预览版本与实删数量。
        confirm_audit = [
            event
            for event in audit_events
            if event.action == "character_binding.repair.confirm"
            and event.outcome == "succeeded"
        ]
        assert len(confirm_audit) == 1, [event.action for event in audit_events]
        assert confirm_audit[0].metadata["version"] == preview_body["version"]
        assert (
            confirm_audit[0].metadata["cleared_count"] == confirm_body["cleared_count"]
        )

        # ---- 清除后：目标 canonical 关系/角色缺失、缓存失效、无自动重绑 ----
        groups_after, members_after = await committed_rows(
            chain.harness.engine, chain.scope
        )
        names_after = {
            row["member_openid"]: row["character_name"] for row in members_after
        }
        if repair_scope == "member":
            assert len(groups_after) == 1, "成员级清理必须保留群身份关系"
            assert target.member_openid not in names_after
            assert names_after == {other.member_openid: _CHARACTER_NAMES[_OTHER_SEAT]}
        else:
            assert groups_after == [], "群级清理必须删除群映射（级联成员）"
            assert members_after == []
        assert (
            manager.get_qq_character_name(
                app_id=chain.scope.app_id,
                group_openid=chain.scope.group_openid,
                member_openid=target.member_openid,
            )
            is None
        ), "修复后目标关系必须立即可见地缺失（缓存已定向失效）"
        remaining = manager.list_group_bindings(
            app_id=chain.scope.app_id,
            group_openid=chain.scope.group_openid,
        )
        if repair_scope == "member":
            assert [record.member_openid for record in remaining] == [
                other.member_openid
            ]
        else:
            assert remaining == ()
        # 清除终态就是“未绑定”：绝不自动重绑，也不发送任何 QQ / OneBot 通知。
        assert len(chain.qq.calls) == qq_calls_before, "修复不得发送任何 QQ 消息"
        assert len(chain.onebot.calls) == onebot_calls_before, (
            "修复不得产生任何 OneBot 调用"
        )
        # 历史 / 结果 / 胜场不被修复改写。
        assert await _history_snapshot(chain) == history_before
        assert await _completed_game_ids(chain) == completed_ids

        # ---- 稍后由用户独立发起真实 /bind 重绑 ----------------------------
        await chain.bind(_TARGET_SEAT, _REBIND_NAME)
        groups_re, members_re = await committed_rows(chain.harness.engine, chain.scope)
        names_re = {row["member_openid"]: row["character_name"] for row in members_re}
        assert len(groups_re) == 1, "重绑必须重建/保留群身份关系"
        assert names_re[target.member_openid] == _REBIND_NAME
        if repair_scope == "member":
            assert names_re[other.member_openid] == _CHARACTER_NAMES[_OTHER_SEAT]
        else:
            assert other.member_openid not in names_re, "未重绑成员不得被自动回填"
        assert (
            manager.get_qq_character_name(
                app_id=chain.scope.app_id,
                group_openid=chain.scope.group_openid,
                member_openid=target.member_openid,
            )
            == _REBIND_NAME
        )
        assert await _history_snapshot(chain) == history_before

        # ---- 真实 /轮盘 开局 读取新名（独立新局）--------------------------
        create_id = chain.next_msg_id("b2-rebind-create")
        sent = await chain.send_qq(
            CMD_CREATE,
            member_openid=target.member_openid,
            message_id=create_id,
        )
        assert len(sent) == 1, sent
        receipts = {
            row["inbound_msg_id"]: row["result_code"]
            for row in await chain.receipt_rows()
        }
        assert receipts[create_id] == "created", receipts
        snapshot = await chain.current_snapshot()
        assert snapshot is not None
        assert snapshot.lifecycle == "waiting"
        frozen_new = {
            seat.member_openid: seat.display_name for seat in snapshot.players
        }
        assert frozen_new == {target.member_openid: _REBIND_NAME}, frozen_new
        # 历史 / 胜场仍不被修复或重绑改写。
        assert await _history_snapshot(chain) == history_before


async def test_window_scoped_patches_do_not_leak_between_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同一用例开多个链窗口时随机源替身不泄漏，且窗口不撤销调用方外层补丁。"""
    require_postgres()

    from nonebot import get_driver

    from komari_bot.plugins.komari_roulette import command_service

    real_source = command_service._DefaultRandomSource
    assert isinstance(real_source, type), real_source
    driver_config_before = get_driver().config

    # 第一个窗口：窗口内安装脚本替身，退出后必须逐条还原真实随机源与 driver。
    async with chain_context(
        monkeypatch,
        seat_names={_TARGET_SEAT: "甲"},
        random_source=ScriptedRandomSource(),
    ):
        inside_source = sys.modules[COMMAND_SERVICE_MODULE]._DefaultRandomSource
        assert inside_source is not real_source, "窗口内必须安装脚本随机源替身"
        assert not isinstance(inside_source, type)

    assert sys.modules[COMMAND_SERVICE_MODULE]._DefaultRandomSource is real_source, (
        "窗口退出必须逐条还原 _DefaultRandomSource"
    )
    assert get_driver().config is driver_config_before, "窗口退出必须还原 driver config"

    # 第二个窗口：外层先写入不透明哨兵（模拟用例级补丁 / 故障注入 / 旧 runtime
    # 哨兵），窗口退出必须还原到外层哨兵而不是真实类。
    outer_sentinel = object()
    with monkeypatch.context() as outer_patch:
        outer_patch.setattr(command_service, "_DefaultRandomSource", outer_sentinel)
        async with chain_context(
            monkeypatch,
            seat_names={_TARGET_SEAT: "乙"},
            random_source=ScriptedRandomSource(),
        ):
            second = sys.modules[COMMAND_SERVICE_MODULE]._DefaultRandomSource
            assert second is not real_source and second is not outer_sentinel, (
                "窗口内必须安装自己的脚本随机源替身"
            )
        assert (
            sys.modules[COMMAND_SERVICE_MODULE]._DefaultRandomSource is outer_sentinel
        ), "窗口退出必须还原调用方外层补丁，不得覆盖为真实类"

    assert sys.modules[COMMAND_SERVICE_MODULE]._DefaultRandomSource is real_source, (
        "外层补丁撤销后必须回到真实随机源"
    )

    # 第三个窗口不得继承任何前一窗口写入的替身。
    async with chain_context(monkeypatch, seat_names={_TARGET_SEAT: "丙"}):
        third = sys.modules[COMMAND_SERVICE_MODULE]._DefaultRandomSource
        assert third is real_source, "后续窗口不得继承前一窗口的随机源替身"
