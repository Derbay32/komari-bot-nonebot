"""TSK-185 验收：真实 OneBot V11 引用读取适配器接入真实判定引擎。

断言穿过公开 seam：真实 ``Bot`` / ``Message`` / ``Reply``、生产
``OneBotReferenceReader``、真实 ``JevDecisionEngine``、官方 SDK 的
``httpx2.MockTransport``，以及一次性隔离库上的真实 Redis/PostgreSQL。
只替换外部 I/O：已连接 Bot 解析、平台 API 传输、准入快照与 SDK transport。

业务期望值全部是手写字面量，不导入生产常量作为预言机。
"""

from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import httpx2
import pytest
from loguru import logger as loguru_logger
from nonebot import get_driver
from nonebot.adapters.onebot.v11 import Adapter
from nonebot.adapters.onebot.v11 import Bot as OneBotV11Bot

from komari_bot.decision.jev import EventIdentity
from komari_bot.plugins.group_admission import (
    AdmissionIntent,
    AdmissionQualification,
)
from komari_bot.plugins.komari_decision.services.jev_onebot import (
    OneBotReferenceReader,
)
from tests.komari_decision.jev_test_support import (
    DecisionHarness,
    HarnessOptions,
    ScriptedTransport,
    build_answers,
    create_scratch_database,
    drop_scratch_database,
    make_identity,
    make_message,
    make_request,
    read_dispatch,
    skip_without_services,
)

pytestmark = pytest.mark.asyncio

_MISSING: Any = object()
"""显式区分「键缺失」与「键存在但值为 null」。"""

Responder = Callable[[str, dict[str, Any]], Any]


@pytest.fixture(scope="module")
def decision_database() -> Iterator[str]:
    skip_without_services()
    url = asyncio.run(create_scratch_database(f"_jevonebot{uuid4().hex[:8]}"))
    try:
        yield url
    finally:
        asyncio.run(drop_scratch_database(url))


