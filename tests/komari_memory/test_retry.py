"""retry_async 日志脱敏守卫直接单测（TSK-151 移植）。

旧 test_summary_worker.py::test_retry_logs_only_error_type（旧 637-667 行）是
全仓唯一覆盖 retry_async 失败日志脱敏（只记 error_type 与函数名、不记异常正文）
的测试。core/retry.py 是共享件且在 processing 生命周期提炼后仍被
conversation_processing_lifecycle 使用，该守卫随 test_summary_worker.py 的
L2 重写迁移到本文件：直接针对 retry_async 本身，普通 import，无 importlib hack。
"""

from __future__ import annotations

import pytest

from komari_bot.plugins.komari_memory.core import retry as retry_module


class _ExcludedError(RuntimeError):
    """exclude 参数专用的被排除异常（与业务无关，纯 retry 单元语义）。"""


async def test_retry_async_fails_after_max_attempts_and_logs_only_error_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """重试恰好 max_attempts 次后原样抛出；日志只含 error_type 与函数名，不含异常正文。"""

    logs: list[tuple[object, tuple[object, ...]]] = []
    sleep_delays: list[float] = []
    calls = 0

    async def _noop_sleep(delay: float) -> None:
        sleep_delays.append(delay)

    async def _always_fail() -> None:
        nonlocal calls
        calls += 1
        raise RuntimeError("绝不能进入日志的用户私密正文")

    monkeypatch.setattr(retry_module.asyncio, "sleep", _noop_sleep)
    monkeypatch.setattr(
        retry_module.logger,
        "warning",
        lambda message, *args: logs.append((message, args)),
    )
    monkeypatch.setattr(
        retry_module.logger,
        "error",
        lambda message, *args: logs.append((message, args)),
    )
    retried = retry_module.retry_async(max_attempts=3, base_delay=0.5)(_always_fail)

    with pytest.raises(RuntimeError, match="绝不能进入日志的用户私密正文"):
        await retried()

    assert calls == 3  # 重试恰好 max_attempts 次后原样抛出
    assert sleep_delays == [0.5, 1.0]  # 指数退避且零真实时钟（打桩 sleep 仍被调用）
    serialized_logs = repr(logs)
    assert "RuntimeError" in serialized_logs  # error_type 出现
    assert "_always_fail" in serialized_logs  # 函数名出现
    assert "绝不能进入日志的用户私密正文" not in serialized_logs  # 异常正文绝不入日志


async def test_retry_async_exclude_hit_raises_immediately_without_logs_or_sleep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """exclude 命中：首次出现即原样抛出，零 sleep、零重试日志，get_retry_attempts == 1（TSK-155）。"""
    logs: list[tuple[object, tuple[object, ...]]] = []
    sleep_delays: list[float] = []
    calls = 0
    excluded_error = _ExcludedError("被排除异常的正文")

    async def _noop_sleep(delay: float) -> None:
        sleep_delays.append(delay)

    async def _raises_excluded() -> None:
        nonlocal calls
        calls += 1
        raise excluded_error

    monkeypatch.setattr(retry_module.asyncio, "sleep", _noop_sleep)
    monkeypatch.setattr(
        retry_module.logger,
        "warning",
        lambda message, *args: logs.append((message, args)),
    )
    monkeypatch.setattr(
        retry_module.logger,
        "error",
        lambda message, *args: logs.append((message, args)),
    )
    retried = retry_module.retry_async(
        max_attempts=3,
        base_delay=0.5,
        exclude=(_ExcludedError,),
    )(_raises_excluded)

    with pytest.raises(_ExcludedError) as exc_info:
        await retried()

    assert calls == 1  # 首次出现即抛出，不进入后续尝试
    assert exc_info.value is excluded_error  # 原异常实例原样抛出
    assert sleep_delays == []  # 不 sleep
    assert logs == []  # 不打重试 warning / error 日志
    assert retry_module.get_retry_attempts(exc_info.value) == 1  # exclude 立即抛出附着 1


async def test_retry_async_attaches_max_attempts_on_exhaustion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """穷尽重试后抛出：get_retry_attempts == max_attempts；exclude 未命中不影响普通异常重试（TSK-155）。"""
    logs: list[tuple[object, tuple[object, ...]]] = []
    calls = 0

    async def _noop_sleep(delay: float) -> None:
        del delay

    async def _always_fail() -> None:
        nonlocal calls
        calls += 1
        raise RuntimeError("普通失败")

    monkeypatch.setattr(retry_module.asyncio, "sleep", _noop_sleep)
    monkeypatch.setattr(
        retry_module.logger,
        "warning",
        lambda message, *args: logs.append((message, args)),
    )
    monkeypatch.setattr(
        retry_module.logger,
        "error",
        lambda message, *args: logs.append((message, args)),
    )
    retried = retry_module.retry_async(
        max_attempts=3,
        base_delay=0.5,
        exclude=(_ExcludedError,),
    )(_always_fail)

    with pytest.raises(RuntimeError, match="普通失败") as exc_info:
        await retried()

    assert calls == 3  # exclude 未命中：exceptions 范围内的普通异常仍按原语义重试
    assert retry_module.get_retry_attempts(exc_info.value) == 3  # 穷尽重试附着 max_attempts


def test_get_retry_attempts_returns_none_without_retry_wrapper() -> None:
    """未经过 retry_async 包装层的异常返回 None（TSK-155）。"""
    assert retry_module.get_retry_attempts(RuntimeError("未经过重试包装层")) is None
