"""TSK-223 阶段 B：扩展金丝雀递归泄漏验收（全部诊断面零内容）。

验收目标（TSK-217 §6/§7/§8 冻结）：

- 扩展标准 bundle 覆盖：群/用户/消息/请求/履约/提案 ID、消息正文/换行、
  prompt、reasoning、工具参数、URL、CQ image、base64、Bearer/API token、恶
  意 trace、异常与 traceback 特征；
- 递归扫描面：``group_admission.*`` 日志 message+extra、``/status``、阶段 A
  HTTP 错误体、管理审计、故障/归属私聊卡、模拟 Sentry logger handoff
  record（loguru record 全投影，且绝不附带异常对象）；
- 唯一允许回显的面仍是已鉴权 policy GET/PUT 成功响应的精确
  ``policy.group_ids`` 子树；其余零命中；
- 扫描器自检 / negative control：无放行必检出；错误报告只含 label / path，
  不回显敏感值。
"""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest
from pydantic import BaseModel

from tests.group_admission.management_support import (
    POLICY_PATH,
    READER_TOKEN,
    STATUS_PATH,
    WRITER_TOKEN,
    RecordingAuditRecorder,
    asgi_client,
    atomic_policy,
    auth_headers,
    normalized_policy,
    prepare_control_plane,
    put_headers,
)
from tests.group_admission.observability_support import (
    FakeAdmissionBot,
    FakeUtcClock,
    MutableBotsProvider,
    MutableSuperusersProvider,
    build_runtime_kwargs,
    capture_admission_logs,
    card_texts,
    runtime_log_projection,
)
from tests.group_admission.runtime_support import (
    AdmissionStorageFake,
    stored_policy,
)
from tests.group_admission.sensitive_canary import (
    SensitiveCanaryBundle,
    SensitiveCanaryToken,
    build_extended_canary_bundle,
)

pytestmark = pytest.mark.group_admission_acceptance

_POLICY_GROUP_IDS_PATH_PREFIXES = frozenset({("policy", "group_ids")})


class _NestedCanaryModel(BaseModel):
    """自检用嵌套 Pydantic 载荷。"""

    body: str


@dataclasses.dataclass(slots=True)
class _NestedCanaryDataclass:
    """自检用嵌套 dataclass 载荷。"""

    trace: str


def _malicious_concat(bundle: SensitiveCanaryBundle) -> str:
    return " ".join(
        token.value for token in bundle.tokens if isinstance(token.value, str)
    )


def test_extended_canary_bundle_self_check_and_report_shape() -> None:
    """自检 + negative control：检出全部植入泄漏；报告只含 label/path。"""
    bundle = build_extended_canary_bundle()
    tokens: dict[str, Any] = {token.label: token.value for token in bundle.tokens}

    payload: dict[str, Any] = {
        "policy": {"mode": "blacklist", "group_ids": [tokens["group-id"], 1]},
        "detail": {
            "message": tokens["exception-trace"],
            "nested": [
                tokens["url"],
                {"deep": tokens["api-key"]},
                {"request": tokens["request-id"], "key-name": "sensitive"},
                tokens["bearer-token"],
            ],
            "attachment": tokens["base64"],
        },
        "records": [
            _NestedCanaryDataclass(trace=tokens["reasoning-body"]),
            _NestedCanaryModel(body=tokens["prompt-body"]),
            tokens["newline-body"].encode("utf-8"),
            {
                tokens["fulfillment-id"],
                tokens["proposal-id"],
                tokens["message-id"],
            },
            {"args": tokens["tool-args"], "user": tokens["user-id"]},
            {"media": tokens["cq-code"]},
        ],
    }

    leaks = bundle.leak_report(payload)
    leaked_labels = {label for label, _path in leaks}
    assert leaked_labels == set(tokens), (
        f"自检失败：未检出的金丝雀 {sorted(set(tokens) - leaked_labels)}"
    )

    # 放行前缀只豁免 policy.group_ids 子树
    allowed = bundle.leak_report(
        payload, allowed_path_prefixes=_POLICY_GROUP_IDS_PATH_PREFIXES
    )
    assert ("group-id", ("policy", "group_ids", "0")) not in allowed
    assert {label for label, _path in allowed} == set(tokens) - {"group-id"}

    # 报告只含 label/path：失败消息绝不回显金丝雀正文
    with pytest.raises(AssertionError) as exc_info:
        bundle.assert_no_leaks(payload, context="self-check")
    failure_message = str(exc_info.value)
    for token in tokens.values():
        assert str(token) not in failure_message, "泄漏报告回显了敏感值"
    for label in tokens:
        assert label in failure_message, f"泄漏报告缺少 label: {label}"


