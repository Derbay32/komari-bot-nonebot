"""TSK-277 真实 QQ handler 装配：state token、真实 Markdown 载荷与发送前重审。

导入真实 `komari_bot.plugins.character_binding` 包（不是 shim），经 274 真实
event_preprocessor 把 token 写入共享 state，再用真实 NoneBot
`message.handle_event` 分发 QQ 事件，断言 `post_group_messages` 的真实载荷。
"""

from __future__ import annotations

import importlib
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest

from tests.character_binding.test_reply_evidence import (
    _real_character_binding_package,
)
from tests.character_binding.tsk277_support import (
    APP_ID,
    BAD_COMMAND,
    CANCEL_BUTTON,
    CHALLENGE_BODY,
    CONTINUE_BUTTON,
    GROUP_ID,
    GROUP_OPENID,
    MEMBER_OPENID,
    OFFICIAL_BOT_QQ,
    FakeCoordinator,
    FrozenClock,
    freeze_qq_now,
    make_claim,
    markdown_content,
    payload_buttons,
    reference_message_id,
    require_wizard_contract,
)
from tests.group_admission.management_support import prepare_control_plane
from tests.group_admission.qq_admission_support import (
    QQProbeBot,
    dispatch_qq,
    event_gate_context,
    make_c2c,
    make_group_at,
    make_plain_group,
)
from tests.group_admission.runtime_support import AdmissionStorageFake, stored_policy

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

pytestmark = pytest.mark.group_admission_acceptance

REPLY_EVIDENCE_MODULE = "komari_bot.plugins.character_binding.reply_evidence"
COORDINATOR_MODULE = "komari_bot.plugins.character_binding.qq_coordinator"


async def _empty_fetcher(_message_id: int) -> dict[str, object]:
    return {}


async def _never_banned(_member_qq: int, _scope: str) -> bool:
    return False


def _unused_factory() -> Any:
    raise AssertionError("挑战/名字输入路径不得打开数据库会话")


class _FailingSendBot(QQProbeBot):
    """第一次发送即抛错，用于验证“发送不确定不追加 fallback”。"""

    async def call_api(self, api: str, **data: object) -> Any:
        self.calls.append((api, data))
        raise RuntimeError("simulated send outcome unknown")  # noqa: TRY003


