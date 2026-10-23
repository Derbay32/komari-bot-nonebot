"""TSK-181 验收 —— 配置到官方 TypeSafe SDK 的请求 seam。

经官方 SDK 支持的外部 transport seam（``AsyncTypeSafeClient(transport=...)``）
与 ``configured_jev_request`` 请求上下文验证：

- 请求只采纳统一配置里的地址、模型与凭据，SDK 环境变量不构成第二个来源；
- 同一次请求同时取得 Choice 与 Noul 的类型化答案；
- 畸形地址/凭据在配置写入边界被拒绝（schema 校验不回显输入），空值可保存
  但在创建 SDK 客户端前显式失败；
- 网络故障、429、5xx、认证与协议失败按 SDK 异常原样传播，不发生自动重试；
- 请求总截止由 ``asyncio.timeout_at`` 主动取消在途请求，覆盖连接、发送、
  接收与校验；抑制取消或同步迟到的结果同样不被接受；
- 取消与并发场景下每个上下文独立释放自己的客户端；
- SDK 请求/响应正文即使开启 debug 也不会进入普通运行日志。
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
from typing import TYPE_CHECKING, Any

import httpx2
import pytest
from sqlalchemy.orm import configure_mappers
from typesafe_sdk import (
    AsyncTypeSafeClient,
    Choice,
    Noul,
    TypeSafeAPIConnectionError,
    TypeSafeAPIResponseValidationError,
    TypeSafeAPITimeoutError,
    TypeSafeAuthenticationError,
    TypeSafeInternalServerError,
    TypeSafeRateLimitError,
)

from komari_bot.plugins.komari_decision.config_schema import (
    KomariDecisionConfigSchema,
)
from komari_bot.plugins.komari_decision.services.jev_request import (
    JevRequestConfigurationError,
    JevRequestTimeoutError,
    configured_jev_request,
)

if TYPE_CHECKING:
    from collections.abc import Callable

DUMMY_API_KEY = "tsk-181-dummy-key-0002"
CONFIGURED_BASE_URL = "https://jev-gateway.example.invalid/decision"
CONFIGURED_MODEL = "jev-1.14.0"
STATE_CANARY = "tsk181-state-canary-2f8e"
OUTPUT_CANARY = "tsk181-output-canary-91bd"

CHOICE_QUESTION = Choice(
    instructions="机器人是否应该在这里回应？",
    criteria={"reply": None, "silent": None},
)
NOUL_QUESTION = Noul(instructions="这条消息是不是在要求群聊总结？")


def state_payload(canary: str = STATE_CANARY) -> dict[str, Any]:
    """合成一份判定 state（含固定 canary 以便验证日志不落正文）。"""
    return {"document": f"[{canary}] 有人在群里问今天谁值班。"}


def system_one_payload(
    request: httpx2.Request,
    *,
    choice: str = "reply",
    noul: float = 0.18,
) -> dict[str, Any]:
    """构造官方 System One 响应体，覆盖请求中的每个问题。"""
    body = json.loads(request.content)
    question_names = list(body["questions"])
    answers: dict[str, Any] = {}
    for name in question_names:
        kind = body["questions"][name]["type"]
        match kind:
            case "choice":
                criteria = list(body["questions"][name]["criteria"])
                answers[name] = {
                    "type": "choice",
                    "choice": choice if choice in criteria else criteria[0],
                    "confidence": 0.82,
                    "probabilities": dict.fromkeys(criteria, 1 / len(criteria)),
                }
            case "noul":
                answers[name] = {"type": "noul", "noul": noul}
            case _:
                answers[name] = {"type": kind}
    return {
        "model": CONFIGURED_MODEL,
        "answers": answers,
        "usage": {"input_tokens": 41, "output_tokens": 2},
        "canary": OUTPUT_CANARY,
    }


class ScriptedTypesafeTransport(httpx2.AsyncBaseTransport):
    """SDK 支持的外部 transport 替身：记录请求并回放脚本化响应。"""

    def __init__(self, handler: Callable[[httpx2.Request], Any]) -> None:
        self._handler = handler
        self.requests: list[httpx2.Request] = []
        self.closed = False

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        produced = self._handler(request)
        if inspect.isawaitable(produced):
            produced = await produced
        if isinstance(produced, httpx2.Response):
            return produced
        return httpx2.Response(200, json=produced, request=request)

    async def aclose(self) -> None:
        self.closed = True

    def request_json(self, index: int = 0) -> dict[str, Any]:
        return json.loads(self.requests[index].content)


def config_for_request(**overrides: Any) -> KomariDecisionConfigSchema:
    """构造一份可用的判定配置（可按用例覆盖单个字段）。"""
    values: dict[str, Any] = {
        "jev_base_url": CONFIGURED_BASE_URL,
        "jev_api_key": DUMMY_API_KEY,
        "jev_model": CONFIGURED_MODEL,
        "jev_http_timeout_seconds": 3.0,
        "jev_request_timeout_seconds": 3.0,
    }
    values.update(overrides)
    return KomariDecisionConfigSchema(**values)


@pytest.mark.asyncio
async def test_request_uses_configured_address_model_and_key() -> None:
    """AC：SDK 请求采纳保存的地址、模型与凭据，并同时取得 Choice 与 Noul。"""
    transport = ScriptedTypesafeTransport(system_one_payload)

    async with configured_jev_request(
        config_for_request(), transport=transport
    ) as client:
        answer = await client.system_one(
            state=state_payload(),
            questions={"chat": CHOICE_QUESTION, "summary": NOUL_QUESTION},
        )

    assert len(transport.requests) == 1
    sent = transport.requests[0]
    assert str(sent.url) == f"{CONFIGURED_BASE_URL}/v1/systemone"
    assert sent.headers["authorization"] == f"Bearer {DUMMY_API_KEY}"
    sent_body = transport.request_json()
    assert sent_body["model"] == CONFIGURED_MODEL
    assert sent_body["state"] == state_payload()
    assert sorted(sent_body["questions"]) == ["chat", "summary"]
    assert answer.model == CONFIGURED_MODEL
    assert answer.choices["chat"].choice == "reply"
    assert answer.choices["chat"].confidence == pytest.approx(0.82)
    assert answer.nouls["summary"].noul == pytest.approx(0.18)
    assert transport.closed is True


@pytest.mark.asyncio
async def test_explicit_config_wins_over_sdk_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC：显式统一配置优先，TYPESAFE_* 环境变量不构成第二个配置来源。"""
    monkeypatch.setenv("TYPESAFE_API_KEY", "env-ignored-key")
    monkeypatch.setenv("TYPESAFE_BASE_URL", "https://env-ignored.invalid")
    monkeypatch.setenv("TYPESAFE_DEFAULT_MODEL", "jev-env-ignored")
    transport = ScriptedTypesafeTransport(system_one_payload)

    async with configured_jev_request(
        config_for_request(), transport=transport
    ) as client:
        await client.system_one(
            state=state_payload(),
            questions={"summary": NOUL_QUESTION},
        )

    sent = transport.requests[0]
    assert str(sent.url) == f"{CONFIGURED_BASE_URL}/v1/systemone"
    assert sent.headers["authorization"] == f"Bearer {DUMMY_API_KEY}"
    assert transport.request_json()["model"] == CONFIGURED_MODEL


