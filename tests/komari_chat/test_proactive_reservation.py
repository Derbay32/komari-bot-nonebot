"""proactive_reservation module 级测试（KOMARIBOT-8 验收基线 + TSK-132 租约形态）。

真实 module + Lua 语义级 fake Redis 客户端：断言 Redis 键 / 成员 / score /
冷却值等外部可观察行为语义，不断言参数转发。now_ms 可前移以模拟时间流逝。
键前缀为新所有权 komari_chat:proactive:*；Lua 语义与 findings §2 一致
（滑动窗口 / 惰性剪枝 / 预占即写冷却键 / 续租延长 / 孤儿纯 TTL 淘汰）。

TSK-132 租约形态：reserve 返回 ``ProactiveLease | ReservationDenied`` 联合，
生成期经 ``async with`` 激活续租、``handoff()`` 内联存活裁决返回移交凭据；
频控拒绝不再以裸字符串字面量暴露。既有 Lua 语义不变量全部保留，仅适配
新返回类型与释放入口（未移交退出自动释放 / 凭据 release()）。
"""

from __future__ import annotations

import asyncio
from importlib import import_module
from types import SimpleNamespace
from typing import Any

import pytest

proactive_reservation_module = import_module(
    "komari_bot.plugins.komari_chat.services.proactive_reservation"
)
ProactiveLease = proactive_reservation_module.ProactiveLease
ReservationDenied = proactive_reservation_module.ReservationDenied
ReservationLostError = proactive_reservation_module.ReservationLostError
ReservationStateError = proactive_reservation_module.ReservationStateError
ProactiveReservationService = proactive_reservation_module.ProactiveReservationService

_RATE_WINDOW_MS = 3_600_000


def _cooldown_key(group_id: str) -> str:
    return f"komari_chat:proactive:cd:{group_id}"


def _slots_key(group_id: str) -> str:
    return f"komari_chat:proactive:slots:{group_id}"


class _FakeRedis:
    """Lua 语义级 fake：纯 Python 复刻四个预占脚本的行为语义。

    移植自 tests/komari_memory/test_redis_manager.py 的 _eval_proactive_*
    手法；fake 不建模 PX TTL 自动过期，冷却键过期由测试手动弹出模拟，
    滑动窗口成员过期经 now_ms 前移 + 惰性剪枝建模。``fail_renew_count``
    用于建模"续租恰好失败一次后恢复"（租约生命周期不变量 7）。
    """

    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.zsets: dict[str, dict[str, float]] = {}
        self.now_ms = 1_000_000.0
        self.fail_renew_count = 0

    async def execute_command(self, command: str, *args: object) -> object:
        assert command == "EVAL"
        script, _key_count, *rest = args
        script_text = str(script)
        if "proactive_reserve" in script_text:
            return self._eval_reserve(rest)
        if "proactive_confirm" in script_text:
            return self._eval_confirm(rest)
        if "proactive_renew" in script_text:
            return self._eval_renew(rest)
        if "proactive_release" in script_text:
            return self._eval_release(rest)
        msg = f"未模拟的 Lua 脚本: {script_text[:60]}"
        raise AssertionError(msg)

    def _prune(self, slots_key: str) -> None:
        slots = self.zsets.setdefault(slots_key, {})
        expired = [m for m, score in slots.items() if score <= self.now_ms]
        for member in expired:
            slots.pop(member, None)

    def _eval_reserve(self, rest: list[object]) -> int:
        cooldown_key, slots_key = map(str, rest[:2])
        reservation_id = str(rest[2])
        max_slots = int(str(rest[3]))
        reservation_ttl_ms = float(str(rest[4]))
        self._prune(slots_key)
        slots = self.zsets[slots_key]
        if (
            f"pending:{reservation_id}" in slots
            or f"confirmed:{reservation_id}" in slots
        ):
            return 3
        if cooldown_key in self.values:
            return 1
        if len(slots) >= max_slots:
            return 2
        slots[f"pending:{reservation_id}"] = self.now_ms + reservation_ttl_ms
        self.values[cooldown_key] = reservation_id
        return 0

    def _eval_confirm(self, rest: list[object]) -> int:
        cooldown_key, slots_key = map(str, rest[:2])
        reservation_id = str(rest[2])
        rate_window_ms = float(str(rest[5]))
        self._prune(slots_key)
        slots = self.zsets[slots_key]
        confirmed_member = f"confirmed:{reservation_id}"
        if confirmed_member in slots:
            return 2
        had_pending = int(slots.pop(f"pending:{reservation_id}", None) is not None)
        slots[confirmed_member] = self.now_ms + rate_window_ms
        current_cooldown = self.values.get(cooldown_key)
        if current_cooldown is None or current_cooldown == reservation_id:
            self.values[cooldown_key] = confirmed_member
        return had_pending

    def _eval_renew(self, rest: list[object]) -> int:
        _cooldown_key, slots_key = map(str, rest[:2])
        reservation_id = str(rest[2])
        reservation_ttl_ms = float(str(rest[3]))
        if self.fail_renew_count > 0:
            # 建模一次"续租恰好丢失后恢复"（不实际改动状态，只返回丢失码）
            self.fail_renew_count -= 1
            return 0
        self._prune(slots_key)
        slots = self.zsets[slots_key]
        if f"confirmed:{reservation_id}" in slots:
            return 2
        pending_member = f"pending:{reservation_id}"
        if pending_member not in slots:
            return 0
        slots[pending_member] = self.now_ms + reservation_ttl_ms
        return 1

    def _eval_release(self, rest: list[object]) -> int:
        cooldown_key, slots_key = map(str, rest[:2])
        reservation_id = str(rest[2])
        slots = self.zsets.setdefault(slots_key, {})
        removed = int(slots.pop(f"pending:{reservation_id}", None) is not None)
        if self.values.get(cooldown_key) == reservation_id:
            self.values.pop(cooldown_key, None)
        return removed