async def test_malicious_storage_exceptions_do_not_leak_into_any_diagnostic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """恶意存储异常：错误体/审计/status/日志/卡全部零命中。"""
    bundle = build_extended_canary_bundle()
    malicious = _malicious_concat(bundle)
    clock = FakeUtcClock()
    bot = FakeAdmissionBot("bot-canary")
    recorder = RecordingAuditRecorder()
    storage = AdmissionStorageFake(stored_policy(1, atomic_policy("blacklist", [200])))
    app, runtime, _manager = await prepare_control_plane(
        monkeypatch,
        storage,
        audit_recorder=recorder,
        runtime_kwargs=build_runtime_kwargs(
            clock=clock,
            bots_provider=MutableBotsProvider((bot,)),
            superusers_provider=MutableSuperusersProvider((42,)),
        ),
    )

    with capture_admission_logs() as capture:
        storage.fetch_error = RuntimeError(malicious)
        async with asgi_client(app) as client:
            get_response = await client.get(
                POLICY_PATH, headers=auth_headers(READER_TOKEN)
            )
            assert get_response.status_code == 503
            bundle.assert_no_leaks(
                {"body": get_response.json(), "headers": dict(get_response.headers)},
                context="GET 503 错误体（恶意异常）",
            )

            status_response = await client.get(
                STATUS_PATH, headers=auth_headers(READER_TOKEN)
            )
            assert status_response.status_code == 200
            bundle.assert_no_leaks(
                status_response.json(), context="恶意异常期间的 status"
            )

        storage.cas_error = RuntimeError(malicious)
        async with asgi_client(app) as client:
            put_response = await client.put(
                POLICY_PATH,
                headers=put_headers(WRITER_TOKEN, if_match='"1"'),
                json=atomic_policy("whitelist", [700]),
            )
            assert put_response.status_code == 503
            bundle.assert_no_leaks(
                {"body": put_response.json(), "headers": dict(put_response.headers)},
                context="PUT 503 错误体（恶意异常）",
            )

        await runtime.process_observability()

    bundle.assert_no_leaks(
        [event.to_dict() for event in recorder.events],
        context="恶意异常的审计事件",
    )
    assert capture.records, "应已捕获故障期结构化日志"
    for record in capture.records:
        bundle.assert_no_leaks(
            runtime_log_projection(record),
            context=f"恶意异常日志 {record['extra'].get('event')}",
        )
    for _user_id, text in card_texts((bot,)):
        bundle.assert_no_leaks(text, context="恶意异常的故障卡")


