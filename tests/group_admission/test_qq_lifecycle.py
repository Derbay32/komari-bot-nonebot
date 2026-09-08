"""TSK-274 listener, collector routing, and coordinator lifecycle contracts."""

from __future__ import annotations

import importlib
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, cast

import pytest
from nonebot.adapters.onebot.v11 import Adapter as OneBotAdapter
from nonebot.adapters.onebot.v11 import Bot as OneBotBot
from nonebot.adapters.onebot.v11 import GroupMessageEvent
from nonebot.adapters.qq.config import BotInfo, Intents
from nonebot.config import Config as NoneBotConfig
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.character_binding.conftest import (
    _reset_shared_orm_engine,
    require_postgres,
)
from tests.character_binding.test_reply_evidence import (
    APP_ID as ONEBOT_APP_ID,
)
from tests.character_binding.test_reply_evidence import (
    BASE_TIME,
    COMMAND,
    OFFICIAL_BOT_QQ,
    FrozenClock,
    MessageFetcher,
    _challenge_event,
    _get_msg_payload,
    _original_event,
    _real_character_binding_package,
)
from tests.group_admission.entry_gate_support import (
    ProbeBot,
    dispatch,
    event_gate_context,
)
from tests.group_admission.management_support import prepare_control_plane
from tests.group_admission.qq_admission_support import (
    APP_ID,
    MEMBER_QQ,
    SECOND_APP_ID,
    QQProbeBot,
    make_group_at,
)
from tests.group_admission.runtime_support import AdmissionStorageFake, stored_policy

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

pytestmark = pytest.mark.group_admission_acceptance

_GROUP_ID = 274101


@contextmanager
def _runtime_collectors_context(
    module: Any,
    collectors: tuple[Any, ...],
) -> Iterator[None]:
    previous = module.get_runtime_collectors()
    module.set_runtime_collectors(collectors)
    try:
        yield
    finally:
        module.set_runtime_collectors(previous)


class _OneBotFetchBot(OneBotBot):
    """Real OneBot V11 identity returning a controlled ``get_msg`` payload."""

    def __init__(self, payload: Mapping[str, object]) -> None:
        adapter = cast("OneBotAdapter", OneBotAdapter.__new__(OneBotAdapter))
        super().__init__(adapter, "onebot-tsk274")
        self.payload = payload
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def call_api(self, api: str, **data: object) -> Mapping[str, object]:
        self.calls.append((api, data))
        assert api == "get_msg"
        return self.payload


@pytest.mark.asyncio
async def test_collector_fetches_reply_from_event_onebot_not_qq_bot() -> None:
    """A simultaneous QQ adapter must never become the ``get_msg`` source."""
    from komari_bot.plugins.character_binding.reply_evidence import (
        ReplyEvidenceCollector,
    )

    original_id = 274101
    challenge_id = 274102
    payload = _get_msg_payload(
        message_id=original_id,
        original_text=COMMAND,
        group_id=_GROUP_ID,
    )

    async def forbidden_fallback(_message_id: int) -> Mapping[str, object]:
        raise AssertionError(  # noqa: TRY003
            "collector selected an unbound fallback fetcher"
        )

    collector = ReplyEvidenceCollector(
        app_id=ONEBOT_APP_ID,
        official_bot_qq=OFFICIAL_BOT_QQ,
        message_fetcher=forbidden_fallback,
        clock=FrozenClock(BASE_TIME),
    )
    collector.open_session(
        session_code="TSK274-BOT-ROUTE",
        group_openid="group-openid-tsk274",
        member_openid="member-openid-tsk274",
        original_command=COMMAND,
        qq_message_id="qq-message-tsk274",
    )

    original = _original_event(
        message_id=original_id,
        group_id=_GROUP_ID,
        text=COMMAND,
    )
    challenge = _challenge_event(
        message_id=challenge_id,
        session_code="TSK274-BOT-ROUTE",
        quoted_message_id=original_id,
        quoted_text=COMMAND,
        group_id=_GROUP_ID,
    )
    onebot = _OneBotFetchBot(payload)
    qq = QQProbeBot()

    await collector.handle_event(original)
    evidence = await collector.handle_event(challenge, bot=onebot)

    assert evidence is not None
    assert onebot.calls == [("get_msg", {"message_id": original_id})]
    assert qq.calls == []


