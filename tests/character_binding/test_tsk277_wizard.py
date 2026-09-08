"""TSK-277 绑定向导：视图、按钮、TTL、作用域、旧名与重审（非 PG）。

所有断言只通过 wizard 公共 seam 观察：`handle_event` 返回的 `WizardReply`、
`get_session` 的不可变视图、以及协调器端口上的调用记录。生产算法未落地时，
`require_wizard_contract()` 给出明确业务 RED，而不是 import 崩溃。
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from tests.character_binding.tsk277_support import (
    APP_ID,
    BAD_COMMAND,
    BINDING_CONFIRM,
    CANCEL_BUTTON,
    CANCELLED,
    CHALLENGE_BODY,
    CONTINUE_BUTTON,
    EXPIRED,
    FILL_NAME_BUTTON,
    GROUP_ID,
    GROUP_OPENID,
    LEGACY_CHOICE,
    MEMBER_OPENID,
    MEMBER_QQ,
    NAME_CONTROL_ERROR,
    NAME_FORMAT_ERROR,
    NAME_INPUT,
    NO_LEGACY,
    NOT_READY,
    NOT_YOUR_FLOW,
    REFILL_BUTTON,
    REUSE_BUTTON,
    SECOND_APP_ID,
    SECOND_GROUP_OPENID,
    SECOND_MEMBER_OPENID,
    SESSION_TTL,
    WRONG_GROUP,
    WRONG_STEP,
    FakeCoordinator,
    FrozenClock,
    buttons_of,
    make_claim,
    make_event,
    make_token,
    make_verified,
    require_wizard_contract,
)

SESSION = "qq277-session"


def _scope(
    module: Any,
    *,
    app_id: str = APP_ID,
    group_openid: str = GROUP_OPENID,
    member_openid: str = MEMBER_OPENID,
) -> Any:
    return module.WizardScope(
        app_id=app_id,
        group_openid=group_openid,
        member_openid=member_openid,
    )


class _UnexpectedSessionOpenError(RuntimeError):
    """非 PG 用例不得打开数据库会话。"""


def _unused_factory() -> Any:
    raise _UnexpectedSessionOpenError


def _wizard(
    module: Any,
    *,
    clock: FrozenClock,
    coordinator: FakeCoordinator,
    legacy: str | None = None,
    manager: Any = None,
) -> Any:
    async def legacy_loader(_member_qq: str) -> str | None:
        return legacy

    return module.BindingWizard(
        coordinator=coordinator,
        session_factory=_unused_factory,
        clock=clock,
        manager=manager,
        legacy_loader=legacy_loader,
    )


async def test_first_unmapped_bind_sends_one_challenge_and_repeat_is_silent() -> None:
    """AC1/AC3/AC4/AC11：首次挑战文案与按钮，重复 /bind 不重建、不续期、不重发。"""
    module = require_wizard_contract()
    clock = FrozenClock()
    claim = make_claim(clock=clock, session_code=SESSION, qq_message_id="qq-msg-1")
    coordinator = FakeCoordinator(claim=claim)
    wizard = _wizard(module, clock=clock, coordinator=coordinator)
    token = make_token(
        scope="binding_challenge",
        claim=claim,
        qq_message_id="qq-msg-1",
    )

    reply = await wizard.handle_event(
        make_event(content="/bind", message_id="qq-msg-1"),
        token,
    )

    assert reply is not None
    assert reply.body == CHALLENGE_BODY.format(session=SESSION)
    assert buttons_of(reply) == [
        (CONTINUE_BUTTON, "/bind", 2),
        (CANCEL_BUTTON, f"/bind cancel {SESSION}", 2),
    ]
    assert reply.reply_to_message_id == "qq-msg-1"

    view = await wizard.get_session(_scope(module))
    assert view is not None
    assert view.step == "challenge_pending"
    assert view.session_code == SESSION
    assert view.expires_at == claim.expires_at
    assert view.challenge_sent is True

    clock.advance(timedelta(minutes=1))
    repeat_claim = make_claim(
        clock=clock,
        session_code=SESSION,
        qq_message_id="qq-msg-2",
        is_new=False,
    )
    repeat_token = make_token(
        scope="binding_challenge",
        claim=repeat_claim,
        qq_message_id="qq-msg-2",
    )
    assert (
        await wizard.handle_event(
            make_event(content="/bind", message_id="qq-msg-2"),
            repeat_token,
        )
        is None
    )

    repeat_view = await wizard.get_session(_scope(module))
    assert repeat_view is not None
    assert repeat_view.session_code == SESSION
    assert repeat_view.expires_at == claim.expires_at
    assert coordinator.claim_calls == []


async def test_mapped_unbound_member_requests_single_claim_then_not_ready() -> None:
    """AC3/AC9：已映射但成员未绑定走 BUSINESS 取证；重复 /bind 只提示未就绪。"""
    module = require_wizard_contract()
    clock = FrozenClock()
    claim = make_claim(clock=clock, session_code=SESSION, qq_message_id="qq-msg-1")
    coordinator = FakeCoordinator(claim=claim)
    wizard = _wizard(module, clock=clock, coordinator=coordinator)

    token = make_token(
        scope="business",
        group_id=277001,
        member_qq=None,
        qq_message_id="qq-msg-1",
    )
    reply = await wizard.handle_event(
        make_event(content="/bind", message_id="qq-msg-1"),
        token,
    )

    assert reply is not None
    assert reply.body == CHALLENGE_BODY.format(session=SESSION)
    assert len(coordinator.claim_calls) == 1
    assert coordinator.claim_calls[0].command.strip() == "/bind"

    repeat_token = make_token(
        scope="business",
        group_id=277001,
        member_qq=None,
        qq_message_id="qq-msg-2",
    )
    repeat = await wizard.handle_event(
        make_event(content="/bind", message_id="qq-msg-2"),
        repeat_token,
    )

    assert repeat is not None
    assert repeat.body == NOT_READY
    assert len(coordinator.claim_calls) == 1
    view = await wizard.get_session(_scope(module))
    assert view is not None
    assert view.session_code == SESSION
    assert view.expires_at == claim.expires_at


async def test_unknown_group_pending_evidence_advances_on_next_bind_without_rebuild() -> None:
    """AC4/AC3：未映射群 pending 会话在证据到达后由再次 /bind 推进，不重建不续期。"""
    module = require_wizard_contract()
    clock = FrozenClock()
    claim = make_claim(clock=clock, session_code=SESSION, qq_message_id="qq-msg-50")
    coordinator = FakeCoordinator(claim=claim)
    wizard = _wizard(module, clock=clock, coordinator=coordinator)
    challenge_token = make_token(
        scope="binding_challenge",
        claim=claim,
        qq_message_id="qq-msg-50",
    )

    first = await wizard.handle_event(
        make_event(content="/bind", message_id="qq-msg-50"),
        challenge_token,
    )
    assert first is not None
    assert first.body == CHALLENGE_BODY.format(session=SESSION)
    pending = await wizard.get_session(_scope(module))
    assert pending is not None
    assert pending.step == "challenge_pending"
    assert pending.expires_at == claim.expires_at

    # 273/274 静默取证只更新协调器；wizard 不主动发送任何 QQ 消息。
    verified = make_verified(
        clock=clock,
        session_code=SESSION,
        qq_message_id="qq-msg-50",
    )
    coordinator.verified = verified
    still_pending = await wizard.get_session(_scope(module))
    assert still_pending is not None
    assert still_pending.step == "challenge_pending"

    clock.advance(timedelta(minutes=1))
    binding_token = make_token(
        scope="binding",
        group_id=GROUP_ID,
        member_qq=MEMBER_QQ,
        verified_session=verified,
        qq_message_id="qq-msg-51",
    )
    advanced = await wizard.handle_event(
        make_event(content="/bind", message_id="qq-msg-51"),
        binding_token,
    )

    assert advanced is not None
    assert advanced.body == NAME_INPUT
    view = await wizard.get_session(_scope(module))
    assert view is not None
    assert view.session_code == SESSION, "证据推进不得重建会话"
    assert view.expires_at == claim.expires_at, "证据推进不得续期"
    assert view.step == "name_input"
    assert coordinator.claim_calls == []


async def test_mapped_pending_evidence_advances_to_legacy_without_reclaim() -> None:
    """AC4/AC7/AC9：已映射未绑定 pending 会话在证据到达后推进旧名选择，不重复取证。"""
    module = require_wizard_contract()
    clock = FrozenClock()
    claim = make_claim(clock=clock, session_code=SESSION, qq_message_id="qq-msg-52")
    coordinator = FakeCoordinator(claim=claim)
    wizard = _wizard(module, clock=clock, coordinator=coordinator, legacy="小明")
    business = make_token(
        scope="business",
        group_id=GROUP_ID,
        member_qq=None,
        qq_message_id="qq-msg-52",
    )

    first = await wizard.handle_event(
        make_event(content="/bind", message_id="qq-msg-52"),
        business,
    )
    assert first is not None
    assert first.body == CHALLENGE_BODY.format(session=SESSION)
    assert len(coordinator.claim_calls) == 1

    verified = make_verified(
        clock=clock,
        session_code=SESSION,
        qq_message_id="qq-msg-52",
    )
    coordinator.verified = verified
    clock.advance(timedelta(minutes=1))
    advanced = await wizard.handle_event(
        make_event(content="/bind", message_id="qq-msg-53"),
        make_token(
            scope="business",
            group_id=GROUP_ID,
            member_qq=None,
            qq_message_id="qq-msg-53",
        ),
    )

    assert advanced is not None
    assert advanced.body == LEGACY_CHOICE.format(name="小明")
    view = await wizard.get_session(_scope(module))
    assert view is not None
    assert view.step == "legacy_choice"
    assert view.session_code == SESSION
    assert view.expires_at == claim.expires_at
    assert len(coordinator.claim_calls) == 1, "证据推进不得重复初始取证"


async def test_verified_identity_skips_challenge_and_shows_name_input() -> None:
    """AC4/AC7：已有有效身份关联跳过核验，直接进入名字输入，不重新取证。"""
    module = require_wizard_contract()
    clock = FrozenClock()
    verified = make_verified(clock=clock, session_code=SESSION)
    coordinator = FakeCoordinator(verified=verified)
    wizard = _wizard(module, clock=clock, coordinator=coordinator)
    token = make_token(
        scope="binding",
        group_id=277001,
        member_qq=MEMBER_QQ,
        verified_session=verified,
        qq_message_id="qq-msg-3",
    )

    reply = await wizard.handle_event(
        make_event(content="/bind", message_id="qq-msg-3"),
        token,
    )

    assert reply is not None
    assert reply.body == NAME_INPUT
    assert buttons_of(reply) == [
        (FILL_NAME_BUTTON, f"/bind name {SESSION} ", 2),
        (CANCEL_BUTTON, f"/bind cancel {SESSION}", 2),
    ]
    assert coordinator.claim_calls == []
    view = await wizard.get_session(_scope(module))
    assert view is not None
    assert view.step == "name_input"
    assert view.operation == "bind"
    assert view.character_name is None


async def test_legacy_candidate_only_after_verified_active_bind() -> None:
    """AC7：旧名只在主动 /bind + 身份已验证 + 当前群未绑定时提供候选。"""
    module = require_wizard_contract()
    clock = FrozenClock()
    verified = make_verified(clock=clock, session_code=SESSION)
    coordinator = FakeCoordinator(verified=verified)
    wizard = _wizard(module, clock=clock, coordinator=coordinator, legacy="小明")
    token = make_token(
        scope="binding",
        group_id=277001,
        member_qq=MEMBER_QQ,
        verified_session=verified,
        qq_message_id="qq-msg-4",
    )

    reply = await wizard.handle_event(
        make_event(content="/bind", message_id="qq-msg-4"),
        token,
    )

    assert reply is not None
    assert reply.body == LEGACY_CHOICE.format(name="小明")
    assert buttons_of(reply) == [
        (REUSE_BUTTON, f"/bind reuse {SESSION}", 2),
        (REFILL_BUTTON, f"/bind name {SESSION} ", 2),
        (CANCEL_BUTTON, f"/bind cancel {SESSION}", 2),
    ]
    view = await wizard.get_session(_scope(module))
    assert view is not None
    assert view.step == "legacy_choice"


async def test_unverified_identity_never_reads_legacy_candidate() -> None:
    """AC7：身份未验证时只发挑战，不读取旧名候选。"""
    module = require_wizard_contract()
    clock = FrozenClock()
    claim = make_claim(clock=clock, session_code=SESSION, qq_message_id="qq-msg-5")
    coordinator = FakeCoordinator(claim=claim)
    reads: list[str] = []

    async def legacy_loader(member_qq: str) -> str | None:
        reads.append(member_qq)
        return "小明"

    wizard = module.BindingWizard(
        coordinator=coordinator,
        session_factory=_unused_factory,
        clock=clock,
        legacy_loader=legacy_loader,
    )
    token = make_token(
        scope="binding_challenge",
        claim=claim,
        qq_message_id="qq-msg-5",
    )

    reply = await wizard.handle_event(
        make_event(content="/bind", message_id="qq-msg-5"),
        token,
    )

    assert reply is not None
    assert reply.body == CHALLENGE_BODY.format(session=SESSION)
    assert reads == []


async def test_name_command_uses_full_remaining_text_and_normalizes() -> None:
    """AC2/AC11：/bind name <session> 之后完整剩余文本交给规范器，不按空格截断。"""
    module = require_wizard_contract()
    clock = FrozenClock()
    verified = make_verified(clock=clock, session_code=SESSION)
    coordinator = FakeCoordinator(verified=verified)
    wizard = _wizard(module, clock=clock, coordinator=coordinator)
    token = make_token(
        scope="binding",
        group_id=277001,
        member_qq=MEMBER_QQ,
        verified_session=verified,
        qq_message_id="qq-msg-6",
    )
    await wizard.handle_event(make_event(content="/bind", message_id="qq-msg-6"), token)

    reply = await wizard.handle_event(
        make_event(
            content=f"/bind name {SESSION}   阿   明  ",
            message_id="qq-msg-7",
        ),
        token,
    )

    assert reply is not None
    assert reply.body == BINDING_CONFIRM.format(name="阿 明")
    view = await wizard.get_session(_scope(module))
    assert view is not None
    assert view.step == "binding_confirm"
    assert view.character_name == "阿 明"


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("a" * 65, NAME_FORMAT_ERROR),
        ("   ", NAME_FORMAT_ERROR),
        ("阿\n明", NAME_CONTROL_ERROR),
        ("阿\u200b明", NAME_CONTROL_ERROR),
        ("阿\u2028明", NAME_CONTROL_ERROR),
        ("阿\u2029明", NAME_CONTROL_ERROR),
    ],
    ids=["too-long", "blank", "newline", "zero-width", "line-sep", "para-sep"],
)
async def test_name_validation_rejects_and_keeps_current_step(
    name: str,
    expected: str,
) -> None:
    """AC2：1–64 码点、拒绝 Cc/Cf/Zl/Zp；错误不推进阶段、不覆盖草稿。"""  # noqa: RUF002
    module = require_wizard_contract()
    clock = FrozenClock()
    verified = make_verified(clock=clock, session_code=SESSION)
    coordinator = FakeCoordinator(verified=verified)
    wizard = _wizard(module, clock=clock, coordinator=coordinator)
    token = make_token(
        scope="binding",
        group_id=277001,
        member_qq=MEMBER_QQ,
        verified_session=verified,
        qq_message_id="qq-msg-8",
    )
    await wizard.handle_event(make_event(content="/bind", message_id="qq-msg-8"), token)

    reply = await wizard.handle_event(
        make_event(content=f"/bind name {SESSION} {name}", message_id="qq-msg-9"),
        token,
    )

    assert reply is not None
    assert reply.body == expected
    view = await wizard.get_session(_scope(module))
    assert view is not None
    assert view.step == "name_input"
    assert view.character_name is None


async def test_name_rendering_does_not_inject_markdown_or_real_mention() -> None:
    """AC2：名字中的 Markdown/mention 记号按普通文本处理，不产生真实提及。"""
    module = require_wizard_contract()
    clock = FrozenClock()
    verified = make_verified(clock=clock, session_code=SESSION)
    coordinator = FakeCoordinator(verified=verified)
    wizard = _wizard(module, clock=clock, coordinator=coordinator)
    token = make_token(
        scope="binding",
        group_id=277001,
        member_qq=MEMBER_QQ,
        verified_session=verified,
        qq_message_id="qq-msg-10",
    )
    await wizard.handle_event(make_event(content="/bind", message_id="qq-msg-10"), token)

    reply = await wizard.handle_event(
        make_event(
            content=f"/bind name {SESSION} <@123456> **加粗** [CQ:at,qq=all]",
            message_id="qq-msg-11",
        ),
        token,
    )

    assert reply is not None
    assert "<@" not in reply.body
    assert "CQ:at,qq=all" in reply.body
    # 模板只有两处 ** 定界；名字里的 ** 必须被转义，不能新增 Markdown 排版。
    assert reply.body.count("**") == 2


async def test_reuse_without_legacy_name_reports_missing_candidate() -> None:
    """AC7：无旧名可沿用时给出定稿提示，不推进阶段。"""
    module = require_wizard_contract()
    clock = FrozenClock()
    verified = make_verified(clock=clock, session_code=SESSION)
    coordinator = FakeCoordinator(verified=verified)
    wizard = _wizard(module, clock=clock, coordinator=coordinator, legacy=None)
    token = make_token(
        scope="binding",
        group_id=277001,
        member_qq=MEMBER_QQ,
        verified_session=verified,
        qq_message_id="qq-msg-12",
    )
    await wizard.handle_event(make_event(content="/bind", message_id="qq-msg-12"), token)

    reply = await wizard.handle_event(
        make_event(content=f"/bind reuse {SESSION}", message_id="qq-msg-13"),
        token,
    )

    assert reply is not None
    assert reply.body == NO_LEGACY
    view = await wizard.get_session(_scope(module))
    assert view is not None
    assert view.step == "name_input"


async def test_ttl_is_absolute_ten_minutes_and_repeat_does_not_extend() -> None:
    """AC3：创建起 10 分钟绝对 TTL；恰好到期拒绝；重复命令不续期。"""
    module = require_wizard_contract()
    clock = FrozenClock()
    verified = make_verified(clock=clock, session_code=SESSION)
    coordinator = FakeCoordinator(verified=verified)
    wizard = _wizard(module, clock=clock, coordinator=coordinator)
    token = make_token(
        scope="binding",
        group_id=277001,
        member_qq=MEMBER_QQ,
        verified_session=verified,
        qq_message_id="qq-msg-14",
    )
    await wizard.handle_event(make_event(content="/bind", message_id="qq-msg-14"), token)
    created = await wizard.get_session(_scope(module))
    assert created is not None
    assert created.expires_at == clock() + SESSION_TTL

    clock.advance(timedelta(minutes=1))
    repeat = await wizard.handle_event(
        make_event(content="/bind", message_id="qq-msg-15"),
        token,
    )
    assert repeat is not None
    assert repeat.body == NAME_INPUT
    refreshed = await wizard.get_session(_scope(module))
    assert refreshed is not None
    assert refreshed.session_code == created.session_code
    assert refreshed.expires_at == created.expires_at

    clock.advance(timedelta(minutes=8, seconds=59))
    still_valid = await wizard.handle_event(
        make_event(content=f"/bind name {SESSION} 阿明", message_id="qq-msg-16"),
        token,
    )
    assert still_valid is not None
    assert still_valid.body == BINDING_CONFIRM.format(name="阿明")

    clock.advance(timedelta(seconds=1))
    expired = await wizard.handle_event(
        make_event(content=f"/bind confirm {SESSION}", message_id="qq-msg-17"),
        token,
    )
    assert expired is not None
    assert expired.body == EXPIRED
    assert await wizard.get_session(_scope(module)) is None


async def test_sessions_are_isolated_per_member_and_cancel_keeps_other() -> None:
    """AC3/AC5：不同成员各自一草稿；取消一方不影响另一方。"""
    module = require_wizard_contract()
    clock = FrozenClock()
    first = make_verified(clock=clock, session_code="session-a")
    second = make_verified(
        clock=clock,
        session_code="session-b",
        member_openid=SECOND_MEMBER_OPENID,
        member_qq=277003,
    )
    coordinator = FakeCoordinator(verified=first)
    wizard = _wizard(module, clock=clock, coordinator=coordinator)
    token_a = make_token(
        scope="binding",
        group_id=277001,
        member_qq=MEMBER_QQ,
        verified_session=first,
        qq_message_id="qq-msg-18",
    )
    token_b = make_token(
        scope="binding",
        group_id=277001,
        member_openid=SECOND_MEMBER_OPENID,
        member_qq=277003,
        verified_session=second,
        qq_message_id="qq-msg-19",
    )

    await wizard.handle_event(make_event(content="/bind", message_id="qq-msg-18"), token_a)
    await wizard.handle_event(
        make_event(
            content="/bind",
            message_id="qq-msg-19",
            member_openid=SECOND_MEMBER_OPENID,
        ),
        token_b,
    )
    cancelled = await wizard.handle_event(
        make_event(content="/bind cancel session-a", message_id="qq-msg-20"),
        token_a,
    )

    assert cancelled is not None
    assert cancelled.body == CANCELLED
    assert await wizard.get_session(_scope(module)) is None
    other = await wizard.get_session(
        _scope(module, member_openid=SECOND_MEMBER_OPENID)
    )
    assert other is not None
    assert other.session_code == "session-b"


async def test_cross_member_group_and_app_commands_are_rejected() -> None:
    """AC5：会话码/成员/群/应用全部核验，旧码失效且不影响原会话。"""
    module = require_wizard_contract()
    clock = FrozenClock()
    verified = make_verified(clock=clock, session_code=SESSION)
    coordinator = FakeCoordinator(verified=verified)
    wizard = _wizard(module, clock=clock, coordinator=coordinator)
    token = make_token(
        scope="binding",
        group_id=277001,
        member_qq=MEMBER_QQ,
        verified_session=verified,
        qq_message_id="qq-msg-21",
    )
    await wizard.handle_event(make_event(content="/bind", message_id="qq-msg-21"), token)

    foreign_member = await wizard.handle_event(
        make_event(
            content=f"/bind cancel {SESSION}",
            message_id="qq-msg-22",
            member_openid=SECOND_MEMBER_OPENID,
        ),
        make_token(
            scope="binding",
            group_id=277001,
            member_openid=SECOND_MEMBER_OPENID,
            member_qq=277003,
            qq_message_id="qq-msg-22",
        ),
    )
    assert foreign_member is not None
    assert foreign_member.body == NOT_YOUR_FLOW

    wrong_group = await wizard.handle_event(
        make_event(
            content=f"/bind cancel {SESSION}",
            message_id="qq-msg-23",
            group_openid=SECOND_GROUP_OPENID,
        ),
        make_token(
            scope="binding",
            group_openid=SECOND_GROUP_OPENID,
            group_id=277002,
            member_qq=MEMBER_QQ,
            qq_message_id="qq-msg-23",
        ),
    )
    assert wrong_group is not None
    assert wrong_group.body == WRONG_GROUP

    wrong_app = await wizard.handle_event(
        make_event(content=f"/bind cancel {SESSION}", message_id="qq-msg-24"),
        make_token(
            scope="binding",
            app_id=SECOND_APP_ID,
            group_id=277001,
            member_qq=MEMBER_QQ,
            qq_message_id="qq-msg-24",
        ),
    )
    assert wrong_app is not None
    assert wrong_app.body == NOT_YOUR_FLOW

    still_there = await wizard.get_session(_scope(module))
    assert still_there is not None
    assert still_there.session_code == SESSION
    assert still_there.step == "name_input"


async def test_cancelled_session_code_is_stale_for_new_session() -> None:
    """AC5：取消后旧按钮/旧确认失效，不影响新会话。"""
    module = require_wizard_contract()
    clock = FrozenClock()
    old = make_verified(clock=clock, session_code="session-old")
    coordinator = FakeCoordinator(verified=old)
    wizard = _wizard(module, clock=clock, coordinator=coordinator)
    token = make_token(
        scope="binding",
        group_id=277001,
        member_qq=MEMBER_QQ,
        verified_session=old,
        qq_message_id="qq-msg-25",
    )
    await wizard.handle_event(make_event(content="/bind", message_id="qq-msg-25"), token)
    await wizard.handle_event(
        make_event(content="/bind cancel session-old", message_id="qq-msg-26"),
        token,
    )

    new = make_verified(clock=clock, session_code="session-new")
    coordinator.verified = new
    new_token = make_token(
        scope="binding",
        group_id=277001,
        member_qq=MEMBER_QQ,
        verified_session=new,
        qq_message_id="qq-msg-27",
    )
    await wizard.handle_event(make_event(content="/bind", message_id="qq-msg-27"), new_token)

    stale = await wizard.handle_event(
        make_event(content="/bind cancel session-old", message_id="qq-msg-28"),
        new_token,
    )
    assert stale is not None
    assert stale.body == EXPIRED
    current = await wizard.get_session(_scope(module))
    assert current is not None
    assert current.session_code == "session-new"
    assert current.step == "name_input"


async def test_cancel_drops_draft_even_when_send_admission_denies() -> None:
    """AC5/AC9：取消不绕过发送准入；被拒时静默但本地草稿仍丢弃。"""
    module = require_wizard_contract()
    clock = FrozenClock()
    verified = make_verified(clock=clock, session_code=SESSION)
    coordinator = FakeCoordinator(verified=verified)
    wizard = _wizard(module, clock=clock, coordinator=coordinator)
    token = make_token(
        scope="binding",
        group_id=277001,
        member_qq=MEMBER_QQ,
        verified_session=verified,
        qq_message_id="qq-msg-29",
    )
    await wizard.handle_event(make_event(content="/bind", message_id="qq-msg-29"), token)

    coordinator.recheck_script = [False]
    denied = await wizard.handle_event(
        make_event(content=f"/bind cancel {SESSION}", message_id="qq-msg-30"),
        token,
    )

    assert denied is None
    assert await wizard.get_session(_scope(module)) is None
    assert SESSION in coordinator.cancelled


async def test_denied_recheck_suppresses_visible_reply_and_draft_effect() -> None:
    """AC9：可见效果前重审失败必须静默且不改变草稿。"""
    module = require_wizard_contract()
    clock = FrozenClock()
    verified = make_verified(clock=clock, session_code=SESSION)
    coordinator = FakeCoordinator(verified=verified)
    wizard = _wizard(module, clock=clock, coordinator=coordinator)
    token = make_token(
        scope="binding",
        group_id=277001,
        member_qq=MEMBER_QQ,
        verified_session=verified,
        qq_message_id="qq-msg-31",
    )
    await wizard.handle_event(make_event(content="/bind", message_id="qq-msg-31"), token)

    coordinator.recheck_script = [False]
    denied = await wizard.handle_event(
        make_event(content=f"/bind name {SESSION} 阿明", message_id="qq-msg-32"),
        token,
    )

    assert denied is None
    view = await wizard.get_session(_scope(module))
    assert view is not None
    assert view.step == "name_input"
    assert view.character_name is None
    assert coordinator.recheck_calls[-1][1] == "binding"


async def test_wrong_step_and_unknown_command_use_authoritative_texts() -> None:
    """AC11：confirm 只在最终阶段可用；未知命令与错误阶段使用定稿文案。"""
    module = require_wizard_contract()
    clock = FrozenClock()
    verified = make_verified(clock=clock, session_code=SESSION)
    coordinator = FakeCoordinator(verified=verified)
    wizard = _wizard(module, clock=clock, coordinator=coordinator)
    token = make_token(
        scope="binding",
        group_id=277001,
        member_qq=MEMBER_QQ,
        verified_session=verified,
        qq_message_id="qq-msg-33",
    )
    await wizard.handle_event(make_event(content="/bind", message_id="qq-msg-33"), token)

    wrong_step = await wizard.handle_event(
        make_event(content=f"/bind confirm {SESSION}", message_id="qq-msg-34"),
        token,
    )
    assert wrong_step is not None
    assert wrong_step.body == WRONG_STEP

    bad_command = await wizard.handle_event(
        make_event(content="/bind bogus", message_id="qq-msg-35"),
        token,
    )
    assert bad_command is not None
    assert bad_command.body == BAD_COMMAND

    unknown_session = await wizard.handle_event(
        make_event(content="/bind name other-session 阿明", message_id="qq-msg-36"),
        token,
    )
    assert unknown_session is not None
    assert unknown_session.body == EXPIRED


async def test_active_draft_blocks_rename_and_unbind_switch() -> None:
    """AC6：有活动流程时 rename/unbind 不覆盖草稿，须先取消。"""
    module = require_wizard_contract()
    clock = FrozenClock()
    verified = make_verified(clock=clock, session_code=SESSION)
    coordinator = FakeCoordinator(verified=verified)
    wizard = _wizard(module, clock=clock, coordinator=coordinator)
    token = make_token(
        scope="binding",
        group_id=277001,
        member_qq=MEMBER_QQ,
        verified_session=verified,
        qq_message_id="qq-msg-37",
    )
    await wizard.handle_event(make_event(content="/bind", message_id="qq-msg-37"), token)

    rename = await wizard.handle_event(
        make_event(content="/bind rename", message_id="qq-msg-38"),
        token,
    )
    assert rename is not None
    assert rename.body == WRONG_STEP
    view = await wizard.get_session(_scope(module))
    assert view is not None
    assert view.step == "name_input"
    assert view.operation == "bind"


async def test_duplicate_inbound_message_is_not_processed_twice() -> None:
    """AC3/AC8：同一入站消息 ID 重复投递不重复推进、不重复发送。"""
    module = require_wizard_contract()
    clock = FrozenClock()
    verified = make_verified(clock=clock, session_code=SESSION)
    coordinator = FakeCoordinator(verified=verified)
    wizard = _wizard(module, clock=clock, coordinator=coordinator)
    token = make_token(
        scope="binding",
        group_id=277001,
        member_qq=MEMBER_QQ,
        verified_session=verified,
        qq_message_id="qq-msg-39",
    )
    await wizard.handle_event(make_event(content="/bind", message_id="qq-msg-39"), token)

    event = make_event(content=f"/bind name {SESSION} 阿明", message_id="qq-msg-40")
    first = await wizard.handle_event(event, token)
    second = await wizard.handle_event(event, token)

    assert first is not None
    assert first.body == BINDING_CONFIRM.format(name="阿明")
    assert second is None
    view = await wizard.get_session(_scope(module))
    assert view is not None
    assert view.character_name == "阿明"
    assert view.step == "binding_confirm"


async def test_duplicate_inbound_with_changed_content_is_not_processed() -> None:
    """AC3/AC8：重复入站键是 scope+message_id；content 变化也不得执行。"""
    module = require_wizard_contract()
    clock = FrozenClock()
    verified = make_verified(clock=clock, session_code=SESSION)
    coordinator = FakeCoordinator(verified=verified)
    wizard = _wizard(module, clock=clock, coordinator=coordinator)
    token = make_token(
        scope="binding",
        group_id=277001,
        member_qq=MEMBER_QQ,
        verified_session=verified,
        qq_message_id="qq-msg-42",
    )
    await wizard.handle_event(
        make_event(content="/bind", message_id="qq-msg-42"),
        token,
    )
    first = await wizard.handle_event(
        make_event(content=f"/bind name {SESSION} 阿明", message_id="qq-msg-43"),
        token,
    )
    assert first is not None
    assert first.body == BINDING_CONFIRM.format(name="阿明")

    # 相同 scope+message_id、不同 content：不得执行（否则会提交绑定）。
    changed = await wizard.handle_event(
        make_event(content=f"/bind confirm {SESSION}", message_id="qq-msg-43"),
        token,
    )

    assert changed is None
    view = await wizard.get_session(_scope(module))
    assert view is not None
    assert view.step == "binding_confirm"
    assert view.character_name == "阿明"


async def test_same_message_id_in_different_scope_is_processed() -> None:
    """AC3/AC8：重复入站键包含 scope；不同成员同 message_id 必须各自处理。"""
    module = require_wizard_contract()
    clock = FrozenClock()
    first_verified = make_verified(clock=clock, session_code="scope-a")
    second_verified = make_verified(
        clock=clock,
        session_code="scope-b",
        member_openid=SECOND_MEMBER_OPENID,
        member_qq=277003,
    )
    coordinator = FakeCoordinator(verified=first_verified)
    wizard = _wizard(module, clock=clock, coordinator=coordinator)
    first_token = make_token(
        scope="binding",
        group_id=277001,
        member_qq=MEMBER_QQ,
        verified_session=first_verified,
        qq_message_id="shared-id",
    )
    second_token = make_token(
        scope="binding",
        group_id=277001,
        member_openid=SECOND_MEMBER_OPENID,
        member_qq=277003,
        verified_session=second_verified,
        qq_message_id="shared-id",
    )

    first = await wizard.handle_event(
        make_event(content="/bind", message_id="shared-id"),
        first_token,
    )
    second = await wizard.handle_event(
        make_event(
            content="/bind",
            message_id="shared-id",
            member_openid=SECOND_MEMBER_OPENID,
        ),
        second_token,
    )

    assert first is not None
    assert first.body == NAME_INPUT
    assert second is not None, "不同 scope 的相同 message_id 不得被去重"
    assert second.body == NAME_INPUT
    assert await wizard.get_session(_scope(module)) is not None
    assert (
        await wizard.get_session(_scope(module, member_openid=SECOND_MEMBER_OPENID))
        is not None
    )


async def test_finish_send_is_noop_for_non_completed_replies() -> None:
    """AC10：非 completed 回复 finish_send 不得取消草稿或临时会话。"""
    module = require_wizard_contract()
    clock = FrozenClock()
    claim = make_claim(clock=clock, session_code=SESSION, qq_message_id="qq-msg-70")
    coordinator = FakeCoordinator(claim=claim)
    wizard = _wizard(module, clock=clock, coordinator=coordinator)
    challenge_token = make_token(
        scope="binding_challenge",
        claim=claim,
        qq_message_id="qq-msg-70",
    )
    challenge = await wizard.handle_event(
        make_event(content="/bind", message_id="qq-msg-70"),
        challenge_token,
    )
    assert challenge is not None
    await wizard.finish_send(challenge)
    assert coordinator.cancelled == []
    pending = await wizard.get_session(_scope(module))
    assert pending is not None
    assert pending.step == "challenge_pending"

    verified = make_verified(
        clock=clock,
        session_code=SESSION,
        qq_message_id="qq-msg-70",
    )
    coordinator.verified = verified
    name_token = make_token(
        scope="binding",
        group_id=277001,
        member_qq=MEMBER_QQ,
        verified_session=verified,
        qq_message_id="qq-msg-71",
    )
    name_reply = await wizard.handle_event(
        make_event(content="/bind", message_id="qq-msg-71"),
        name_token,
    )
    assert name_reply is not None
    assert name_reply.body == NAME_INPUT
    await wizard.finish_send(name_reply)
    assert coordinator.cancelled == []
    still = await wizard.get_session(_scope(module))
    assert still is not None
    assert still.step == "name_input"
