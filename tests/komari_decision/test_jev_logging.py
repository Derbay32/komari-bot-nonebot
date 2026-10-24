"""TSK-182 判定诊断日志验收：单次观测、SDK 捕获、脱敏与日志安全。

全部断言都穿过公开 seam：``evaluate`` / ``claim_handoff`` / ``close``、官方 SDK
的外部 transport，以及一次性隔离库上的**外部 SQL 观测**（直接读取
``komari_decision_logs`` 原始列）。业务期望值是手写字面量，不导入生产常量。
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

import httpx2
import pytest
from loguru import logger as loguru_logger
from typesafe_sdk import AsyncTypeSafeClient, Noul

from komari_bot.plugins.komari_decision.config_schema import (
    KomariDecisionConfigSchema,
)
from komari_bot.plugins.komari_decision.services.jev_request import (
    configured_jev_request,
)
from tests.komari_decision.jev_test_support import (
    CONFIGURED_API_KEY,
    CONFIGURED_BASE_URL,
    CONFIGURED_MODEL,
    PROMPT_TEXTS,
    DecisionHarness,
    HarnessOptions,
    build_answers,
    create_scratch_database,
    delete_prompt,
    drop_scratch_database,
    make_identity,
    make_message,
    make_request,
    read_dispatch,
    skip_without_services,
    wait_until,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

pytestmark = pytest.mark.asyncio

_SDK_LOGGER_NAME = "typesafe_sdk"
_ANSWER_SENTINEL = "tsk182-unknown-answer-sentinel-6f2a"
_TYPE_SENTINEL = "tsk182-unknown-type-sentinel-b91c"
_NOTICE_QUESTION = Noul(instructions="这条消息是不是在要求群聊总结？")


def request_config(**overrides: Any) -> KomariDecisionConfigSchema:
    """构造一份可用的判定配置（可按用例覆盖单个字段）。"""
    values: dict[str, Any] = {
        "jev_base_url": CONFIGURED_BASE_URL,
        "jev_api_key": CONFIGURED_API_KEY,
        "jev_model": CONFIGURED_MODEL,
        "jev_http_timeout_seconds": 3.0,
        "jev_request_timeout_seconds": 3.0,
    }
    values.update(overrides)
    return KomariDecisionConfigSchema(**values)


def unknown_answer_payload(response_factory: Any) -> Any:
    """构造带「未来 answer 类型」的响应体（名字与类型都带合成哨兵）。"""

    def handler(request: httpx2.Request) -> httpx2.Response:
        payload = response_factory(request)
        payload["answers"][_ANSWER_SENTINEL] = {"type": _TYPE_SENTINEL}
        return httpx2.Response(200, json=payload, request=request)

    return handler


def noul_payload(request: httpx2.Request) -> dict[str, Any]:
    """只回答请求里的 Noul 问题（合成响应体）。"""
    body = json.loads(request.content)
    answers = {
        name: {"type": "noul", "noul": 0.18}
        for name, question in body["questions"].items()
        if question["type"] == "noul"
    }
    return {
        "model": CONFIGURED_MODEL,
        "answers": answers,
        "usage": {"input_tokens": 31, "output_tokens": 1},
    }


# ------------------------------ SDK 普通日志安全 ------------------------------


async def test_unknown_answer_type_never_reaches_ordinary_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """AC：SDK 丢弃未来 answer 类型的 WARNING 不得把 answer 名字/类型写进普通日志。

    先直接调用官方 SDK 作为对照，证明捕获装置能发现该 WARNING；再经判定请求
    seam（真实 SDK + MockTransport）请求，哨兵不得出现在普通日志里。
    """
    sdk_logger = logging.getLogger(_SDK_LOGGER_NAME)
    previous_level = sdk_logger.level
    handler = unknown_answer_payload(noul_payload)
    try:
        # 复现 TYPESAFE_LOG_LEVEL=debug：SDK logger 被显式降到 DEBUG。
        sdk_logger.setLevel(logging.DEBUG)
        with caplog.at_level(logging.DEBUG):
            control = httpx2.MockTransport(handler)
            async with AsyncTypeSafeClient(
                api_key=CONFIGURED_API_KEY,
                base_url=CONFIGURED_BASE_URL,
                transport=control,
            ) as raw_client:
                await raw_client.system_one(
                    state={"document": "对照"}, questions={"q": _NOTICE_QUESTION}
                )
        assert _ANSWER_SENTINEL in caplog.text or _TYPE_SENTINEL in caplog.text

        caplog.clear()
        sdk_logger.setLevel(logging.DEBUG)
        with caplog.at_level(logging.DEBUG):
            transport = httpx2.MockTransport(handler)
            async with configured_jev_request(
                request_config(), transport=transport
            ) as client:
                answer = await client.system_one(
                    state={"document": "有人在群里问今天谁值班。"},
                    questions={"q": _NOTICE_QUESTION},
                )
        captured = caplog.text
        assert answer.nouls["q"].noul == pytest.approx(0.18)
    finally:
        sdk_logger.setLevel(previous_level)

    assert _ANSWER_SENTINEL not in captured
    assert _TYPE_SENTINEL not in captured


# ------------------------------ 夹具与外部观测 ------------------------------


@pytest.fixture(scope="module")
def decision_database() -> Iterator[str]:
    """派生一个迁移到 head 的一次性隔离库（模块级共享，用例内清表）。"""
    skip_without_services()
    url = asyncio.run(create_scratch_database(f"_jevdiag{uuid4().hex[:8]}"))
    try:
        yield url
    finally:
        asyncio.run(drop_scratch_database(url))


async def raw_log_row(engine: AsyncEngine, decision_id: str) -> dict[str, Any] | None:
    """外部 SQL 读取一条判定日志的原始行（不经过仓储投影）。"""
    from sqlalchemy import text

    async with engine.connect() as connection:
        row = (
            (
                await connection.execute(
                    text("SELECT * FROM komari_decision_logs WHERE id = :id"),
                    {"id": decision_id},
                )
            )
            .mappings()
            .first()
        )
    return None if row is None else dict(row)


async def raw_log_text(engine: AsyncEngine, decision_id: str) -> str:
    """整行 JSON 文本：证明任何列都没有残留原始凭据或媒体载荷。"""
    from sqlalchemy import text

    async with engine.connect() as connection:
        raw = await connection.scalar(
            text(
                "SELECT to_jsonb(komari_decision_logs)::text"
                " FROM komari_decision_logs WHERE id = :id"
            ),
            {"id": decision_id},
        )
    assert raw is not None, "外部 SQL 未读到判定日志行"
    return str(raw)


async def raw_log_ids(engine: AsyncEngine) -> list[str]:
    """按创建顺序返回日志 id（单次观测/不重复写入的观测面）。"""
    from sqlalchemy import text

    async with engine.connect() as connection:
        rows = (
            await connection.execute(
                text("SELECT id FROM komari_decision_logs ORDER BY created_at, id")
            )
        ).scalars()
    return [str(row) for row in rows]


@contextmanager
def capture_ordinary_logs() -> Iterator[list[str]]:
    """捕获判定插件自身的运行日志（``nonebot.logger`` 是 loguru，不走标准 logging）。"""
    messages: list[str] = []

    def sink(message: Any) -> None:
        messages.append(str(message))

    handler_id = loguru_logger.add(sink, level="DEBUG")
    try:
        yield messages
    finally:
        loguru_logger.remove(handler_id)


async def prompt_revision() -> int:
    """读取当前判定 Prompt 的实际修订（公开加载器，不猜字面量）。"""
    from komari_bot.config.prompt_storage import PromptTemplateLoader
    from komari_bot.plugins.komari_decision.prompt_schema import (
        DISPLAY_NAME,
        RESOURCE_ID,
    )

    loader = PromptTemplateLoader(
        resource_id=RESOURCE_ID,
        display_name=DISPLAY_NAME,
        log_prefix="[test-log]",
    )
    snapshot = await loader.get_versioned_snapshot_async()
    assert snapshot is not None
    return snapshot.revision


async def prompt_texts() -> dict[str, str]:
    """读取当前判定 Prompt 的正文（证明 Prompt 快照本身未被改写）。"""
    from komari_bot.config.prompt_storage import PromptTemplateLoader
    from komari_bot.plugins.komari_decision.prompt_schema import (
        DISPLAY_NAME,
        RESOURCE_ID,
    )

    loader = PromptTemplateLoader(
        resource_id=RESOURCE_ID,
        display_name=DISPLAY_NAME,
        log_prefix="[test-log]",
    )
    snapshot = await loader.get_versioned_snapshot_async()
    assert snapshot is not None
    return dict(snapshot.prompt_data)


def state_texts(body: Any) -> str:
    """把日志正文里的 state 渲染成文本（用于断言正文事实）。"""
    return json.dumps(body["state"], ensure_ascii=False, sort_keys=True)


class LogGate:
    """诊断日志写入的外部观测点：只观测/阻塞/失败 ``komari_decision_logs`` 的 INSERT。"""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.fail = False
        self.inserts = 0

    def wrap(self, factory: Callable[[], AsyncSession]) -> Callable[[], AsyncSession]:
        """包装会话工厂（真实会话，只在日志 INSERT 处插入观测点）。"""
        return lambda: cast("AsyncSession", _GatedSession(factory(), self))


class _GatedSession:
    def __init__(self, inner: AsyncSession, gate: LogGate) -> None:
        self._inner = inner
        self._gate = gate

    async def execute(self, statement: Any, *args: Any, **kwargs: Any) -> Any:
        if str(statement).startswith("INSERT INTO komari_decision_logs"):
            self._gate.inserts += 1
            self._gate.started.set()
            if self._gate.fail:
                msg = "synthetic diagnostic storage failure"
                raise RuntimeError(msg)
            await self._gate.release.wait()
        return await self._inner.execute(statement, *args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


# ------------------------------ 成功观测与真实 SDK 捕获 ------------------------------


async def test_success_observation_matches_actual_sdk_request(
    decision_database: str,
) -> None:
    """AC：成功观测的 state/questions 等于真实请求体；响应原始字段只在正文里。"""
    future_marker = "tsk182-future-extra-9c31"

    async def responder(request: httpx2.Request, index: int) -> dict[str, Any]:
        del index
        payload = build_answers(json.loads(request.content)["questions"])
        payload["future_top_level"] = future_marker
        payload["future_meta"] = {"note": future_marker}
        return payload

    async with DecisionHarness(
        decision_database, HarnessOptions(responder=responder)
    ) as harness:
        assert harness.decision is not None and harness.engine is not None
        identity = make_identity()
        text = "今天谁值班？"
        result = await harness.decision.evaluate(
            make_request(
                identity=identity, target=make_message(identity=identity, text=text)
            )
        )

        assert result.status == "success"
        assert result.reason_code == "classified"
        row = await raw_log_row(harness.engine, result.decision_id)
        assert row is not None
        body = row["body"]
        transmitted = harness.transport.payload(0)

        # 实际提交的 state 与问题集（含 role/task 指令）与真实请求一致。
        assert body["state"] == transmitted["state"]
        assert body["questions"] == transmitted["questions"]
        instructions = body["questions"]["reply_eligibility"]["instructions"]
        assert instructions["role"] == PROMPT_TEXTS["role_description"]
        assert instructions["task"] == PROMPT_TEXTS["reply_eligibility"]
        assert instructions["labels"] == ["required", "optional", "silent"]

        # 响应：真实状态码 + 完整原始 JSON（SDK 丢弃的未来字段只保留在正文）。
        assert body["response_status"] == 200
        assert body["response"]["future_top_level"] == future_marker
        assert body["response"]["model"] == CONFIGURED_MODEL

        # 元数据：没有正文/原始响应，只有事实字段。
        assert row["status"] == "success"
        assert row["reason_code"] == "classified"
        assert row["requested_model"] == CONFIGURED_MODEL
        assert row["actual_model"] == CONFIGURED_MODEL
        assert row["config_revision"] == 1
        assert row["prompt_revision"] == await prompt_revision()
        assert row["dispatch_row_id"] is not None
        details = json.dumps(row["details"], ensure_ascii=False)
        assert text not in details
        assert future_marker not in details
        assert row["details"]["usage"] == {"input_tokens": 128, "output_tokens": 9}
        assert row["details"]["durations"]["queue_ms"] is not None
        assert row["details"]["durations"]["request_ms"] is not None
        assert row["details"]["durations"]["references_ms"] is not None


async def test_credentials_never_reach_sdk_input_or_persisted_log(
    decision_database: str,
) -> None:
    """AC：state/Prompt 里的凭据既不到 SDK 输入，也不到已落库日志。"""
    state_secret = "tsk182-state-key-4d21"
    prompt_secret = "tsk182-prompt-key-7b83"
    configured_secret = "tsk182-configured-key-19ce"
    texts = dict(PROMPT_TEXTS)
    texts["role_description"] = f"判定角色：api_key={prompt_secret} 时照常判定。"

    options = HarnessOptions(
        prompt_texts=texts, config={"jev_api_key": configured_secret}
    )
    async with DecisionHarness(decision_database, options) as harness:
        assert harness.decision is not None and harness.engine is not None
        identity = make_identity()
        text = f"口令是 password={state_secret} 别外传"
        result = await harness.decision.evaluate(
            make_request(
                identity=identity, target=make_message(identity=identity, text=text)
            )
        )

        assert result.status == "success"
        # SDK 输入：真实请求体里没有任何原始凭据。
        transmitted = harness.transport.requests[0].content.decode("utf-8")
        for secret in (state_secret, prompt_secret, configured_secret):
            assert secret not in transmitted
        # 已落库日志：整行 JSON 里没有任何原始凭据，且掩码确实生效。
        stored = await raw_log_text(harness.engine, result.decision_id)
        for secret in (state_secret, prompt_secret, configured_secret):
            assert secret not in stored
        assert "[已移除凭据]" in stored
        # 普通文字仍然保留（脱敏不是整段丢弃）。
        stored_row = await raw_log_row(harness.engine, result.decision_id)
        assert stored_row is not None
        assert "别外传" in state_texts(stored_row["body"])
        # Prompt/请求快照未被改写。
        assert (await prompt_texts())["role_description"] == texts["role_description"]


async def test_media_payloads_never_reach_sdk_input_or_log(
    decision_database: str,
) -> None:
    """AC：显式媒体描述与内联 data URI 都不以原始载荷进入 SDK 或日志。"""
    payload = (
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8AAAwAB/AF+p1Gq"
    )
    data_uri = f"data:image/png;base64,{payload}"
    texts = dict(PROMPT_TEXTS)
    texts["reply_eligibility"] = data_uri

    options = HarnessOptions(prompt_texts=texts)
    async with DecisionHarness(decision_database, options) as harness:
        assert harness.decision is not None and harness.engine is not None
        identity = make_identity()
        # 内联媒体放在同群前文里（目标正文受 50 码点过滤限制，不适合塞长载荷）。
        await harness.decision.observe_delivered(
            make_message(
                identity=identity,
                message_id="m-context-media",
                text=f"前面提到 {data_uri} 的情况",
            ),
            protocol=identity.protocol,
            account_id=identity.account_id,
        )
        result = await harness.decision.evaluate(
            make_request(
                identity=identity,
                target=make_message(
                    identity=identity,
                    text="看图说看不懂",
                    image_present=True,
                    image_description=(
                        f"截图 https://cdn.example.invalid/private-shot.png "
                        f"以及 {data_uri} 都在这里"
                    ),
                ),
            )
        )

        assert result.status == "success"
        transmitted = harness.transport.requests[0].content.decode("utf-8")
        assert payload not in transmitted
        assert "private-shot.png" not in transmitted

        media_row = await raw_log_row(harness.engine, result.decision_id)
        assert media_row is not None
        body = media_row["body"]
        assert payload not in json.dumps(body, ensure_ascii=False)
        state = body["state"]["untrusted_group_conversation"]
        target = state["messages"][state["target"]]
        # 内联 data URI 被替换为固定占位符，周围普通文字保留。
        rendered = json.dumps(state["messages"], ensure_ascii=False)
        assert "前面提到" in rendered
        assert "的情况" in rendered
        assert "[已移除媒体内容]" in rendered
        # 图片事实保留：present 仍在，描述里的 URL 片段被替换。
        assert target["images"]["present"] is True
        assert "[已移除媒体内容]" in target["images"]["known_description"]
        assert "截图" in target["images"]["known_description"]
        # 整段媒体 Prompt 换成占位符，但问题本身仍被请求。
        question = body["questions"]["reply_eligibility"]
        assert question["instructions"]["task"] == "[已移除媒体内容]"
        assert question["instructions"]["labels"] == ["required", "optional", "silent"]
        # Prompt 快照未被改写。
        assert (await prompt_texts())["reply_eligibility"] == texts["reply_eligibility"]


# ------------------------------ 元数据专用路径 ------------------------------


async def test_filtered_bypass_and_duplicate_record_metadata_only(
    decision_database: str,
) -> None:
    """AC：过滤/直通/重投只记录元数据，正文必须为空且不覆盖已有正文。"""
    async with DecisionHarness(decision_database) as harness:
        assert harness.decision is not None and harness.engine is not None

        filtered_identity = make_identity()
        filtered = await harness.decision.evaluate(
            make_request(
                identity=filtered_identity,
                target=make_message(identity=filtered_identity, text="！？"),
            )
        )
        assert filtered.status == "filtered"
        filtered_row = await raw_log_row(harness.engine, filtered.decision_id)
        assert filtered_row is not None
        assert filtered_row["status"] == "filtered"
        assert filtered_row["body"] is None

        bypass_identity = make_identity()
        bypass = await harness.decision.evaluate(
            make_request(
                identity=bypass_identity,
                target=make_message(identity=bypass_identity, text="在吗"),
                real_platform_at=True,
            )
        )
        assert bypass.status == "bypass"
        bypass_row = await raw_log_row(harness.engine, bypass.decision_id)
        assert bypass_row is not None
        assert bypass_row["status"] == "bypass"
        assert bypass_row["body"] is None

        # 同一次分发内部复用：只有一个任务、一条日志。
        duplicate_identity = make_identity()
        request = make_request(
            identity=duplicate_identity,
            target=make_message(identity=duplicate_identity, text="请把结论整理一下"),
        )
        first = await harness.decision.evaluate(request)
        reused = await harness.decision.evaluate(request)
        assert reused.decision_id == first.decision_id
        first_row = await raw_log_row(harness.engine, first.decision_id)
        assert first_row is not None and first_row["body"] is not None
        original_body = first_row["body"]

        # 外部重投：另起一条元数据，且绝不改写原始正文。
        redelivered = await harness.decision.evaluate(
            make_request(
                identity=duplicate_identity,
                target=make_message(
                    identity=duplicate_identity, text="请把结论整理一下"
                ),
            )
        )
        assert redelivered.status == "duplicate"
        assert redelivered.reason_code == "duplicate_event"
        duplicate_row = await raw_log_row(harness.engine, redelivered.decision_id)
        assert duplicate_row is not None
        assert duplicate_row["status"] == "duplicate"
        assert duplicate_row["body"] is None
        reread = await raw_log_row(harness.engine, first.decision_id)
        assert reread is not None
        assert reread["body"] == original_body
        assert len(harness.transport.requests) == 1
        # 重投不抽签、不交接、不调用模型。
        assert harness.random_draws == 0
        assert (
            await harness.decision.claim_handoff(request, redelivered, "chat") is False
        )
        assert len(await raw_log_ids(harness.engine)) == 4


# ------------------------------ 准入与启动前失败 ------------------------------


async def test_entry_admission_denial_never_acquires_diagnostic_history(
    decision_database: str,
) -> None:
    """AC：入口即被准入拒绝的事件不取得任何可查询诊断历史。"""
    options = HarnessOptions(admission=lambda _identity: False)
    async with DecisionHarness(decision_database, options) as harness:
        assert harness.decision is not None and harness.engine is not None
        result = await harness.decision.evaluate(make_request())

        assert result.status == "cancelled"
        assert result.reason_code == "admission_revoked"
        assert harness.transport.requests == []
        assert await raw_log_ids(harness.engine) == []


async def test_storage_readiness_failure_records_metadata_by_current_admission(
    decision_database: str,
) -> None:
    """AC：入口路径尚未形成时，只按当前同步准入裁决决定元数据是否可查询。"""
    denied = {"value": False}
    options = HarnessOptions(
        initialize=False, admission=lambda _identity: not denied["value"]
    )
    async with DecisionHarness(decision_database, options) as harness:
        assert harness.decision is not None and harness.engine is not None

        denied["value"] = True
        refused = await harness.decision.evaluate(make_request())
        assert refused.status == "failed"
        assert refused.reason_code == "storage_unavailable"
        assert await raw_log_ids(harness.engine) == []

        denied["value"] = False
        admitted = await harness.decision.evaluate(make_request())
        assert admitted.reason_code == "storage_unavailable"
        row = await raw_log_row(harness.engine, admitted.decision_id)
        assert row is not None
        assert row["status"] == "failed"
        assert row["reason_code"] == "storage_unavailable"
        assert row["body"] is None
        assert harness.transport.requests == []


async def test_midflight_revocation_keeps_diagnostics_without_handoff(
    decision_database: str,
) -> None:
    """AC：中途撤权保留真实诊断，但没有额外 SDK 调用、没有交接。"""
    admitted = {"value": True}

    async def responder(request: httpx2.Request, index: int) -> dict[str, Any]:
        del index
        payload = build_answers(json.loads(request.content)["questions"])
        admitted["value"] = False
        return payload

    options = HarnessOptions(
        responder=responder, admission=lambda _identity: admitted["value"]
    )
    async with DecisionHarness(decision_database, options) as harness:
        assert harness.decision is not None and harness.engine is not None
        identity = make_identity()
        request = make_request(
            identity=identity, target=make_message(identity=identity, text="撤权测试")
        )
        result = await harness.decision.evaluate(request)

        assert result.status == "cancelled"
        assert result.reason_code == "admission_revoked"
        assert result.route == "none"
        row = await raw_log_row(harness.engine, result.decision_id)
        assert row is not None
        assert row["status"] == "cancelled"
        assert row["reason_code"] == "admission_revoked"
        assert row["details"]["route"] == "none"
        assert row["details"]["reply_choice"] is None
        # 真实观测到的请求与响应仍然保留在正文里。
        body = row["body"]
        assert body["state"] == harness.transport.state(0)
        assert body["response"]["answers"]["reply_eligibility"]["choice"] == "required"
        assert harness.decision.get_health().last_request_status == "success"
        assert harness.decision.get_health().last_success_at is not None
        assert len(harness.transport.requests) == 1
        handed = await harness.decision.claim_handoff(request, result, "chat")
        assert handed is False
        assert len(harness.transport.requests) == 1
        dispatch = await read_dispatch(harness.engine, identity)
        assert dispatch is not None
        assert dispatch["state"] == "cancelled"


# ------------------------------ 技术失败 ------------------------------


async def test_provider_error_response_is_preserved_in_sanitized_body(
    decision_database: str,
) -> None:
    """AC：SDK 协议错误把真实错误响应体保留在脱敏正文里。"""
    error_payload = {"error": "synthetic upstream unavailable", "code": "unavailable"}

    async def responder(request: httpx2.Request, index: int) -> httpx2.Response:
        del index
        return httpx2.Response(503, json=error_payload, request=request)

    async with DecisionHarness(
        decision_database, HarnessOptions(responder=responder)
    ) as harness:
        assert harness.decision is not None and harness.engine is not None
        result = await harness.decision.evaluate(make_request())

        assert result.status == "failed"
        assert result.reason_code == "provider_error"
        assert result.route == "none"
        row = await raw_log_row(harness.engine, result.decision_id)
        assert row is not None
        assert row["status"] == "failed"
        assert row["reason_code"] == "provider_error"
        assert row["details"]["reply_choice"] is None
        assert row["details"]["actual_model"] is None
        body = row["body"]
        assert body["response_status"] == 503
        assert body["response"] == error_payload
        assert body["state"] == harness.transport.state(0)
        assert row["details"]["durations"]["request_ms"] is not None


async def test_sdk_validation_failure_keeps_raw_body_without_model_facts(
    decision_database: str,
) -> None:
    """AC：SDK 层校验失败保留原始响应，但不从中摘取模型/用量事实。"""

    async def responder(request: httpx2.Request, index: int) -> dict[str, Any]:
        del index
        questions = json.loads(request.content)["questions"]
        payload = build_answers(questions)
        payload.pop("usage")
        return payload

    async with DecisionHarness(
        decision_database, HarnessOptions(responder=responder)
    ) as harness:
        assert harness.decision is not None and harness.engine is not None
        result = await harness.decision.evaluate(make_request())

        assert result.status == "failed"
        assert result.reason_code == "protocol_error"
        row = await raw_log_row(harness.engine, result.decision_id)
        assert row is not None
        assert row["body"]["response_status"] == 200
        assert row["body"]["response"]["answers"]["reply_eligibility"]["choice"] == (
            "required"
        )
        assert row["details"]["actual_model"] is None
        assert row["details"]["usage"] == {
            "input_tokens": None,
            "output_tokens": None,
        }
        assert row["details"]["reply_choice"] is None


async def test_application_protocol_failure_keeps_response_without_classification(
    decision_database: str,
) -> None:
    """AC：应用级答案校验失败保留真实响应与模型事实，但不伪造分类。"""

    async def responder(request: httpx2.Request, index: int) -> dict[str, Any]:
        del index
        questions = json.loads(request.content)["questions"]
        payload = build_answers(questions)
        payload["answers"]["reply_eligibility"]["probabilities"] = {
            "required": 1.0,
            "optional": 0.0,
        }
        return payload

    async with DecisionHarness(
        decision_database, HarnessOptions(responder=responder)
    ) as harness:
        assert harness.decision is not None and harness.engine is not None
        result = await harness.decision.evaluate(make_request())

        assert result.status == "failed"
        assert result.reason_code == "protocol_error"
        assert result.reply_choice is None
        assert result.route == "none"
        row = await raw_log_row(harness.engine, result.decision_id)
        assert row is not None
        assert row["body"]["response_status"] == 200
        assert row["body"]["response"]["model"] == CONFIGURED_MODEL
        assert row["details"]["actual_model"] == CONFIGURED_MODEL
        assert row["details"]["reply_choice"] is None
        assert row["details"]["usage"] == {"input_tokens": 128, "output_tokens": 9}


async def test_network_failure_records_no_invented_response(
    decision_database: str,
) -> None:
    """AC：无响应的网络失败只记录请求事实，响应字段保持为空。"""

    async def responder(request: httpx2.Request, index: int) -> Any:
        del index
        msg = "synthetic connect failure"
        raise httpx2.ConnectError(msg, request=request)

    async with DecisionHarness(
        decision_database, HarnessOptions(responder=responder)
    ) as harness:
        assert harness.decision is not None and harness.engine is not None
        result = await harness.decision.evaluate(make_request())

        assert result.status == "failed"
        assert result.reason_code == "network_error"
        row = await raw_log_row(harness.engine, result.decision_id)
        assert row is not None
        body = row["body"]
        assert body["state"] == harness.transport.state(0)
        assert body["response"] is None
        assert body["response_status"] is None
        assert row["details"]["actual_model"] is None
        assert row["details"]["durations"]["request_ms"] is not None


async def test_request_deadline_records_no_response(decision_database: str) -> None:
    """AC：请求总截止中止在途请求时，正文只保留请求事实。"""

    async def responder(request: httpx2.Request, index: int) -> dict[str, Any]:
        del index
        await asyncio.sleep(1.0)
        return build_answers(json.loads(request.content)["questions"])

    options = HarnessOptions(
        responder=responder,
        config={"jev_request_timeout_seconds": 0.05, "jev_http_timeout_seconds": 2.0},
    )
    async with DecisionHarness(decision_database, options) as harness:
        assert harness.decision is not None and harness.engine is not None
        result = await harness.decision.evaluate(make_request())

        assert result.status == "failed"
        assert result.reason_code == "request_timeout"
        row = await raw_log_row(harness.engine, result.decision_id)
        assert row is not None
        assert row["body"]["response"] is None
        assert row["body"]["response_status"] is None
        assert row["details"]["durations"]["request_ms"] is not None


async def test_configuration_and_prompt_failures_record_metadata_without_body(
    decision_database: str,
) -> None:
    """AC：配置与 Prompt 失败记录元数据，但没有正文也没有 SDK 调用。"""
    options = HarnessOptions(config={"jev_api_key": ""})
    async with DecisionHarness(decision_database, options) as harness:
        assert harness.decision is not None and harness.engine is not None
        result = await harness.decision.evaluate(make_request())

        assert result.status == "failed"
        assert result.reason_code == "configuration_invalid"
        row = await raw_log_row(harness.engine, result.decision_id)
        assert row is not None
        assert row["reason_code"] == "configuration_invalid"
        assert row["body"] is None
        assert harness.transport.requests == []
        assert row["details"]["requested_model"] == CONFIGURED_MODEL
        assert row["details"]["actual_model"] is None

    # Prompt 缺失：同样只有元数据（Prompt 行在进入前显式清除）。
    await delete_prompt()
    options = HarnessOptions(write_prompt_row=False)
    async with DecisionHarness(decision_database, options) as harness:
        assert harness.decision is not None and harness.engine is not None
        result = await harness.decision.evaluate(make_request())

        assert result.status == "failed"
        assert result.reason_code == "prompt_unavailable"
        row = await raw_log_row(harness.engine, result.decision_id)
        assert row is not None
        assert row["body"] is None
        assert harness.transport.requests == []
        assert row["details"]["prompt_revision"] is None


# ------------------------------ 队列拒绝 ------------------------------


async def test_queue_rejection_and_timeout_record_metadata_without_body(
    decision_database: str,
) -> None:
    """AC：队列满/排队超时只记录元数据，不补取引用也不调用模型。"""
    gate = asyncio.Event()

    async def responder(request: httpx2.Request, index: int) -> dict[str, Any]:
        del index
        await gate.wait()
        return build_answers(json.loads(request.content)["questions"])

    options = HarnessOptions(
        responder=responder,
        config={
            "max_concurrent_decisions": 1,
            "max_queued_messages_per_group": 1,
            "queue_timeout_seconds": 0.2,
        },
    )
    async with DecisionHarness(decision_database, options) as harness:
        assert harness.decision is not None and harness.engine is not None
        account_id = f"acct-{uuid4().hex[:8]}"
        group_id = f"group-{uuid4().hex[:8]}"

        def request_for(message_id: str) -> Any:
            identity = make_identity(
                account_id=account_id, group_id=group_id, message_id=message_id
            )
            return make_request(
                identity=identity,
                target=make_message(identity=identity, text=f"消息-{message_id}"),
            )

        running = asyncio.create_task(harness.decision.evaluate(request_for("q1")))
        await wait_until(lambda: bool(harness.transport.requests))
        waiting = asyncio.create_task(harness.decision.evaluate(request_for("q2")))
        await asyncio.sleep(0.05)
        full = await harness.decision.evaluate(request_for("q3"))
        assert full.status == "failed"
        assert full.reason_code == "queue_full"
        # 排队的 q2 在队列截止后失败，而不是拿到名额。
        expired = await waiting
        assert expired.status == "failed"
        assert expired.reason_code == "queue_timeout"

        gate.set()
        assert (await running).status == "success"

        for result, reason in ((full, "queue_full"), (expired, "queue_timeout")):
            row = await raw_log_row(harness.engine, result.decision_id)
            assert row is not None
            assert row["status"] == "failed"
            assert row["reason_code"] == reason
            assert row["body"] is None
            assert row["details"]["actual_model"] is None
            assert row["details"]["reply_choice"] is None
        assert len(harness.transport.requests) == 1


# ------------------------------ 日志写入失败与隔离 ------------------------------


async def test_log_storage_failure_keeps_business_result_and_lottery(
    decision_database: str,
) -> None:
    """AC：日志写入失败不改变结果/抽签/调用次数/交接，只留安全诊断。"""
    gate = LogGate()
    gate.fail = True
    gate.release.set()
    text = "可选回应抽签今天谁值班"

    async def responder(request: httpx2.Request, index: int) -> dict[str, Any]:
        del index
        return build_answers(json.loads(request.content)["questions"], reply="optional")

    options = HarnessOptions(
        responder=responder, session_factory_wrapper=gate.wrap, random_values=[0.1]
    )
    async with DecisionHarness(decision_database, options) as harness:
        assert harness.decision is not None and harness.engine is not None
        identity = make_identity()
        request = make_request(
            identity=identity, target=make_message(identity=identity, text=text)
        )
        with capture_ordinary_logs() as logged:
            result = await harness.decision.evaluate(request)
            handed = await harness.decision.claim_handoff(request, result, "chat")
        ordinary = "\n".join(logged)

        assert result.status == "success"
        assert result.reason_code == "optional_selected"
        assert result.route == "chat"
        assert result.optional_selected is True
        assert handed is True
        assert harness.random_draws == 1
        assert len(harness.transport.requests) == 1
        assert gate.inserts == 1
        assert await raw_log_ids(harness.engine) == []
        assert "诊断日志写入失败" in ordinary
        assert "DecisionLogStorageUnavailable" in ordinary
        for leaked in (text, CONFIGURED_API_KEY, "synthetic diagnostic"):
            assert leaked not in ordinary


async def test_concurrent_groups_keep_isolated_observations(
    decision_database: str,
) -> None:
    """AC：并发群的状态/请求/响应观测互不串台。"""
    async with DecisionHarness(decision_database) as harness:
        assert harness.decision is not None and harness.engine is not None
        first_identity = make_identity()
        second_identity = make_identity()
        first_text = "并发群一今天谁值班"
        second_text = "并发群二今天吃什么"

        first, second = await asyncio.gather(
            harness.decision.evaluate(
                make_request(
                    identity=first_identity,
                    target=make_message(identity=first_identity, text=first_text),
                )
            ),
            harness.decision.evaluate(
                make_request(
                    identity=second_identity,
                    target=make_message(identity=second_identity, text=second_text),
                )
            ),
        )

        assert first.status == "success"
        assert second.status == "success"
        first_row = await raw_log_row(harness.engine, first.decision_id)
        second_row = await raw_log_row(harness.engine, second.decision_id)
        assert first_row is not None and second_row is not None
        first_body = first_row["body"]
        second_body = second_row["body"]
        first_rendered = state_texts(first_body)
        second_rendered = state_texts(second_body)
        assert first_text in first_rendered
        assert second_text not in first_rendered
        assert second_text in second_rendered
        assert first_text not in second_rendered
        assert first_body["state"]["untrusted_group_conversation"]["messages"]
        for body, identity in (
            (first_body, first_identity),
            (second_body, second_identity),
        ):
            for item in body["state"]["untrusted_group_conversation"][
                "messages"
            ].values():
                assert item["speaker"].startswith("speaker-")
                assert identity.group_id not in json.dumps(item, ensure_ascii=False)


async def test_cancelled_log_write_finishes_before_close_returns(
    decision_database: str,
) -> None:
    """AC：取消发生在日志写入期间时写完该次观测再传播；close 等待诊断完成。"""
    gate = LogGate()
    options = HarnessOptions(session_factory_wrapper=gate.wrap)
    async with DecisionHarness(decision_database, options) as harness:
        assert harness.decision is not None and harness.engine is not None
        identity = make_identity()
        pending = asyncio.create_task(
            harness.decision.evaluate(
                make_request(
                    identity=identity,
                    target=make_message(identity=identity, text="写入期间取消"),
                )
            )
        )
        closing: asyncio.Task[None] | None = None
        try:
            await asyncio.wait_for(gate.started.wait(), timeout=10)
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
            # 写入仍被阻塞：close 不得在诊断完成之前返回。
            closing = asyncio.create_task(harness.decision.close())
            await asyncio.sleep(0.1)
            assert not closing.done()
        finally:
            gate.release.set()
            tasks = [closing] if closing is not None else []
            if tasks:
                await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=True), timeout=10
                )

        assert gate.inserts == 1
        ids = await raw_log_ids(harness.engine)
        assert len(ids) == 1
        row = await raw_log_row(harness.engine, ids[0])
        assert row is not None
        assert row["status"] == "success"
        assert "写入期间取消" in state_texts(row["body"])
        orphans = [
            task
            for task in asyncio.all_tasks()
            if task is not asyncio.current_task() and not task.done()
        ]
        assert orphans == []


async def test_repeated_cancellation_during_model_cleanup_leaves_one_observation(
    decision_database: str,
) -> None:
    """AC：模型清理期间的重复取消只留下一条取消观测，且没有遗留任务。"""
    request_started = asyncio.Event()
    release = asyncio.Event()

    async def responder(request: httpx2.Request, index: int) -> dict[str, Any]:
        del index
        request_started.set()
        await release.wait()
        return build_answers(json.loads(request.content)["questions"])

    async with DecisionHarness(
        decision_database, HarnessOptions(responder=responder)
    ) as harness:
        assert harness.decision is not None and harness.engine is not None
        identity = make_identity()
        pending = asyncio.create_task(
            harness.decision.evaluate(
                make_request(
                    identity=identity,
                    target=make_message(identity=identity, text="清理期间重复取消"),
                )
            )
        )
        await asyncio.wait_for(request_started.wait(), timeout=10)
        pending.cancel()
        await asyncio.sleep(0)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        close_task = asyncio.create_task(harness.decision.close())
        try:
            await asyncio.wait_for(
                asyncio.gather(close_task, return_exceptions=True), 10
            )
        finally:
            release.set()

        ids = await raw_log_ids(harness.engine)
        assert len(ids) == 1
        row = await raw_log_row(harness.engine, ids[0])
        assert row is not None
        assert row["status"] == "cancelled"
        assert row["reason_code"] == "interrupted"
        assert row["details"]["route"] == "none"
        assert row["details"]["reply_choice"] is None
        body = row["body"]
        assert body["state"] == harness.transport.state(0)
        assert body["response"] is None
        assert body["response_status"] is None
        dispatch = await read_dispatch(harness.engine, identity)
        assert dispatch is not None
        assert dispatch["state"] == "cancelled"
        assert row["details"]["durations"]["request_ms"] is not None
        assert row["details"]["durations"]["request_ms"] > 0
        # 容量语义不变：取消后不残留执行名额。
        assert dispatch["slot_held"] is False
        orphans = [
            task
            for task in asyncio.all_tasks()
            if task is not asyncio.current_task() and not task.done()
        ]
        assert orphans == []


async def test_config_key_and_prompt_revisions_freeze_per_task(
    decision_database: str,
) -> None:
    """AC：配置/凭据/Prompt 修订按任务冻结；后续任务看到新值。"""
    old_key = "tsk182-frozen-key-old-11aa"
    new_key = "tsk182-frozen-key-new-22bb"
    new_task_text = "新一轮 Prompt：先判断是否需要引用处。"
    gate = asyncio.Event()

    async def responder(request: httpx2.Request, index: int) -> dict[str, Any]:
        del index
        await gate.wait()
        return build_answers(json.loads(request.content)["questions"])

    options = HarnessOptions(responder=responder, config={"jev_api_key": old_key})
    async with DecisionHarness(decision_database, options) as harness:
        assert harness.decision is not None and harness.engine is not None
        first_revision = await prompt_revision()
        old_identity = make_identity()
        pending = asyncio.create_task(
            harness.decision.evaluate(
                make_request(
                    identity=old_identity,
                    target=make_message(
                        identity=old_identity, text=f"旧凭据 {old_key} 出现在这里"
                    ),
                )
            )
        )
        await wait_until(lambda: bool(harness.transport.requests))

        # 在旧任务仍在途时更换配置与 Prompt 修订。
        harness.set_config({"jev_api_key": new_key})
        from komari_bot.config.prompt_storage import get_prompt_storage
        from komari_bot.plugins.komari_decision.prompt_schema import RESOURCE_ID

        updated = await get_prompt_storage().replace_if_revision_async(
            resource_id=RESOURCE_ID,
            prompt_data={**PROMPT_TEXTS, "reply_eligibility": new_task_text},
            expected_revision=first_revision,
        )
        assert updated is not None
        second_revision = updated.revision
        assert second_revision != first_revision

        gate.set()
        first = await pending
        assert first.status == "success"
        first_row = await raw_log_row(harness.engine, first.decision_id)
        assert first_row is not None
        assert first_row["config_revision"] == 1
        assert first_row["prompt_revision"] == first_revision
        first_stored = await raw_log_text(harness.engine, first.decision_id)
        assert old_key not in first_stored
        assert "[已移除凭据]" in first_stored
        first_questions = first_row["body"]["questions"]
        assert (
            first_questions["reply_eligibility"]["instructions"]["task"]
            == (PROMPT_TEXTS["reply_eligibility"])
        )

        new_identity = make_identity()
        second = await harness.decision.evaluate(
            make_request(
                identity=new_identity,
                target=make_message(
                    identity=new_identity, text=f"新凭据 {new_key} 出现在这里"
                ),
            )
        )
        assert second.status == "success"
        second_row = await raw_log_row(harness.engine, second.decision_id)
        assert second_row is not None
        assert second_row["config_revision"] == 2
        assert second_row["prompt_revision"] == second_revision
        second_stored = await raw_log_text(harness.engine, second.decision_id)
        assert new_key not in second_stored
        assert old_key not in second_stored
        assert "[已移除凭据]" in second_stored
        second_questions = second_row["body"]["questions"]
        assert second_questions["reply_eligibility"]["instructions"]["task"] == (
            new_task_text
        )


# ------------------------------ 其他边界 ------------------------------


async def test_platform_summary_observation_keeps_noul_question_shape(
    decision_database: str,
) -> None:
    """AC：只请求 Noul 的平台召唤也把实际问题形状（含 criteria）写进正文。"""

    async def responder(request: httpx2.Request, index: int) -> dict[str, Any]:
        del index
        return build_answers(json.loads(request.content)["questions"], summary=0.9)

    async with DecisionHarness(
        decision_database, HarnessOptions(responder=responder)
    ) as harness:
        assert harness.decision is not None and harness.engine is not None
        result = await harness.decision.evaluate(
            make_request(real_platform_at=True, summary_candidate=True)
        )

        assert result.status == "success"
        assert result.route == "summary"
        row = await raw_log_row(harness.engine, result.decision_id)
        assert row is not None
        body = row["body"]
        assert body["questions"] == harness.transport.payload(0)["questions"]
        summary_question = body["questions"]["summary_request"]
        assert summary_question["type"] == "noul"
        assert (
            summary_question["instructions"]["task"]
            == (PROMPT_TEXTS["summary_recognition"])
        )
        assert set(summary_question["criteria"]) == {"true", "false"}
        assert row["details"]["summary_probability"] == 0.9
        assert row["details"]["reply_choice"] is None


async def test_unexpected_failure_records_internal_error_metadata(
    decision_database: str,
) -> None:
    """AC：未预期失败仍到达单次观测边界，记录内部错误而不伪造分类。"""
    calls = {"count": 0}

    def flaky_admission(_identity: Any) -> bool:
        calls["count"] += 1
        if calls["count"] == 1:
            msg = "synthetic admission dependency failure"
            raise RuntimeError(msg)
        return True

    options = HarnessOptions(admission=flaky_admission)
    async with DecisionHarness(decision_database, options) as harness:
        assert harness.decision is not None and harness.engine is not None
        result = await harness.decision.evaluate(make_request())

        assert result.status == "failed"
        assert result.reason_code == "internal_error"
        assert result.route == "none"
        ids = await raw_log_ids(harness.engine)
        assert len(ids) == 1
        row = await raw_log_row(harness.engine, ids[0])
        assert row is not None
        assert row["status"] == "failed"
        assert row["reason_code"] == "internal_error"
        assert row["details"]["reply_choice"] is None
        assert row["details"]["actual_model"] is None
        assert row["body"] is None
        assert harness.transport.requests == []


async def test_engine_path_unknown_answer_keeps_body_but_not_ordinary_logs(
    decision_database: str, caplog: pytest.LogCaptureFixture
) -> None:
    """AC：SDK 丢弃的未来 answer 类型只留在受权正文里，不进任何普通日志。"""
    answer_sentinel = "tsk182-engine-answer-sentinel-57ad"
    type_sentinel = "tsk182-engine-type-sentinel-8e02"

    async def responder(request: httpx2.Request, index: int) -> dict[str, Any]:
        del index
        payload = build_answers(json.loads(request.content)["questions"])
        payload["answers"][answer_sentinel] = {"type": type_sentinel}
        return payload

    async with DecisionHarness(
        decision_database, HarnessOptions(responder=responder)
    ) as harness:
        assert harness.decision is not None and harness.engine is not None
        with caplog.at_level(logging.DEBUG), capture_ordinary_logs() as logged:
            result = await harness.decision.evaluate(make_request())
        ordinary = "\n".join(logged) + "\n" + caplog.text

        assert result.status == "success"
        row = await raw_log_row(harness.engine, result.decision_id)
        assert row is not None
        # 完整原始响应仍可通过受权正文取得（含 SDK 丢弃的那条 answer）。
        assert row["body"]["response"]["answers"][answer_sentinel] == {
            "type": type_sentinel
        }
        # 普通日志里既没有 answer 名字也没有类型。
        assert answer_sentinel not in ordinary
        assert type_sentinel not in ordinary


async def test_unreported_usage_stays_null_in_metadata(decision_database: str) -> None:
    """AC：供应方未报告用量时元数据保持空值，不补默认值。"""

    async def responder(request: httpx2.Request, index: int) -> dict[str, Any]:
        del index
        questions = json.loads(request.content)["questions"]
        payload = build_answers(questions)
        payload["usage"] = {}
        return payload

    async with DecisionHarness(
        decision_database, HarnessOptions(responder=responder)
    ) as harness:
        assert harness.decision is not None and harness.engine is not None
        result = await harness.decision.evaluate(make_request())

        assert result.status == "success"
        assert result.usage.input_tokens is None
        assert result.usage.output_tokens is None
        row = await raw_log_row(harness.engine, result.decision_id)
        assert row is not None
        assert row["details"]["usage"] == {
            "input_tokens": None,
            "output_tokens": None,
        }
        assert row["details"]["actual_model"] == CONFIGURED_MODEL


async def test_cancellation_during_reference_resolution_marks_incomplete(
    decision_database: str,
) -> None:
    """AC：引用补取期间取消时记录「引用不完整」，而不是声称链接已完成。"""
    resolution_started = asyncio.Event()
    release = asyncio.Event()

    async def reference_reader(identity: Any, message_id: str) -> Any:
        del identity, message_id
        resolution_started.set()
        await release.wait()
        return None

    async with DecisionHarness(
        decision_database, HarnessOptions(reference_reader=reference_reader)
    ) as harness:
        assert harness.decision is not None and harness.engine is not None
        identity = make_identity()
        pending = asyncio.create_task(
            harness.decision.evaluate(
                make_request(
                    identity=identity,
                    target=make_message(
                        identity=identity,
                        text="请看这条引用",
                        reference_ids=("ref-quoted-1",),
                    ),
                )
            )
        )
        close_task: asyncio.Task[None] | None = None
        try:
            await asyncio.wait_for(resolution_started.wait(), timeout=10)
            close_task = asyncio.create_task(harness.decision.close())
            await asyncio.wait_for(
                asyncio.gather(pending, close_task, return_exceptions=True), 10
            )
        finally:
            release.set()

        ids = await raw_log_ids(harness.engine)
        assert len(ids) == 1
        row = await raw_log_row(harness.engine, ids[0])
        assert row is not None
        assert row["status"] == "cancelled"
        assert row["reason_code"] == "interrupted"
        assert row["details"]["references"]["complete"] is False
        assert row["details"]["durations"]["references_ms"] is not None
        assert row["details"]["durations"]["references_ms"] > 0
        assert row["details"]["durations"]["request_ms"] is None
        # 没有发起 SDK 请求：正文保持为空。
        assert row["body"] is None
        assert harness.transport.requests == []


async def test_cancel_finalization_fault_still_records_and_recovers_ownership(
    decision_database: str,
) -> None:
    """取消收尾写库失败仍留下诊断，并在恢复所有权后释放旧名额。"""
    admitted = True
    armed = False
    failed_writes = 0
    started = asyncio.Event()
    release = asyncio.Event()

    class SessionProbe:
        def __init__(self, inner: AsyncSession) -> None:
            self.inner = inner

        async def execute(self, statement: Any, *args: Any, **kwargs: Any) -> Any:
            nonlocal armed, failed_writes
            if armed and str(statement).startswith(
                "UPDATE komari_decision_dispatches SET"
            ):
                parameters = statement.compile().params
                if (
                    parameters.get("reason_code") == "interrupted"
                    and parameters.get("slot_held") is False
                ):
                    armed = False
                    failed_writes += 1
                    message = "synthetic transient finalization fault"
                    raise RuntimeError(message)
            return await self.inner.execute(statement, *args, **kwargs)

        def __getattr__(self, name: str) -> Any:
            return getattr(self.inner, name)

    def wrap(factory: Callable[[], AsyncSession]) -> Callable[[], AsyncSession]:
        return lambda: cast("AsyncSession", SessionProbe(factory()))

    async def responder(request: httpx2.Request, index: int) -> dict[str, Any]:
        del index
        started.set()
        await release.wait()
        return build_answers(json.loads(request.content)["questions"])

    options = HarnessOptions(
        responder=responder,
        admission=lambda _identity: admitted,
        session_factory_wrapper=wrap,
        config={"max_concurrent_decisions": 1},
    )
    async with DecisionHarness(decision_database, options) as harness:
        assert harness.decision is not None and harness.engine is not None
        identity = make_identity()
        request = make_request(identity=identity)
        pending = asyncio.create_task(harness.decision.evaluate(request))
        try:
            await asyncio.wait_for(started.wait(), timeout=5)
            armed = True
            admitted = False
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(pending, timeout=10)
            assert failed_writes == 1
            ids = await raw_log_ids(harness.engine)
            assert len(ids) == 1
            row = await raw_log_row(harness.engine, ids[0])
            assert row is not None
            assert row["status"] == "cancelled"
            assert row["details"]["route"] == "none"
            assert harness.decision.get_health().runtime_status == "failed"
            assert harness.decision.get_health().reason_code == "storage_unavailable"
            admitted = True
            release.set()
            await harness.decision.refresh()
            original = await read_dispatch(harness.engine, identity)
            assert original is not None and original["slot_held"] is False
            assert original["reason_code"] == "admission_revoked"
            assert (await harness.decision.evaluate(make_request())).status == "success"
            assert (
                await harness.decision.evaluate(make_request(identity=identity))
            ).status == "duplicate"
            assert len(harness.transport.requests) == 2
        finally:
            release.set()
            await asyncio.gather(pending, return_exceptions=True)


async def test_dead_owner_cancelled_slot_is_reclaimed_without_rewriting_revocation(
    decision_database: str,
) -> None:
    """死所有者遗留的已取消名额会回收，撤权事实仍保持不变。"""
    from sqlalchemy import text

    options = HarnessOptions(config={"max_concurrent_decisions": 1})
    async with DecisionHarness(decision_database, options) as harness:
        assert harness.decision is not None and harness.engine is not None
        identity = make_identity()
        result = await harness.decision.evaluate(
            make_request(identity=identity, real_platform_at=True)
        )
        assert result.status == "bypass"
        async with harness.engine.begin() as connection:
            orphan_id = await connection.scalar(
                text(
                    "INSERT INTO komari_decision_processes DEFAULT VALUES RETURNING id"
                )
            )
            await connection.execute(
                text(
                    "UPDATE komari_decision_dispatches SET owner_process_id=:owner, "
                    "state='cancelled', reason_code='admission_revoked', slot_held=true, "
                    "business_route='none' WHERE decision_id=:decision"
                ),
                {"owner": orphan_id, "decision": result.decision_id},
            )
        # 新所有者没有会话锁：这是外部可验证的死所有者，不用年龄推测。
        following = await harness.decision.evaluate(make_request())
        assert following.status == "success"
        old = await read_dispatch(harness.engine, identity)
        assert old is not None
        assert old["slot_held"] is False
        assert old["reason_code"] == "admission_revoked"
        assert len(harness.transport.requests) == 1
