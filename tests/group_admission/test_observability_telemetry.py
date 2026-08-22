"""TSK-223 阶段 B：低基数遥测、并发计数与正常拒绝静默。

验收目标（TSK-217 §1/§3 冻结）：

- 四张低基数 map 始终精确预初始化封闭键集：7 reason、3 intent、4 event
  family（message/notice/request/unknown）、4 runtime problem；零值也在；
  任何恶意 family 不得新增动态键，一律归一到 ``unknown``；
- 每次 ``adjudicate`` 结果恰记一次：
  admitted/restricted/fact/cleanup/attribution/unavailable 全覆盖；
  ``record_private_input_rejected`` 同样进入 ``adjudications_total`` /
  ``by_reason_code`` 并按 BUSINESS intent 记账（冻结该语义），但不进入归属
  失败 map（归属窗口只键于 ``group_attribution_unavailable``）；
- 多线程 Barrier/Executor 确定性并发调用，计数不丢失；全程无真实 sleep；
- 正常 ``policy_restricted`` 与 ``private_input_rejected``：只有计数；捕获窗
  口内零 ``group_admission.*`` 结构化日志、零 SUPERUSER/群平台输出、零存储
  读写、零拒绝持久记录；``effective_policy_unavailable`` 不逐事件日志/卡，
  由故障 episode 统一覆盖。

遥测只经 ``/status`` 观察（生产不公开 telemetry getter）。
"""

from __future__ import annotations

import inspect
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

import pytest

from tests.group_admission.management_support import (
    POLICY_PATH,
    READER_TOKEN,
    STATUS_PATH,
    WRITER_TOKEN,
    asgi_client,
    atomic_policy,
    auth_headers,
    management_credentials,
    prepare_control_plane,
    put_headers,
)
from tests.group_admission.observability_support import (
    CLOSED_EVENT_FAMILIES,
    FakeAdmissionBot,
    FakeUtcClock,
    MutableBotsProvider,
    MutableSuperusersProvider,
    assert_status_exact_shape,
    assert_telemetry_closed_maps,
    build_runtime_kwargs,
    capture_admission_logs,
)
from tests.group_admission.runtime_support import (
    AdmissionStorageFake,
    build_runtime,
    import_admission_package,
    install_singleton,
    stored_policy,
)

pytestmark = pytest.mark.group_admission_acceptance

_THREAD_COUNT = 8
_ITERATIONS_PER_THREAD = 20
_CONCURRENCY_TIMEOUT_SECONDS = 30.0


async def _get_status_body(client: Any) -> dict[str, Any]:
    response = await client.get(STATUS_PATH, headers=auth_headers(READER_TOKEN))
    assert response.status_code == 200, response.text
    return response.json()


async def test_telemetry_maps_are_preinitialized_with_closed_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """四张 map 预初始化封闭键（零值也在）；恶意 family 不产生动态键。"""
    clock = FakeUtcClock()
    storage = AdmissionStorageFake(stored_policy(1, atomic_policy("blacklist", [200])))
    app, runtime, _manager = await prepare_control_plane(
        monkeypatch,
        storage,
        runtime_kwargs=build_runtime_kwargs(
            clock=clock,
            bots_provider=MutableBotsProvider(),
            superusers_provider=MutableSuperusersProvider(),
        ),
    )

    async with asgi_client(app) as client:
        fresh = await _get_status_body(client)
        assert_status_exact_shape(fresh)
        telemetry = fresh["telemetry"]
        assert_telemetry_closed_maps(telemetry, total=0)
        for mapping_name in (
            "by_reason_code",
            "by_intent",
            "attribution_failures_by_event_family",
            "runtime_problem_occurrences",
        ):
            assert all(
                value == 0 for value in telemetry[mapping_name].values()
            ), f"{mapping_name} 初始必须全零"

        # 恶意 family 注入：归一到 unknown，不新增动态键
        runtime.adjudicate(["bad"], event_family="__evil_dynamic_family__")
        runtime.record_private_input_rejected(event_family="CANARY-family-77ab")

        after = await _get_status_body(client)
        telemetry = after["telemetry"]
        assert_telemetry_closed_maps(telemetry, total=2)
        assert set(telemetry["attribution_failures_by_event_family"]) == (
            CLOSED_EVENT_FAMILIES
        )
        assert telemetry["attribution_failures_by_event_family"]["unknown"] == 1
        assert telemetry["by_reason_code"]["group_attribution_unavailable"] == 1
        assert telemetry["by_reason_code"]["private_input_rejected"] == 1