@pytest.mark.asyncio
async def test_default_model_is_jev_1_14_0() -> None:
    """AC：默认模型固定为 jev-1.14.0，不需要额外配置即可与规格一致。"""
    transport = ScriptedTypesafeTransport(system_one_payload)

    async with configured_jev_request(
        config_for_request(
            jev_model=KomariDecisionConfigSchema.model_fields["jev_model"].default
        ),
        transport=transport,
    ) as client:
        answer = await client.system_one(
            state=state_payload(),
            questions={"summary": NOUL_QUESTION},
        )

    assert transport.request_json()["model"] == "jev-1.14.0"
    assert answer.model == CONFIGURED_MODEL


@pytest.mark.asyncio
async def test_fresh_install_configuration_fails_before_any_request() -> None:
    """AC：新装空配置在创建 SDK 客户端前显式失败，不发任何请求。"""
    transport = ScriptedTypesafeTransport(system_one_payload)

    with pytest.raises(JevRequestConfigurationError) as caught:
        async with configured_jev_request(
            config_for_request(jev_base_url="", jev_api_key=""),
            transport=transport,
        ):
            pytest.fail("空配置不得进入 SDK 请求上下文")

    assert transport.requests == []
    assert transport.closed is False
    assert "jev_base_url" in str(caught.value)