@pytest.mark.asyncio
async def test_real_listener_passes_received_onebot_to_each_collector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The actual matcher wiring must preserve the event's protocol identity."""
    storage = AdmissionStorageFake(
        stored_policy(1, {"mode": "blacklist", "group_ids": []})
    )
    await prepare_control_plane(monkeypatch, storage)
    calls: list[tuple[GroupMessageEvent, object]] = []

    async with event_gate_context():
        with _real_character_binding_package():
            reply_evidence = importlib.import_module(
                "komari_bot.plugins.character_binding.reply_evidence"
            )
            collector = reply_evidence.ReplyEvidenceCollector(
                app_id=ONEBOT_APP_ID,
                official_bot_qq=OFFICIAL_BOT_QQ,
                message_fetcher=MessageFetcher({}),
                clock=FrozenClock(BASE_TIME),
            )

            async def spy(event: object, *, bot: object) -> None:
                assert isinstance(event, GroupMessageEvent)
                calls.append((event, bot))

            monkeypatch.setattr(collector, "handle_event", spy)
            onebot = ProbeBot(self_id="onebot-tsk274")
            qq = QQProbeBot()
            admitted = _challenge_event(
                message_id=274111,
                session_code="TSK274-LISTENER",
                quoted_message_id=274112,
                quoted_text=COMMAND,
                group_id=_GROUP_ID,
            )

            with _runtime_collectors_context(reply_evidence, (collector,)):
                await dispatch(onebot, admitted)

            assert calls == [(admitted, onebot)]
            assert qq.calls == []


