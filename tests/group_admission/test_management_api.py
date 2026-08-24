"""TSK-223 阶段 A：``register_group_admission_api`` 管理控制面契约。

验收目标（ADR-0012「管理 HTTP Adapter」+ 本阶段冻结 external seam）：

- 唯一公开装配入口 ``register_group_admission_api(app, *, api_token,
  allowed_origins, audit_recorder=None)``；测试经公共注册 + ASGI 内存客户
  端驱动，不依赖也不暴露生产 ``create_router``；
- GET policy（config:read）强制真实持久刷新（不得只读 cached/LKG），返回
  规范化完整策略 + revision + UTC updated_at，强 ETag 精确 ``"<revision>"``；
  LKG 已建立后持久读取抛错 → 503 ``storage_unavailable``（不以缓存策略冒充
  200），运行时收敛 DEGRADED 并继续按 LKG 裁决；非法持久策略 → 503
  ``stored_policy_invalid``（不回显正文）；
- PUT policy（config:write）提交完整原子策略 + 强 ``If-Match`` + 变更原因；
  先严格校验/规范化，再对 TSK-221 strict CAS 恰好一次调用（无 fetch/retry/
  replay）；成功仅在本地 runtime effective revision 已发布后返回 200 + 新
  ETag + 规范化策略；冲突 409，存储写失败 503（persisted=false，运行时
  DEGRADED 保留 LKG，后续成功刷新可恢复 READY），已持久化未发布 503
  ``snapshot_publish_failed``（persisted=true，runtime 安全收敛 DEGRADED），
  未分类内部错误 500 ``internal_error``（不回显异常）；422/409 不令运行时降级；
- 全部本 Module 409/422/503/500 使用 FastAPI 标准外壳下的精确白名单
  ``{"detail": {"code","message","configured_revision","effective_revision",
  "persisted"}}``，code 取 7 个封闭值，message 固定不拼异常/策略/ID；
  configured/effective 投影响应构造时点运行时的当前修订值（从未启动 →
  None/None）；
- GET status（config:read）从 module singleton 内存读取、零隐藏 I/O，
  ready/degraded/failed 恒 200，不出现 mode/group_ids/policy/fingerprint/
  异常；阶段 B 升级为 TSK-217 完整固定投影（精确键集 + UTC RFC 3339 时间 +
  封闭低基数 telemetry，详见 test_observability_status.py）；
- 管理凭据只影响 API auth，不形成裁决 bypass；审计 started/final 安全
  metadata 精确（旧/新 revision、规范化 SHA-256 指纹、persisted/published、
  closed result code），绝不含策略正文/群号，且 publication success 在
  audit final 观察时 runtime effective 已是新 revision。

生产 ``register_group_admission_api`` 缺失时，装配 helper 抛
``AttributeError``，全部用例红（不使用 hasattr/skip/xfail 绕过）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from tests.group_admission.management_support import (
    ADMIN_TOKEN,
    CLOSED_ERROR_CODES,
    EXPECTED_AUDIT_METADATA_KEYS,
    FROZEN_AUDIT_ACTION,
    FROZEN_AUDIT_RESOURCE,
    POLICY_PATH,
    READER_TOKEN,
    RESULT_CODE_SUCCESS,
    STATUS_PATH,
    WRITER_TOKEN,
    RecordingAuditRecorder,
    asgi_client,
    assert_whitelist_detail,
    atomic_policy,
    auth_headers,
    canonical_policy_fingerprint,
    normalized_policy,
    prepare_control_plane,
    put_headers,
)
from tests.group_admission.observability_support import (
    assert_rfc3339_utc,
    assert_status_exact_shape,
    assert_telemetry_closed_maps,
)
from tests.group_admission.runtime_support import (
    AdmissionStorageFake,
    detach_runtime_listener,
    import_admission_package,
    stored_policy,
)
from tests.group_admission.sensitive_canary import (
    SensitiveCanaryBundle,
    SensitiveCanaryToken,
    build_standard_canary_bundle,
)

pytestmark = pytest.mark.group_admission_acceptance

#: 非法 If-Match 矩阵：缺失 / 弱 / 无引号 / 非数字 / 负数 / 多值 / 空。
INVALID_IF_MATCH_CASES: list[tuple[str | None, str]] = [
    (None, "missing"),
    ('W/"1"', "weak"),
    ("1", "unquoted"),
    ('"abc"', "non-numeric"),
    ('"-1"', "negative"),
    ('"1", "2"', "multi-value"),
    ('""', "empty"),
]

#: 非法 policy body 矩阵（恰好 mode/group_ids 语义的完整反例集）。
INVALID_POLICY_BODIES: list[tuple[dict[str, object], str]] = [
    ({"group_ids": []}, "missing-mode"),
    ({"mode": "blacklist"}, "missing-group-ids"),
    ({}, "empty-object"),
    ({"mode": "blacklist", "group_ids": [], "note": "extra"}, "extra-field"),
    ({"mode": "graylist", "group_ids": []}, "invalid-mode-value"),
    ({"mode": 123, "group_ids": []}, "invalid-mode-type"),
    ({"mode": None, "group_ids": []}, "mode-null"),
    ({"mode": "blacklist", "group_ids": "200"}, "group-ids-string"),
    ({"mode": "blacklist", "group_ids": 200}, "group-ids-int"),
    ({"mode": "blacklist", "group_ids": {"200": True}}, "group-ids-dict"),
    ({"mode": "blacklist", "group_ids": [True]}, "element-bool"),
    ({"mode": "blacklist", "group_ids": [0]}, "element-zero"),
    ({"mode": "blacklist", "group_ids": [-5]}, "element-negative"),
    ({"mode": "blacklist", "group_ids": ["200"]}, "element-string"),
    ({"mode": "blacklist", "group_ids": [1.5]}, "element-float"),
    ({"mode": "blacklist", "group_ids": [None]}, "element-null"),
]

#: status 响应中绝不出现的敏感/越界键（阶段 B 起由精确键集断言承接，此处保留
#: 作为双重防线：即使未来新增字段也不得引入这些键）。
_STATUS_FORBIDDEN_KEYS = frozenset(
    {
        "mode",
        "group_ids",
        "policy",
        "fingerprint",
        "exception",
        "error",
        "detail",
    }
)


def _assert_runtime_ready_at(
    runtime: Any,
    *,
    revision: int,
) -> None:
    """422/409 负向控制：请求被拒不得令运行时降级。"""
    state = runtime.get_state()
    assert state.status.value == "ready", "422/409 不得令运行时降级"
    assert state.problem_code is None
    assert state.configured_revision == revision
    assert state.effective_revision == revision
    assert state.using_last_known_good is False


# ---------------------------------------------------------------------------
# GET policy
# ---------------------------------------------------------------------------


async def test_policy_get_returns_normalized_policy_with_strong_etag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """已鉴权 GET policy 是唯一允许回显策略/群号的面。

    存储中的策略带重复乱序群号；响应必须返回确定性去重排序的规范化策略、
    revision、UTC updated_at，强 ETag 精确 ``"<revision>"``。
    """
    stored = stored_policy(2, {"mode": "blacklist", "group_ids": [300, 100, 300]})
    storage = AdmissionStorageFake(stored)
    app, _runtime, _manager = await prepare_control_plane(monkeypatch, storage)

    async with asgi_client(app) as client:
        response = await client.get(
            POLICY_PATH, headers=auth_headers(READER_TOKEN)
        )

    assert response.status_code == 200, response.text
    assert response.headers["etag"] == '"2"'
    body = response.json()
    assert set(body) == {"policy", "revision", "updated_at"}
    assert body["policy"] == normalized_policy("blacklist", [300, 100, 300])
    assert body["policy"]["group_ids"] == [100, 300], "group_ids 必须去重排序"
    assert body["revision"] == 2
    updated_at = datetime.fromisoformat(body["updated_at"])
    assert updated_at == stored.updated_at
    assert updated_at.tzinfo is not None, "updated_at 必须是带时区的 UTC 时间"


async def test_policy_get_forces_persistent_refresh_on_every_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GET 必须每次请求强制真实持久刷新，不得只读 cached/LKG。

    外部受支持写入（``set_stored``，不经 watcher 投递）后，下一次 GET 必
    须观察到新 revision 与新策略，且存储读取计数增长；只读缓存的实现在此
    红（负向控制：缓存冒充 GET）。
    """
    storage = AdmissionStorageFake(stored_policy(1, atomic_policy("blacklist", [200])))
    app, _runtime, _manager = await prepare_control_plane(monkeypatch, storage)

    async with asgi_client(app) as client:
        storage.set_stored(stored_policy(3, atomic_policy("whitelist", [700])))
        fetch_before = storage.fetch_calls
        first = await client.get(POLICY_PATH, headers=auth_headers(READER_TOKEN))
        assert storage.fetch_calls > fetch_before, "GET 未触发真实存储读取"
        assert first.status_code == 200
        assert first.json()["revision"] == 3
        assert first.json()["policy"] == normalized_policy("whitelist", [700])
        assert first.headers["etag"] == '"3"'

        storage.set_stored(stored_policy(4, atomic_policy("blacklist", [])))
        fetch_before = storage.fetch_calls
        second = await client.get(POLICY_PATH, headers=auth_headers(READER_TOKEN))
        assert storage.fetch_calls > fetch_before, "第二次 GET 未再读存储"
        assert second.json()["revision"] == 4
        assert second.headers["etag"] == '"4"'