async def test_invalid_policy_content_canaries_do_not_leak(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """非法策略正文（含群号/URL/prompt 等金丝雀）不进入日志/状态/卡。"""
    bundle = build_extended_canary_bundle()
    tokens = {token.label: token.value for token in bundle.tokens}
    clock = FakeUtcClock()
    bot = FakeAdmissionBot("bot-policy-canary")
    storage = AdmissionStorageFake(stored_policy(1, atomic_policy("blacklist", [200])))
    app, runtime, _manager = await prepare_control_plane(
        monkeypatch,
        storage,
        runtime_kwargs=build_runtime_kwargs(
            clock=clock,
            bots_provider=MutableBotsProvider((bot,)),
            superusers_provider=MutableSuperusersProvider((42,)),
        ),
    )

    invalid_policy = {
        "mode": tokens["prompt-body"],
        "group_ids": [tokens["cq-code"], tokens["group-id"], tokens["url"]],
    }

    with capture_admission_logs() as capture:
        storage.deliver(stored_policy(2, invalid_policy))
        await runtime.process_observability()

        async with asgi_client(app) as client:
            status_response = await client.get(
                STATUS_PATH, headers=auth_headers(READER_TOKEN)
            )
            assert status_response.status_code == 200
            bundle.assert_no_leaks(
                status_response.json(), context="非法策略期间的 status"
            )

    assert capture.records, "更高非法修订必须产生故障日志"
    for record in capture.records:
        projection = runtime_log_projection(record)
        bundle.assert_no_leaks(
            projection, context=f"非法策略日志 {record['extra'].get('event')}"
        )
        # 群号/正文也绝不以数字或子串形态出现
        message = str(record["message"])
        assert str(tokens["group-id"]) not in message

    for _user_id, text in card_texts((bot,)):
        bundle.assert_no_leaks(text, context="非法策略的故障卡")
        assert str(tokens["group-id"]) not in text


async def test_attribution_canaries_do_not_leak_into_logs_or_cards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """恶意归因载荷与非法 family：日志/卡零命中，family 归一 unknown。"""
    bundle = build_extended_canary_bundle()
    tokens = {token.label: token.value for token in bundle.tokens}
    clock = FakeUtcClock()
    bot = FakeAdmissionBot("bot-attribution-canary")
    storage = AdmissionStorageFake(stored_policy(1, atomic_policy("blacklist", [])))
    _app, runtime, _manager = await prepare_control_plane(
        monkeypatch,
        storage,
        runtime_kwargs=build_runtime_kwargs(
            clock=clock,
            bots_provider=MutableBotsProvider((bot,)),
            superusers_provider=MutableSuperusersProvider((42,)),
        ),
    )

    with capture_admission_logs() as capture:
        runtime.adjudicate(
            [
                tokens["message-id"],
                tokens["request-id"],
                tokens["group-id"],
                tokens["user-id"],
            ],
            event_family=tokens["fulfillment-id"],
        )
        runtime.record_private_input_rejected(event_family=tokens["proposal-id"])
        await runtime.process_observability()

    for record in capture.records:
        bundle.assert_no_leaks(
            runtime_log_projection(record),
            context=f"归因金丝雀日志 {record['extra'].get('event')}",
        )
        if "event_family" in record["extra"]:
            assert record["extra"]["event_family"] == "unknown"

    for _user_id, text in card_texts((bot,)):
        bundle.assert_no_leaks(text, context="归因金丝雀卡")


async def test_sentry_handoff_records_carry_no_exception_or_canary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """模拟 Sentry logger handoff：record 全投影零命中且不附带异常对象。"""
    bundle = build_extended_canary_bundle()
    malicious = _malicious_concat(bundle)
    clock = FakeUtcClock()
    storage = AdmissionStorageFake(stored_policy(1, atomic_policy("blacklist", [200])))
    app, runtime, _manager = await prepare_control_plane(
        monkeypatch,
        storage,
        runtime_kwargs=build_runtime_kwargs(clock=clock),
    )

    with capture_admission_logs() as capture:
        storage.fetch_error = RuntimeError(malicious)
        async with asgi_client(app) as client:
            assert (
                await client.get(POLICY_PATH, headers=auth_headers(READER_TOKEN))
            ).status_code == 503
        storage.deliver(stored_policy(2, {"mode": "graylist", "group_ids": []}))

    assert capture.records
    for record in capture.records:
        # 生产不得把原始异常挂到日志（Sentry 集成会消费 record["exception"]）
        assert record["exception"] is None, (
            f"{record['extra'].get('event')} 日志附带了异常对象"
        )
        handoff = {
            "message": str(record["message"]),
            "level": str(record["level"].name),
            "name": str(record["name"]),
            "extra": {str(key): value for key, value in record["extra"].items()},
            "exception": record["exception"],
        }
        bundle.assert_no_leaks(
            handoff,
            context=f"Sentry handoff record {record['extra'].get('event')}",
        )
    await runtime.close()


async def test_policy_echo_boundary_holds_with_extended_bundle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """扩展 bundle 下：群号只允许出现在已鉴权 policy 响应的 group_ids。"""
    recorder = RecordingAuditRecorder()
    storage = AdmissionStorageFake(stored_policy(1, atomic_policy("blacklist", [])))
    app, _runtime, _manager = await prepare_control_plane(
        monkeypatch, storage, audit_recorder=recorder
    )
    group_bundle = SensitiveCanaryBundle(
        (
            SensitiveCanaryToken(label="group-779101", value=779101),
            SensitiveCanaryToken(label="group-779102", value=779102),
        )
    )

    async with asgi_client(app) as client:
        put_response = await client.put(
            POLICY_PATH,
            headers=put_headers(WRITER_TOKEN, if_match='"1"'),
            json=atomic_policy("blacklist", [779102, 779101, 779102]),
        )
        assert put_response.status_code == 200
        group_bundle.assert_no_leaks(
            put_response.json(),
            allowed_path_prefixes=_POLICY_GROUP_IDS_PATH_PREFIXES,
            context="PUT 200 响应体（扩展群号）",
        )
        assert put_response.json()["policy"] == normalized_policy(
            "blacklist", [779102, 779101]
        )

        get_response = await client.get(
            POLICY_PATH, headers=auth_headers(READER_TOKEN)
        )
        assert get_response.status_code == 200
        group_bundle.assert_no_leaks(
            get_response.json(),
            allowed_path_prefixes=_POLICY_GROUP_IDS_PATH_PREFIXES,
            context="GET 200 响应体（扩展群号）",
        )
        # 自检：无放行路径时同一响应必须被检出
        assert group_bundle.leak_report(get_response.json()) != []

        status_response = await client.get(
            STATUS_PATH, headers=auth_headers(READER_TOKEN)
        )
        group_bundle.assert_no_leaks(
            status_response.json(), context="status（扩展群号）"
        )

    group_bundle.assert_no_leaks(
        [event.to_dict() for event in recorder.events],
        context="PUT 成功的审计事件（扩展群号）",
    )