async def test_every_adjudication_outcome_counts_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """7 个原因码逐一触发，每个结果恰记一次；私聊拒绝按 BUSINESS intent。

    顺序契约：同一运行时先在未 start 状态裁决 ``effective_policy_unavailable``
    （INITIAL 聚合无有效策略），再 ``start`` manager、构造 app/API，最后触发
    其余 6 个；关闭后的 unavailable 不计入（归 fault_episode 用例证明
    healthy close 后 unavailable silent 可保留）。
    """
    clock = FakeUtcClock()
    storage = AdmissionStorageFake(stored_policy(1, atomic_policy("blacklist", [200])))
    admission = import_admission_package()

    # 同一运行时：未 start 先裁决 unavailable（INITIAL 聚合，无有效策略）
    runtime, manager = build_runtime(
        monkeypatch, storage, runtime_kwargs=build_runtime_kwargs(clock=clock)
    )
    runtime.adjudicate([100])  # effective_policy_unavailable（未启动）

    # 再 start manager，再构造 app/API（既有 register 装配，不冻结新 public）
    await runtime.start(manager)
    install_singleton(monkeypatch, runtime)
    from fastapi import FastAPI

    app = FastAPI()
    admission.register_group_admission_api(
        app,
        api_token=management_credentials(),
        allowed_origins=[],
    )

    # 触发其余 6 个原因码
    runtime.adjudicate([100])  # policy_admitted
    runtime.adjudicate([200])  # policy_restricted
    runtime.adjudicate(
        [200], intent=admission.AdmissionIntent.FACT_FINALIZATION
    )  # fact_finalization_granted
    runtime.adjudicate(
        [], intent=admission.AdmissionIntent.TECHNICAL_CLEANUP
    )  # technical_cleanup_granted
    runtime.adjudicate(["bad"], event_family="message")  # attribution
    runtime.record_private_input_rejected(event_family="notice")  # private

    async with asgi_client(app) as client:
        body = await _get_status_body(client)

    telemetry = body["telemetry"]
    assert_telemetry_closed_maps(telemetry, total=7)
    assert telemetry["by_reason_code"] == {
        "policy_admitted": 1,
        "policy_restricted": 1,
        "group_attribution_unavailable": 1,
        "effective_policy_unavailable": 1,
        "fact_finalization_granted": 1,
        "technical_cleanup_granted": 1,
        "private_input_rejected": 1,
    }
    # private_input_rejected 冻结按 BUSINESS intent 记账；
    # effective_policy_unavailable 的裁决同样按调用方声明的 BUSINESS intent
    # 记账（每个裁决在 total / by_reason_code / by_intent 三维各恰记一次，
    # closed-maps 合计不变量由此成立）。
    assert telemetry["by_intent"] == {
        "business": 5,
        "fact_finalization": 1,
        "technical_cleanup": 1,
    }
    assert telemetry["attribution_failures_by_event_family"] == {
        "message": 1,
        "notice": 0,
        "request": 0,
        "unknown": 0,
    }