async def test_policy_get_storage_failure_returns_503_and_degrades_to_lkg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LKG 已建立后存储不可读：503，运行时 DEGRADED 按 LKG 继续裁决。

    ready@1 后持久读取抛错：响应绝不以缓存策略冒充 200；运行时安全收敛为
    DEGRADED/storage_unavailable（configured=effective 保留 1，
    using_last_known_good=true），裁决继续按 LKG 执行；错误体投影响应构造
    时点运行时的当前修订值 1/1。
    """
    storage = AdmissionStorageFake(stored_policy(1, atomic_policy("blacklist", [200])))
    app, runtime, _manager = await prepare_control_plane(monkeypatch, storage)
    assert runtime.get_state().effective_revision == 1  # LKG 在进程内存在

    storage.fetch_error = RuntimeError("pg connection CANARY-GET-503 refused")
    bundle = build_standard_canary_bundle()

    async with asgi_client(app) as client:
        response = await client.get(
            POLICY_PATH, headers=auth_headers(READER_TOKEN)
        )

    detail = assert_whitelist_detail(
        response,
        503,
        "storage_unavailable",
        configured_revision=1,
        effective_revision=1,
    )
    bundle.assert_no_leaks(detail, context="GET 503 错误体")
    assert "200" not in detail["message"], "错误 message 不得拼群号/策略"

    state = runtime.get_state()
    assert state.status.value == "degraded"
    assert state.problem_code == "storage_unavailable"
    assert state.configured_revision == 1
    assert state.effective_revision == 1
    assert state.using_last_known_good is True

    # 裁决仍按 LKG（blacklist [200]）执行
    admission = import_admission_package()
    restricted = admission.adjudicate([200])
    admitted = admission.adjudicate([300])
    assert restricted.qualification.value == "rejected"
    assert restricted.effective_revision == 1
    assert admitted.qualification.value == "business"


async def test_policy_get_invalid_stored_policy_returns_503_without_echo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """读到非法 persisted policy：503 stored_policy_invalid，不冒充 LKG/不回显。"""
    invalid_policy = {"mode": "graylist", "group_ids": [777001]}
    storage = AdmissionStorageFake(stored_policy(1, invalid_policy))
    app, _runtime, _manager = await prepare_control_plane(monkeypatch, storage)

    async with asgi_client(app) as client:
        response = await client.get(
            POLICY_PATH, headers=auth_headers(READER_TOKEN)
        )

    detail = assert_whitelist_detail(
        response,
        503,
        "stored_policy_invalid",
        configured_revision=1,
        effective_revision=None,
    )
    group_echo_bundle = SensitiveCanaryBundle(
        (SensitiveCanaryToken(label="group-777001", value=777001),)
    )
    group_echo_bundle.assert_no_leaks(
        detail, context="非法持久策略 503 错误体"
    )
    raw_text = response.text
    assert "777001" not in raw_text, "不得回显非法策略群号"
    assert "graylist" not in raw_text, "不得回显非法策略正文"


# ---------------------------------------------------------------------------
# PUT policy：成功路径与 strict CAS exactly-once
# ---------------------------------------------------------------------------


async def test_put_success_persists_exactly_one_strict_cas_and_publishes_before_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """成功 PUT：恰好一次严格 CAS、无 fetch/replay，响应前本地已发布。

    - 提交乱序重复群号的完整策略，响应返回规范化策略 + 新 ETag；
    - ``cas_calls`` 恰为 1 且入参为 If-Match revision 与 ``{"policy"}`` 字段集；
    - PUT 全程存储读取计数不变（无 fetch/retry/replay，负向控制：CAS 重试）；
    - 响应返回时 module singleton 运行时 effective 已是新 revision。
    """
    recorder = RecordingAuditRecorder()
    storage = AdmissionStorageFake(stored_policy(1, atomic_policy("blacklist", [200])))
    app, _runtime, _manager = await prepare_control_plane(
        monkeypatch, storage, audit_recorder=recorder
    )
    old_fingerprint = canonical_policy_fingerprint(
        normalized_policy("blacklist", [200])
    )

    async with asgi_client(app) as client:
        fetch_before = storage.fetch_calls
        response = await client.put(
            POLICY_PATH,
            headers=put_headers(WRITER_TOKEN, if_match='"1"'),
            json=atomic_policy("whitelist", [900, 800, 900]),
        )
        assert storage.fetch_calls == fetch_before, "PUT 不得额外读取存储"

    assert response.status_code == 200, response.text
    assert response.headers["etag"] == '"2"'
    body = response.json()
    assert set(body) == {"policy", "revision", "updated_at"}
    assert body["policy"] == normalized_policy("whitelist", [900, 800, 900])
    assert body["revision"] == 2

    assert len(storage.cas_calls) == 1, "strict CAS 必须恰好一次"
    cas = storage.cas_calls[0]
    assert cas.expected_revision == 1
    assert cas.field_names == frozenset({"policy"})
    assert cas.plugin_name == "group_admission"
    assert cas.config_dump["policy"] == normalized_policy("whitelist", [900, 800, 900])
    assert storage.current_revision == 2

    admission = import_admission_package()
    state = admission.get_runtime_state()
    assert state.effective_revision == 2, "响应完成前本地必须已发布新 revision"
    assert state.configured_revision == 2
    assert state.status.value == "ready"

    # 审计 final 观察时 runtime 已发布（本地生效先于响应/审计完成）
    final_events = recorder.final_events()
    assert len(final_events) == 1
    assert recorder.runtime_state_observations[-1].effective_revision == 2

    final = final_events[0]
    assert final.outcome == "succeeded"
    assert final.status_code == 200
    assert set(final.metadata) == EXPECTED_AUDIT_METADATA_KEYS
    assert final.metadata["old_revision"] == 1
    assert final.metadata["new_revision"] == 2
    assert final.metadata["old_policy_fingerprint"] == old_fingerprint
    assert final.metadata["new_policy_fingerprint"] == canonical_policy_fingerprint(
        normalized_policy("whitelist", [900, 800, 900])
    )
    assert final.metadata["persisted"] is True
    assert final.metadata["published"] is True
    assert final.metadata["result_code"] == RESULT_CODE_SUCCESS


async def test_put_audit_fingerprint_matches_shared_canonical_for_multigroup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC3：多群号策略的管理审计指纹必须与 CLI 共享 canonical 指纹一致。

    当前管理 ``_policy_fingerprint`` 取规范化（升序）形态摘要，而 CLI 共享
    ``policy_fingerprint`` 取去重降序形态摘要 → 多群号分叉（红）。
    """
    from komari_bot.admission_policy import policy_fingerprint as shared_fingerprint

    recorder = RecordingAuditRecorder()
    storage = AdmissionStorageFake(stored_policy(1, atomic_policy("blacklist", [200])))
    app, _runtime, _manager = await prepare_control_plane(
        monkeypatch,
        storage,
        audit_recorder=recorder,
    )
    raw = {"mode": "whitelist", "group_ids": [900, 800, 900, 700]}

    async with asgi_client(app) as client:
        response = await client.put(
            POLICY_PATH,
            headers=put_headers(WRITER_TOKEN, if_match='"1"'),
            json=raw,
        )
        assert response.status_code == 200, response.text

    final_events = recorder.final_events()
    assert len(final_events) == 1
    final = final_events[0]
    assert final.outcome == "succeeded"
    assert final.metadata["new_policy_fingerprint"] == shared_fingerprint(raw), (
        "审计 new_policy_fingerprint 必须与 CLI 共享 canonical 指纹一致"
    )


