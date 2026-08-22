"""TSK-226 komari_custom 群归属生命周期验收测试共享支撑（测试专用）。

提供测试专用辅助（不承载生产语义、不新增生产 seam）：

- ``AdmissionProbe`` / ``install_admission_probe``：把
  ``group_admission.runtime._runtime`` module singleton 替换为可编程裁决探
  针。ADR-0012 要求业务插件在每个效果 seam 前消费顶层同步 ``adjudicate``
  （内部经 ``group_admission.runtime._runtime`` 解析），因此替换该 module 属
  性能同时拦截「顶层导入 adjudicate」与「模块解析 adjudicate」两条调用路径。
- ``FakeSessionRedis``：手写内存 Redis 客户端，只实现
  ``CustomSessionManager`` 实际消费的命令面（``GET`` / ``EVAL`` CAS /
  ``DELETE`` / ``PTTL``），维护内存 TTL 与业务存活时钟，用于无真实 Redis 时
  验收 PTTL 冻结/恢复与编辑会话休眠。绝不发起外部连接。

真实 PostgreSQL/Redis 门控断言按 AGENTS.md 的 ``tests/db`` 同库门控模式写
在 ``*_gated`` 用例中，缺库时 ``skipif`` 合法；本文件 non-gated 用例本地即
可运行并产生预期红基线。
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

from komari_bot.plugins.group_admission import (
    AdmissionIntent,
    AdmissionQualification,
    AdmissionResult,
    AdmissionRuntimeState,
    AdmissionRuntimeStatus,
)

if TYPE_CHECKING:
    from collections.abc import Collection

    import pytest


class AdmissionProbe:
    """可编程准入运行时 probe。``adjudicate`` 同步无 I/O，只按状态返回结果。"""

    def __init__(
        self,
        *,
        admitted: bool = True,
        revision: int | None = 1,
    ) -> None:
        self._admitted = admitted
        self._revision = revision
        self.business_calls: list[tuple[int, ...]] = []
        self.fact_calls: list[tuple[int, ...]] = []
        self.cleanup_calls: list[tuple[int, ...]] = []
        self.adjudicate_count = 0

    def set_admitted(self, *, admitted: bool, revision: int | None = 1) -> None:
        self._admitted = admitted
        self._revision = revision

    def adjudicate(
        self,
        associated_group_ids: Collection[int],
        *,
        intent: AdmissionIntent = AdmissionIntent.BUSINESS,
    ) -> AdmissionResult:
        self.adjudicate_count += 1
        ids = tuple(associated_group_ids)
        if intent is AdmissionIntent.BUSINESS:
            self.business_calls.append(ids)
            if self._admitted:
                return AdmissionResult(
                    qualification=AdmissionQualification.BUSINESS,
                    effective_revision=self._revision,
                    reason_code="policy_admitted",
                )
            return AdmissionResult(
                qualification=AdmissionQualification.REJECTED,
                effective_revision=self._revision,
                reason_code="policy_restricted",
            )
        if intent is AdmissionIntent.FACT_FINALIZATION:
            self.fact_calls.append(ids)
            return AdmissionResult(
                qualification=AdmissionQualification.FACT_FINALIZATION,
                effective_revision=self._revision,
                reason_code="fact_finalization_granted",
            )
        self.cleanup_calls.append(ids)
        return AdmissionResult(
            qualification=AdmissionQualification.TECHNICAL_CLEANUP,
            effective_revision=self._revision,
            reason_code="technical_cleanup_granted",
        )

    def get_state(self) -> AdmissionRuntimeState:
        return AdmissionRuntimeState(
            status=AdmissionRuntimeStatus.READY,
            problem_code=None,
            configured_revision=self._revision,
            effective_revision=self._revision,
            using_last_known_good=False,
        )


def install_admission_probe(
    monkeypatch: pytest.MonkeyPatch,
    probe: AdmissionProbe,
) -> None:
    """把 probe 安装为 ``group_admission.runtime._runtime`` module singleton。"""
    runtime_module = importlib.import_module(
        "komari_bot.plugins.group_admission.runtime"
    )
    monkeypatch.setattr(runtime_module, "_runtime", probe)


class FakeSessionRedis:
    """内存 Redis 客户端（``CustomSessionManager`` 消费的命令面）。

    维护 ``{key: (value, remaining_ttl)`` 状态；``remaining_ttl`` 随外部时钟
    递减（默认初始为 ``SESSION_TTL_SECONDS``），``pttl`` 返回剩余秒数，用于验
    收受限期 PTTL 冻结（不应随时间续期/前移）与编辑会话休眠。实现
    ``get`` / ``execute_command(EVAL CAS)`` / ``delete`` / ``pttl``，绝不联网。
    """

    def __init__(self) -> None:
        self._store: dict[str, tuple[str, int]] = {}
        self.eval_calls: list[tuple[str, ...]] = []

    def advance(self, seconds: int) -> None:
        """推进外部时钟，让所有已存键的剩余 TTL 同步减少。"""
        for key, (value, ttl) in list(self._store.items()):
            if ttl is not None:
                remaining = ttl - seconds
                self._store[key] = (value, max(0, remaining))

    async def get(self, key: str) -> str | None:
        entry = self._store.get(key)
        if entry is None:
            return None
        value, ttl = entry
        if ttl is not None and ttl <= 0:
            self._store.pop(key, None)
            return None
        return value

    async def execute_command(self, *args: object) -> int:
        # EVAL script numkeys key expected replacement ttl expect_missing
        self.eval_calls.append(tuple(str(item) for item in args))
        key = str(args[3])
        expected = str(args[4])
        replacement = str(args[5])
        ttl = int(str(args[6]))
        expect_missing = str(args[7]) == "1"
        current = self._store.get(key)
        if expect_missing:
            if current is not None:
                return 0
        elif current is None or current[0] != expected:
            return 0
        self._store[key] = (replacement, ttl)
        return 1

    async def delete(self, *keys: object) -> int:
        removed = 0
        for key in keys:
            if self._store.pop(str(key), None) is not None:
                removed += 1
        return removed

    async def pttl(self, key: str) -> int:
        entry = self._store.get(key)
        if entry is None:
            return -2
        _value, ttl = entry
        if ttl is None:
            return -1
        return max(0, ttl)