def _build_service(
    monkeypatch: pytest.MonkeyPatch,
    **config_overrides: object,
) -> tuple[Any, _FakeRedis]:
    """构造真实 module 服务 + fake 客户端，配置桩挂到 module 的 get_config。"""
    fake = _FakeRedis()
    defaults: dict[str, object] = {
        "proactive_cooldown": 300,
        "proactive_max_per_hour": 10,
        "proactive_reservation_ttl_seconds": 360,
    }
    config = SimpleNamespace(**{**defaults, **config_overrides})
    monkeypatch.setattr(proactive_reservation_module, "get_config", lambda: config)
    service = ProactiveReservationService(fake)
    return service, fake


# ── reserve 成功路径：租约形态 + 冻结快照 ──────────────────────


def test_reserve_returns_lease_with_frozen_config_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """reserve 成功返回租约，配置快照在 reserve 时一次性冻结（经凭据暴露 cooldown）。"""
    service, fake = _build_service(
        monkeypatch,
        proactive_cooldown=123,
        proactive_max_per_hour=7,
        proactive_reservation_ttl_seconds=45,
    )

    async def _scenario() -> None:
        lease = await service.reserve("group-1", "message-1")
        assert isinstance(lease, ProactiveLease)
        assert lease.group_id == "group-1"
        assert lease.reservation_id == "message-1"
        # 预占即写冷却键（同群生成期串行阻断），键前缀为新所有权
        assert fake.values[_cooldown_key("group-1")] == "message-1"
        slots = fake.zsets[_slots_key("group-1")]
        assert slots["pending:message-1"] == fake.now_ms + 45_000
        # 冻结快照经移交凭据暴露：cooldown_seconds 为 reserve 时刻的值
        async with lease:
            handoff = await lease.handoff()
        assert handoff.group_id == "group-1"
        assert handoff.reservation_id == "message-1"
        assert handoff.cooldown_seconds == 123

    asyncio.run(_scenario())


# ── reserve 拒绝路径：ReservationDenied 联合（不再裸字符串） ──


def test_reserve_rejects_during_cooldown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, fake = _build_service(monkeypatch)

    first = asyncio.run(service.reserve("group-1", "message-1"))
    second = asyncio.run(service.reserve("group-1", "message-2"))

    assert isinstance(first, ProactiveLease)
    assert isinstance(second, ReservationDenied)
    assert second.group_id == "group-1"
    assert second.reservation_id == "message-2"
    assert second.reason == "cooldown"
    assert "pending:message-2" not in fake.zsets[_slots_key("group-1")]