# ---------------------------------------------------------------------------
# PUT policy：If-Match 与 body 校验矩阵（422，不触存储）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("if_match", "case_id"),
    INVALID_IF_MATCH_CASES,
    ids=[case_id for _value, case_id in INVALID_IF_MATCH_CASES],
)
async def test_put_if_match_violations_return_422_invalid_if_match(
    monkeypatch: pytest.MonkeyPatch,
    if_match: str | None,
    case_id: str,
) -> None:
    """If-Match 必须是有双引号的强 ETag；矩阵违规一律 422 且不触存储。

    错误体投影 ready@1 运行时的当前修订值 1/1；422 不令运行时降级。
    """
    del case_id
    storage = AdmissionStorageFake(stored_policy(1, atomic_policy("blacklist", [200])))
    app, runtime, _manager = await prepare_control_plane(monkeypatch, storage)

    async with asgi_client(app) as client:
        fetch_before = storage.fetch_calls
        response = await client.put(
            POLICY_PATH,
            headers=put_headers(WRITER_TOKEN, if_match=if_match),
            json=atomic_policy("blacklist", [300]),
        )

    assert storage.cas_calls == [], "If-Match 非法时不得写存储"
    assert storage.fetch_calls == fetch_before
    assert storage.current_revision == 1
    assert_whitelist_detail(
        response,
        422,
        "invalid_if_match",
        configured_revision=1,
        effective_revision=1,
    )
    _assert_runtime_ready_at(runtime, revision=1)


@pytest.mark.parametrize(
    "invalid_body",
    [body for body, _case_id in INVALID_POLICY_BODIES],
    ids=[case_id for _body, case_id in INVALID_POLICY_BODIES],
)
async def test_put_invalid_policy_matrix_returns_422_without_storage_write(
    monkeypatch: pytest.MonkeyPatch,
    invalid_body: dict[str, object],
) -> None:
    """非法 policy 矩阵：先校验后 CAS，422 invalid_policy 且不触存储。

    错误体投影 ready@1 运行时的当前修订值 1/1；422 不令运行时降级。
    """
    storage = AdmissionStorageFake(stored_policy(1, atomic_policy("blacklist", [200])))
    app, runtime, _manager = await prepare_control_plane(monkeypatch, storage)

    async with asgi_client(app) as client:
        fetch_before = storage.fetch_calls
        response = await client.put(
            POLICY_PATH,
            headers=put_headers(WRITER_TOKEN, if_match='"1"'),
            json=invalid_body,
        )

    assert storage.cas_calls == [], "非法 policy 不得写存储"
    assert storage.fetch_calls == fetch_before
    assert storage.current_revision == 1
    assert_whitelist_detail(
        response,
        422,
        "invalid_policy",
        configured_revision=1,
        effective_revision=1,
    )
    _assert_runtime_ready_at(runtime, revision=1)


