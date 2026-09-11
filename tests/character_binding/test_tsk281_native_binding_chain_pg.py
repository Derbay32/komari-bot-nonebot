"""TSK-281 阶段 B1a：真实首次绑定链端到端贯通（测试专用装配）。

只覆盖一条链：真实 ``character_binding.init_plugin`` 安装的 OneBot 监听器
（真实 ``reply_evidence`` matcher）经 ``get_msg`` 采集引用证据 → 真实
``QQBindingCoordinator`` 核验身份 → 真实 ``BindingWizard`` 完成
``/bind name`` / ``/bind confirm`` → 真实 ``CharacterBindingManager`` /
``BindingTransaction`` 在真实 PostgreSQL 上原子提交群/成员/角色名并发布
进程内快照。OneBot 侧只允许 ``get_msg`` 读取，绝不发送。

替换缝（本用例并非「只替换平台传输」）：

* 平台传输：``QQProbeBot`` / ``OneBotGetMsgTransport`` 覆盖 ``call_api``，QQ
  发送只记录载荷、OneBot ``get_msg`` 返回受控载荷，不触网；
* 准入策略存储：``AdmissionStorageFake`` 替换真实 ``ConfigStorage``（无后台
  轮询、快照完全由测试驱动），真实 ``_AdmissionRuntime`` 仍据此提供
  ``effective_revision``；策略为本用例数字群的受限白名单；
* 封禁检查：monkeypatch ``user_ban.is_configured_superuser_id`` /
  ``is_user_banned`` 为恒不封禁替身，真实 ``komari_user_bans`` 表与 revision
  缓存未被本用例执行。

真实保留：包导入注册的 OneBot 监听器、``group_admission`` 事件门禁
（``event_gate_context`` 重载注册）、``QQBindingCoordinator``、
``BindingWizard``、``CharacterBindingManager``、``BindingTransaction`` 与
PostgreSQL（共享 ORM 引擎、canonical 群/成员表、组级 advisory 事务锁）。

正式绑定不通过 ``bind_group_member`` 预置，证据不经合成后直接
``accept_reply_evidence``。

本用例复用 ``tsk281_native_chain_support`` 抽取的真实装配窗口；所有原断言
逐条保留。
"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import uuid4

import pytest

from tests.character_binding.conftest import (
    _reset_shared_orm_engine,
    require_postgres,
)
from tests.character_binding.test_reply_evidence import (
    COMMAND,
    _challenge_event,
    _get_msg_payload,
    _original_event,
)
from tests.character_binding.tsk277_support import (
    BIND_SUCCESS,
    BINDING_CONFIRM,
    NAME_INPUT,
    markdown_content,
    reference_message_id,
)
from tests.character_binding.tsk281_native_chain_support import (
    OneBotGetMsgTransport,
    binding_chain_window,
    cleanup_scope_rows,
    committed_rows,
    native_scope,
    numeric_id,
    scope_counts,
)
from tests.group_admission.entry_gate_support import dispatch
from tests.group_admission.qq_admission_support import (
    QQProbeBot,
    dispatch_qq,
    make_group_at,
)
from tests.komari_roulette.command_support import PG_REQUIRED, create_engine_and_factory

pytestmark = [pytest.mark.asyncio, PG_REQUIRED]

_CHARACTER_NAME = "阿明"


async def test_first_binding_chain_commits_once_through_real_onebot_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真实首次绑定：/bind → 引用挑战 → 身份核验 → name → confirm → 原子提交。"""
    require_postgres()
    current = native_scope("native")

    async for engine, _factory in create_engine_and_factory():
        await _reset_shared_orm_engine()
        try:
            async with binding_chain_window(monkeypatch, scope=current) as package:
                wizard = package.get_binding_wizard()
                assert wizard is not None, "init_plugin 必须安装真实向导"
                coordinator = wizard._coordinator
                qq = QQProbeBot(current.app_id)

                # 两端消息 id 本就互不相通：QQ 入站 id 是不可当 OneBot
                # 整数解析的独立字符串，OneBot 原消息/挑战使用另一个独立
                # 整数 id；两侧仅通过 session/原生引用证据桥接，id 不得相等。
                qq_original_message_id = f"tsk281-qq-{uuid4().hex[:10]}"
                onebot_original_message_id = numeric_id(uuid4().hex[:16])
                assert not qq_original_message_id.isdigit(), (
                    "QQ 原始消息 id 必须保留为不可当 OneBot 整数的字符串"
                )
                assert str(onebot_original_message_id) != qq_original_message_id, (
                    "QQ 与 OneBot 原始消息 id 必须互相独立，不得相等"
                )

                # 1) QQ 侧 /bind：真实 claim + 原生引用挑战
                await dispatch_qq(
                    qq,
                    make_group_at(
                        content="/bind",
                        message_id=qq_original_message_id,
                        group_openid=current.group_openid,
                        member_openid=current.member_openid,
                    ),
                )
                assert len(qq.calls) == 1
                first_challenge = qq.calls[0][1]
                challenge = markdown_content(first_challenge)
                assert challenge.startswith("正在确认你的本群身份。")
                session_code = challenge.split("会话码：", 1)[1].split("\n", 1)[0]
                # QQ 首次挑战的原生引用必须指向 QQ 自己的原始入站消息；
                # 它既不是 OneBot 的数字消息 id，也不与其共享任何 id。
                assert reference_message_id(first_challenge) == qq_original_message_id, (
                    "QQ 首次挑战必须原生引用本次原始入站消息"
                )

                # 2) OneBot 侧真实监听：自己的原消息 + 引用挑战 → get_msg → 证据
                challenge_id = onebot_original_message_id + 1
                onebot = OneBotGetMsgTransport()
                onebot.serve(
                    onebot_original_message_id,
                    _get_msg_payload(
                        message_id=onebot_original_message_id,
                        original_text=COMMAND,
                        group_id=current.group_id,
                        sender_id=current.member_qq,
                    ),
                )
                await dispatch(
                    onebot,
                    _original_event(
                        message_id=onebot_original_message_id,
                        group_id=current.group_id,
                        member_qq=current.member_qq,
                        text=COMMAND,
                    ),
                )
                await dispatch(
                    onebot,
                    _challenge_event(
                        message_id=challenge_id,
                        session_code=session_code,
                        quoted_message_id=onebot_original_message_id,
                        quoted_text=COMMAND,
                        group_id=current.group_id,
                        quoted_sender=current.member_qq,
                    ),
                )
                assert onebot.calls == [
                    ("get_msg", {"message_id": onebot_original_message_id})
                ]

                verified = await coordinator.resolve_verified_binding_session(
                    current.app_id,
                    current.group_openid,
                    current.member_openid,
                )
                assert verified is not None, "OneBot 证据必须经真实协调器核验"
                assert verified.session_code == session_code
                assert verified.group_id == current.group_id
                assert verified.member_qq == current.member_qq

                # 3) QQ 侧继续：/bind → 名字输入
                await dispatch_qq(
                    qq,
                    make_group_at(
                        content="/bind",
                        message_id="tsk281-2",
                        group_openid=current.group_openid,
                        member_openid=current.member_openid,
                    ),
                )
                assert len(qq.calls) == 2
                assert markdown_content(qq.calls[1][1]) == NAME_INPUT

                # 4) /bind name <会话码> <名字>
                await dispatch_qq(
                    qq,
                    make_group_at(
                        content=f"/bind name {session_code} {_CHARACTER_NAME}",
                        message_id="tsk281-3",
                        group_openid=current.group_openid,
                        member_openid=current.member_openid,
                    ),
                )
                assert len(qq.calls) == 3
                assert markdown_content(qq.calls[2][1]) == BINDING_CONFIRM.format(
                    name=_CHARACTER_NAME
                )

                # 5) confirm 之前不得存在任何正式群/成员/角色
                assert await scope_counts(engine, current) == (0, 0)
                manager = package.get_binding_manager()
                assert (
                    manager.get_qq_character_name(
                        app_id=current.app_id,
                        group_openid=current.group_openid,
                        member_openid=current.member_openid,
                    )
                    is None
                )

                # 6) /bind confirm <会话码> → 原子提交
                await asyncio.wait_for(
                    dispatch_qq(
                        qq,
                        make_group_at(
                            content=f"/bind confirm {session_code}",
                            message_id="tsk281-4",
                            group_openid=current.group_openid,
                            member_openid=current.member_openid,
                        ),
                    ),
                    timeout=30,
                )
                assert len(qq.calls) == 4
                assert markdown_content(qq.calls[3][1]) == BIND_SUCCESS.format(
                    name=_CHARACTER_NAME
                )

                # 7) 正式表：准确一份群映射 + 一份成员角色
                groups, members = await committed_rows(engine, current)
                assert len(groups) == 1
                assert groups[0]["group_id"] == str(current.group_id)
                assert len(members) == 1
                assert members[0]["member_openid"] == current.member_openid
                assert members[0]["member_qq"] == str(current.member_qq)
                assert members[0]["character_name"] == _CHARACTER_NAME

                # 8) 真实进程内快照发布
                assert (
                    manager.get_qq_character_name(
                        app_id=current.app_id,
                        group_openid=current.group_openid,
                        member_openid=current.member_openid,
                    )
                    == _CHARACTER_NAME
                )
                bindings = manager.list_group_bindings(
                    app_id=current.app_id,
                    group_openid=current.group_openid,
                )
                assert len(bindings) == 1
                assert bindings[0].member_openid == current.member_openid
                assert bindings[0].character_name == _CHARACTER_NAME
        finally:
            await cleanup_scope_rows(engine, current)
            await _reset_shared_orm_engine()