async def test_concurrent_adjudications_do_not_lose_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Barrier/Executor 确定性并发：全部计数不丢失，无真实 sleep。"""
    clock = FakeUtcClock()
    storage = AdmissionStorageFake(stored_policy(1, atomic_policy("blacklist", [200])))
    app, runtime, _manager = await prepare_control_plane(
        monkeypatch,
        storage,
        runtime_kwargs=build_runtime_kwargs(clock=clock),
    )
    admission = import_admission_package()
    fact_intent = admission.AdmissionIntent.FACT_FINALIZATION
    cleanup_intent = admission.AdmissionIntent.TECHNICAL_CLEANUP

    def _call_sequence(index: int) -> list[Callable[[], None]]:
        families = ("message", "notice", "request", "__not_a_family__")
        return [
            lambda: runtime.adjudicate([100]),
            lambda: runtime.adjudicate([200]),
            lambda: runtime.adjudicate([200], intent=fact_intent),
            lambda: runtime.adjudicate([], intent=cleanup_intent),
            lambda: runtime.adjudicate(
                ["bad"], event_family=families[index % len(families)]
            ),
            lambda: runtime.record_private_input_rejected(event_family="message"),
        ]

    barrier = threading.Barrier(_THREAD_COUNT)
    errors: list[BaseException] = []

    def _worker(worker_index: int) -> None:
        try:
            barrier.wait(timeout=_CONCURRENCY_TIMEOUT_SECONDS)
            for _iteration in range(_ITERATIONS_PER_THREAD):
                for call in _call_sequence(worker_index):
                    call()
        except BaseException as exc:  # 线程异常显式归并
            errors.append(exc)

    with ThreadPoolExecutor(max_workers=_THREAD_COUNT) as executor:
        futures = [
            executor.submit(_worker, index) for index in range(_THREAD_COUNT)
        ]
        for future in futures:
            future.result(timeout=_CONCURRENCY_TIMEOUT_SECONDS)

    assert errors == [], f"并发调用出现异常: {errors!r}"

    total = _THREAD_COUNT * _ITERATIONS_PER_THREAD * 6
    async with asgi_client(app) as client:
        body = await _get_status_body(client)

    telemetry = body["telemetry"]
    assert_telemetry_closed_maps(telemetry, total=total)

    threads = _THREAD_COUNT
    iterations = _ITERATIONS_PER_THREAD
    expected_attribution_per_family = threads * iterations // 4
    assert telemetry["by_reason_code"]["policy_admitted"] == threads * iterations
    assert telemetry["by_reason_code"]["policy_restricted"] == threads * iterations
    assert (
        telemetry["by_reason_code"]["fact_finalization_granted"]
        == threads * iterations
    )
    assert (
        telemetry["by_reason_code"]["technical_cleanup_granted"]
        == threads * iterations
    )
    assert (
        telemetry["by_reason_code"]["group_attribution_unavailable"]
        == threads * iterations
    )
    assert (
        telemetry["by_reason_code"]["private_input_rejected"]
        == threads * iterations
    )
    assert telemetry["by_intent"]["business"] == threads * iterations * 4
    assert telemetry["by_intent"]["fact_finalization"] == threads * iterations
    assert telemetry["by_intent"]["technical_cleanup"] == threads * iterations
    families = telemetry["attribution_failures_by_event_family"]
    assert families["message"] == expected_attribution_per_family
    assert families["notice"] == expected_attribution_per_family
    assert families["request"] == expected_attribution_per_family
    assert families["unknown"] == expected_attribution_per_family
    assert all(value == 0 for value in telemetry["runtime_problem_occurrences"].values())


async def test_normal_denials_only_count_and_stay_silent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """正常受限与私聊拒绝：只计数，日志/平台/存储/持久面全部静默。"""
    clock = FakeUtcClock()
    storage = AdmissionStorageFake(stored_policy(1, atomic_policy("blacklist", [200])))
    bot = FakeAdmissionBot("bot-silence")
    app, runtime, _manager = await prepare_control_plane(
        monkeypatch,
        storage,
        runtime_kwargs=build_runtime_kwargs(
            clock=clock,
            bots_provider=MutableBotsProvider((bot,)),
            superusers_provider=MutableSuperusersProvider((42,)),
        ),
    )

    fetch_before = storage.fetch_calls
    cas_before = len(storage.cas_calls)

    with capture_admission_logs() as capture:
        runtime.adjudicate([200])
        runtime.adjudicate([200, 300])
        runtime.adjudicate([200])
        runtime.record_private_input_rejected(event_family="message")
        runtime.record_private_input_rejected(event_family="message")
        await runtime.process_observability()

        capture.assert_no_events()

    assert bot.sent == [], "正常拒绝不得产生 SUPERUSER 通知"
    assert bot.attempts == 0
    assert storage.fetch_calls == fetch_before, "正常拒绝不得读取存储"
    assert len(storage.cas_calls) == cas_before, "正常拒绝不得写入存储"
    assert storage.insert_calls == 0

    async with asgi_client(app) as client:
        body = await _get_status_body(client)
    telemetry = body["telemetry"]
    assert_telemetry_closed_maps(telemetry, total=5)
    assert telemetry["by_reason_code"]["policy_restricted"] == 3
    assert telemetry["by_reason_code"]["private_input_rejected"] == 2


async def test_effective_policy_unavailable_counts_without_per_event_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """无有效策略的拒绝只计数：不逐事件日志/卡，诊断由故障 episode 统一覆盖。"""
    storage = AdmissionStorageFake(stored_policy(1, atomic_policy("blacklist", [])))
    bot = FakeAdmissionBot("bot-unavailable")
    runtime, _manager = build_runtime(
        monkeypatch,
        storage,
        runtime_kwargs=build_runtime_kwargs(
            bots_provider=MutableBotsProvider((bot,)),
            superusers_provider=MutableSuperusersProvider((42,)),
        ),
    )

    with capture_admission_logs() as capture:
        # 未启动运行时没有有效策略：拒绝但不产生逐事件诊断
        result = runtime.adjudicate([100])
        assert result.reason_code == "effective_policy_unavailable"
        result = runtime.adjudicate([100])
        assert result.reason_code == "effective_policy_unavailable"
        await runtime.process_observability()
        capture.assert_no_events()

    assert bot.sent == []
    assert bot.attempts == 0


async def test_empty_and_private_event_family_defaults_to_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """空 family 与 private 拒绝默认归一到 ``unknown``，不新增动态键。

    非法（空串）family 在 adjudicate / record_private_input_rejected 两条
    记账路径上都必须归一到 closed ``unknown`` 键；归属失败 map 的键集始终
    保持封闭（private 不进归属失败 map，归属窗口只键于
    ``group_attribution_unavailable``）。
    """
    clock = FakeUtcClock()
    storage = AdmissionStorageFake(stored_policy(1, atomic_policy("blacklist", [200])))
    bot = FakeAdmissionBot("bot-family-default")
    app, runtime, _manager = await prepare_control_plane(
        monkeypatch,
        storage,
        runtime_kwargs=build_runtime_kwargs(
            clock=clock,
            bots_provider=MutableBotsProvider((bot,)),
            superusers_provider=MutableSuperusersProvider((42,)),
        ),
    )

    runtime.adjudicate(["bad"], event_family="")  # 空 family → unknown
    runtime.record_private_input_rejected(event_family="")  # 空 family → unknown

    async with asgi_client(app) as client:
        body = await _get_status_body(client)
    telemetry = body["telemetry"]
    assert telemetry["attribution_failures_by_event_family"] == {
        "message": 0,
        "notice": 0,
        "request": 0,
        "unknown": 1,
    }
    assert telemetry["by_reason_code"]["group_attribution_unavailable"] == 1
    assert telemetry["by_reason_code"]["private_input_rejected"] == 1
    assert_telemetry_closed_maps(telemetry, total=2)


async def test_api_internal_error_audit_does_not_increment_runtime_problem_counter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """API 审计 internal_error 不增加运行时 runtime_problem_occurrences 计数。

    internal_error 问题码无自然安全触发（只经运行时内部异常链产生）；当它
    仅作为管理审计 result_code 出现时，运行时低基数 problem occurrence 不得
    被它推动（不造测试 hook 模拟运行时内部 internal_error）。
    """
    clock = FakeUtcClock()
    storage = AdmissionStorageFake(stored_policy(1, atomic_policy("blacklist", [200])))
    bot = FakeAdmissionBot("bot-internal")
    app, runtime, _manager = await prepare_control_plane(
        monkeypatch,
        storage,
        runtime_kwargs=build_runtime_kwargs(
            clock=clock,
            bots_provider=MutableBotsProvider((bot,)),
            superusers_provider=MutableSuperusersProvider((42,)),
        ),
    )

    runtime.adjudicate([100])  # 基线计数 1
    baseline_total = 1

    # 制造 API 内部错误：控制面 PUT 触及已断裂存储 → 审计 internal_error
    storage.break_all(RuntimeError("internal io forbidden"))
    async with asgi_client(app) as client:
        put_response = await client.put(
            POLICY_PATH,
            headers=put_headers(WRITER_TOKEN, if_match='"1"'),
            json=atomic_policy("whitelist", [700]),
        )
        # 错误体 code 白名单确实含 internal_error（审计侧登记）
        if put_response.status_code >= 400:
            assert put_response.json()["detail"]["code"] in {
                "internal_error",
                "storage_unavailable",
            }
        body = await _get_status_body(client)

    telemetry = body["telemetry"]
    # 运行时 problem occurrence 的 internal_error 不得因 API 审计而增长
    assert telemetry["runtime_problem_occurrences"]["internal_error"] == 0
    # 基线裁决计数不变（API 内部错误不计入 adjudication telemetry）
    assert telemetry["adjudications_total"] == baseline_total
    assert_telemetry_closed_maps(telemetry, total=baseline_total)


def test_reserved_private_observability_seams_have_frozen_shapes() -> None:
    """私有生产接缝形态冻结：可选 DI / event_family / process / private input。

    这些是正常内部生产接缝，不是测试 hook：``_AdmissionRuntime`` 继续允许无
    参构造；``adjudicate`` 的 ``event_family`` 是关键字参数（默认
    ``unknown``，不进顶层公开签名）；``process_observability`` 是异步无必选
    参数方法；``record_private_input_rejected`` 要求关键字 ``event_family``。
    """
    runtime_module = __import__(
        "komari_bot.plugins.group_admission.runtime",
        fromlist=["_AdmissionRuntime"],
    )
    runtime_cls = runtime_module._AdmissionRuntime

    # 无参构造保持合法（生产默认 DI）
    runtime_cls()

    adjudicate_params = inspect.signature(runtime_cls.adjudicate).parameters
    assert list(adjudicate_params) == [
        "self",
        "associated_group_ids",
        "intent",
        "event_family",
    ], "私有 adjudicate 必须新增且仅新增 keyword-only event_family"
    event_family = adjudicate_params["event_family"]
    assert event_family.kind is inspect.Parameter.KEYWORD_ONLY
    assert event_family.default == "unknown"

    process_params = inspect.signature(runtime_cls.process_observability).parameters
    assert list(process_params) == ["self"], (
        "process_observability 不接受业务参数（供后续 scheduler 直接调用）"
    )
    assert inspect.iscoroutinefunction(runtime_cls.process_observability)

    record_params = inspect.signature(
        runtime_cls.record_private_input_rejected
    ).parameters
    assert list(record_params) == ["self", "event_family"]
    assert record_params["event_family"].kind is inspect.Parameter.KEYWORD_ONLY
    assert record_params["event_family"].default is inspect.Parameter.empty

    # 顶层公开 adjudicate 绝不暴露 event_family
    admission = import_admission_package()
    assert list(inspect.signature(admission.adjudicate).parameters) == [
        "associated_group_ids",
        "intent",
    ]