@asynccontextmanager
async def _real_coordinator_env(
    monkeypatch: pytest.MonkeyPatch,
    *,
    group_resolver: Callable[[str, str], Awaitable[int | None]],
    member_resolver: Callable[[str, str, str], Awaitable[int | None]] | None = None,
    policy: dict[str, object] | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """真实 preprocessor + 真实 character_binding 包 + 真实 QQBindingCoordinator。"""
    storage = AdmissionStorageFake(
        stored_policy(1, policy or {"mode": "blacklist", "group_ids": []})
    )
    await prepare_control_plane(monkeypatch, storage)
    async with event_gate_context():
        with _real_character_binding_package():
            module = require_wizard_contract()
            reply_evidence = importlib.import_module(REPLY_EVIDENCE_MODULE)
            coordinator_module = importlib.import_module(COORDINATOR_MODULE)
            clock = FrozenClock(datetime(2026, 9, 8, 12, 0, tzinfo=UTC))
            freeze_qq_now(monkeypatch, clock)
            collector = reply_evidence.ReplyEvidenceCollector(
                app_id=APP_ID,
                official_bot_qq=OFFICIAL_BOT_QQ,
                message_fetcher=_empty_fetcher,
                clock=clock,
            )
            coordinator = coordinator_module.QQBindingCoordinator(
                collectors=(collector,),
                group_resolver=group_resolver,
                member_resolver=member_resolver,
                ban_checker=_never_banned,
                clock=clock,
            )
            claim_calls: list[Any] = []
            original_claim = coordinator.claim_initial_bind

            async def counting_claim(request: Any) -> Any:
                claim_calls.append(request)
                return await original_claim(request)

            monkeypatch.setattr(
                coordinator,
                "claim_initial_bind",
                counting_claim,
                raising=False,
            )
            await coordinator.start()
            wizard = module.BindingWizard(
                coordinator=coordinator,
                session_factory=_unused_factory,
                clock=clock,
            )
            module.set_binding_wizard(wizard)
            try:
                yield {
                    "coordinator": coordinator,
                    "claim_calls": claim_calls,
                    "wizard": wizard,
                    "module": module,
                    "collector": collector,
                }
            finally:
                module.set_binding_wizard(None)
                await coordinator.close()
                reply_evidence.set_runtime_collectors(())


@asynccontextmanager
async def _fake_coordinator_env(
    monkeypatch: pytest.MonkeyPatch,
    *,
    group_resolver: Callable[[str, str], Awaitable[int | None]],
    member_resolver: Callable[[str, str, str], Awaitable[int | None]] | None = None,
    coordinator: FakeCoordinator,
    policy: dict[str, object] | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """真实 preprocessor + 真实 handler + 可脚本化协调器（用于发送窗口用例）。"""
    storage = AdmissionStorageFake(
        stored_policy(1, policy or {"mode": "blacklist", "group_ids": []})
    )
    await prepare_control_plane(monkeypatch, storage)
    async with event_gate_context():
        with _real_character_binding_package():
            module = require_wizard_contract()
            reply_evidence = importlib.import_module(REPLY_EVIDENCE_MODULE)
            clock = FrozenClock(datetime(2026, 9, 8, 12, 0, tzinfo=UTC))
            freeze_qq_now(monkeypatch, clock)
            wizard = module.BindingWizard(
                coordinator=coordinator,
                session_factory=_unused_factory,
                clock=clock,
            )
            module.set_binding_wizard(wizard)
            admission = importlib.import_module("komari_bot.plugins.group_admission")
            admission.register_qq_group_resolver(
                group_resolver,
                member_resolver=member_resolver,
            )
            try:
                yield {"module": module, "wizard": wizard}
            finally:
                module.set_binding_wizard(None)
                admission.register_qq_group_resolver(None)
                reply_evidence.set_runtime_collectors(())


def _only_call(bot: QQProbeBot) -> dict[str, Any]:
    assert len(bot.calls) == 1, f"expected exactly one send, got {bot.calls}"
    api, data = bot.calls[0]
    assert api == "post_group_messages"
    return data


async def test_real_handler_sends_markdown_challenge_with_quote_and_no_second_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC1/AC4/AC9/AC11：真实 handler 消费 state token、真实 Markdown 载荷、原生引用。"""

    async def resolve_group(_app_id: str, _group_openid: str) -> int | None:
        return None

    async with _real_coordinator_env(
        monkeypatch,
        group_resolver=resolve_group,
    ) as env:
        bot = QQProbeBot(APP_ID)
        await dispatch_qq(
            bot,
            make_group_at(
                content="/bind",
                message_id="qq-handler-1",
                group_openid=GROUP_OPENID,
                member_openid=MEMBER_OPENID,
            ),
        )

        assert len(env["claim_calls"]) == 1, "handler 不得重复初始 claim"
        data = _only_call(bot)
        body = markdown_content(data)
        assert body.startswith("正在确认你的本群身份。\n会话码：")
        session_code = body.split("会话码：", 1)[1].split("\n", 1)[0]
        assert body == CHALLENGE_BODY.format(session=session_code)
        assert data["msg_type"] == 2
        assert data.get("content") is None
        assert payload_buttons(data) == [
            (CONTINUE_BUTTON, "/bind", 2),
            (CANCEL_BUTTON, f"/bind cancel {session_code}", 2),
        ]
        assert reference_message_id(data) == "qq-handler-1"
        assert data["group_openid"] == GROUP_OPENID


async def test_real_handler_does_not_send_twice_for_duplicate_inbound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC3/AC8：同一入站消息重复投递只发送一次。"""

    async def resolve_group(_app_id: str, _group_openid: str) -> int | None:
        return None

    async with _real_coordinator_env(
        monkeypatch,
        group_resolver=resolve_group,
    ) as env:
        bot = QQProbeBot(APP_ID)
        event = make_group_at(
            content="/bind",
            message_id="qq-handler-dup",
            group_openid=GROUP_OPENID,
            member_openid=MEMBER_OPENID,
        )
        await dispatch_qq(bot, event)
        await dispatch_qq(bot, event)

        assert len(env["claim_calls"]) == 2, "preprocessor 可重复查询，但 handler 不得重复处理"
        assert len(bot.calls) == 1, f"duplicate inbound must not send twice: {bot.calls}"


async def test_real_handler_ignores_non_bind_and_non_native_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC1/AC12：只响应 QQ 群 @ 的 /bind 英文子命令；其他事件静默。"""

    async def resolve_group(_app_id: str, _group_openid: str) -> int:
        return GROUP_ID

    async def resolve_member(_app_id: str, _group_openid: str, _openid: str) -> None:
        return None

    async with _real_coordinator_env(
        monkeypatch,
        group_resolver=resolve_group,
        member_resolver=resolve_member,
    ):
        bot = QQProbeBot(APP_ID)
        await dispatch_qq(
            bot,
            make_group_at(
                content="/轮盘 开枪",
                message_id="qq-handler-game",
                group_openid=GROUP_OPENID,
                member_openid=MEMBER_OPENID,
            ),
        )
        await dispatch_qq(
            bot,
            make_group_at(
                content="普通聊天",
                message_id="qq-handler-chat",
                group_openid=GROUP_OPENID,
                member_openid=MEMBER_OPENID,
            ),
        )
        await dispatch_qq(
            bot,
            make_plain_group(content="/bind"),
        )
        await dispatch_qq(bot, make_c2c(content="/bind"))

        assert bot.calls == [], f"non /bind or non native @ must stay silent: {bot.calls}"


async def test_real_handler_send_failure_does_not_append_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC1：发送结果不确定时只发生一次尝试，不补发任何替代消息。"""

    async def resolve_group(_app_id: str, _group_openid: str) -> int | None:
        return None

    async with _real_coordinator_env(
        monkeypatch,
        group_resolver=resolve_group,
    ):
        bot = _FailingSendBot(APP_ID)
        with suppress(Exception):
            await dispatch_qq(
                bot,
                make_group_at(
                    content="/bind",
                    message_id="qq-handler-fail",
                    group_openid=GROUP_OPENID,
                    member_openid=MEMBER_OPENID,
                ),
            )

        assert len(bot.calls) == 1, f"no fallback allowed: {bot.calls}"


async def test_restricted_group_stays_silent_even_for_configured_superuser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC9：映射≠白名单、SUPERUSER 无群 bypass；受限群静默且不建草稿。"""

    async def resolve_group(_app_id: str, _group_openid: str) -> int:
        return GROUP_ID

    async def resolve_member(_app_id: str, _group_openid: str, _openid: str) -> int:
        # conftest SUPERUSERS = {"42", "669293859"}
        return 42

    async with _real_coordinator_env(
        monkeypatch,
        group_resolver=resolve_group,
        member_resolver=resolve_member,
        policy={"mode": "blacklist", "group_ids": [GROUP_ID]},
    ) as env:
        bot = QQProbeBot(APP_ID)
        await dispatch_qq(
            bot,
            make_group_at(
                content="/bind",
                message_id="qq-handler-restricted",
                group_openid=GROUP_OPENID,
                member_openid=MEMBER_OPENID,
            ),
        )

        assert bot.calls == []
        wizard = env["wizard"]
        scope = env["module"].WizardScope(
            app_id=APP_ID,
            group_openid=GROUP_OPENID,
            member_openid=MEMBER_OPENID,
        )
        assert await wizard.get_session(scope) is None


async def test_send_time_recheck_suppresses_reply_in_wait_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC9：handler 在真正发送前再次重审；handle_event 返回后失格不得发送。"""

    async def resolve_group(_app_id: str, _group_openid: str) -> int:
        return GROUP_ID

    async def resolve_member(_app_id: str, _group_openid: str, _openid: str) -> None:
        return None

    clock = FrozenClock(datetime(2026, 9, 8, 12, 0, tzinfo=UTC))
    claim = make_claim(
        clock=clock,
        session_code="qq-window-session",
        qq_message_id="qq-handler-window",
    )
    coordinator = FakeCoordinator(claim=claim, allow_first_rechecks=1)
    async with _fake_coordinator_env(
        monkeypatch,
        group_resolver=resolve_group,
        member_resolver=resolve_member,
        coordinator=coordinator,
    ):
        bot = QQProbeBot(APP_ID)
        await dispatch_qq(
            bot,
            make_group_at(
                content="/bind",
                message_id="qq-handler-window",
                group_openid=GROUP_OPENID,
                member_openid=MEMBER_OPENID,
            ),
        )

        assert bot.calls == [], "等待窗口内失格后不得发送"
        assert len(coordinator.recheck_calls) >= 2, (
            "handle_event 与真实发送前都必须重审"
        )


async def test_positive_control_send_performs_send_time_recheck(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC9 对照：允许时仍须出现发送前重审，且恰好一次真实发送。"""

    async def resolve_group(_app_id: str, _group_openid: str) -> int:
        return GROUP_ID

    async def resolve_member(_app_id: str, _group_openid: str, _openid: str) -> None:
        return None

    clock = FrozenClock(datetime(2026, 9, 8, 12, 0, tzinfo=UTC))
    claim = make_claim(
        clock=clock,
        session_code="qq-window-ok",
        qq_message_id="qq-handler-window-ok",
    )
    coordinator = FakeCoordinator(claim=claim)
    async with _fake_coordinator_env(
        monkeypatch,
        group_resolver=resolve_group,
        member_resolver=resolve_member,
        coordinator=coordinator,
    ):
        bot = QQProbeBot(APP_ID)
        await dispatch_qq(
            bot,
            make_group_at(
                content="/bind",
                message_id="qq-handler-window-ok",
                group_openid=GROUP_OPENID,
                member_openid=MEMBER_OPENID,
            ),
        )

        assert len(bot.calls) == 1
        assert len(coordinator.recheck_calls) >= 2
        assert coordinator.recheck_calls[-1][1] == "business"


async def test_real_handler_ignores_changed_content_for_same_inbound_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC3/AC8：重复入站按 scope+message_id 判定，content 变化也不得执行。"""

    async def resolve_group(_app_id: str, _group_openid: str) -> int:
        return GROUP_ID

    async def resolve_member(_app_id: str, _group_openid: str, _openid: str) -> None:
        return None

    clock = FrozenClock(datetime(2026, 9, 8, 12, 0, tzinfo=UTC))
    claim = make_claim(
        clock=clock,
        session_code="qq-dup-content",
        qq_message_id="qq-handler-dup-content",
    )
    coordinator = FakeCoordinator(claim=claim)
    async with _fake_coordinator_env(
        monkeypatch,
        group_resolver=resolve_group,
        member_resolver=resolve_member,
        coordinator=coordinator,
    ):
        bot = QQProbeBot(APP_ID)
        await dispatch_qq(
            bot,
            make_group_at(
                content="/bind",
                message_id="qq-handler-dup-content",
                group_openid=GROUP_OPENID,
                member_openid=MEMBER_OPENID,
            ),
        )
        assert len(bot.calls) == 1

        # 同一 scope+message_id、不同 content：不得再次执行或发送。
        await dispatch_qq(
            bot,
            make_group_at(
                content="/bind rename",
                message_id="qq-handler-dup-content",
                group_openid=GROUP_OPENID,
                member_openid=MEMBER_OPENID,
            ),
        )

        assert len(bot.calls) == 1, f"changed content with same id must be ignored: {bot.calls}"


async def test_handler_contract_is_registered_as_census_matcher() -> None:
    """AC12：真实包导入必须注册唯一 QQ handler matcher（census 对账的运行时面）。"""
    import nonebot.matcher as matcher_module

    async with event_gate_context():
        with _real_character_binding_package():
            module = require_wizard_contract()
            del module
            handler_module = importlib.import_module(
                "komari_bot.plugins.character_binding.qq_commands"
            )
            bind_qq = getattr(handler_module, "bind_qq", None)
            assert bind_qq is not None, "qq_commands.bind_qq 必须存在"
            registered = {
                matcher
                for matchers in matcher_module.matchers.values()
                for matcher in matchers
                if getattr(getattr(matcher, "_source", None), "module_name", None)
                == "komari_bot.plugins.character_binding.qq_commands"
            }
            assert len(registered) == 1, (
                f"expected exactly one QQ handler matcher: {registered}"
            )


async def test_real_handler_accepts_trimmed_initial_bind_without_losing_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC1/AC4：门禁按 strip 后精确 /bind 消耗 claim，handler 必须同样发挑战。"""

    async def resolve_group(_app_id: str, _group_openid: str) -> int | None:
        return None

    async with _real_coordinator_env(
        monkeypatch,
        group_resolver=resolve_group,
    ) as env:
        bot = QQProbeBot(APP_ID)
        await dispatch_qq(
            bot,
            make_group_at(
                content=" /bind ",
                message_id="qq-handler-trimmed",
                group_openid=GROUP_OPENID,
                member_openid=MEMBER_OPENID,
            ),
        )

        assert len(env["claim_calls"]) == 1, "门禁已按 strip 后的 /bind 消耗初始 claim"
        data = _only_call(bot)
        body = markdown_content(data)
        assert body.startswith("正在确认你的本群身份。\n会话码："), (
            "trimmed /bind 不得只消耗 claim 而不发挑战"
        )
        assert data["group_openid"] == GROUP_OPENID


async def test_real_handler_ignores_bind_prefixed_non_command_but_reports_unknown_subcommand(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC1/AC11：/bindfoo 不是命令必须静默；/bind bogus 仍是未知子命令。"""

    async def resolve_group(_app_id: str, _group_openid: str) -> int:
        return GROUP_ID

    async def resolve_member(_app_id: str, _group_openid: str, _openid: str) -> None:
        return None

    coordinator = FakeCoordinator()
    async with _fake_coordinator_env(
        monkeypatch,
        group_resolver=resolve_group,
        member_resolver=resolve_member,
        coordinator=coordinator,
    ):
        bot = QQProbeBot(APP_ID)
        await dispatch_qq(
            bot,
            make_group_at(
                content="/bindfoo",
                message_id="qq-handler-prefix",
                group_openid=GROUP_OPENID,
                member_openid=MEMBER_OPENID,
            ),
        )
        assert bot.calls == [], f"/bindfoo 不是命令，必须静默: {bot.calls}"

        await dispatch_qq(
            bot,
            make_group_at(
                content="/bind bogus",
                message_id="qq-handler-unknown",
                group_openid=GROUP_OPENID,
                member_openid=MEMBER_OPENID,
            ),
        )
        assert len(bot.calls) == 1
        assert markdown_content(bot.calls[0][1]) == BAD_COMMAND