@pytest.fixture(autouse=True)
def _configure_sqlmodel_mappers() -> None:
    """SQLModel table 模型的属性访问需要已配置的 mapper。

    ``model_construct`` 绕过校验，也不会触发 SQLAlchemy 的惰性映射配置；
    不先配置 mapper 时属性访问会抛 ``supports_population`` 的 AttributeError。
    """
    configure_mappers()


@pytest.mark.parametrize(
    ("overrides", "expected_field", "echo_fragment"),
    [
        (
            {"jev_base_url": "https://jev-user:tsk181-url-secret@example.invalid"},
            "jev_base_url",
            "tsk181-url-secret",
        ),
        (
            {"jev_base_url": f"{CONFIGURED_BASE_URL}?tenant=tsk181"},
            "jev_base_url",
            "tsk181",
        ),
        (
            {"jev_base_url": f"{CONFIGURED_BASE_URL}#tsk181"},
            "jev_base_url",
            "tsk181",
        ),
        (
            {"jev_base_url": "jev-gateway.example.invalid"},
            "jev_base_url",
            "jev-gateway",
        ),
        ({"jev_base_url": "ftp://jev.example.invalid"}, "jev_base_url", "ftp"),
        (
            {"jev_base_url": "https://jev.example.invalid:not-a-port"},
            "jev_base_url",
            "not-a-port",
        ),
        (
            {"jev_base_url": "https://jev.example.invalid/tsk181\r\nX: c"},
            "jev_base_url",
            "tsk181",
        ),
        (
            {"jev_base_url": "https://jev example.invalid"},
            "jev_base_url",
            "jev example",
        ),
        ({"jev_api_key": "tsk181 key with space"}, "jev_api_key", "tsk181 key"),
        (
            {"jev_api_key": "tsk181-\u043a\u043b\u044e\u0447"},
            "jev_api_key",
            "\u043a\u043b\u044e\u0447",
        ),
        ({"jev_model": "   "}, "jev_model", "   "),
        (
            {"jev_request_timeout_seconds": 0.0},
            "jev_request_timeout_seconds",
            "0",
        ),
        (
            {"jev_http_timeout_seconds": float("inf")},
            "jev_http_timeout_seconds",
            "inf",
        ),
        ({"jev_base_url": ""}, "jev_base_url", None),
        ({"jev_api_key": ""}, "jev_api_key", None),
    ],
)
@pytest.mark.asyncio
async def test_corrupted_snapshot_fails_before_any_request(
    overrides: dict[str, Any],
    expected_field: str,
    echo_fragment: str | None,
) -> None:
    """AC：畸形值在写入边界已被拒绝；请求 seam 对绕过校验的快照仍自行预检。

    ``model_construct`` 是 Pydantic 公开构造入口，用来表示"未经 Schema 校验
    的冻结快照"（任务冻结或异常来源）。请求 seam 必须重做校验并在创建 SDK
    客户端前失败，错误文本只指出字段与规则，不回显被拒值。
    """
    transport = ScriptedTypesafeTransport(system_one_payload)
    config = KomariDecisionConfigSchema.model_construct(
        **{
            "jev_base_url": CONFIGURED_BASE_URL,
            "jev_api_key": DUMMY_API_KEY,
            "jev_model": CONFIGURED_MODEL,
            "jev_http_timeout_seconds": 3.0,
            "jev_request_timeout_seconds": 3.0,
            **overrides,
        }
    )

    with pytest.raises(JevRequestConfigurationError) as caught:
        async with configured_jev_request(config, transport=transport):
            pytest.fail("不可用配置不得进入 SDK 请求上下文")

    assert transport.requests == []
    assert transport.closed is False
    assert expected_field in str(caught.value)
    if echo_fragment is not None:
        assert echo_fragment not in str(caught.value)