async def test_put_non_object_body_returns_422_invalid_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """非对象 body 同样是非法 policy：白名单 422（框架校验错误必须归一）。"""
    storage = AdmissionStorageFake(stored_policy(1, atomic_policy("blacklist", [200])))
    app, runtime, _manager = await prepare_control_plane(monkeypatch, storage)

    async with asgi_client(app) as client:
        response = await client.put(
            POLICY_PATH,
            headers=put_headers(WRITER_TOKEN, if_match='"1"'),
            json=[1, 2],
        )

    assert storage.cas_calls == []
    assert_whitelist_detail(
        response,
        422,
        "invalid_policy",
        configured_revision=1,
        effective_revision=1,
    )
    _assert_runtime_ready_at(runtime, revision=1)


# ---------------------------------------------------------------------------
# PUT policy：冲突 / 存储失败 / 已持久化未发布 / 内部错误
# ---------------------------------------------------------------------------


async def test_put_revision_conflict_returns_409(monkeypatch: pytest.MonkeyPatch) -> None:
    """CAS 返回 None：409 revision_conflict，恰好一次 CAS、无重试。

    错误体投影 ready@1 运行时的当前修订值 1/1；冲突不令运行时降级。
    """
    storage = AdmissionStorageFake(stored_policy(1, atomic_policy("blacklist", [200])))
    app, runtime, _manager = await prepare_control_plane(monkeypatch, storage)

    async with asgi_client(app) as client:
        fetch_before = storage.fetch_calls
        response = await client.put(
            POLICY_PATH,
            headers=put_headers(WRITER_TOKEN, if_match='"9"'),
            json=atomic_policy("blacklist", [300]),
        )

    assert_whitelist_detail(
        response,
        409,
        "revision_conflict",
        configured_revision=1,
        effective_revision=1,
    )
    assert len(storage.cas_calls) == 1, "冲突也必须恰好一次 CAS（不重试）"
    assert storage.cas_calls[0].expected_revision == 9
    assert storage.fetch_calls == fetch_before
    assert storage.current_revision == 1, "冲突不得改变存储"
    _assert_runtime_ready_at(runtime, revision=1)


async def test_put_storage_write_failure_returns_503_not_persisted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """存储写异常：503，运行时 DEGRADED 保留 LKG，存储未变。

    ready@1/LKG 已建立后严格 CAS 抛错：响应 503 storage_unavailable
    （persisted=false）且投影当前修订值 1/1；运行时安全收敛为
    DEGRADED/storage_unavailable（configured=effective 保留 1，
    using_last_known_good=true），裁决继续按 LKG 执行。
    """
    storage = AdmissionStorageFake(stored_policy(1, atomic_policy("blacklist", [200])))
    app, runtime, _manager = await prepare_control_plane(monkeypatch, storage)
    storage.cas_error = RuntimeError("pg write CANARY-CAS-77aa failure")
    bundle = build_standard_canary_bundle()

    async with asgi_client(app) as client:
        response = await client.put(
            POLICY_PATH,
            headers=put_headers(WRITER_TOKEN, if_match='"1"'),
            json=atomic_policy("whitelist", [700]),
        )

    detail = assert_whitelist_detail(
        response,
        503,
        "storage_unavailable",
        configured_revision=1,
        effective_revision=1,
    )
    bundle.assert_no_leaks(detail, context="PUT 503 错误体")
    assert storage.current_revision == 1, "写失败不得改变存储"
    assert len(storage.cas_calls) == 1

    state = runtime.get_state()
    assert state.status.value == "degraded"
    assert state.problem_code == "storage_unavailable"
    assert state.configured_revision == 1
    assert state.effective_revision == 1
    assert state.using_last_known_good is True

    # 裁决仍按 LKG（blacklist [200]）执行
    admission = import_admission_package()
    restricted = admission.adjudicate([200])
    admitted = admission.adjudicate([300])
    assert restricted.qualification.value == "rejected"
    assert restricted.effective_revision == 1
    assert admitted.qualification.value == "business"


async def test_runtime_recovers_ready_after_successful_refresh_following_write_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """写失败降级后，相同合法修订的成功持久刷新立即恢复 READY。

    PUT 严格 CAS 写异常使运行时 DEGRADED/storage_unavailable（LKG 1/1）；
    存储恢复后，下一次 GET 强制刷新成功读到相同合法修订 1：运行时 status
    立即回到 READY（1/1，using_last_known_good=false）并继续正常裁决。
    完整恢复告警与故障 episode 簿记归阶段 B，本处不冻结。
    """
    storage = AdmissionStorageFake(stored_policy(1, atomic_policy("blacklist", [200])))
    app, runtime, _manager = await prepare_control_plane(monkeypatch, storage)
    storage.cas_error = RuntimeError("pg write failure before recovery")

    async with asgi_client(app) as client:
        failed = await client.put(
            POLICY_PATH,
            headers=put_headers(WRITER_TOKEN, if_match='"1"'),
            json=atomic_policy("whitelist", [700]),
        )
        assert failed.status_code == 503
        degraded = runtime.get_state()
        assert degraded.status.value == "degraded"
        assert degraded.problem_code == "storage_unavailable"
        assert degraded.configured_revision == 1
        assert degraded.effective_revision == 1
        assert degraded.using_last_known_good is True

        storage.cas_error = None
        refreshed = await client.get(
            POLICY_PATH, headers=auth_headers(READER_TOKEN)
        )

    assert refreshed.status_code == 200, refreshed.text
    assert refreshed.json()["revision"] == 1

    state = runtime.get_state()
    assert state.status.value == "ready", "成功刷新必须立即恢复 READY"
    assert state.problem_code is None
    assert state.configured_revision == 1
    assert state.effective_revision == 1
    assert state.using_last_known_good is False

    admission = import_admission_package()
    restricted = admission.adjudicate([200])
    admitted = admission.adjudicate([300])
    assert restricted.qualification.value == "rejected"
    assert admitted.qualification.value == "business"