def test_reserve_rejects_when_rate_limited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, fake = _build_service(monkeypatch, proactive_max_per_hour=1)

    first = asyncio.run(service.reserve("group-1", "message-1"))
    # 模拟生成期冷却键过期（fake 不建模 PX TTL）
    fake.values.pop(_cooldown_key("group-1"), None)
    second = asyncio.run(service.reserve("group-1", "message-2"))

    assert isinstance(first, ProactiveLease)
    assert isinstance(second, ReservationDenied)
    assert second.reason == "rate_limited"


def test_reserve_rejects_duplicate_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _fake = _build_service(monkeypatch)

    first = asyncio.run(service.reserve("group-1", "message-1"))
    duplicate = asyncio.run(service.reserve("group-1", "message-1"))

    assert isinstance(first, ProactiveLease)
    assert isinstance(duplicate, ReservationDenied)
    assert duplicate.reason == "duplicate"


def test_reserve_rejects_duplicate_against_confirmed_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _fake = _build_service(monkeypatch)

    lease = asyncio.run(service.reserve("group-1", "message-1"))
    assert isinstance(lease, ProactiveLease)
    asyncio.run(
        service.confirm("group-1", "message-1", cooldown_seconds=300),
    )
    duplicate = asyncio.run(service.reserve("group-1", "message-1"))

    assert isinstance(duplicate, ReservationDenied)
    assert duplicate.reason == "duplicate"


def test_reserve_is_atomic_for_concurrent_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _fake = _build_service(monkeypatch)

    async def _reserve_pair() -> list[object]:
        return list(
            await asyncio.gather(
                service.reserve("group-1", "message-1"),
                service.reserve("group-1", "message-2"),
            )
        )

    results = asyncio.run(_reserve_pair())

    leases = [r for r in results if isinstance(r, ProactiveLease)]
    denied = [r for r in results if isinstance(r, ReservationDenied)]
    assert len(leases) == 1
    assert len(denied) == 1
    assert denied[0].reason == "cooldown"


# ── 释放入口：未移交退出自动释放 / 凭据 release / 持久化通道 ──


def test_lease_exit_without_handoff_releases_pending_and_clears_cooldown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """未移交直接退出 → pending 成员移除且自持冷却键清除（失败路径释放默认结构）。

    承接旧 ``Reservation.release()`` 的语义：释放后恢复容量、清除冷却键。
    """
    service, fake = _build_service(monkeypatch, proactive_max_per_hour=1)

    async def _scenario() -> None:
        lease = await service.reserve("group-1", "message-1")
        assert isinstance(lease, ProactiveLease)
        async with lease:
            pass

    asyncio.run(_scenario())

    assert fake.zsets[_slots_key("group-1")] == {}
    assert _cooldown_key("group-1") not in fake.values

    second = asyncio.run(service.reserve("group-1", "message-2"))
    assert isinstance(second, ProactiveLease)


def test_handoff_release_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """移交凭据 release() 幂等：首次移除 pending，重复释放返回 False。"""
    service, fake = _build_service(monkeypatch)

    async def _scenario() -> None:
        lease = await service.reserve("group-1", "message-1")
        assert isinstance(lease, ProactiveLease)
        async with lease:
            handoff = await lease.handoff()
        assert await handoff.release() is True
        assert await handoff.release() is False
        assert fake.zsets[_slots_key("group-1")] == {}

    asyncio.run(_scenario())


def test_service_can_release_persisted_reservation_without_process_handle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """崩溃恢复只凭冻结的群与预占 ID 也能幂等释放 pending 名额。"""
    service, fake = _build_service(monkeypatch)
    lease = asyncio.run(service.reserve("group-1", "message-1"))
    assert isinstance(lease, ProactiveLease)

    assert asyncio.run(service.release("group-1", "message-1")) is True
    assert asyncio.run(service.release("group-1", "message-1")) is False
    assert fake.zsets[_slots_key("group-1")] == {}
    assert _cooldown_key("group-1") not in fake.values