def _status_response(status: int) -> Callable[[httpx2.Request], httpx2.Response]:
    def _handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            status, json={"error": "synthetic-upstream-failure"}, request=request
        )

    return _handler


def _invalid_protocol_response(request: httpx2.Request) -> dict[str, Any]:
    """200 但 Choice 答案缺少必需字段，触发 SDK 响应校验失败。"""
    del request
    return {
        "model": CONFIGURED_MODEL,
        "answers": {"chat": {"type": "choice", "choice": "reply"}},
        "usage": {"input_tokens": 7, "output_tokens": 1},
    }


def _raise_read_timeout(request: httpx2.Request) -> dict[str, Any]:
    """模拟 SDK 单次操作超时（httpx2 读写超时），与请求总截止无关。"""
    del request
    msg = "simulated SDK read timeout"
    raise httpx2.ReadTimeout(msg)


def _raise_connect_error(request: httpx2.Request) -> dict[str, Any]:
    msg = f"cannot connect to {request.url.host}"
    raise httpx2.ConnectError(msg)


@pytest.mark.parametrize(
    ("handler", "expected_error"),
    [
        (_raise_connect_error, TypeSafeAPIConnectionError),
        (_status_response(429), TypeSafeRateLimitError),
        (_status_response(500), TypeSafeInternalServerError),
        (_status_response(503), TypeSafeInternalServerError),
        (_status_response(401), TypeSafeAuthenticationError),
        (_raise_read_timeout, TypeSafeAPITimeoutError),
        (_invalid_protocol_response, TypeSafeAPIResponseValidationError),
    ],
)
@pytest.mark.asyncio
async def test_sdk_failures_propagate_without_retry(
    handler: Callable[[httpx2.Request], Any],
    expected_error: type[BaseException],
) -> None:
    """AC：网络故障、429、5xx、认证、SDK 超时与协议失败按 SDK 异常传播且不自动重发。

    ``TypeSafeAPITimeoutError`` 也是 ``TimeoutError``；请求总截止未到期时
    必须原样传播，不能被换算成本模块的 ``JevRequestTimeoutError``。
    """
    transport = ScriptedTypesafeTransport(handler)

    with pytest.raises(expected_error):
        async with configured_jev_request(
            config_for_request(), transport=transport
        ) as client:
            await client.system_one(
                state=state_payload(),
                questions={"chat": CHOICE_QUESTION, "summary": NOUL_QUESTION},
            )

    assert len(transport.requests) == 1
    assert transport.closed is True


def _delayed_payload(
    delay_seconds: float,
) -> Callable[[httpx2.Request], Any]:
    async def _handler(request: httpx2.Request) -> httpx2.Response:
        await asyncio.sleep(delay_seconds)
        return httpx2.Response(200, json=system_one_payload(request), request=request)

    return _handler


@pytest.mark.asyncio
async def test_total_deadline_rejects_late_result_across_phases() -> None:
    """AC：多段等待各自未超 HTTP 超时，累计超过总截止后不接受迟到结果。"""
    transport = ScriptedTypesafeTransport(_delayed_payload(0.04))
    config = config_for_request(
        jev_http_timeout_seconds=0.5,
        jev_request_timeout_seconds=0.06,
    )
    started = asyncio.get_running_loop().time()

    with pytest.raises(JevRequestTimeoutError):
        async with configured_jev_request(config, transport=transport) as client:
            for _ in range(2):
                answer = await client.system_one(
                    state=state_payload(),
                    questions={"summary": NOUL_QUESTION},
                )
                # 每段自身都在 HTTP 单次操作超时内返回
                assert answer.nouls["summary"].noul == pytest.approx(0.18)

    elapsed = asyncio.get_running_loop().time() - started
    assert len(transport.requests) == 2
    assert elapsed >= config.jev_request_timeout_seconds
    assert transport.closed is True