@pytest.mark.asyncio
async def test_real_listener_delivers_accepted_evidence_to_coordinator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真实 collector/listener 链路把 evidence 交给 coordinator 并立即裁决。"""
    storage = AdmissionStorageFake(
        stored_policy(1, {"mode": "blacklist", "group_ids": []})
    )
    await prepare_control_plane(monkeypatch, storage)

    async def resolve_group(_app_id: str, _group_openid: str) -> int | None:
        return None

    async with event_gate_context():
        with _real_character_binding_package() as binding:
            admission = importlib.import_module(
                "komari_bot.plugins.group_admission"
            )
            reply_evidence = importlib.import_module(
                "komari_bot.plugins.character_binding.reply_evidence"
            )
            collector = reply_evidence.ReplyEvidenceCollector(
                app_id=APP_ID,
                official_bot_qq=OFFICIAL_BOT_QQ,
                message_fetcher=lambda _message_id: forbidden_fetcher(),
                clock=FrozenClock(BASE_TIME),
            )
            binding_public = cast("Any", binding)
            coordinator = binding_public.QQBindingCoordinator(
                collectors=(collector,),
                group_resolver=resolve_group,
                clock=FrozenClock(BASE_TIME),
            )
            await coordinator.start()
            try:
                request = admission.QQInitialBindRequest(
                    app_id=APP_ID,
                    group_openid="group-openid-tsk274",
                    member_openid="member-openid-tsk274",
                    qq_message_id="qq-original-tsk274",
                    command=COMMAND,
                )
                claim = await coordinator.claim_initial_bind(request)
                assert claim is not None
                original_id = 274121
                challenge_id = 274122
                payload = _get_msg_payload(
                    message_id=original_id,
                    original_text=COMMAND,
                    group_id=_GROUP_ID,
                )
                onebot = _OneBotFetchBot(payload)
                qq = QQProbeBot()
                with _runtime_collectors_context(reply_evidence, (collector,)):
                    await dispatch(
                        onebot,
                        _original_event(
                            message_id=original_id,
                            group_id=_GROUP_ID,
                            text=COMMAND,
                        ),
                    )
                    await dispatch(
                        onebot,
                        _challenge_event(
                            message_id=challenge_id,
                            session_code=claim.session_code,
                            quoted_message_id=original_id,
                            quoted_text=COMMAND,
                            group_id=_GROUP_ID,
                        ),
                    )

                active = await coordinator.resolve_verified_binding_session(
                    APP_ID,
                    "group-openid-tsk274",
                    "member-openid-tsk274",
                )
                assert active is not None
                assert active.group_id == _GROUP_ID
                assert active.member_qq == MEMBER_QQ
                assert onebot.calls == [("get_msg", {"message_id": original_id})]
                assert qq.calls == []
            finally:
                await coordinator.close()


async def forbidden_fetcher() -> Mapping[str, object]:
    raise AssertionError("collector used an unbound fallback fetcher")  # noqa: TRY003


@pytest.mark.asyncio
async def test_real_character_binding_startup_installs_and_closes_qq_admission(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """生产插件 hook 必须装配有效 app，并在 close 后撤销全部资格。"""
    require_postgres()
    from nonebot import get_driver

    from tests.character_binding.test_reply_evidence import (
        _real_character_binding_package,
    )

    driver = get_driver()
    valid_info = BotInfo(
        id=APP_ID,
        token="lifecycle-test-token",
        secret="lifecycle-test-secret",
        intent=Intents(c2c_group_at_messages=True),
        use_websocket=False,
    )
    invalid_info = BotInfo(
        id=SECOND_APP_ID,
        token="invalid-app-test-token",
        secret="invalid-app-test-secret",
        intent=Intents(c2c_group_at_messages=True),
        use_websocket=False,
    )
    config_values = driver.config.model_dump()
    config_values.update(
        {
            "qq_is_sandbox": True,
            "qq_bots": [valid_info.model_dump(), invalid_info.model_dump()],
            "qq_official_bot_qq_by_app": {
                APP_ID: str(OFFICIAL_BOT_QQ),
                SECOND_APP_ID: "official-qq-is-invalid",
            },
        }
    )
    monkeypatch.setattr(
        driver,
        "config",
        NoneBotConfig.model_validate(config_values),
    )

    storage = AdmissionStorageFake(
        stored_policy(1, {"mode": "blacklist", "group_ids": []})
    )
    await prepare_control_plane(monkeypatch, storage)

    await _reset_shared_orm_engine()
    dispose_calls: list[AsyncEngine] = []
    original_dispose = AsyncEngine.dispose

    async def observe_dispose(
        self: AsyncEngine,
        *,
        close: bool = True,
    ) -> None:
        dispose_calls.append(self)
        await original_dispose(self, close=close)

    monkeypatch.setattr(AsyncEngine, "dispose", observe_dispose)

    try:
        async with event_gate_context():
            with _real_character_binding_package() as binding:
                binding_public = cast("Any", binding)
                admission = importlib.import_module(
                    "komari_bot.plugins.group_admission"
                )
                reply_evidence = importlib.import_module(
                    "komari_bot.plugins.character_binding.reply_evidence"
                )
                reply_evidence.set_runtime_collectors(())
                for register_name in (
                    "register_qq_group_resolver",
                    "register_qq_initial_bind_claimer",
                    "register_qq_binding_session_resolver",
                    "register_qq_ban_checker",
                ):
                    getattr(admission, register_name)(None)

                unknown_group = "group-openid-tsk274-lifecycle-unknown"
                valid_event = make_group_at(
                    group_openid=unknown_group,
                    message_id="qq-lifecycle-valid",
                )
                invalid_event = make_group_at(
                    group_openid=unknown_group,
                    message_id="qq-lifecycle-invalid",
                )
                valid_bot = QQProbeBot(APP_ID)
                invalid_bot = QQProbeBot(SECOND_APP_ID)

                assert await admission.qualify_qq_event(valid_bot, valid_event) is None
                assert reply_evidence.get_runtime_collectors() == ()

                driver_lifespan = driver._lifespan
                assert binding_public.init_plugin in driver_lifespan._startup_funcs
                assert binding_public.close_plugin in driver_lifespan._shutdown_funcs

                started = False
                valid_token: Any = None
                try:
                    started = True
                    await binding_public.init_plugin()
                    collectors = reply_evidence.get_runtime_collectors()
                    assert tuple(collector.app_id for collector in collectors) == (APP_ID,)
                    assert collectors[0].official_bot_qq == str(OFFICIAL_BOT_QQ)

                    valid_token = await admission.qualify_qq_event(
                        valid_bot,
                        valid_event,
                    )
                    assert valid_token is not None
                    assert valid_token.scope == "binding_challenge"
                    assert valid_token.claim is not None

                    original_id = 274131
                    challenge_id = 274132
                    onebot = _OneBotFetchBot(
                        _get_msg_payload(
                            message_id=original_id,
                            original_text=COMMAND,
                            group_id=_GROUP_ID,
                        )
                    )
                    qq = QQProbeBot(APP_ID)
                    with _runtime_collectors_context(
                        reply_evidence,
                        reply_evidence.get_runtime_collectors(),
                    ):
                        await dispatch(
                            onebot,
                            _original_event(
                                message_id=original_id,
                                group_id=_GROUP_ID,
                                text=COMMAND,
                            ),
                        )
                        await dispatch(
                            onebot,
                            _challenge_event(
                                message_id=challenge_id,
                                session_code=valid_token.claim.session_code,
                                quoted_message_id=original_id,
                                quoted_text=COMMAND,
                                group_id=_GROUP_ID,
                            ),
                        )
                    continued = await admission.qualify_qq_event(
                        valid_bot,
                        make_group_at(
                            content="/bind continue",
                            group_openid=unknown_group,
                            message_id="qq-lifecycle-continue",
                        ),
                    )
                    assert continued is not None
                    assert continued.scope == "binding"
                    assert onebot.calls == [("get_msg", {"message_id": original_id})]
                    assert qq.calls == []
                    assert (
                        await admission.qualify_qq_event(invalid_bot, invalid_event)
                    ) is None
                finally:
                    if started:
                        await binding_public.close_plugin()

                assert reply_evidence.get_runtime_collectors() == ()
                assert await admission.qualify_qq_event(valid_bot, valid_event) is None
                closed_decision = await admission.recheck_qq_effect(
                    valid_token,
                    effect="binding_challenge",
                )
                assert closed_decision.allowed is False
                assert closed_decision.effect == "binding_challenge"
        assert dispose_calls == []
    finally:
        monkeypatch.setattr(AsyncEngine, "dispose", original_dispose)
        await _reset_shared_orm_engine()

    captured_output = capsys.readouterr()
    captured_logs = "\n".join((caplog.text, captured_output.out, captured_output.err))
    for secret in (
        "lifecycle-test-token",
        "lifecycle-test-secret",
        "invalid-app-test-token",
        "invalid-app-test-secret",
        APP_ID,
        SECOND_APP_ID,
        str(OFFICIAL_BOT_QQ),
        "official-qq-is-invalid",
    ):
        assert secret not in captured_logs
