"""TSK-223 阶段 A：敏感金丝雀泄漏验收（SensitiveCanaryBundle 消费面）。

验收目标（冻结）：

- 已鉴权 GET policy 响应是 **唯一** 允许回显策略/群号的面，且金丝雀只允许
  出现在精确路径 ``policy.group_ids`` 子树；
- GET/PUT 错误体、审计事件、状态投影、响应头出现任何金丝雀（恶意存储异常
  正文、非法 body 额外字段、URL/base64/CQ 码/Bearer token/换行正文、特征群
  号）都构成泄漏（测试红，负向控制：错误泄漏）；
- 扫描器自检：同一响应在无放行路径时必须被检出，证明工具不是恒绿摆设。
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.group_admission.management_support import (
    POLICY_PATH,
    READER_TOKEN,
    STATUS_PATH,
    WRITER_TOKEN,
    RecordingAuditRecorder,
    asgi_client,
    atomic_policy,
    auth_headers,
    prepare_control_plane,
    put_headers,
)
from tests.group_admission.runtime_support import (
    AdmissionStorageFake,
    stored_policy,
)
from tests.group_admission.sensitive_canary import (
    SensitiveCanaryBundle,
    SensitiveCanaryToken,
    build_standard_canary_bundle,
)

pytestmark = pytest.mark.group_admission_acceptance

_POLICY_GROUP_IDS_PATH_PREFIXES = frozenset({("policy", "group_ids")})


def _scan_http_exchange(
    bundle: SensitiveCanaryBundle,
    response: Any,
    *,
    allowed_path_prefixes: frozenset[tuple[str, ...]] = frozenset(),
    context: str,
) -> None:
    """扫描一次 HTTP 交换的响应体与响应头。"""
    payload = {"body": response.json(), "headers": dict(response.headers)}
    bundle.assert_no_leaks(
        payload, allowed_path_prefixes=allowed_path_prefixes, context=context
    )


def test_canary_scanner_detects_leaks_and_respects_allowed_paths() -> None:
    """扫描器自检（负向控制）：无放行必检出，放行前缀子树内不误报。"""
    bundle = SensitiveCanaryBundle(
        (
            SensitiveCanaryToken(label="url", value="https://canary.example/x"),
            SensitiveCanaryToken(label="group-id", value=77001),
        )
    )
    payload = {
        "policy": {"mode": "blacklist", "group_ids": [77001, 200]},
        "detail": {"message": "see https://canary.example/x"},
    }

    leaks = bundle.leak_report(payload)
    assert ("url", ("detail", "message")) in leaks
    assert ("group-id", ("policy", "group_ids", "0")) in leaks

    allowed_only = bundle.leak_report(
        payload, allowed_path_prefixes=_POLICY_GROUP_IDS_PATH_PREFIXES
    )
    assert allowed_only == [("url", ("detail", "message"))]


async def test_get_policy_echo_is_allowed_only_at_policy_group_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GET policy 回显群号只允许在精确路径 policy.group_ids。"""
    storage = AdmissionStorageFake(
        stored_policy(1, atomic_policy("blacklist", [77002, 77001, 77002]))
    )
    app, _runtime, _manager = await prepare_control_plane(monkeypatch, storage)
    bundle = SensitiveCanaryBundle(
        (
            SensitiveCanaryToken(label="group-77001", value=77001),
            SensitiveCanaryToken(label="group-77002", value=77002),
        )
    )

    async with asgi_client(app) as client:
        response = await client.get(
            POLICY_PATH, headers=auth_headers(READER_TOKEN)
        )
    assert response.status_code == 200

    bundle.assert_no_leaks(
        response.json(),
        allowed_path_prefixes=_POLICY_GROUP_IDS_PATH_PREFIXES,
        context="GET policy 响应体",
    )
    # 自检：无放行路径时同一响应必须被检出回显（工具不是恒绿）
    assert bundle.leak_report(response.json()) != []