@pytest.mark.asyncio
async def test_result_within_total_deadline_is_accepted() -> None:
    """AC：未超过总截止的请求正常取得类型化结果并释放客户端。"""
    transport = ScriptedTypesafeTransport(_delayed_payload(0.01))
    config = config_for_request(
        jev_http_timeout_seconds=0.5,
        jev_request_timeout_seconds=0.5,
    )

    async with configured_jev_request(config, transport=transport) as client:
        answer = await client.system_one(
            state=state_payload(),
            questions={"chat": CHOICE_QUESTION},
        )

    assert answer.choices["chat"].choice == "reply"
    assert transport.closed is True


async def _issue_single_request(
    config: KomariDecisionConfigSchema,
    transport: httpx2.AsyncBaseTransport,
) -> None:
    async with configured_jev_request(config, transport=transport) as client:
        await client.system_one(
            state=state_payload(),
            questions={"summary": NOUL_QUESTION},
        )


@pytest.mark.asyncio
async def test_cancellation_closes_owned_transport() -> None:
    """AC：判定任务被取消时仍释放本上下文独占的 SDK 客户端。"""
    started = asyncio.Event()

    async def _handler(request: httpx2.Request) -> httpx2.Response:
        started.set()
        await asyncio.sleep(30)
        return httpx2.Response(200, json=system_one_payload(request), request=request)

    transport = ScriptedTypesafeTransport(_handler)
    task = asyncio.create_task(_issue_single_request(config_for_request(), transport))
    await started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert transport.closed is True


@pytest.mark.asyncio
async def test_closed_context_does_not_affect_other_context() -> None:
    """AC：一个上下文的关闭不影响另一个（配置已变更）上下文的客户端。"""
    transport_a = ScriptedTypesafeTransport(system_one_payload)
    transport_b = ScriptedTypesafeTransport(system_one_payload)
    config_b = config_for_request(
        jev_base_url="https://jev-gateway-b.example.invalid/decision",
        jev_api_key="tsk-181-dummy-key-b",
        jev_model="jev-1.14.0-b",
    )

    async with configured_jev_request(
        config_for_request(), transport=transport_a
    ) as client_a:
        async with configured_jev_request(config_b, transport=transport_b) as client_b:
            await client_b.system_one(
                state=state_payload(), questions={"summary": NOUL_QUESTION}
            )
        # 新配置的上下文已关闭：它自己释放，旧上下文仍然可用
        assert transport_b.closed is True
        assert transport_a.closed is False
        await client_a.system_one(
            state=state_payload(), questions={"summary": NOUL_QUESTION}
        )

    assert transport_a.closed is True
    assert str(transport_a.requests[0].url).startswith(CONFIGURED_BASE_URL)
    assert str(transport_b.requests[0].url).startswith(
        "https://jev-gateway-b.example.invalid/decision"
    )
    assert transport_a.requests[0].headers["authorization"] == f"Bearer {DUMMY_API_KEY}"
    assert transport_b.requests[0].headers["authorization"] == (
        "Bearer tsk-181-dummy-key-b"
    )
    assert transport_a.request_json()["model"] == CONFIGURED_MODEL
    assert transport_b.request_json()["model"] == "jev-1.14.0-b"