@pytest.fixture(scope="module", autouse=True)
def scratch_prompt_storage(decision_database: str) -> Iterator[None]:
    """把真实共享 Prompt 存储重定向到本模块的一次性隔离库。

    ``DecisionHarness.write_prompt`` 走真实 ``nonebot_plugin_orm`` 会话入口；
    这里只把该外部持久化端点指向 scratch 库，绝不写共享门控库。
    """
    from nonebot import require

    require("nonebot_plugin_orm")
    import nonebot_plugin_orm
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    engine = create_async_engine(decision_database, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    original = nonebot_plugin_orm.get_session
    nonebot_plugin_orm.get_session = (  # type: ignore[method-assign]
        lambda *_args, **_kwargs: factory()
    )
    try:
        yield
    finally:
        nonebot_plugin_orm.get_session = original  # type: ignore[method-assign]
        asyncio.run(engine.dispose())


# ------------------------------ 真实 OneBot 桩 ------------------------------


class _ScriptedOneBot(OneBotV11Bot):
    """真实 OneBot V11 Bot，只替换平台 API 传输（``call_api``）。"""

    def __init__(self, adapter: Adapter, self_id: str, responder: Responder) -> None:
        super().__init__(adapter, self_id)
        self.calls: list[dict[str, Any]] = []
        self._responder = responder

    async def call_api(self, api: str, **data: Any) -> Any:
        self.calls.append({"api": api, **data})
        produced = self._responder(api, data)
        if inspect.isawaitable(produced):
            return await cast("Awaitable[Any]", produced)
        return produced


def _no_call(api: str, data: dict[str, Any]) -> Any:
    msg = f"unexpected platform call: {api} {sorted(data)}"
    raise AssertionError(msg)


def _bot(
    *,
    account_id: str = "10001",
    responder: Responder = _no_call,
) -> _ScriptedOneBot:
    adapter = Adapter(get_driver())
    return _ScriptedOneBot(adapter, account_id, responder)


def _identity(
    *,
    group_id: str = "7001",
    account_id: str = "10001",
    message_id: str = "5001",
    protocol: str = "onebot_v11",
) -> EventIdentity:
    return EventIdentity(
        protocol=protocol,
        account_id=account_id,
        group_id=group_id,
        message_id=message_id,
    )


def _reader(
    bot: object,
    *,
    admitted: Callable[[str], bool] | None = None,
) -> OneBotReferenceReader:
    return OneBotReferenceReader(
        bot_resolver=lambda _account_id: bot,
        group_admitted=admitted or (lambda _group_id: True),
    )


def _payload(
    *,
    message_id: object,
    text: str = "",
    message: object = _MISSING,
    group_id: object = _MISSING,
    sender_id: object = 20002,
    timestamp: object = 1_700_000_000,
    message_type: str = "group",
    sender: object = _MISSING,
) -> dict[str, Any]:
    """按 OneBot 11 ``get_msg`` 标准字段构造响应（可显式缺省键）。"""
    if message is _MISSING:
        message = [{"type": "text", "data": {"text": text}}]
    if sender is _MISSING:
        sender = {"user_id": sender_id, "nickname": f"昵称-{sender_id}", "card": ""}
    payload: dict[str, Any] = {
        "message_type": message_type,
        "message_id": message_id,
        "real_id": message_id,
        "sender": sender,
        "message": message,
    }
    if timestamp is not _MISSING:
        payload["time"] = timestamp
    if group_id is not _MISSING:
        payload["group_id"] = group_id
    return payload


def _mock_sdk() -> tuple[httpx2.MockTransport, list[httpx2.Request]]:
    """官方 SDK 的 ``MockTransport``：只记录并回放模型响应。"""
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        body = json.loads(request.content)
        return httpx2.Response(
            200,
            json=build_answers(body["questions"]),
            request=request,
        )

    return httpx2.MockTransport(handler), requests


def _state(requests: Sequence[httpx2.Request], index: int = 0) -> dict[str, Any]:
    return json.loads(requests[index].content)["state"]


def _conversation(state: Mapping[str, Any]) -> dict[str, Any]:
    return cast("dict[str, Any]", state["untrusted_group_conversation"])


def _rendered(state: Mapping[str, Any]) -> str:
    return json.dumps(state, ensure_ascii=False)


def _messages(state: Mapping[str, Any]) -> list[dict[str, Any]]:
    return list(_conversation(state)["messages"].values())


def _message_with_text(state: Mapping[str, Any], suffix: str) -> dict[str, Any]:
    for message in _messages(state):
        if str(message["text"]).endswith(suffix):
            return message
    msg = f"model state has no message ending with {suffix!r}"
    raise AssertionError(msg)


def _transport(value: httpx2.MockTransport) -> ScriptedTransport:
    """Harness 只声明 ScriptedTransport；运行时接受任意 SDK transport。"""
    return cast("ScriptedTransport", cast("object", value))


# ------------------------------ 适配器投影 ------------------------------


async def test_standard_get_msg_without_group_id_keeps_trusted_provenance() -> None:
    """AC：标准响应缺少 group_id 时沿用可信群来源，并投影真实事实。"""

    def responder(api: str, data: dict[str, Any]) -> Any:
        assert api == "get_msg"
        requested = int(cast("int", data["message_id"]))
        sender = 10001 if requested == 4002 else 20002
        card = "群名片" if requested == 4001 else ""
        message: object = _MISSING
        if requested == 4002:
            message = [
                {"type": "at", "data": {"qq": "30003"}},
                {"type": "text", "data": {"text": f"原文-{requested}"}},
            ]
        return _payload(
            message_id=requested,
            text=f"原文-{requested}",
            message=message,
            sender_id=sender,
            sender={
                "user_id": sender,
                "nickname": "昵称",
                "card": card,
            },
        )

    bot = _bot(responder=responder)
    reader = _reader(bot)
    first = await reader(_identity(), "4001")
    second = await reader(_identity(), "4002")

    assert first is not None
    assert first.message_id == "4001"
    assert first.group_id == "7001"
    assert first.sender_id == "20002"
    assert first.display_name == "群名片"
    assert first.text == "原文-4001"
    assert first.timestamp == datetime.fromtimestamp(1_700_000_000, UTC)
    assert first.timestamp.utcoffset() == timedelta(0)
    assert first.is_bot is False
    assert first.reference_ids == ()
    assert first.mention_ids == ()
    assert first.image_present is False
    assert first.image_description is None

    assert second is not None
    assert second.is_bot is True
    assert second.display_name == "昵称"
    assert second.text == "原文-4002"
    assert second.mention_ids == ("30003",)
    assert bot.calls == [
        {"api": "get_msg", "message_id": 4001},
        {"api": "get_msg", "message_id": 4002},
    ]


@pytest.mark.parametrize(
    "case",
    [
        "sender_bool",
        "sender_float",
        "time_bool",
        "time_float",
        "message_id_bool",
        "message_id_float",
    ],
)
async def test_raw_bool_or_float_identity_is_never_coerced(case: str) -> None:
    """AC：SDK 的宽松整形不得把 bool/float 变成合法身份或时间。"""
    requested = "1" if case == "message_id_bool" else "4001"
    overrides: dict[str, Any] = {
        "sender_bool": {"sender": {"user_id": True, "nickname": "x"}},
        "sender_float": {"sender": {"user_id": 20002.0, "nickname": "x"}},
        "time_bool": {"timestamp": True},
        "time_float": {"timestamp": 1_700_000_000.5},
        "message_id_bool": {"message_id": True},
        "message_id_float": {"message_id": 4001.0},
    }[case]
    payload = _payload(
        message_id=overrides.get("message_id", 4001),
        text="不该出现的宽松整形正文",
        timestamp=overrides.get("timestamp", 1_700_000_000),
        sender=overrides.get("sender", _MISSING),
    )
    bot = _bot(responder=lambda _api, _data: payload)

    assert await _reader(bot)(_identity(), requested) is None
    assert len(bot.calls) == 1


@pytest.mark.parametrize("group_id", [None, 7002, True, 0, "not-a-group"])
async def test_explicit_group_id_is_always_contradiction_checked(
    group_id: object,
) -> None:
    """AC：只要响应里存在 group_id 键，就必须是合法且相同的群号。"""
    bot = _bot(
        responder=lambda _api, _data: _payload(
            message_id=4001, text="不应交付的群号矛盾正文", group_id=group_id
        )
    )

    assert await _reader(bot)(_identity(), "4001") is None
    assert len(bot.calls) == 1


async def test_matching_explicit_group_id_is_accepted() -> None:
    """AC：显式给出的相同群号不算矛盾。"""
    bot = _bot(
        responder=lambda _api, _data: _payload(
            message_id=4001, text="同群显式群号", group_id=7001
        )
    )

    message = await _reader(bot)(_identity(), "4001")

    assert message is not None
    assert message.group_id == "7001"
    assert message.text == "同群显式群号"


# ------------------------------ 身份与准入 ------------------------------


async def test_wrong_protocol_adapter_account_and_restricted_group_are_missing() -> (
    None
):
    """AC：协议/适配器/账号/准入不匹配时缺失，且不发起远程读取。"""
    calls: list[str] = []
    bot = _bot(responder=lambda _api, _data: (_ for _ in ()).throw(AssertionError))
    reader = _reader(bot, admitted=lambda group_id: calls.append(group_id) or True)

    assert await reader(_identity(protocol="qq"), "4001") is None
    assert bot.calls == []

    wrong_adapter = _reader(object())
    assert await wrong_adapter(_identity(), "4001") is None

    other_bot = _bot(account_id="99999", responder=_no_call)
    wrong_account = _reader(other_bot)
    assert await wrong_account(_identity(account_id="10001"), "4001") is None
    assert other_bot.calls == []

    invalid_account = _reader(bot)
    assert await invalid_account(_identity(account_id="acct-x"), "4001") is None

    invalid_group = _reader(bot)
    assert await invalid_group(_identity(group_id="group-x"), "4001") is None

    restricted = _reader(bot, admitted=lambda _group_id: False)
    assert await restricted(_identity(), "4001") is None
    assert bot.calls == []
    assert calls == []


async def test_disconnected_account_is_missing_without_remote_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC：账号没有已连接 Bot 时缺失，且不尝试任何平台读取。"""

    def missing_bot(_account_id: str) -> Any:
        msg = "no connected bot"
        raise ValueError(msg)

    import nonebot

    monkeypatch.setattr(nonebot, "get_bot", missing_bot)
    reader = OneBotReferenceReader(group_admitted=lambda _group_id: True)

    assert await reader(_identity(account_id="10001"), "4001") is None


async def test_default_admission_uses_toplevel_business_adjudicate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC：默认实现经顶层 ``group_admission.adjudicate`` 以 BUSINESS 复核。"""
    from komari_bot.plugins import group_admission

    decisions: list[tuple[set[int], AdmissionIntent]] = []

    def fake_adjudicate(
        associated_group_ids: Any,
        *,
        intent: AdmissionIntent = AdmissionIntent.BUSINESS,
    ) -> Any:
        decisions.append((set(associated_group_ids), intent))
        return SimpleNamespace(qualification=AdmissionQualification.BUSINESS)

    monkeypatch.setattr(group_admission, "adjudicate", fake_adjudicate)
    bot = _bot(
        responder=lambda _api, _data: _payload(message_id=4001, text="准入裁决后的正文")
    )
    reader = OneBotReferenceReader(bot_resolver=lambda _account_id: bot)

    message = await reader(_identity(group_id="7009"), "4001")

    assert message is not None
    assert decisions == [
        ({7009}, AdmissionIntent.BUSINESS),
        ({7009}, AdmissionIntent.BUSINESS),
    ]


# ------------------------------ 最终输入（真实引擎 + MockTransport） ------------------------------


async def test_engine_final_state_contains_reference_once(
    decision_database: str,
) -> None:
    """AC：标准无 group_id 响应经真实读取进入真实引擎的最终 SDK state。"""
    group_id = "7101"
    account_id = "10001"
    sentinel = "引用原文-无群号-TSK185-ALPHA"
    bot = _bot(
        responder=lambda _api, _data: _payload(
            message_id=4001,
            text=sentinel,
            sender={
                "user_id": 20002,
                "nickname": "引用者",
                "card": "引用者名片",
            },
        )
    )
    reader = _reader(bot)
    transport, requests = _mock_sdk()
    options = HarnessOptions(
        reference_reader=reader,
        transport=_transport(transport),
    )

    async with DecisionHarness(decision_database, options) as harness:
        assert harness.decision is not None
        identity = make_identity(
            account_id=account_id, group_id=group_id, message_id="5101"
        )
        target = make_message(
            identity=identity, text="请看引用", reference_ids=("4001",)
        )
        result = await harness.decision.evaluate(
            make_request(identity=identity, target=target)
        )

        assert result.status == "success"
        assert result.reason_code == "classified"
        assert result.references.complete is True
        assert result.references.resolved_count == 1
        assert result.references.missing_count == 0
        assert len(requests) == 1
        rendered = _rendered(_state(requests))
        assert rendered.count(sentinel) == 1
        message = _message_with_text(_state(requests), sentinel)
        assert message["display_name"] == "引用者名片"
        assert message["is_bot"] is False
        assert message["images"] == {"present": False, "known_description": None}
        assert bot.calls == [{"api": "get_msg", "message_id": 4001}]


async def test_native_array_and_cq_edges_but_literal_cq_text_is_not_an_edge(
    decision_database: str,
) -> None:
    """AC：数组原生段与 CQ 序列化都保边；字面/转义 CQ 字样不产生引用边。"""
    group_id = "7102"
    responder_calls: list[int] = []

    def responder(_api: str, data: dict[str, Any]) -> Any:
        requested = int(cast("int", data["message_id"]))
        responder_calls.append(requested)
        if requested == 4001:
            return _payload(
                message_id=4001,
                message=[
                    {"type": "reply", "data": {"id": "4003"}},
                    {"type": "text", "data": {"text": "数组一级"}},
                ],
            )
        if requested == 4002:
            return _payload(message_id=4002, message="[CQ:reply,id=4004]CQ一级")
        if requested == 4003:
            return _payload(message_id=4003, text="数组二级")
        if requested == 4004:
            return _payload(message_id=4004, text="CQ二级")
        if requested == 4005:
            return _payload(
                message_id=4005,
                message=[
                    {
                        "type": "text",
                        "data": {"text": "[CQ:reply,id=4009]数组里的字样"},
                    }
                ],
            )
        if requested == 4006:
            return _payload(
                message_id=4006,
                message="&#91;CQ:reply,id=4010&#93;转义字样",
            )
        msg = f"unexpected referenced node {requested}"
        raise AssertionError(msg)

    bot = _bot(responder=responder)
    transport, requests = _mock_sdk()
    options = HarnessOptions(
        reference_reader=_reader(bot),
        transport=_transport(transport),
    )

    async with DecisionHarness(decision_database, options) as harness:
        assert harness.decision is not None
        identity = make_identity(
            account_id="10001", group_id=group_id, message_id="5102"
        )
        target = make_message(
            identity=identity,
            text="引用四种写法",
            reference_ids=("4001", "4002", "4005", "4006"),
        )
        result = await harness.decision.evaluate(
            make_request(identity=identity, target=target)
        )

        assert result.status == "success"
        assert result.references.complete is True
        assert result.references.resolved_count == 6
        assert sorted(responder_calls) == [4001, 4002, 4003, 4004, 4005, 4006]
        state = _state(requests)
        rendered = _rendered(state)
        for text in ("数组二级", "CQ二级", "数组里的字样", "转义字样"):
            assert rendered.count(text) == 1
        assert _message_with_text(state, "数组一级")["references"]
        assert _message_with_text(state, "CQ一级")["references"]
        assert _message_with_text(state, "数组里的字样")["references"] == []
        assert _message_with_text(state, "转义字样")["references"] == []


@pytest.mark.parametrize(
    "invalid",
    [
        "cross_group",
        "explicit_null_group",
        "private_type",
        "unknown_type",
        "wrong_id",
        "sender_bool",
        "sender_float",
        "time_bool",
        "time_float",
        "missing_sender",
        "missing_time",
        "message_id_bool",
    ],
)
async def test_invalid_platform_response_is_missing_without_leaked_input(
    decision_database: str, invalid: str
) -> None:
    """AC：跨群/null/私聊/错配/非法原始字段都只记缺失，正文不进入模型输入。"""
    group_id = "7103"
    sentinel = f"泄露正文-{invalid}-TSK185"
    requested = "1" if invalid == "message_id_bool" else "4101"
    payload: dict[str, Any] = {
        "cross_group": _payload(message_id=4101, text=sentinel, group_id=7999),
        "explicit_null_group": _payload(message_id=4101, text=sentinel, group_id=None),
        "private_type": _payload(
            message_id=4101, text=sentinel, message_type="private"
        ),
        "unknown_type": _payload(
            message_id=4101, text=sentinel, message_type="channel"
        ),
        "wrong_id": _payload(message_id=9999, text=sentinel),
        "sender_bool": _payload(
            message_id=4101,
            text=sentinel,
            sender={"user_id": True, "nickname": "x"},
        ),
        "sender_float": _payload(
            message_id=4101,
            text=sentinel,
            sender={"user_id": 20002.0, "nickname": "x"},
        ),
        "time_bool": _payload(message_id=4101, text=sentinel, timestamp=True),
        "time_float": _payload(
            message_id=4101, text=sentinel, timestamp=1_700_000_000.5
        ),
        "missing_sender": _payload(message_id=4101, text=sentinel, sender=None),
        "missing_time": _payload(message_id=4101, text=sentinel, timestamp=_MISSING),
        "message_id_bool": _payload(message_id=True, text=sentinel),
    }[invalid]
    bot = _bot(responder=lambda _api, _data: payload)
    transport, requests = _mock_sdk()
    options = HarnessOptions(
        reference_reader=_reader(bot),
        transport=_transport(transport),
    )

    async with DecisionHarness(decision_database, options) as harness:
        assert harness.decision is not None
        identity = make_identity(
            account_id="10001", group_id=group_id, message_id="5103"
        )
        target = make_message(
            identity=identity, text="引用坏节点", reference_ids=(requested,)
        )
        result = await harness.decision.evaluate(
            make_request(identity=identity, target=target)
        )

        assert result.status == "success"
        assert result.references.complete is False
        assert result.references.resolved_count == 0
        assert result.references.missing_count == 1
        assert sentinel not in _rendered(_state(requests))
        assert len(bot.calls) == 1


async def test_admission_revoked_during_await_blocks_content_delivery(
    decision_database: str,
) -> None:
    """AC：远程读取期间撤权，即使响应合法也不交付正文。"""
    group_id = "7104"
    sentinel = "撤权后不得交付的引用正文"
    admission_calls: list[str] = []

    def admitted(group_id_value: str) -> bool:
        admission_calls.append(group_id_value)
        return len(admission_calls) == 1

    bot = _bot(responder=lambda _api, _data: _payload(message_id=4001, text=sentinel))
    transport, requests = _mock_sdk()
    options = HarnessOptions(
        reference_reader=_reader(bot, admitted=admitted),
        transport=_transport(transport),
    )

    async with DecisionHarness(decision_database, options) as harness:
        assert harness.decision is not None
        identity = make_identity(
            account_id="10001", group_id=group_id, message_id="5104"
        )
        target = make_message(
            identity=identity, text="引用后撤权", reference_ids=("4001",)
        )
        result = await harness.decision.evaluate(
            make_request(identity=identity, target=target)
        )

        assert result.status == "success"
        assert result.references.resolved_count == 0
        assert result.references.missing_count == 1
        assert sentinel not in _rendered(_state(requests))
        assert admission_calls == [group_id, group_id]
        assert len(bot.calls) == 1


async def test_multihop_overlap_and_cycle_terminate_through_real_resolver(
    decision_database: str,
) -> None:
    """AC：真实原生引用图的多跳/重叠/循环由既有 resolver 终止并如实报告。"""
    group_id = "7105"
    calls: list[int] = []

    def responder(_api: str, data: dict[str, Any]) -> Any:
        requested = int(cast("int", data["message_id"]))
        calls.append(requested)
        mapping = {
            4201: ("多跳一级", ("4202", "4203")),
            4202: ("多跳二级", ("5201",)),
            4203: ("重叠三级", ("4201",)),
        }
        if requested not in mapping:
            msg = f"unexpected referenced node {requested}"
            raise AssertionError(msg)
        text, references = mapping[requested]
        return _payload(
            message_id=requested,
            message=[
                *[
                    {"type": "reply", "data": {"id": reference}}
                    for reference in references
                ],
                {"type": "text", "data": {"text": text}},
            ],
        )

    bot = _bot(responder=responder)
    transport, requests = _mock_sdk()
    options = HarnessOptions(
        reference_reader=_reader(bot),
        transport=_transport(transport),
    )

    async with DecisionHarness(decision_database, options) as harness:
        assert harness.decision is not None
        identity = make_identity(
            account_id="10001", group_id=group_id, message_id="5201"
        )
        target = make_message(
            identity=identity,
            text="引用多跳与循环",
            reference_ids=("4201", "4202", "4203"),
        )
        result = await harness.decision.evaluate(
            make_request(identity=identity, target=target)
        )

        assert result.status == "success"
        assert result.references.complete is False
        assert result.references.cycle_count >= 1
        assert result.references.missing_count == 0
        assert result.references.resolved_count == 3
        assert sorted(calls) == [4201, 4202, 4203]
        state = _state(requests)
        rendered = _rendered(state)
        for text in ("多跳一级", "多跳二级", "重叠三级"):
            assert rendered.count(text) == 1
        status = _conversation(state)["reference_status"]
        assert status["cyclic"] >= 1
        assert status["timed_out"] is False


async def test_filtered_long_message_can_be_referenced_without_becoming_target(
    decision_database: str,
) -> None:
    """AC：被过滤的长正文可作显式引用原文，但不重新成为目标或普通近期上下文。"""
    group_id = "7106"
    account_id = "10001"
    long_text = "".join(chr(0x4E00 + index) for index in range(80))
    bot = _bot(responder=lambda _api, _data: _payload(message_id=5202, text=long_text))
    transport, requests = _mock_sdk()
    options = HarnessOptions(
        reference_reader=_reader(bot),
        transport=_transport(transport),
    )

    async with DecisionHarness(decision_database, options) as harness:
        assert harness.decision is not None and harness.engine is not None
        filtered_identity = make_identity(
            account_id=account_id, group_id=group_id, message_id="5202"
        )
        filtered_target = make_message(identity=filtered_identity, text=long_text)
        filtered = await harness.decision.evaluate(
            make_request(identity=filtered_identity, target=filtered_target)
        )
        assert filtered.status == "filtered"
        assert requests == []

        identity = make_identity(
            account_id=account_id, group_id=group_id, message_id="5203"
        )
        target = make_message(
            identity=identity,
            text="引用那条长文",
            reference_ids=("5202",),
        )
        result = await harness.decision.evaluate(
            make_request(identity=identity, target=target)
        )

        assert result.status == "success"
        assert result.references.complete is True
        assert result.references.resolved_count == 1
        assert len(requests) == 1
        state = _state(requests)
        assert _rendered(state).count(long_text) == 1
        conversation = _conversation(state)
        assert len(conversation["timeline"]) == 1
        assert len(_messages(state)) == 2
        assert await read_dispatch(harness.engine, filtered_identity) is not None
        assert bot.calls == [{"api": "get_msg", "message_id": 5202}]


async def test_future_reference_cannot_enter_old_target_input(
    decision_database: str,
) -> None:
    """AC：平台时间晚于接收截止的引用节点不保留正文。"""
    group_id = "7107"
    sentinel = "未来消息不得进入旧目标输入"
    now = datetime.now(UTC)
    future = int((now + timedelta(minutes=5)).timestamp())
    bot = _bot(
        responder=lambda _api, _data: _payload(
            message_id=4301, text=sentinel, timestamp=future
        )
    )
    transport, requests = _mock_sdk()
    options = HarnessOptions(
        reference_reader=_reader(bot),
        transport=_transport(transport),
    )

    async with DecisionHarness(decision_database, options) as harness:
        assert harness.decision is not None
        identity = make_identity(
            account_id="10001", group_id=group_id, message_id="5204"
        )
        target = make_message(
            identity=identity, text="引用未来消息", reference_ids=("4301",)
        )
        result = await harness.decision.evaluate(
            make_request(
                identity=identity,
                target=target,
                received_at=now,
            )
        )

        assert result.status == "success"
        assert result.references.missing_count == 1
        assert result.references.resolved_count == 0
        assert sentinel not in _rendered(_state(requests))
        assert len(bot.calls) == 1


async def test_whole_chain_deadline_keeps_retrieved_content_and_flag(
    decision_database: str,
) -> None:
    """AC：整链共用一个截止；已取得内容保留，迟到节点不覆盖不完整标记。"""
    group_id = "7108"
    fast_text = "截止前取得的引用正文"
    late_text = "截止后才返回的引用正文"
    started = asyncio.Event()

    async def responder(_api: str, data: dict[str, Any]) -> Any:
        requested = int(cast("int", data["message_id"]))
        if requested == 4301:
            # 每跳均短于整链预算，但两跳累计超过预算。
            await asyncio.sleep(0.6)
            return _payload(
                message_id=4301,
                message=[
                    {"type": "reply", "data": {"id": "4302"}},
                    {"type": "text", "data": {"text": fast_text}},
                ],
            )
        started.set()
        await asyncio.sleep(0.6)
        return _payload(message_id=4302, text=late_text)

    bot = _bot(responder=responder)
    transport, requests = _mock_sdk()
    options = HarnessOptions(
        reference_reader=_reader(bot),
        transport=_transport(transport),
        config={"reference_timeout_seconds": 1.0},
    )

    async with DecisionHarness(decision_database, options) as harness:
        assert harness.decision is not None
        identity = make_identity(
            account_id="10001", group_id=group_id, message_id="5205"
        )
        target = make_message(
            identity=identity, text="引用慢节点", reference_ids=("4301",)
        )
        result = await harness.decision.evaluate(
            make_request(identity=identity, target=target)
        )

        assert started.is_set()
        assert result.status == "success"
        assert result.references.timed_out is True
        assert result.references.complete is False
        assert result.references.resolved_count == 1
        assert len(requests) == 1
        state = _state(requests)
        rendered = _rendered(state)
        assert fast_text in rendered
        assert late_text not in rendered
        status = _conversation(state)["reference_status"]
        assert status["timed_out"] is True
        assert [call["message_id"] for call in bot.calls] == [4301, 4302]


async def test_image_payload_never_enters_message_state_or_logs(
    decision_database: str,
) -> None:
    """AC：只保留「发送了图片」事实；URL/base64 不进入 state、SDK 请求或日志。"""
    group_id = "7109"
    url = "https://cdn.example.invalid/private-TSK185.png"
    base64_payload = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8"
    bot = _bot(
        responder=lambda _api, _data: _payload(
            message_id=4401,
            message=[
                {"type": "text", "data": {"text": "看这张图"}},
                {
                    "type": "image",
                    "data": {
                        "file": f"base64://{base64_payload}",
                        "url": url,
                    },
                },
            ],
        )
    )
    projected = await _reader(bot)(
        _identity(group_id=group_id, message_id="5206"), "4401"
    )
    assert projected is not None
    assert projected.image_present is True
    assert projected.image_description is None
    for secret in (url, base64_payload):
        assert secret not in repr(projected)

    transport, requests = _mock_sdk()
    options = HarnessOptions(
        reference_reader=_reader(bot),
        transport=_transport(transport),
    )
    logs: list[str] = []

    def sink(message: Any) -> None:
        logs.append(str(message))

    handler_id = loguru_logger.add(sink, level="DEBUG")
    try:
        async with DecisionHarness(decision_database, options) as harness:
            assert harness.decision is not None
            identity = make_identity(
                account_id="10001", group_id=group_id, message_id="5206"
            )
            target = make_message(
                identity=identity, text="引用图片", reference_ids=("4401",)
            )
            result = await harness.decision.evaluate(
                make_request(identity=identity, target=target)
            )
    finally:
        loguru_logger.remove(handler_id)

    assert result.status == "success"
    assert len(requests) == 1
    transmitted = requests[0].content.decode("utf-8")
    rendered = _rendered(_state(requests))
    ordinary = "\n".join(logs)
    for secret in (url, base64_payload):
        assert secret not in transmitted
        assert secret not in rendered
        assert secret not in ordinary
    message = _message_with_text(_state(requests), "看这张图")
    assert message["images"] == {"present": True, "known_description": None}


async def test_cancellation_reaches_platform_await_without_retry() -> None:
    """AC：取消传播到真实 ``get_msg`` await，不被吞掉也不重试。"""
    started = asyncio.Event()
    release = asyncio.Event()
    admission_calls: list[str] = []

    async def responder(_api: str, data: dict[str, Any]) -> Any:
        assert data["message_id"] == 4501
        started.set()
        await release.wait()
        return _payload(message_id=4501, text="取消后不得交付")

    def admitted(group_id: str) -> bool:
        admission_calls.append(group_id)
        return True

    bot = _bot(responder=responder)
    reader = _reader(bot, admitted=admitted)
    task = asyncio.create_task(reader(_identity(), "4501"))
    await asyncio.wait_for(started.wait(), timeout=5)
    task.cancel()
    try:
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()

    assert task.cancelled() is True
    assert admission_calls == ["7001"]
    assert bot.calls == [{"api": "get_msg", "message_id": 4501}]