async def test_put_persisted_but_unpublished_returns_503_and_runtime_degrades(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """已持久化但 listener 未发布：503 snapshot_publish_failed，persisted=true。

    注销运行时自己的快照 listener 后严格 CAS 成功：响应必须携带
    configured/effective revision 且运行时安全收敛为 DEGRADED（LKG 保留，
    configured=新/effective=旧）；裁决继续按 LKG 执行（负向控制：persisted
    未 publish 误报 200）。
    """
    storage = AdmissionStorageFake(stored_policy(1, atomic_policy("blacklist", [200])))
    app, runtime, manager = await prepare_control_plane(monkeypatch, storage)
    detach_runtime_listener(manager, runtime)

    async with asgi_client(app) as client:
        response = await client.put(
            POLICY_PATH,
            headers=put_headers(WRITER_TOKEN, if_match='"1"'),
            json=atomic_policy("whitelist", [700]),
        )

    assert_whitelist_detail(
        response,
        503,
        "snapshot_publish_failed",
        configured_revision=2,
        effective_revision=1,
        persisted=True,
    )

    assert storage.current_revision == 2, "持久化必须已真实发生"

    admission = import_admission_package()
    state = admission.get_runtime_state()
    assert state.status.value == "degraded"
    assert state.problem_code == "snapshot_publish_failed"
    assert state.configured_revision == 2
    assert state.effective_revision == 1
    assert state.using_last_known_good is True

    # 裁决仍按 LKG（blacklist [200]）执行
    restricted = admission.adjudicate([200])
    admitted = admission.adjudicate([300])
    assert restricted.qualification.value == "rejected"
    assert restricted.effective_revision == 1
    assert admitted.qualification.value == "business"


async def test_put_missing_change_reason_returns_shared_400(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PUT 要求变更原因；共享依赖既有错误格式不在白名单改造范围。"""
    storage = AdmissionStorageFake(stored_policy(1, atomic_policy("blacklist", [200])))
    app, _runtime, _manager = await prepare_control_plane(monkeypatch, storage)

    async with asgi_client(app) as client:
        response = await client.put(
            POLICY_PATH,
            headers=put_headers(WRITER_TOKEN, if_match='"1"', change_reason=None),
            json=atomic_policy("blacklist", [300]),
        )

    assert response.status_code == 400
    assert response.json()["detail"] == "写操作必须提供 X-Komari-Change-Reason"
    assert storage.cas_calls == [], "缺失变更原因不得写存储"


class _RaisingAuditRecorder:
    """首个事件即抛未分类异常的审计 recorder（internal_error 触发器）。"""

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, event: Any) -> None:
        del event
        self.calls += 1
        msg = "audit sink CANARY-INTERNAL-9d4e unavailable"
        raise RuntimeError(msg)


async def test_put_unclassified_internal_error_returns_500_without_echo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """未分类内部错误：500 internal_error，白名单默认值，不回显异常。"""
    recorder = _RaisingAuditRecorder()
    storage = AdmissionStorageFake(stored_policy(1, atomic_policy("blacklist", [200])))
    app, _runtime, _manager = await prepare_control_plane(
        monkeypatch, storage, audit_recorder=recorder
    )
    bundle = build_standard_canary_bundle()

    async with asgi_client(app) as client:
        response = await client.put(
            POLICY_PATH,
            headers=put_headers(WRITER_TOKEN, if_match='"1"'),
            json=atomic_policy("whitelist", [700]),
        )

    detail = assert_whitelist_detail(
        response,
        500,
        "internal_error",
        configured_revision=1,
        effective_revision=1,
    )
    bundle.assert_no_leaks(detail, context="500 错误体")
    assert "CANARY" not in response.text
    assert storage.cas_calls == [], "审计启动失败时不得执行 CAS"


# ---------------------------------------------------------------------------
# 鉴权边界：凭据只影响 API auth，不形成裁决 bypass
# ---------------------------------------------------------------------------


async def test_endpoint_auth_boundaries_read_write_and_wildcard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GET/status 要求 config:read；PUT 要求 config:write；401/403 用共享格式。"""
    storage = AdmissionStorageFake(stored_policy(1, atomic_policy("blacklist", [200])))
    app, _runtime, _manager = await prepare_control_plane(monkeypatch, storage)
    valid_put_body = atomic_policy("blacklist", [300])

    async with asgi_client(app) as client:
        # 无凭据 / 错误凭据 → 401（三个端点）
        for path in (POLICY_PATH, STATUS_PATH):
            assert (await client.get(path)).status_code == 401
        assert (
            await client.get(POLICY_PATH, headers=auth_headers("wrong-token-000000"))
        ).status_code == 401
        assert (
            await client.put(
                POLICY_PATH,
                headers=put_headers("wrong-token-000000", if_match='"1"'),
                json=valid_put_body,
            )
        ).status_code == 401

        # 只读凭据：GET/status 200，PUT 403（共享格式）
        reader_get = await client.get(
            POLICY_PATH, headers=auth_headers(READER_TOKEN)
        )
        reader_status = await client.get(
            STATUS_PATH, headers=auth_headers(READER_TOKEN)
        )
        reader_put = await client.put(
            POLICY_PATH,
            headers=put_headers(READER_TOKEN, if_match='"1"'),
            json=valid_put_body,
        )
        assert reader_get.status_code == 200
        assert reader_status.status_code == 200
        assert reader_put.status_code == 403
        assert reader_put.json()["detail"] == "当前管理凭据没有所需权限"
        assert storage.cas_calls == []

        # 写凭据：PUT 200；config:write 蕴含 config:read → GET 200
        writer_put = await client.put(
            POLICY_PATH,
            headers=put_headers(WRITER_TOKEN, if_match='"1"'),
            json=valid_put_body,
        )
        writer_get = await client.get(
            POLICY_PATH, headers=auth_headers(WRITER_TOKEN)
        )
        assert writer_put.status_code == 200
        assert writer_get.status_code == 200

        # 通配凭据：全部端点可用
        admin_status = await client.get(
            STATUS_PATH, headers=auth_headers(ADMIN_TOKEN)
        )
        assert admin_status.status_code == 200


async def test_credentials_do_not_form_adjudication_bypass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """任何凭据（含通配）只影响 API auth；裁决仍严格按当前策略执行。"""
    storage = AdmissionStorageFake(stored_policy(1, atomic_policy("blacklist", [])))
    app, _runtime, _manager = await prepare_control_plane(monkeypatch, storage)

    async with asgi_client(app) as client:
        response = await client.put(
            POLICY_PATH,
            headers=put_headers(ADMIN_TOKEN, if_match='"1"'),
            json=atomic_policy("blacklist", [200]),
        )
    assert response.status_code == 200

    admission = import_admission_package()
    restricted = admission.adjudicate([200])
    admitted = admission.adjudicate([300])
    assert restricted.qualification.value == "rejected"
    assert restricted.reason_code == "policy_restricted"
    assert admitted.qualification.value == "business"


# ---------------------------------------------------------------------------
# GET status：module singleton 内存投影，零隐藏 I/O
# ---------------------------------------------------------------------------


async def test_status_ready_projects_singleton_state_with_zero_storage_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ready 状态 200；存储全断裂仍 200，证明零隐藏 I/O。

    响应投影 TSK-217 完整固定字段集（阶段 B 升级）：5 个运行时字段 +
    5 个 UTC RFC 3339 时间（未发生为 null）+ telemetry（封闭低基数键集）；
    精确键集排除 mode/group_ids/policy/fingerprint/异常/重试计划。
    """
    storage = AdmissionStorageFake(stored_policy(1, atomic_policy("blacklist", [200])))
    app, runtime, _manager = await prepare_control_plane(monkeypatch, storage)
    expected = runtime.get_state()

    storage.break_all(RuntimeError("all storage io CANARY-IO-4be1 forbidden"))

    async with asgi_client(app) as client:
        fetch_before = storage.fetch_calls
        cas_before = len(storage.cas_calls)
        response = await client.get(STATUS_PATH, headers=auth_headers(READER_TOKEN))

    assert response.status_code == 200, response.text
    assert storage.fetch_calls == fetch_before, "status 触发存储读取"
    assert len(storage.cas_calls) == cas_before, "status 触发存储写入"

    body = response.json()
    assert_status_exact_shape(body)
    leaked_keys = _STATUS_FORBIDDEN_KEYS & set(body)
    assert leaked_keys == set(), f"status 出现越界键: {leaked_keys}"
    assert body["status"] == expected.status.value == "ready"
    assert body["problem_code"] == expected.problem_code is None
    assert body["configured_revision"] == expected.configured_revision == 1
    assert body["effective_revision"] == expected.effective_revision == 1
    assert body["using_last_known_good"] == expected.using_last_known_good is False

    # ready：全部时间锚点已有值，问题时间为 null
    assert_rfc3339_utc(
        body["configured_updated_at"], field_name="configured_updated_at"
    )
    assert_rfc3339_utc(body["effective_loaded_at"], field_name="effective_loaded_at")
    assert_rfc3339_utc(
        body["last_refresh_attempt_at"], field_name="last_refresh_attempt_at"
    )
    assert_rfc3339_utc(
        body["last_storage_success_at"], field_name="last_storage_success_at"
    )
    assert body["problem_since"] is None

    telemetry = body["telemetry"]
    assert_rfc3339_utc(telemetry["started_at"], field_name="telemetry.started_at")
    assert_telemetry_closed_maps(telemetry, total=0)

    build_standard_canary_bundle().assert_no_leaks(
        body, context="status 响应体"
    )


@pytest.mark.parametrize("scenario", ["degraded", "failed"])
async def test_status_degraded_and_failed_still_return_200(
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
) -> None:
    """degraded/failed 同样恒 200，投影运行时真实状态与完整时间锚点。"""
    if scenario == "degraded":
        storage = AdmissionStorageFake(
            stored_policy(1, atomic_policy("blacklist", [200]))
        )
        app, runtime, _manager = await prepare_control_plane(monkeypatch, storage)
        storage.deliver(stored_policy(2, {"mode": "graylist", "group_ids": []}))
    else:
        storage = AdmissionStorageFake(
            fetch_error=RuntimeError("pg unavailable at cold start")
        )
        app, runtime, _manager = await prepare_control_plane(monkeypatch, storage)
    expected = runtime.get_state()

    async with asgi_client(app) as client:
        response = await client.get(STATUS_PATH, headers=auth_headers(READER_TOKEN))

    assert response.status_code == 200, response.text
    body = response.json()
    assert_status_exact_shape(body)
    assert body["status"] == expected.status.value == scenario
    assert body["problem_code"] == expected.problem_code
    assert body["configured_revision"] == expected.configured_revision
    assert body["effective_revision"] == expected.effective_revision
    assert body["using_last_known_good"] == expected.using_last_known_good
    if scenario == "degraded":
        assert body["problem_code"] == "stored_policy_invalid"
        assert body["configured_revision"] == 2
        assert body["effective_revision"] == 1
        assert body["using_last_known_good"] is True
        # LKG 建立后降级：时间锚点全部保留，problem_since 置位
        assert_rfc3339_utc(
            body["configured_updated_at"], field_name="configured_updated_at"
        )
        assert_rfc3339_utc(
            body["effective_loaded_at"], field_name="effective_loaded_at"
        )
        assert_rfc3339_utc(
            body["last_refresh_attempt_at"], field_name="last_refresh_attempt_at"
        )
        assert_rfc3339_utc(
            body["last_storage_success_at"], field_name="last_storage_success_at"
        )
        assert_rfc3339_utc(body["problem_since"], field_name="problem_since")
        telemetry = body["telemetry"]
        assert telemetry["runtime_problem_occurrences"]["stored_policy_invalid"] == 1
    else:
        assert body["problem_code"] == "storage_unavailable"
        assert body["configured_revision"] is None
        assert body["effective_revision"] is None
        # 冷启动失败：尝试过读取但从未成功，无快照时间；problem_since 置位
        assert_rfc3339_utc(
            body["last_refresh_attempt_at"], field_name="last_refresh_attempt_at"
        )
        assert body["last_storage_success_at"] is None
        assert body["effective_loaded_at"] is None
        assert body["configured_updated_at"] is None
        assert_rfc3339_utc(body["problem_since"], field_name="problem_since")
        telemetry = body["telemetry"]
        assert telemetry["runtime_problem_occurrences"]["storage_unavailable"] == 1
    assert_telemetry_closed_maps(body["telemetry"])


# ---------------------------------------------------------------------------
# 审计安全字段与 closed result code
# ---------------------------------------------------------------------------


async def test_put_success_audit_metadata_exact_safe_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """成功 PUT：started/final 两阶段事件；安全 metadata 精确、无正文/群号。"""
    recorder = RecordingAuditRecorder()
    storage = AdmissionStorageFake(stored_policy(1, atomic_policy("blacklist", [200])))
    app, _runtime, _manager = await prepare_control_plane(
        monkeypatch, storage, audit_recorder=recorder
    )

    async with asgi_client(app) as client:
        response = await client.put(
            POLICY_PATH,
            headers=put_headers(
                WRITER_TOKEN,
                if_match='"1"',
                request_id="ga-audit-success",
                change_reason="audit-success-reason",
            ),
            json=atomic_policy("whitelist", [77003]),
        )
    assert response.status_code == 200

    assert [event.outcome for event in recorder.events] == ["started", "succeeded"]
    started, final = recorder.events
    for event in (started, final):
        assert event.action == FROZEN_AUDIT_ACTION
        assert event.resource == FROZEN_AUDIT_RESOURCE
        assert event.operator_id == "writer"
        assert event.request_id == "ga-audit-success"
        assert event.reason == "audit-success-reason"

    assert set(final.metadata) == EXPECTED_AUDIT_METADATA_KEYS
    assert final.metadata["old_revision"] == 1
    assert final.metadata["new_revision"] == 2
    assert final.metadata["old_policy_fingerprint"] == canonical_policy_fingerprint(
        normalized_policy("blacklist", [200])
    )
    assert final.metadata["new_policy_fingerprint"] == canonical_policy_fingerprint(
        normalized_policy("whitelist", [77003])
    )
    assert final.metadata["persisted"] is True
    assert final.metadata["published"] is True
    assert final.metadata["result_code"] == RESULT_CODE_SUCCESS

    serialized = str([event.to_dict() for event in recorder.events])
    assert "77003" not in serialized, "审计不得记录群号明文"
    assert "whitelist" not in serialized, "审计不得记录策略正文"
    assert "blacklist" not in serialized, "审计不得记录策略正文"


@pytest.mark.parametrize(
    ("scenario", "expected_code"),
    [
        ("conflict", "revision_conflict"),
        ("storage-failure", "storage_unavailable"),
        ("unpublished", "snapshot_publish_failed"),
    ],
    ids=["conflict", "storage-failure", "unpublished"],
)
async def test_put_audit_final_result_codes(
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
    expected_code: str,
) -> None:
    """conflict / storage fail / persisted-unpublished 的审计 final 安全字段。"""
    recorder = RecordingAuditRecorder()
    storage = AdmissionStorageFake(stored_policy(1, atomic_policy("blacklist", [200])))
    app, runtime, manager = await prepare_control_plane(
        monkeypatch, storage, audit_recorder=recorder
    )

    if_match = '"1"'
    if scenario == "conflict":
        if_match = '"9"'
    elif scenario == "storage-failure":
        storage.cas_error = RuntimeError("pg write unavailable")
    else:
        detach_runtime_listener(manager, runtime)

    async with asgi_client(app) as client:
        await client.put(
            POLICY_PATH,
            headers=put_headers(WRITER_TOKEN, if_match=if_match),
            json=atomic_policy("whitelist", [77009]),
        )

    final_events = recorder.final_events()
    assert len(final_events) == 1, f"{scenario} 必须恰有一个 final 审计事件"
    final = final_events[0]
    assert final.outcome == "failed"
    assert set(final.metadata) == EXPECTED_AUDIT_METADATA_KEYS
    assert final.metadata["result_code"] == expected_code
    assert final.metadata["old_revision"] == 1
    assert final.metadata["old_policy_fingerprint"] == canonical_policy_fingerprint(
        normalized_policy("blacklist", [200])
    )
    assert final.metadata["new_policy_fingerprint"] == canonical_policy_fingerprint(
        normalized_policy("whitelist", [77009])
    )
    if scenario == "conflict":
        assert final.metadata["new_revision"] is None
        assert final.metadata["persisted"] is False
        assert final.metadata["published"] is False
        assert final.status_code == 409
    elif scenario == "storage-failure":
        assert final.metadata["new_revision"] is None
        assert final.metadata["persisted"] is False
        assert final.metadata["published"] is False
        assert final.status_code == 503
    else:
        assert final.metadata["new_revision"] == 2
        assert final.metadata["persisted"] is True
        assert final.metadata["published"] is False
        assert final.status_code == 503
        assert runtime.get_state().problem_code == "snapshot_publish_failed"

    serialized = str([event.to_dict() for event in recorder.events])
    assert "77009" not in serialized, "审计不得记录群号明文"
    assert "whitelist" not in serialized, "审计不得记录策略正文"


async def test_put_old_cached_policy_invalid_keeps_safe_empty_fingerprint_and_enters_audit_span(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """旧缓存策略非法：PUT 不泄露非白名单 500，仍进入审计 span（PhaseA F1）。

    PUT 计算 old cached policy fingerprint 时若旧策略非法，``_normalize_policy``
    会抛 ``PolicyCompilationError``。修复前该异常在审计 span 之前逃逸，成为
    泄露原异常的非白名单 500，且审计 span 根本不被进入。修复后：

    - ``old_policy_fingerprint`` 保持安全空值，不写入旧策略正文；
    - 请求仍正常推进（新合法策略经严格 CAS 写入并本地发布，返回 200），不泄露
      原始异常为非白名单 500；
    - 审计 span 被正常进入，记录 old_revision / 安全空值旧指纹 / 新指纹。
    """
    recorder = RecordingAuditRecorder()
    # 初始持久化一条非法策略（mode 非法），rev 1。
    storage = AdmissionStorageFake(
        stored_policy(1, {"mode": "graylist", "group_ids": [200]})
    )
    app, _runtime, _manager = await prepare_control_plane(
        monkeypatch, storage, audit_recorder=recorder
    )

    async with asgi_client(app) as client:
        response = await client.put(
            POLICY_PATH,
            headers=put_headers(WRITER_TOKEN, if_match='"1"'),
            json=atomic_policy("whitelist", [77011]),
        )

    # 修复后请求正常推进：新合法策略经严格 CAS 写入并本地发布，返回 200，
    # 绝不泄露原始 PolicyCompilationError 为非白名单 500。
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"policy", "revision", "updated_at"}
    assert body["policy"] == normalized_policy("whitelist", [77011])
    assert body["revision"] == 2

    # 审计 span 必须被进入；旧指纹为安全空值，新指纹为合法新策略指纹。
    final_events = recorder.final_events()
    assert len(final_events) == 1, "旧策略非法时也必须进入审计 span"
    final = final_events[0]
    assert set(final.metadata) == EXPECTED_AUDIT_METADATA_KEYS
    assert final.metadata["old_revision"] == 1
    assert final.metadata["old_policy_fingerprint"] == "", (
        "旧策略非法时 old_policy_fingerprint 必须保持安全空值"
    )
    assert final.metadata["new_policy_fingerprint"] == canonical_policy_fingerprint(
        normalized_policy("whitelist", [77011])
    )
    assert final.metadata["result_code"] == RESULT_CODE_SUCCESS
    assert final.metadata["persisted"] is True
    assert final.metadata["published"] is True
    assert final.metadata["new_revision"] == 2

    # canary：非法旧策略的 mode（graylist）与群号（200）绝不回显。响应体只含
    # 新合法策略（whitelist/77011），审计只记录 revision 与安全规范化指纹
    # （old_policy_fingerprint 为空、new_policy_fingerprint 为哈希），不泄露
    # 旧策略正文；审计同样不回显新策略正文（只记指纹）。
    assert "graylist" not in response.text, "响应不得回显旧非法策略 mode"
    assert "200" not in response.text, "响应不得回显旧非法群号"
    serialized = str([event.to_dict() for event in recorder.events])
    assert "graylist" not in serialized, "审计不得记录旧非法策略正文"
    assert "whitelist" not in serialized, "审计不得记录新策略正文（仅指纹）"


# ---------------------------------------------------------------------------
# 错误 message 固定性（不拼异常/策略/revision/ID）
# ---------------------------------------------------------------------------


async def test_error_messages_are_fixed_and_do_not_embed_dynamic_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同一 code 的 message 在不同触发载荷/不同 revision 下恒等。

    负向控制（错误泄漏/动态拼接）：恶意异常正文、不同群号、不同 revision
    都不得出现在 message 中；message 恒定证明不可拼接动态值。
    """
    messages: dict[str, list[str]] = {}

    def _record(code: str, detail: dict[str, Any]) -> None:
        messages.setdefault(code, []).append(detail["message"])

    # storage_unavailable：GET 两个不同 revision + 不同恶意异常；PUT 写失败。
    # 错误体投影各自主体的当前修订值：ready@N 降级后仍为 N/N。
    for revision, canary in (
        (1, "alpha CANARY-A1 https://canary.example/a"),
        (3, "beta CANARY-B2 [CQ:image,file=b.jpg]"),
    ):
        storage = AdmissionStorageFake(
            stored_policy(revision, atomic_policy("blacklist", [200]))
        )
        app, _runtime, _manager = await prepare_control_plane(monkeypatch, storage)
        storage.fetch_error = RuntimeError(canary)
        async with asgi_client(app) as client:
            response = await client.get(
                POLICY_PATH, headers=auth_headers(READER_TOKEN)
            )
        _record(
            "storage_unavailable",
            assert_whitelist_detail(
                response,
                503,
                "storage_unavailable",
                configured_revision=revision,
                effective_revision=revision,
            ),
        )

    storage = AdmissionStorageFake(stored_policy(1, atomic_policy("blacklist", [200])))
    app, _runtime, _manager = await prepare_control_plane(monkeypatch, storage)
    storage.cas_error = RuntimeError("gamma CANARY-C3 write failure")
    async with asgi_client(app) as client:
        response = await client.put(
            POLICY_PATH,
            headers=put_headers(WRITER_TOKEN, if_match='"1"'),
            json=atomic_policy("whitelist", [700]),
        )
    _record(
        "storage_unavailable",
        assert_whitelist_detail(
            response,
            503,
            "storage_unavailable",
            configured_revision=1,
            effective_revision=1,
        ),
    )

    # invalid_policy：两种不同非法 body；ready@1 投影 1/1。
    storage = AdmissionStorageFake(stored_policy(1, atomic_policy("blacklist", [200])))
    app, _runtime, _manager = await prepare_control_plane(monkeypatch, storage)
    async with asgi_client(app) as client:
        for body in (
            {"mode": "graylist", "group_ids": [111]},
            {"mode": "blacklist", "group_ids": ["CANARY-D4"], "x": 1},
        ):
            response = await client.put(
                POLICY_PATH,
                headers=put_headers(WRITER_TOKEN, if_match='"1"'),
                json=body,
            )
            _record(
                "invalid_policy",
                assert_whitelist_detail(
                    response,
                    422,
                    "invalid_policy",
                    configured_revision=1,
                    effective_revision=1,
                ),
            )

    # invalid_if_match：两种不同非法头部；ready@1 投影 1/1。
    async with asgi_client(app) as client:
        for bad_if_match in ('W/"5"', "not-an-etag"):
            response = await client.put(
                POLICY_PATH,
                headers=put_headers(WRITER_TOKEN, if_match=bad_if_match),
                json=atomic_policy("blacklist", [300]),
            )
            _record(
                "invalid_if_match",
                assert_whitelist_detail(
                    response,
                    422,
                    "invalid_if_match",
                    configured_revision=1,
                    effective_revision=1,
                ),
            )

    # revision_conflict：两个不同存储 revision 场景，各按真实当前值投影。
    for stored_revision, if_match in ((1, '"5"'), (3, '"7"')):
        storage = AdmissionStorageFake(
            stored_policy(stored_revision, atomic_policy("blacklist", [200]))
        )
        app, _runtime, _manager = await prepare_control_plane(monkeypatch, storage)
        async with asgi_client(app) as client:
            response = await client.put(
                POLICY_PATH,
                headers=put_headers(WRITER_TOKEN, if_match=if_match),
                json=atomic_policy("blacklist", [300]),
            )
        _record(
            "revision_conflict",
            assert_whitelist_detail(
                response,
                409,
                "revision_conflict",
                configured_revision=stored_revision,
                effective_revision=stored_revision,
            ),
        )

    for code, values in messages.items():
        assert len(set(values)) == 1, f"{code} 的 message 随触发条件变化: {values}"
    for code, values in messages.items():
        for canary in ("CANARY-A1", "CANARY-B2", "CANARY-C3", "CANARY-D4"):
            assert not any(canary in value for value in values), code
        for value in values:
            assert "111" not in value and "300" not in value, (
                f"{code} message 疑似拼接群号: {value}"
            )

    # 全部观察到的 code 都在封闭白名单内
    assert set(messages) <= CLOSED_ERROR_CODES