async def test_init_failure_after_coordinator_start_still_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真实 init 在 coordinator.start 之后半途失败仍必须关闭自有 manager/coordinator。

    故障注入点在 ``init_plugin`` 最后一步的 ``BindingWizard`` 构造：此时真实
    ``QQBindingCoordinator`` 已经 ``start()`` 并写入全局状态，属于“真实启动后
    失败”的最小真实路径。把 ``init_plugin`` 放在必达关闭范围之外时，窗口退出
    不会执行 ``close_plugin``，自有 coordinator / manager 会泄漏；本用例正以
    “失败后全局 coordinator 已清空、manager 已 close”作为区分性证据。
    """
    require_postgres()
    current = native_scope("init-fault")
    captured: dict[str, Any] = {}

    def before_init(package: Any) -> None:
        captured["package"] = package

        def _fail_wizard_construction(*_args: Any, **_kwargs: Any) -> Any:
            # 真实 coordinator 已 start，manager 已 initialize：这里取证后才失败。
            coordinator = package._qq_plugin_state.coordinator
            manager = package.get_manager()
            captured["coordinator"] = coordinator
            captured["manager"] = manager
            captured["coordinator_started"] = (
                coordinator is not None and coordinator._active is True
            )
            captured["manager_ready"] = manager.is_ready
            raise RuntimeError("tsk281 injected post-start init failure")  # noqa: TRY003

        monkeypatch.setattr(package, "BindingWizard", _fail_wizard_construction)

    async for engine, _factory in create_engine_and_factory():
        await _reset_shared_orm_engine()
        try:
            with pytest.raises(RuntimeError, match="injected"):
                async with binding_chain_window(
                    monkeypatch, scope=current, before_init=before_init
                ):
                    pytest.fail("init_plugin 失败时不得进入用例正文")

            package = captured["package"]
            manager = captured["manager"]
            assert captured["coordinator"] is not None, (
                "故障必须发生在真实 coordinator.start 之后"
            )
            assert captured["coordinator_started"] is True, (
                "取证时真实 coordinator 必须已启动"
            )
            assert captured["manager_ready"] is True, "取证时真实 manager 必须已就绪"
            assert package._qq_plugin_state.coordinator is None, (
                "失败窗口退出后必须清空自有 coordinator"
            )
            assert manager.is_ready is False, "失败窗口退出后必须 close 自有 manager"
            assert package.get_binding_wizard() is None, (
                "失败窗口退出后不得残留向导"
            )
        finally:
            await cleanup_scope_rows(engine, current)
            await _reset_shared_orm_engine()