def test_handoff_release_does_not_revoke_confirmed_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """凭据 release() 幂等且永不撤销已确认名额。"""
    service, fake = _build_service(monkeypatch)

    async def _scenario() -> None:
        lease = await service.reserve("group-1", "message-1")
        assert isinstance(lease, ProactiveLease)
        async with lease:
            handoff = await lease.handoff()
        await service.confirm("group-1", "message-1", cooldown_seconds=300)

        assert await handoff.release() is False
        assert "confirmed:message-1" in fake.zsets[_slots_key("group-1")]

    asyncio.run(_scenario())


# ── 租约生命周期不变量 ────────────────────────────────────────


def test_lease_renewal_extends_pending_ttl_while_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """进入 async with 后内部续租随时间前移延长 pending 成员 TTL。"""
    service, fake = _build_service(
        monkeypatch,
        proactive_reservation_ttl_seconds=30,
    )
    original_sleep = asyncio.sleep

    async def _fast_sleep(_delay: float) -> None:
        await original_sleep(0)

    monkeypatch.setattr(proactive_reservation_module.asyncio, "sleep", _fast_sleep)

    async def _scenario() -> None:
        lease = await service.reserve("group-1", "message-1")
        assert isinstance(lease, ProactiveLease)
        async with lease:
            original_expiry = fake.zsets[_slots_key("group-1")]["pending:message-1"]
            fake.now_ms += 10_000
            for _ in range(5):
                await asyncio.sleep(0)
            new_expiry = fake.zsets[_slots_key("group-1")]["pending:message-1"]
            assert new_expiry == fake.now_ms + 30_000
            assert new_expiry > original_expiry

    asyncio.run(_scenario())


def test_lease_renewal_stops_after_handoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """移交成功后续租停止：pending TTL 不再随时间前移被延长。"""
    service, fake = _build_service(
        monkeypatch,
        proactive_reservation_ttl_seconds=30,
    )
    original_sleep = asyncio.sleep

    async def _fast_sleep(_delay: float) -> None:
        await original_sleep(0)

    monkeypatch.setattr(proactive_reservation_module.asyncio, "sleep", _fast_sleep)

    async def _scenario() -> None:
        lease = await service.reserve("group-1", "message-1")
        assert isinstance(lease, ProactiveLease)
        async with lease:
            for _ in range(5):
                await asyncio.sleep(0)
            handoff = await lease.handoff()
            assert handoff.reservation_id == "message-1"
            expiry_after_handoff = fake.zsets[_slots_key("group-1")][
                "pending:message-1"
            ]
            fake.now_ms += 10_000
            for _ in range(10):
                await asyncio.sleep(0)
            assert (
                fake.zsets[_slots_key("group-1")]["pending:message-1"]
                == expiry_after_handoff
            )
        # 移交后退出不动 pending 成员
        assert "pending:message-1" in fake.zsets[_slots_key("group-1")]

    asyncio.run(_scenario())


def test_lease_exit_after_handoff_keeps_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """移交成功后退出：pending 成员保留（释放职责移交履约链凭据）。"""
    service, fake = _build_service(monkeypatch)

    async def _scenario() -> None:
        lease = await service.reserve("group-1", "message-1")
        assert isinstance(lease, ProactiveLease)
        async with lease:
            handoff = await lease.handoff()
            assert handoff.group_id == "group-1"

    asyncio.run(_scenario())

    assert "pending:message-1" in fake.zsets[_slots_key("group-1")]
    assert fake.values[_cooldown_key("group-1")] == "message-1"


