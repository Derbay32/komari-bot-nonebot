"""TSK-274 listener, collector routing, and coordinator lifecycle contracts."""

from __future__ import annotations

import importlib
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, cast

import pytest
from nonebot.adapters.onebot.v11 import Adapter as OneBotAdapter
from nonebot.adapters.onebot.v11 import Bot as OneBotBot
from nonebot.adapters.onebot.v11 import GroupMessageEvent

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
    QQProbeBot,
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