@pytest.mark.asyncio
async def test_concurrent_contexts_use_their_own_config() -> None:
    """AC：并发请求各自使用自己的冻结配置，并各自释放资源。"""
    transport_old = ScriptedTypesafeTransport(_delayed_payload(0.02))
    transport_new = ScriptedTypesafeTransport(_delayed_payload(0.02))
    config_new = config_for_request(
        jev_base_url="https://jev-gateway-new.example.invalid/decision",
        jev_api_key="tsk-181-dummy-key-new",
        jev_model="jev-1.14.0-new",
    )

    await asyncio.gather(
        _issue_single_request(config_for_request(), transport_old),
        _issue_single_request(config_new, transport_new),
    )

    assert len(transport_old.requests) == 1
    assert len(transport_new.requests) == 1
    assert str(transport_old.requests[0].url).startswith(CONFIGURED_BASE_URL)
    assert str(transport_new.requests[0].url).startswith(
        "https://jev-gateway-new.example.invalid/decision"
    )
    assert transport_old.request_json()["model"] == CONFIGURED_MODEL
    assert transport_new.request_json()["model"] == "jev-1.14.0-new"
    assert transport_old.closed is True
    assert transport_new.closed is True


@pytest.mark.asyncio
async def test_debug_logging_cannot_leak_sdk_bodies(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """AC：即使 SDK 开启 debug，请求/响应正文也不会进入普通运行日志。

    先用同一 transport 直接调用官方 SDK 作为对照（SDK 文档声明正文不被脱敏），
    证明捕获装置能发现泄漏；再经本 seam 请求，断言凭据、state 与响应正文
    canary 均不出现。
    """
    monkeypatch.setenv("TYPESAFE_LOG_LEVEL", "debug")
    sdk_logger = logging.getLogger("typesafe_sdk")
    previous_level = sdk_logger.level
    sdk_logger.setLevel(logging.DEBUG)  # 复现 TYPESAFE_LOG_LEVEL=debug 的导入期效果
    try:
        assert sdk_logger.isEnabledFor(logging.DEBUG)
        with caplog.at_level(logging.DEBUG):
            control = ScriptedTypesafeTransport(system_one_payload)
            async with AsyncTypeSafeClient(
                api_key=DUMMY_API_KEY,
                base_url=CONFIGURED_BASE_URL,
                transport=control,
            ) as raw_client:
                await raw_client.system_one(
                    state=state_payload(), questions={"summary": NOUL_QUESTION}
                )
        assert STATE_CANARY in caplog.text

        caplog.clear()
        sdk_logger.setLevel(logging.DEBUG)
        with caplog.at_level(logging.DEBUG):
            transport = ScriptedTypesafeTransport(system_one_payload)
            async with configured_jev_request(
                config_for_request(), transport=transport
            ) as client:
                answer = await client.system_one(
                    state=state_payload(), questions={"summary": NOUL_QUESTION}
                )
        captured = caplog.text
        assert answer.nouls["summary"].noul == pytest.approx(0.18)
    finally:
        sdk_logger.setLevel(previous_level)

    assert captured  # 普通运行日志仍在工作，只是不含正文
    assert STATE_CANARY not in captured
    assert OUTPUT_CANARY not in captured
    assert DUMMY_API_KEY not in captured


class BlockingTypesafeTransport(httpx2.AsyncBaseTransport):
    """永不自行返回的 transport：只有取消能结束请求，记录取消与关闭。"""

    def __init__(self) -> None:
        self.requests: list[httpx2.Request] = []
        self.cancelled = False
        self.closed = False
        self._release = asyncio.Event()

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        try:
            await self._release.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        msg = "阻塞 transport 不应自行放行"
        raise AssertionError(msg)

    async def aclose(self) -> None:
        self.closed = True


class CancellationSuppressingTypesafeTransport(httpx2.AsyncBaseTransport):
    """抑制取消并返回"成功"响应的 transport：模拟不可信的上游实现。"""

    def __init__(self) -> None:
        self.requests: list[httpx2.Request] = []
        self.suppressed = False
        self.closed = False
        self._release = asyncio.Event()

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        try:
            await self._release.wait()
        except asyncio.CancelledError:
            self.suppressed = True
            return httpx2.Response(
                200, json=system_one_payload(request), request=request
            )
        msg = "抑制取消的 transport 只能经取消路径返回"
        raise AssertionError(msg)

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_indefinitely_blocked_request_ends_at_total_deadline() -> None:
    """AC：总截止主动取消被无限阻塞的请求，而非等它自己返回。

    HTTP 单次操作超时被放大到 30s，只有总截止（0.05s）能结束请求；外层看门狗
    只是防止"截止未生效"时测试挂死。
    """
    transport = BlockingTypesafeTransport()
    config = config_for_request(
        jev_http_timeout_seconds=30.0,
        jev_request_timeout_seconds=0.05,
    )
    loop = asyncio.get_running_loop()
    started = loop.time()
    task = asyncio.create_task(_issue_single_request(config, transport))

    with pytest.raises(JevRequestTimeoutError):
        await asyncio.wait_for(task, timeout=5.0)

    elapsed = loop.time() - started
    assert elapsed < 1.0
    assert len(transport.requests) == 1
    assert transport.cancelled is True
    assert transport.closed is True


async def _issue_suppressing_request(
    config: KomariDecisionConfigSchema,
    transport: CancellationSuppressingTypesafeTransport,
    arrived: list[float],
) -> None:
    async with configured_jev_request(config, transport=transport) as client:
        answer = await client.system_one(
            state=state_payload(), questions={"summary": NOUL_QUESTION}
        )
        arrived.append(answer.nouls["summary"].noul)


@pytest.mark.asyncio
async def test_cancellation_suppressing_transport_late_result_is_rejected() -> None:
    """AC：抑制取消、迟到返回的答案仍不被接受（上下文退出必须失败）。"""
    transport = CancellationSuppressingTypesafeTransport()
    config = config_for_request(
        jev_http_timeout_seconds=30.0,
        jev_request_timeout_seconds=0.05,
    )
    arrived: list[float] = []
    loop = asyncio.get_running_loop()
    started = loop.time()
    task = asyncio.create_task(_issue_suppressing_request(config, transport, arrived))

    with pytest.raises(JevRequestTimeoutError):
        await asyncio.wait_for(task, timeout=5.0)

    elapsed = loop.time() - started
    assert elapsed < 1.0
    # 迟到的答案确实产生了，但上下文退出把它作废
    assert arrived == [pytest.approx(0.18)]
    assert transport.suppressed is True
    assert transport.closed is True


class BlockingCloseTransport(ScriptedTypesafeTransport):
    """在异步关闭边界暂停，验证调用方取消不会留下未释放的传输。"""

    def __init__(self, status_code: int) -> None:
        super().__init__(
            system_one_payload if status_code == 200 else _status_response(status_code)
        )
        self.close_started = asyncio.Event()
        self.close_release = asyncio.Event()
        self.close_calls = 0

    async def aclose(self) -> None:
        self.close_calls += 1
        self.close_started.set()
        await self.close_release.wait()
        self.closed = True


@pytest.mark.parametrize("status_code", [200, 503])
@pytest.mark.asyncio
async def test_cancellation_during_cleanup_waits_for_owned_transport(
    status_code: int,
) -> None:
    """关闭期间的重复取消等待资源释放；请求已失败时保留原始错误。"""
    transport = BlockingCloseTransport(status_code)

    async def request() -> None:
        async with configured_jev_request(
            config_for_request(), transport=transport
        ) as client:
            await client.system_one(
                state=state_payload(), questions={"summary": NOUL_QUESTION}
            )

    task = asyncio.create_task(request())
    try:
        await asyncio.wait_for(transport.close_started.wait(), timeout=1)
        for _ in range(2):
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done(), "取消后必须等待已启动的传输关闭"
            assert not transport.closed
        transport.close_release.set()
        expected = (
            asyncio.CancelledError
            if status_code == 200
            else TypeSafeInternalServerError
        )
        with pytest.raises(expected):
            await asyncio.wait_for(task, timeout=1)
        assert transport.closed
        assert transport.close_calls == 1
    finally:
        transport.close_release.set()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), timeout=1)