async def test_error_and_audit_do_not_leak_malicious_storage_exceptions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """恶意存储异常正文不得出现在错误体、响应头或审计事件中。"""
    bundle = build_standard_canary_bundle()
    malicious_message = " ".join(
        token.value
        for token in bundle.tokens
        if isinstance(token.value, str)
    )

    # GET：存储读取抛恶意异常
    storage = AdmissionStorageFake(stored_policy(1, atomic_policy("blacklist", [200])))
    app, _runtime, _manager = await prepare_control_plane(monkeypatch, storage)
    storage.fetch_error = RuntimeError(malicious_message)
    async with asgi_client(app) as client:
        get_response = await client.get(
            POLICY_PATH, headers=auth_headers(READER_TOKEN)
        )
    assert get_response.status_code == 503
    _scan_http_exchange(
        bundle, get_response, context="GET 503（恶意存储异常）"
    )

    # PUT：CAS 写抛恶意异常；审计事件同样不得携带异常正文
    recorder = RecordingAuditRecorder()
    storage = AdmissionStorageFake(stored_policy(1, atomic_policy("blacklist", [200])))
    app, _runtime, _manager = await prepare_control_plane(
        monkeypatch, storage, audit_recorder=recorder
    )
    storage.cas_error = RuntimeError(malicious_message)
    async with asgi_client(app) as client:
        put_response = await client.put(
            POLICY_PATH,
            headers=put_headers(WRITER_TOKEN, if_match='"1"'),
            json=atomic_policy("whitelist", [700]),
        )
    assert put_response.status_code == 503
    _scan_http_exchange(
        bundle, put_response, context="PUT 503（恶意存储异常）"
    )
    bundle.assert_no_leaks(
        [event.to_dict() for event in recorder.events],
        context="恶意存储异常的审计事件",
    )


async def test_invalid_body_canaries_do_not_leak_into_error_or_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """非法 body 中的金丝雀（额外字段/元素/换行正文）不得泄漏。"""
    bundle = build_standard_canary_bundle()
    tokens = {token.label: token.value for token in bundle.tokens}
    recorder = RecordingAuditRecorder()
    storage = AdmissionStorageFake(stored_policy(1, atomic_policy("blacklist", [200])))
    app, _runtime, _manager = await prepare_control_plane(
        monkeypatch, storage, audit_recorder=recorder
    )

    invalid_bodies = [
        {
            "mode": "blacklist",
            "group_ids": [],
            "note": tokens["url"],
        },
        {
            "mode": "blacklist",
            "group_ids": [tokens["cq-code"]],
        },
        {
            "mode": tokens["newline-body"],
            "group_ids": [],
        },
        {
            "mode": "blacklist",
            "group_ids": [],
            "credential": tokens["bearer-token"],
        },
    ]

    async with asgi_client(app) as client:
        for body in invalid_bodies:
            response = await client.put(
                POLICY_PATH,
                headers=put_headers(WRITER_TOKEN, if_match='"1"'),
                json=body,
            )
            assert response.status_code == 422
            _scan_http_exchange(
                bundle, response, context="非法 body 422 错误体"
            )

    bundle.assert_no_leaks(
        [event.to_dict() for event in recorder.events],
        context="非法 body 的审计事件",
    )
    assert storage.cas_calls == []


async def test_status_and_success_audit_do_not_leak_group_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """特征群号只允许出现在 GET policy 的 policy.group_ids；status/审计不得回显。"""
    recorder = RecordingAuditRecorder()
    storage = AdmissionStorageFake(stored_policy(1, atomic_policy("blacklist", [])))
    app, _runtime, _manager = await prepare_control_plane(
        monkeypatch, storage, audit_recorder=recorder
    )
    bundle = SensitiveCanaryBundle(
        (
            SensitiveCanaryToken(label="group-77004", value=77004),
            SensitiveCanaryToken(label="group-77005", value=77005),
        )
    )

    async with asgi_client(app) as client:
        put_response = await client.put(
            POLICY_PATH,
            headers=put_headers(WRITER_TOKEN, if_match='"1"'),
            json=atomic_policy("blacklist", [77004, 77005]),
        )
        assert put_response.status_code == 200
        # 成功 PUT 响应同样只在 policy.group_ids 回显
        bundle.assert_no_leaks(
            put_response.json(),
            allowed_path_prefixes=_POLICY_GROUP_IDS_PATH_PREFIXES,
            context="PUT 200 响应体",
        )

        get_response = await client.get(
            POLICY_PATH, headers=auth_headers(READER_TOKEN)
        )
        bundle.assert_no_leaks(
            get_response.json(),
            allowed_path_prefixes=_POLICY_GROUP_IDS_PATH_PREFIXES,
            context="GET 200 响应体",
        )

        status_response = await client.get(
            STATUS_PATH, headers=auth_headers(READER_TOKEN)
        )
        assert status_response.status_code == 200
        bundle.assert_no_leaks(
            {"body": status_response.json(), "headers": dict(status_response.headers)},
            context="status 响应",
        )

    bundle.assert_no_leaks(
        [event.to_dict() for event in recorder.events],
        context="PUT 成功的审计事件",
    )