def test_handoff_raises_lost_when_pending_expired(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """pending 已过期（前移 now_ms 超过 TTL）时 handoff 抛 ReservationLostError。"""
    service, fake = _build_service(
        monkeypatch,
        proactive_reservation_ttl_seconds=30,
    )

    async def _scenario() -> None:
        lease = await service.reserve("group-1", "message-1")
        assert isinstance(lease, ProactiveLease)
        async with lease:
            # 模拟真实 Redis PX 过期（fake 不建模 TTL）：移除冷却键并前移 now_ms，
            # handoff 的内联续租裁决在惰性剪枝后判定租约丢失
            fake.values.pop(_cooldown_key("group-1"), None)
            fake.now_ms += 31_000
            with pytest.raises(ReservationLostError) as excinfo:
                await lease.handoff()
            assert excinfo.value.group_id == "group-1"
            assert excinfo.value.reservation_id == "message-1"

    asyncio.run(_scenario())


def test_handoff_treats_confirmed_reservation_as_alive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """已被 confirm 的预占 handoff 视为存活，正常返回凭据。"""
    service, _fake = _build_service(monkeypatch)

    async def _scenario() -> None:
        lease = await service.reserve("group-1", "message-1")
        assert isinstance(lease, ProactiveLease)
        async with lease:
            await service.confirm("group-1", "message-1", cooldown_seconds=300)
            handoff = await lease.handoff()
            assert handoff.group_id == "group-1"
            assert handoff.reservation_id == "message-1"
            assert handoff.cooldown_seconds == 300

    asyncio.run(_scenario())


def test_double_handoff_raises_state_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """重复 handoff 抛 ReservationStateError（状态机非法迁移）。"""
    service, _fake = _build_service(monkeypatch)

    async def _scenario() -> None:
        lease = await service.reserve("group-1", "message-1")
        assert isinstance(lease, ProactiveLease)
        async with lease:
            first = await lease.handoff()
            assert first.reservation_id == "message-1"
            with pytest.raises(ReservationStateError):
                await lease.handoff()

    asyncio.run(_scenario())


def test_handoff_after_exit_raises_state_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """租约退出（未移交已释放）后再 handoff 抛 ReservationStateError（终态拒绝）。"""
    service, _fake = _build_service(monkeypatch)

    async def _scenario() -> None:
        lease = await service.reserve("group-1", "message-1")
        assert isinstance(lease, ProactiveLease)
        async with lease:
            pass
        with pytest.raises(ReservationStateError):
            await lease.handoff()

    asyncio.run(_scenario())


def test_background_renewal_failure_does_not_kill_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """内部续租遇一次失败只记日志，租约仍可用，后续 handoff 正常成功。

    只断言领域结果（handoff 成功、pending 仍在），不断言日志调用编排。
    """
    service, fake = _build_service(
        monkeypatch,
        proactive_reservation_ttl_seconds=30,
    )
    original_sleep = asyncio.sleep

    async def _fast_sleep(_delay: float) -> None:
        await original_sleep(0)

    monkeypatch.setattr(proactive_reservation_module.asyncio, "sleep", _fast_sleep)

    async def _scenario() -> None:
        lease = await service.reserve("group-1", "message-1")
        assert isinstance(lease, ProactiveLease)
        async with lease:
            # 让后台续租循环先跑起来，再注入一次"续租丢失后恢复"
            for _ in range(3):
                await asyncio.sleep(0)
            fake.fail_renew_count = 1
            for _ in range(5):
                await asyncio.sleep(0)
            handoff = await lease.handoff()
            assert handoff.reservation_id == "message-1"
            assert "pending:message-1" in fake.zsets[_slots_key("group-1")]

    asyncio.run(_scenario())


def test_lease_renewal_uses_frozen_ttl_not_live_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """冻结快照不受配置热更漂移影响：续租延长量按 reserve 时的 ttl。"""
    service, fake = _build_service(
        monkeypatch,
        proactive_reservation_ttl_seconds=30,
    )
    original_sleep = asyncio.sleep

    async def _fast_sleep(_delay: float) -> None:
        await original_sleep(0)

    monkeypatch.setattr(proactive_reservation_module.asyncio, "sleep", _fast_sleep)

    async def _scenario() -> None:
        lease = await service.reserve("group-1", "message-1")
        assert isinstance(lease, ProactiveLease)
        # 配置热更：ttl 改为 900 秒（须在 reserve 冻结快照之后，
        # 续租延长量才按 reserve 时的 30 秒而非热更后的 900 秒）
        monkeypatch.setattr(
            proactive_reservation_module,
            "get_config",
            lambda: SimpleNamespace(
                proactive_cooldown=300,
                proactive_max_per_hour=10,
                proactive_reservation_ttl_seconds=900,
            ),
        )
        async with lease:
            fake.now_ms += 10_000
            for _ in range(5):
                await asyncio.sleep(0)
            assert (
                fake.zsets[_slots_key("group-1")]["pending:message-1"]
                == fake.now_ms + 30_000
            )

    asyncio.run(_scenario())


# ── confirm 语义（持久化身份通道，与租约形态无关） ─────────────


def test_confirm_marks_delivered_and_starts_cooldown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """confirm 把预占转为已送达记录（一小时窗口），冷却值归属 confirmed。"""
    service, fake = _build_service(monkeypatch)

    lease = asyncio.run(service.reserve("group-1", "message-1"))
    assert isinstance(lease, ProactiveLease)
    asyncio.run(
        service.confirm("group-1", "message-1", cooldown_seconds=300),
    )

    slots = fake.zsets[_slots_key("group-1")]
    assert "pending:message-1" not in slots
    assert slots["confirmed:message-1"] == fake.now_ms + _RATE_WINDOW_MS
    assert fake.values[_cooldown_key("group-1")] == "confirmed:message-1"
    # 冷却期内同群拒绝新预占
    denied = asyncio.run(service.reserve("group-1", "message-2"))
    assert isinstance(denied, ReservationDenied)
    assert denied.reason == "cooldown"


def test_confirm_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, fake = _build_service(monkeypatch)

    lease = asyncio.run(service.reserve("group-1", "message-1"))
    assert isinstance(lease, ProactiveLease)
    asyncio.run(
        service.confirm("group-1", "message-1", cooldown_seconds=300),
    )
    asyncio.run(
        service.confirm("group-1", "message-1", cooldown_seconds=300),
    )

    slots = fake.zsets[_slots_key("group-1")]
    assert list(slots) == ["confirmed:message-1"]


def test_confirm_records_expired_reservation_as_delivered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """预占过期按已送达补记：confirmed 成员与冷却仍写入，不抛异常。"""
    service, fake = _build_service(
        monkeypatch,
        proactive_reservation_ttl_seconds=30,
    )

    lease = asyncio.run(service.reserve("group-1", "message-1"))
    assert isinstance(lease, ProactiveLease)
    # 预占 TTL 过期（惰性剪枝在 confirm 时生效）
    fake.values.pop(_cooldown_key("group-1"), None)
    fake.now_ms += 31_000

    asyncio.run(
        service.confirm("group-1", "message-1", cooldown_seconds=300),
    )

    slots = fake.zsets[_slots_key("group-1")]
    assert list(slots) == ["confirmed:message-1"]
    assert fake.values[_cooldown_key("group-1")] == "confirmed:message-1"


def test_expired_reservation_is_lazily_pruned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """过期 pending 成员在下一次滑动窗口操作时被惰性剪枝，释放容量。"""
    service, fake = _build_service(
        monkeypatch,
        proactive_max_per_hour=1,
        proactive_reservation_ttl_seconds=30,
    )

    first = asyncio.run(service.reserve("group-1", "message-1"))
    assert isinstance(first, ProactiveLease)
    fake.values.pop(_cooldown_key("group-1"), None)
    fake.now_ms += 31_000

    second = asyncio.run(service.reserve("group-1", "message-2"))

    assert isinstance(second, ProactiveLease)
    assert list(fake.zsets[_slots_key("group-1")]) == ["pending:message-2"]


def test_confirmed_slot_counts_against_rate_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """已送达名额在一小时窗口内计入防重计数。"""
    service, fake = _build_service(monkeypatch, proactive_max_per_hour=1)

    lease = asyncio.run(service.reserve("group-1", "message-1"))
    assert isinstance(lease, ProactiveLease)
    asyncio.run(
        service.confirm("group-1", "message-1", cooldown_seconds=300),
    )
    # 模拟冷却结束（cooldown PX 过期），但一小时窗口未过期
    fake.values.pop(_cooldown_key("group-1"), None)

    denied = asyncio.run(service.reserve("group-1", "message-2"))
    assert isinstance(denied, ReservationDenied)
    assert denied.reason == "rate_limited"
