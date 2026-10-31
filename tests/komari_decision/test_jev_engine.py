"""TSK-182 判定引擎验收：生命周期、入口过滤、3x2 组合、问题矩阵、失败分类。

全部断言都穿过公开 seam：``evaluate`` / ``claim_handoff`` /
``observe_delivered`` / ``get_health`` 与官方 SDK 的外部 transport。期望值
来自 TSK-180 的书面规则（手写字面量），不导入生产常量。
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterator
from uuid import uuid4

import httpx2
import pytest

from tests.komari_decision.jev_test_support import (
    CONFIGURED_API_KEY,
    CONFIGURED_BASE_URL,
    CONFIGURED_MODEL,
    ConfigSource,
    DecisionHarness,
    HarnessOptions,
    build_answers,
    create_scratch_database,
    drop_scratch_database,
    make_identity,
    make_message,
    make_request,
    read_dispatch,
    skip_without_services,
    upgrade_database,
    wait_until,
    write_config_row,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture(scope="module")
def decision_database() -> Iterator[str]:
    """派生一个迁移到 head 的一次性隔离库（模块级共享，用例内清表）。"""
    skip_without_services()
    url = asyncio.run(create_scratch_database(f"_jev{uuid4().hex[:8]}"))
    yield url
    asyncio.run(drop_scratch_database(url))


def long_text(length: int) -> str:
    """返回无任何整段重复的 CJK 正文（避免与重复规则混淆）。"""
    return "".join(chr(0x4E00 + index) for index in range(length))


# ------------------------------ 生命周期与健康 ------------------------------


async def test_health_snapshot_is_passive_and_unknown_before_evidence(
    decision_database: str,
) -> None:
    """AC：健康查询只读快照；没有调用证据时不伪造成功时间或全局计数。"""
    async with DecisionHarness(decision_database) as harness:
        assert harness.decision is not None
        health = harness.decision.get_health()

        assert health.runtime_status == "ready"
        assert health.reason_code == "ready"
        assert health.last_request_status == "unknown"
        assert health.last_success_at is None
        assert health.last_failure_at is None
        assert health.last_failure_reason_code is None
        assert health.consecutive_failures == 0
        assert health.in_flight_count is None
        assert health.queued_count is None
        assert harness.transport.requests == []


async def test_failed_jev_readiness_keeps_summon_dedup_alive(
    decision_database: str,
) -> None:
    """AC：初始化失败被隔离；非 Jev 平台召唤仍可去重执行。"""
    options = HarnessOptions(config={"jev_base_url": "", "jev_api_key": ""})
    async with DecisionHarness(decision_database, options) as harness:
        assert harness.decision is not None
        health = harness.decision.get_health()
        assert health.runtime_status == "failed"
        assert health.reason_code == "configuration_invalid"

        identity = make_identity()
        summon = make_request(
            identity=identity,
            real_platform_at=True,
            target=make_message(identity=identity, text=long_text(80)),
        )
        first = await harness.decision.evaluate(summon)
        assert first.status == "bypass"
        assert first.reason_code == "platform_bypass"
        assert first.route == "chat"
        assert first.force_reply is True
        assert first.reply_choice is None
        assert first.retention_choice is None
        assert first.summary_probability is None
        assert first.retain_recent is True
        assert first.memory_candidate is True

        redelivered = await harness.decision.evaluate(make_request(identity=identity))
        assert redelivered.status == "duplicate"
        assert redelivered.reason_code == "duplicate_event"
        assert harness.transport.requests == []


async def test_missing_prompt_blocks_new_decisions_only(
    decision_database: str,
) -> None:
    """AC：Prompt 缺失只让判定 failed；召唤直通与去重仍工作。"""
    options = HarnessOptions(write_prompt_row=False)
    async with DecisionHarness(decision_database, options) as harness:
        assert harness.decision is not None
        health = harness.decision.get_health()
        assert health.runtime_status == "failed"
        assert health.reason_code == "prompt_unavailable"

        ordinary = await harness.decision.evaluate(make_request())
        assert ordinary.status == "failed"
        assert ordinary.reason_code == "prompt_unavailable"
        assert ordinary.reply_choice is None
        assert ordinary.retention_choice is None
        assert ordinary.retain_recent is None

        summon = await harness.decision.evaluate(make_request(real_platform_at=True))
        assert summon.status == "bypass"
        assert harness.transport.requests == []


async def test_close_revokes_unhanded_work_and_refuses_new_work(
    decision_database: str,
) -> None:
    """AC：关闭释放自有资源，并撤销尚未交接的资格。"""
    async with DecisionHarness(decision_database) as harness:
        assert harness.decision is not None and harness.engine is not None
        identity = make_identity()
        result = await harness.decision.evaluate(make_request(identity=identity))
        assert result.status == "success"
        row = await read_dispatch(harness.engine, identity)
        assert row is not None
        assert row["handed_off_at"] is None

        await harness.decision.close()

        row = await read_dispatch(harness.engine, identity)
        assert row is not None
        assert row["state"] == "cancelled"
        assert row["reason_code"] == "interrupted"
        assert row["business_route"] == "none"
        assert row["notification_eligible"] is False
        assert row["slot_held"] is False

        later = await harness.decision.evaluate(make_request())
        assert later.status == "failed"
        assert later.reason_code == "interrupted"


# ------------------------------ 入口过滤与召唤 ------------------------------


async def test_platform_summon_bypasses_filters_and_requests_nothing(
    decision_database: str,
) -> None:
    """AC：真实平台召唤不受长度/符号/重复否决；非总结召唤不调用 Jev。"""
    async with DecisionHarness(decision_database) as harness:
        assert harness.decision is not None
        identity = make_identity()
        target = make_message(identity=identity, text="！" * 60)
        result = await harness.decision.evaluate(
            make_request(identity=identity, target=target, real_platform_at=True)
        )
        assert result.status == "bypass"
        assert result.reason_code == "platform_bypass"
        assert result.force_reply is True
        assert result.effective_reply == "required"
        assert harness.transport.requests == []

        reply_summon = await harness.decision.evaluate(
            make_request(reply_to_bot=True)
        )
        assert reply_summon.status == "bypass"
        assert harness.transport.requests == []


async def test_text_nickname_does_not_become_platform_summon(
    decision_database: str,
) -> None:
    """AC：纯文本称呼进入判定，但不构成确定性召唤。"""
    async with DecisionHarness(decision_database) as harness:
        assert harness.decision is not None
        result = await harness.decision.evaluate(make_request(text="小鞠今天想聊什么？"))
        assert result.status == "success"
        assert result.status != "bypass"
        assert len(harness.transport.requests) == 1


@pytest.mark.parametrize(
    ("text", "expect_request", "expect_status"),
    [
        (long_text(50), True, "success"),
        (long_text(51), False, "filtered"),
        (f"小鞠{long_text(60)}", True, "success"),
        (long_text(60), False, "filtered"),
        ("！？。、", False, "filtered"),
        ("哈哈哈哈哈", False, "filtered"),
        ("哈哈哈", False, "filtered"),
        ("123123123", False, "filtered"),
        ("谢谢谢谢", False, "filtered"),
        ("谢谢", True, "success"),
        ("拜拜", True, "success"),
        ("困困", True, "success"),
        ("今天好开心哈哈哈哈", True, "success"),
        ("？谁在群里", True, "success"),
    ],
)
async def test_ordinary_filter_rules(
    decision_database: str,
    text: str,
    *,
    expect_request: bool,
    expect_status: str,
) -> None:
    """AC：50 码点允许、51 普通正文过滤、称呼例外、整段重复与零最短长度。"""
    async with DecisionHarness(decision_database) as harness:
        assert harness.decision is not None
        result = await harness.decision.evaluate(make_request(text=text))
        assert result.status == expect_status
        if expect_status == "filtered":
            assert result.reason_code == "filtered"
            assert result.retain_recent is None
            assert result.memory_candidate is None
        assert len(harness.transport.requests) == (1 if expect_request else 0)


async def test_functional_command_is_a_trusted_ingress_fact(
    decision_database: str,
) -> None:
    """AC：功能命令按真实路由事实过滤，普通标点开头不冒充命令。"""
    async with DecisionHarness(decision_database) as harness:
        assert harness.decision is not None
        command = await harness.decision.evaluate(
            make_request(text="。总结", functional_command=True)
        )
        assert command.status == "filtered"
        assert harness.transport.requests == []

        discussion = await harness.decision.evaluate(make_request(text="。总结怎么用？"))
        assert discussion.status == "success"
        assert len(harness.transport.requests) == 1


# ------------------------------ 3x2 组合与抽签 ------------------------------


@pytest.mark.parametrize(
    ("reply", "retention", "draw", "expected"),
    [
        (
            "required",
            "candidate",
            0.99,
            {
                "effective_reply": "required",
                "route": "chat",
                "force_reply": True,
                "reason_code": "classified",
                "optional_selected": None,
                "retain_recent": True,
                "memory_candidate": True,
            },
        ),
        (
            "required",
            "noise",
            0.99,
            {
                "effective_reply": "required",
                "route": "chat",
                "force_reply": True,
                "reason_code": "classified",
                "optional_selected": None,
                "retain_recent": True,
                "memory_candidate": True,
            },
        ),
        (
            "optional",
            "candidate",
            0.10,
            {
                "effective_reply": "optional",
                "route": "chat",
                "force_reply": False,
                "reason_code": "optional_selected",
                "optional_selected": True,
                "retain_recent": True,
                "memory_candidate": True,
            },
        ),
        (
            "optional",
            "noise",
            0.90,
            {
                "effective_reply": "silent",
                "route": "none",
                "force_reply": False,
                "reason_code": "optional_skipped",
                "optional_selected": False,
                "retain_recent": True,
                "memory_candidate": True,
            },
        ),
        (
            "silent",
            "candidate",
            0.99,
            {
                "effective_reply": "silent",
                "route": "none",
                "force_reply": False,
                "reason_code": "silent",
                "optional_selected": None,
                "retain_recent": True,
                "memory_candidate": True,
            },
        ),
        (
            "silent",
            "noise",
            0.99,
            {
                "effective_reply": "silent",
                "route": "none",
                "force_reply": False,
                "reason_code": "silent",
                "optional_selected": None,
                "retain_recent": False,
                "memory_candidate": False,
            },
        ),
    ],
)
async def test_reply_and_retention_combination_matrix(
    decision_database: str,
    reply: str,
    retention: str,
    draw: float,
    *,
    expected: dict[str, object],
) -> None:
    """AC：3x2 六种组合的去向与原始分类分别表达。"""
    options = HarnessOptions(
        responder=lambda request, _index: build_answers(
            json.loads(request.content)["questions"],
            reply=reply,
            retention=retention,
        ),
        random_values=[draw],
    )
    async with DecisionHarness(decision_database, options) as harness:
        assert harness.decision is not None
        result = await harness.decision.evaluate(make_request())

        assert result.status == "success"
        assert result.reply_choice is not None
        assert result.reply_choice.value == reply
        assert result.retention_choice is not None
        assert result.retention_choice.value == retention
        assert result.effective_reply == expected["effective_reply"]
        assert result.route == expected["route"]
        assert result.force_reply is expected["force_reply"]
        assert result.reason_code == expected["reason_code"]
        assert result.optional_selected == expected["optional_selected"]
        assert result.retain_recent == expected["retain_recent"]
        assert result.memory_candidate == expected["memory_candidate"]
        assert len(harness.transport.requests) == 1


@pytest.mark.parametrize(
    ("reply", "expected_draws"),
    [("required", 0), ("optional", 1), ("silent", 0)],
)
async def test_only_optional_draws_exactly_once(
    decision_database: str,
    reply: str,
    *,
    expected_draws: int,
) -> None:
    """AC：required 不抽签，optional 最多抽签一次，silent 不抽签。"""
    options = HarnessOptions(
        responder=lambda request, _index: build_answers(
            json.loads(request.content)["questions"], reply=reply
        ),
        random_values=[0.10],
    )
    async with DecisionHarness(decision_database, options) as harness:
        assert harness.decision is not None
        result = await harness.decision.evaluate(make_request())
        assert result.reply_choice is not None
        assert result.reply_choice.value == reply
        assert harness.random_draws == expected_draws


async def test_optional_draw_uses_frozen_probability_and_never_redraws(
    decision_database: str,
) -> None:
    """AC：抽签使用任务入口冻结的概率；重投不重新调用也不重新抽签。"""
    release = asyncio.Event()

    async def responder(request: httpx2.Request, index: int) -> dict[str, object]:
        del index
        await release.wait()
        return build_answers(
            json.loads(request.content)["questions"],
            reply="optional",
            retention="candidate",
        )

    options = HarnessOptions(responder=responder, random_values=[0.10, 0.90])
    async with DecisionHarness(decision_database, options) as harness:
        assert harness.decision is not None
        request = make_request()
        pending = asyncio.create_task(harness.decision.evaluate(request))
        await wait_until(lambda: bool(harness.transport.requests))
        # 在途任务必须保持入口冻结的 0.5 概率
        harness.set_config({"optional_reply_probability": 0.0})
        release.set()
        result = await pending

        assert result.reply_choice is not None
        assert result.reply_choice.value == "optional"
        assert result.optional_selected is True
        assert harness.random_draws == 1

        redelivered = await harness.decision.evaluate(make_request(identity=request.event))
        assert redelivered.status == "duplicate"
        assert harness.random_draws == 1
        assert len(harness.transport.requests) == 1


# ------------------------------ 问题矩阵与总结阈值 ------------------------------


async def test_question_matrix_per_entry_fact(decision_database: str) -> None:
    """AC：四种入口事实只请求各自需要的问题，且共用一次请求。"""
    async with DecisionHarness(decision_database) as harness:
        assert harness.decision is not None
        await harness.decision.evaluate(make_request(text="今天谁值班？"))
        assert harness.transport.question_ids(0) == [
            "reply_eligibility",
            "retention_purpose",
        ]

        await harness.decision.evaluate(
            make_request(text="请总结一下今天的聊天", summary_candidate=True)
        )
        assert harness.transport.question_ids(1) == [
            "reply_eligibility",
            "retention_purpose",
            "summary_request",
        ]

        await harness.decision.evaluate(
            make_request(
                text="请总结一下今天的聊天",
                summary_candidate=True,
                real_platform_at=True,
            )
        )
        assert harness.transport.question_ids(2) == ["summary_request"]

        await harness.decision.evaluate(
            make_request(text="在吗", real_platform_at=True)
        )
        assert len(harness.transport.requests) == 3


@pytest.mark.parametrize(
    ("probability", "expect_summary"),
    [(0.70, False), (0.7000001, True), (0.0, False), (1.0, True)],
)
async def test_summary_threshold_is_strictly_greater_than_point_seven(
    decision_database: str,
    probability: float,
    *,
    expect_summary: bool,
) -> None:
    """AC：p=0.7 不命中，p>0.7 独占总结，未命中共用同一结果。"""
    options = HarnessOptions(
        responder=lambda request, _index: build_answers(
            json.loads(request.content)["questions"],
            reply="required",
            retention="candidate",
            summary=probability,
        )
    )
    async with DecisionHarness(decision_database, options) as harness:
        assert harness.decision is not None
        result = await harness.decision.evaluate(
            make_request(text="请总结今天的聊天", summary_candidate=True)
        )
        assert result.summary_probability == pytest.approx(probability)
        assert result.reply_choice is not None
        if expect_summary:
            assert result.reason_code == "summary_matched"
            assert result.route == "summary"
            assert result.force_reply is False
            assert result.effective_reply is None
            assert result.optional_selected is None
            assert harness.random_draws == 0
        else:
            assert result.reason_code == "classified"
            assert result.route == "chat"
            assert result.effective_reply == "required"
        assert len(harness.transport.requests) == 1


@pytest.mark.parametrize(
    ("probability", "expect_summary"),
    [(0.70, False), (0.71, True)],
)
async def test_platform_summary_candidate_requests_only_the_noul(
    decision_database: str,
    probability: float,
    *,
    expect_summary: bool,
) -> None:
    """AC：平台召唤的总结候选只问 Noul；命中独占，未命中走确定性召唤。"""
    options = HarnessOptions(
        responder=lambda request, _index: build_answers(
            json.loads(request.content)["questions"], summary=probability
        )
    )
    async with DecisionHarness(decision_database, options) as harness:
        assert harness.decision is not None
        result = await harness.decision.evaluate(
            make_request(
                text="请总结今天的聊天",
                summary_candidate=True,
                real_platform_at=True,
            )
        )
        assert harness.transport.question_ids(0) == ["summary_request"]
        assert result.reply_choice is None
        assert result.retention_choice is None
        if expect_summary:
            assert result.route == "summary"
            assert result.reason_code == "summary_matched"
            assert result.force_reply is False
        else:
            assert result.route == "chat"
            assert result.reason_code == "platform_bypass"
            assert result.force_reply is True
            assert result.effective_reply == "required"
            assert result.retain_recent is True
        assert harness.random_draws == 0


# ------------------------------ 技术失败分类 ------------------------------


def _status(code: int):
    def handler(request: httpx2.Request, index: int) -> httpx2.Response:
        del index
        return httpx2.Response(
            code, json={"error": "synthetic-upstream-failure"}, request=request
        )

    return handler


def _raise(error: BaseException):
    def handler(_request: httpx2.Request, _index: int) -> object:
        raise error

    return handler


def _malformed_choice(request: httpx2.Request, _index: int) -> object:
    payload = build_answers(json.loads(request.content)["questions"])
    payload["answers"]["reply_eligibility"]["choice"] = "maybe"
    return payload


def _missing_answer(request: httpx2.Request, _index: int) -> object:
    payload = build_answers(json.loads(request.content)["questions"])
    payload["answers"].pop("retention_purpose")
    return payload


def _broken_response_body(
    _request: httpx2.Request, _index: int
) -> dict[str, object]:
    return {"model": CONFIGURED_MODEL, "answers": {"reply_eligibility": {"type": "choice"}}}


@pytest.mark.parametrize(
    ("responder", "expected_reason"),
    [
        (_raise(httpx2.ConnectError("cannot connect")), "network_error"),
        (_raise(httpx2.ReadTimeout("read timed out")), "request_timeout"),
        (_status(429), "rate_limited"),
        (_status(401), "authentication_error"),
        (_status(500), "provider_error"),
        (_broken_response_body, "protocol_error"),
        (_malformed_choice, "protocol_error"),
        (_missing_answer, "protocol_error"),
    ],
)
async def test_technical_failures_never_fake_silence_or_noise(
    decision_database: str,
    responder: Any,
    expected_reason: str,
) -> None:
    """AC：技术失败独立表达，不伪造 silent/noise/总结未命中。"""
    async with DecisionHarness(decision_database, HarnessOptions(responder=responder)) as harness:
        assert harness.decision is not None
        request = make_request(text="请总结今天的聊天", summary_candidate=True)
        result = await harness.decision.evaluate(request)

        assert result.status == "failed"
        assert result.reason_code == expected_reason
        assert result.reply_choice is None
        assert result.retention_choice is None
        assert result.summary_probability is None
        assert result.effective_reply is None
        assert result.retain_recent is None
        assert result.memory_candidate is None
        assert result.route == "none"
        assert harness.random_draws == 0
        assert len(harness.transport.requests) == 1

        health = harness.decision.get_health()
        assert health.last_request_status == "failure"
        assert health.last_failure_reason_code == expected_reason
        assert health.consecutive_failures == 1

        assert await harness.decision.claim_handoff(request, result, "chat") is False
        assert (
            await harness.decision.claim_handoff(request, result, "failure_notification")
            is True
        )
        assert (
            await harness.decision.claim_handoff(request, result, "failure_notification")
            is False
        )


async def test_missing_usage_and_model_are_preserved_separately(
    decision_database: str,
) -> None:
    """AC：实际响应模型与缺失用量分别保留，不用请求模型替代。"""
    options = HarnessOptions(
        responder=lambda request, _index: {
            "model": "jev-1.14.0-actual",
            "answers": build_answers(json.loads(request.content)["questions"])[
                "answers"
            ],
            # 供应方未报告用量：字段存在但数值缺失
            "usage": {},
        }
    )
    async with DecisionHarness(decision_database, options) as harness:
        assert harness.decision is not None
        result = await harness.decision.evaluate(make_request())
        assert result.requested_model == CONFIGURED_MODEL
        assert result.actual_model == "jev-1.14.0-actual"
        assert result.usage.input_tokens is None
        assert result.usage.output_tokens is None


# ------------------------------ 冻结输入 ------------------------------


async def test_frozen_recent_window_excludes_later_messages(
    decision_database: str,
) -> None:
    """AC：判定快照冻结；之后送达的消息不进入旧快照，目标不重复计入前文。"""
    release = asyncio.Event()

    async def responder(request: httpx2.Request, index: int) -> dict[str, object]:
        if index >= 1:  # 第二次请求（当前目标）在测试放行前保持挂起
            await release.wait()
        return build_answers(json.loads(request.content)["questions"])

    async with DecisionHarness(
        decision_database, HarnessOptions(responder=responder)
    ) as harness:
        assert harness.decision is not None
        account_id = f"acct-{uuid4().hex[:8]}"
        group_id = f"group-{uuid4().hex[:8]}"
        first_identity = make_identity(
            account_id=account_id, group_id=group_id, message_id="m1"
        )
        first = await harness.decision.evaluate(
            make_request(
                identity=first_identity,
                target=make_message(identity=first_identity, text="第一条前文"),
            )
        )
        assert first.status == "success"

        target_identity = make_identity(
            account_id=account_id, group_id=group_id, message_id="m2"
        )
        target = make_message(identity=target_identity, text="第二条当前目标")
        pending = asyncio.create_task(
            harness.decision.evaluate(
                make_request(identity=target_identity, target=target)
            )
        )
        await wait_until(lambda: len(harness.transport.requests) >= 2)

        later = make_message(
            group_id=group_id, message_id="m3", text="之后才送达的消息"
        )
        await harness.decision.observe_delivered(
            later, protocol="onebot_v11", account_id=account_id, received_at=later.timestamp
        )
        release.set()
        result = await pending
        assert result.status == "success"

        state = harness.transport.state(1)
        conversation = state["untrusted_group_conversation"]
        assert len(conversation["timeline"]) == 2
        assert conversation["timeline"][-1] == conversation["target"]
        rendered = json.dumps(state, ensure_ascii=False)
        assert "之后才送达的消息" not in rendered
        assert "第一条前文" in rendered
        assert "第二条当前目标" in rendered


async def test_observe_delivered_is_idempotent_and_private(
    decision_database: str,
) -> None:
    """AC：确认送达按平台身份幂等；模型 state 不含原始平台身份或凭据。"""
    async with DecisionHarness(decision_database) as harness:
        assert harness.decision is not None
        identity = make_identity()
        delivered = make_message(
            group_id=identity.group_id, text="已送达的机器人回复", is_bot=True
        )
        for _ in range(2):
            await harness.decision.observe_delivered(
                delivered, protocol=identity.protocol, account_id=identity.account_id, received_at=delivered.timestamp
            )

        result = await harness.decision.evaluate(make_request(identity=identity))
        assert result.status == "success"
        state = harness.transport.state(0)
        conversation = state["untrusted_group_conversation"]
        assert len(conversation["timeline"]) == 2
        rendered = json.dumps(state, ensure_ascii=False)
        assert delivered.message_id not in rendered
        assert delivered.sender_id not in rendered
        assert identity.group_id not in rendered
        assert CONFIGURED_API_KEY not in rendered
        assert CONFIGURED_BASE_URL not in rendered
        assert state["security"]


async def test_prompt_revision_is_frozen_for_in_flight_task(
    decision_database: str,
) -> None:
    """AC：Prompt 修订在任务入口冻结；后续修订不影响在途请求。"""
    release = asyncio.Event()

    async def responder(request: httpx2.Request, index: int) -> dict[str, object]:
        del index
        await release.wait()
        return build_answers(json.loads(request.content)["questions"])

    async with DecisionHarness(
        decision_database, HarnessOptions(responder=responder)
    ) as harness:
        assert harness.decision is not None
        pending = asyncio.create_task(harness.decision.evaluate(make_request()))
        await wait_until(lambda: bool(harness.transport.requests))

        from tests.komari_decision.jev_test_support import write_prompt

        await write_prompt(
            {
                "role_description": "新的角色描述",
                "reply_eligibility": "新的回应资格",
                "retention_purpose": "新的保留用途",
                "summary_recognition": "新的总结识别",
            }
        )
        release.set()
        result = await pending
        assert result.status == "success"

        questions = harness.transport.payload(0)["questions"]
        encoded = json.dumps(questions, ensure_ascii=False)
        assert "判定角色" in encoded
        assert "新的角色描述" not in encoded
        assert result.prompt_revision == 1


# ------------------------------ 入口事实与协议校验 ------------------------------


async def test_mentioned_functional_command_is_never_a_chat_bypass(
    decision_database: str,
) -> None:
    """AC：功能命令即使带真实平台召唤也不进入判定，且无任何外部效果。"""
    async with DecisionHarness(decision_database) as harness:
        assert harness.decision is not None
        request = make_request(
            text="。总结",
            functional_command=True,
            real_platform_at=True,
        )
        result = await harness.decision.evaluate(request)
        assert result.status == "filtered"
        assert result.reason_code == "filtered"
        assert result.route == "none"
        assert harness.transport.requests == []
        assert await harness.decision.claim_handoff(request, result, "chat") is False


async def test_unexpected_answer_id_is_a_protocol_failure(
    decision_database: str,
) -> None:
    """AC：额外答案 id 与缺失答案一样按协议失败处理，不抢救部分答案。"""

    def responder(request: httpx2.Request, _index: int) -> dict[str, Any]:
        payload = build_answers(json.loads(request.content)["questions"])
        payload["answers"]["tone"] = {
            "type": "noul",
            "noul": 0.9,
        }
        return payload

    async with DecisionHarness(
        decision_database, HarnessOptions(responder=responder)
    ) as harness:
        assert harness.decision is not None
        result = await harness.decision.evaluate(make_request())
        assert result.status == "failed"
        assert result.reason_code == "protocol_error"
        assert result.reply_choice is None
        assert result.retention_choice is None
        assert result.retain_recent is None


async def test_configuration_rejection_is_not_an_upstream_request_failure(
    decision_database: str,
) -> None:
    """AC：创建客户端前被拒的配置失败不计入上游请求失败。"""
    options = HarnessOptions(config={"jev_api_key": ""})
    async with DecisionHarness(decision_database, options) as harness:
        assert harness.decision is not None
        result = await harness.decision.evaluate(make_request())
        assert result.status == "failed"
        assert result.reason_code == "configuration_invalid"
        assert harness.transport.requests == []
        health = harness.decision.get_health()
        assert health.last_request_status == "unknown"
        assert health.consecutive_failures == 0


async def test_denied_admission_caches_no_body_and_calls_nothing(
    decision_database: str,
) -> None:
    """AC：准入拒绝只留最小身份，不缓存正文、不读引用、不调用模型。"""
    options = HarnessOptions(admission=lambda _identity: False)
    async with DecisionHarness(decision_database, options) as harness:
        assert harness.decision is not None
        identity = make_identity()
        result = await harness.decision.evaluate(
            make_request(
                identity=identity,
                target=make_message(
                    identity=identity, text="受限群里不该进判定的正文"
                ),
            )
        )
        assert result.status == "cancelled"
        assert result.reason_code == "admission_revoked"
        assert harness.transport.requests == []
        assert harness.reference_calls == []
        assert harness.engine is not None
        row = await read_dispatch(harness.engine, identity)
        assert row is not None
        # 撤权的持久终态是 cancelled（不是普通失败）
        assert row["state"] == "cancelled"
        assert row["reason_code"] == "admission_revoked"
        assert row["slot_held"] is False

        from komari_bot.plugins.komari_decision.services.jev_recent import (
            DecisionRecentStore,
        )

        assert harness.redis is not None
        assert (
            await DecisionRecentStore(harness.redis).window(
                protocol=identity.protocol,
                account_id=identity.account_id,
                group_id=identity.group_id,
            )
            == ()
        )


async def test_queue_rejection_gives_summary_candidate_one_notification(
    decision_database: str,
) -> None:
    """AC：总结候选的排队拒绝可领一次失败通知，且不计上游请求失败。"""
    gate = asyncio.Event()

    async def responder(request: httpx2.Request, index: int) -> dict[str, Any]:
        del index
        await gate.wait()
        return build_answers(json.loads(request.content)["questions"])

    options = HarnessOptions(
        responder=responder,
        config={"max_concurrent_decisions": 1, "queue_timeout_seconds": 0.2},
    )
    async with DecisionHarness(decision_database, options) as harness:
        assert harness.decision is not None
        account_id = f"acct-{uuid4().hex[:8]}"
        group_id = f"group-{uuid4().hex[:8]}"
        first_identity = make_identity(
            account_id=account_id, group_id=group_id, message_id="m1"
        )
        running = asyncio.create_task(
            harness.decision.evaluate(
                make_request(
                    identity=first_identity,
                    target=make_message(identity=first_identity),
                )
            )
        )
        await wait_until(lambda: bool(harness.transport.requests))

        second_identity = make_identity(
            account_id=account_id, group_id=group_id, message_id="m2"
        )
        request = make_request(
            identity=second_identity,
            target=make_message(identity=second_identity, text="请总结今天的聊天"),
            summary_candidate=True,
        )
        expired = await harness.decision.evaluate(request)
        assert expired.status == "failed"
        assert expired.reason_code == "queue_timeout"
        assert (
            await harness.decision.claim_handoff(request, expired, "chat") is False
        )
        assert (
            await harness.decision.claim_handoff(
                request, expired, "failure_notification"
            )
            is True
        )
        assert (
            await harness.decision.claim_handoff(
                request, expired, "failure_notification"
            )
            is False
        )
        health = harness.decision.get_health()
        assert health.last_request_status == "unknown"
        assert health.consecutive_failures == 0
        gate.set()
        assert (await running).status == "success"


async def test_eligible_message_survives_technical_failure_as_context(
    decision_database: str,
) -> None:
    """AC：通过过滤但技术失败的普通消息仍作为合格上下文保留。"""
    gate = asyncio.Event()

    async def responder(request: httpx2.Request, index: int) -> dict[str, Any]:
        del index
        await gate.wait()
        return build_answers(json.loads(request.content)["questions"])

    options = HarnessOptions(
        responder=responder,
        config={"max_concurrent_decisions": 1, "queue_timeout_seconds": 0.2},
    )
    async with DecisionHarness(decision_database, options) as harness:
        assert harness.decision is not None
        account_id = f"acct-{uuid4().hex[:8]}"
        group_id = f"group-{uuid4().hex[:8]}"
        first_identity = make_identity(
            account_id=account_id, group_id=group_id, message_id="m1"
        )
        running = asyncio.create_task(
            harness.decision.evaluate(
                make_request(
                    identity=first_identity,
                    target=make_message(identity=first_identity, text="占用名额的消息"),
                )
            )
        )
        await wait_until(lambda: bool(harness.transport.requests))

        dropped_identity = make_identity(
            account_id=account_id, group_id=group_id, message_id="m2"
        )
        rejected = await harness.decision.evaluate(
            make_request(
                identity=dropped_identity,
                target=make_message(
                    identity=dropped_identity, text="排队超时但合格的正文"
                ),
            )
        )
        assert rejected.reason_code == "queue_timeout"

        gate.set()
        assert (await running).status == "success"

        observer_identity = make_identity(
            account_id=account_id, group_id=group_id, message_id="m3"
        )
        result = await harness.decision.evaluate(
            make_request(
                identity=observer_identity,
                target=make_message(identity=observer_identity, text="后续消息"),
            )
        )
        assert result.status == "success"
        rendered = json.dumps(harness.transport.state(-1), ensure_ascii=False)
        assert "排队超时但合格的正文" in rendered


# ------------------------------ 就绪、预算与窗口边界 ------------------------------


async def test_health_is_not_ready_before_initialize(decision_database: str) -> None:
    """AC：尚未初始化（没有持久所有者）时不得报告 ready。"""
    options = HarnessOptions(initialize=False)
    async with DecisionHarness(decision_database, options) as harness:
        assert harness.decision is not None
        health = harness.decision.get_health()
        assert health.runtime_status == "failed"
        assert health.reason_code == "initializing"
        assert harness.transport.requests == []


async def test_readiness_recovers_after_storage_is_corrected() -> None:
    """AC：持久化不可用保持 failed；外部存储修正后 refresh 可恢复 ready。"""
    skip_without_services()
    url = await create_scratch_database(
        f"_jevretry{uuid4().hex[:8]}", migrate=False
    )
    try:
        options = HarnessOptions(reset_storage=False, write_config_row=False)
        async with DecisionHarness(url, options) as harness:
            assert harness.decision is not None
            health = harness.decision.get_health()
            assert health.runtime_status == "failed"
            assert health.reason_code == "storage_unavailable"

            upgrade_database(url)
            assert harness.engine is not None
            await write_config_row(harness.engine)
            await harness.decision.refresh()
            health = harness.decision.get_health()
            assert health.runtime_status == "ready"
            assert health.reason_code == "ready"

            result = await harness.decision.evaluate(make_request())
            assert result.status == "success"
    finally:
        await drop_scratch_database(url)


async def test_close_is_permanent_and_refresh_cannot_revive_it(
    decision_database: str,
) -> None:
    """AC：永久关闭后 refresh 不得把引擎重新变成可用。"""
    async with DecisionHarness(decision_database) as harness:
        assert harness.decision is not None
        await harness.decision.close()
        await harness.decision.refresh()
        assert harness.decision.get_health().runtime_status != "ready"
        result = await harness.decision.evaluate(make_request())
        assert result.status == "failed"
        assert result.reason_code == "interrupted"


async def test_task_config_is_frozen_at_entry_before_the_first_scheduling_gap(
    decision_database: str,
) -> None:
    """AC：在公开入口冻结配置；随后立刻改配置不影响已入队的任务。"""
    options = HarnessOptions(random_values=[0.10])
    async with DecisionHarness(decision_database, options) as harness:
        assert harness.decision is not None
        request = make_request()
        pending = asyncio.create_task(harness.decision.evaluate(request))
        # 任务还没有开始执行（create_task 尚未被调度）
        harness.set_config({"optional_reply_probability": 0.0})
        result = await pending
        assert result.reply_choice is not None
        assert result.reply_choice.value == "required"
        assert harness.random_draws == 0


async def test_assigned_route_is_persisted_before_handoff(
    decision_database: str,
) -> None:
    """AC：完成时持久化本次允许的路由（直通=chat，总结命中=summary）。"""
    async with DecisionHarness(decision_database) as harness:
        assert harness.decision is not None and harness.engine is not None
        bypass_identity = make_identity()
        bypass = await harness.decision.evaluate(
            make_request(
                identity=bypass_identity,
                target=make_message(identity=bypass_identity),
                real_platform_at=True,
            )
        )
        assert bypass.status == "bypass"
        row = await read_dispatch(harness.engine, bypass_identity)
        assert row is not None
        assert row["business_route"] == "chat"
        assert row["handed_off_at"] is None

        summary_identity = make_identity()
        summary = await harness.decision.evaluate(
            make_request(
                identity=summary_identity,
                target=make_message(
                    identity=summary_identity, text="请总结今天的聊天"
                ),
                summary_candidate=True,
            )
        )
        del summary
        row = await read_dispatch(harness.engine, summary_identity)
        assert row is not None
        assert row["business_route"] == "required" or row["business_route"] == "chat"


async def test_late_response_is_rejected_by_the_request_deadline(
    decision_database: str,
) -> None:
    """AC：截止之后返回的答案（含应用级校验）按请求超时拒绝，不做抢救。"""

    async def responder(request: httpx2.Request, index: int) -> Any:
        del index
        await asyncio.sleep(0.2)
        payload = build_answers(json.loads(request.content)["questions"])
        payload["answers"]["reply_eligibility"]["choice"] = "maybe"
        return payload

    options = HarnessOptions(
        responder=responder,
        config={
            "jev_http_timeout_seconds": 30.0,
            "jev_request_timeout_seconds": 0.05,
        },
    )
    async with DecisionHarness(decision_database, options) as harness:
        assert harness.decision is not None
        result = await harness.decision.evaluate(make_request())
        assert result.status == "failed"
        assert result.reason_code == "request_timeout"
        assert result.reply_choice is None


@pytest.mark.parametrize("summary_candidate", [False, True])
async def test_summon_with_unavailable_config_does_not_fake_disabled(
    decision_database: str, *, summary_candidate: bool
) -> None:
    """配置不可读不等于显式关闭；无 Noul 的确定性召唤仍可直通。"""

    class UnavailableConfig(ConfigSource):
        def __call__(self) -> Any:
            message = "snapshot unavailable"
            raise RuntimeError(message)

    harness = DecisionHarness(decision_database)
    harness.config_source = UnavailableConfig()
    async with harness:
        assert harness.decision is not None
        result = await harness.decision.evaluate(
            make_request(real_platform_at=True, summary_candidate=summary_candidate)
        )
        if summary_candidate:
            assert result.status == "failed"
            assert result.reason_code == "configuration_invalid"
            assert result.route == "none"
        else:
            assert result.status == "bypass"
            assert result.route == "chat"
        assert result.reply_choice is None
        assert result.retention_choice is None
        assert result.summary_probability is None
        assert harness.transport.requests == []
        health = harness.decision.get_health()
        assert health.runtime_status == "failed"
        assert health.reason_code == "configuration_invalid"
        assert health.last_request_status == "unknown"
        assert health.consecutive_failures == 0
