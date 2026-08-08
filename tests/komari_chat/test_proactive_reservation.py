"""proactive_reservation module 级测试（KOMARIBOT-8 验收基线）。

真实 module + Lua 语义级 fake Redis 客户端：断言 Redis 键 / 成员 / score /
冷却值等外部可观察行为语义，不断言参数转发。now_ms 可前移以模拟时间流逝。
键前缀为新所有权 komari_chat:proactive:*；Lua 语义与 findings §2 一致
（滑动窗口 / 惰性剪枝 / 预占即写冷却键 / 续租延长 / 孤儿纯 TTL 淘汰）。
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
Reservation = proactive_reservation_module.Reservation
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
    滑动窗口成员过期经 now_ms 前移 + 惰性剪枝建模。
    """

    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.zsets: dict[str, dict[str, float]] = {}
        self.now_ms = 1_000_000.0

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


def test_reserve_returns_handle_with_frozen_config_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """reserve 成功返回句柄，配置快照在 reserve 时一次性冻结。"""
    service, fake = _build_service(
        monkeypatch,
        proactive_cooldown=123,
        proactive_max_per_hour=7,
        proactive_reservation_ttl_seconds=45,
    )

    reservation = asyncio.run(service.reserve("group-1", "message-1"))

    assert isinstance(reservation, Reservation)
    assert reservation.group_id == "group-1"
    assert reservation.reservation_id == "message-1"
    assert reservation.reservation_ttl_seconds == 45
    assert reservation.cooldown_seconds == 123
    assert reservation.max_per_hour == 7
    # 预占即写冷却键（同群生成期串行阻断），键前缀为新所有权
    assert fake.values[_cooldown_key("group-1")] == "message-1"
    slots = fake.zsets[_slots_key("group-1")]
    assert slots["pending:message-1"] == fake.now_ms + 45_000


def test_reserve_rejects_during_cooldown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, fake = _build_service(monkeypatch)

    first = asyncio.run(service.reserve("group-1", "message-1"))
    second = asyncio.run(service.reserve("group-1", "message-2"))

    assert isinstance(first, Reservation)
    assert second == "cooldown"
    assert "pending:message-2" not in fake.zsets[_slots_key("group-1")]


def test_reserve_rejects_when_rate_limited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, fake = _build_service(monkeypatch, proactive_max_per_hour=1)

    first = asyncio.run(service.reserve("group-1", "message-1"))
    # 模拟生成期冷却键过期（fake 不建模 PX TTL）
    fake.values.pop(_cooldown_key("group-1"), None)
    second = asyncio.run(service.reserve("group-1", "message-2"))

    assert isinstance(first, Reservation)
    assert second == "rate_limited"


def test_reserve_rejects_duplicate_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _fake = _build_service(monkeypatch)

    first = asyncio.run(service.reserve("group-1", "message-1"))
    duplicate = asyncio.run(service.reserve("group-1", "message-1"))

    assert isinstance(first, Reservation)
    assert duplicate == "duplicate"


def test_reserve_rejects_duplicate_against_confirmed_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _fake = _build_service(monkeypatch)

    reservation = asyncio.run(service.reserve("group-1", "message-1"))
    assert isinstance(reservation, Reservation)
    asyncio.run(
        service.confirm("group-1", "message-1", cooldown_seconds=300),
    )
    duplicate = asyncio.run(service.reserve("group-1", "message-1"))

    assert duplicate == "duplicate"


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

    handles = [r for r in results if isinstance(r, Reservation)]
    assert len(handles) == 1
    assert results.count("cooldown") == 1


def test_release_restores_capacity_and_clears_cooldown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, fake = _build_service(monkeypatch, proactive_max_per_hour=1)

    reservation = asyncio.run(service.reserve("group-1", "message-1"))
    assert isinstance(reservation, Reservation)
    released = asyncio.run(reservation.release())
    second = asyncio.run(service.reserve("group-1", "message-2"))

    assert released is True
    assert fake.zsets[_slots_key("group-1")] == {}
    assert _cooldown_key("group-1") not in fake.values
    assert isinstance(second, Reservation)


def test_release_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _fake = _build_service(monkeypatch)

    reservation = asyncio.run(service.reserve("group-1", "message-1"))
    assert isinstance(reservation, Reservation)

    assert asyncio.run(reservation.release()) is True
    assert asyncio.run(reservation.release()) is False


def test_release_does_not_revoke_confirmed_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """release 幂等且永不撤销已确认名额。"""
    service, fake = _build_service(monkeypatch)

    reservation = asyncio.run(service.reserve("group-1", "message-1"))
    assert isinstance(reservation, Reservation)
    asyncio.run(
        service.confirm("group-1", "message-1", cooldown_seconds=300),
    )

    assert asyncio.run(reservation.release()) is False
    assert "confirmed:message-1" in fake.zsets[_slots_key("group-1")]


def test_renew_extends_pending_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, fake = _build_service(
        monkeypatch,
        proactive_reservation_ttl_seconds=30,
    )

    reservation = asyncio.run(service.reserve("group-1", "message-1"))
    assert isinstance(reservation, Reservation)
    original_expiry = fake.zsets[_slots_key("group-1")]["pending:message-1"]
    fake.now_ms += 10_000

    renewed = asyncio.run(reservation.renew())

    assert renewed is True
    new_expiry = fake.zsets[_slots_key("group-1")]["pending:message-1"]
    assert new_expiry == fake.now_ms + 30_000
    assert new_expiry > original_expiry


def test_renew_returns_false_after_lease_expires(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """孤儿预占纯靠 TTL 淘汰：过期后续租失败，不引入后台回收。"""
    service, fake = _build_service(
        monkeypatch,
        proactive_reservation_ttl_seconds=30,
    )

    reservation = asyncio.run(service.reserve("group-1", "message-1"))
    assert isinstance(reservation, Reservation)
    fake.now_ms += 31_000

    assert asyncio.run(reservation.renew()) is False


def test_renew_treats_confirmed_as_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _fake = _build_service(monkeypatch)

    reservation = asyncio.run(service.reserve("group-1", "message-1"))
    assert isinstance(reservation, Reservation)
    asyncio.run(
        service.confirm("group-1", "message-1", cooldown_seconds=300),
    )

    assert asyncio.run(reservation.renew()) is True


def test_renew_uses_frozen_ttl_not_live_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """冻结快照不受配置热更漂移影响：续租延长量按 reserve 时的 ttl。"""
    service, fake = _build_service(
        monkeypatch,
        proactive_reservation_ttl_seconds=30,
    )

    reservation = asyncio.run(service.reserve("group-1", "message-1"))
    assert isinstance(reservation, Reservation)
    # 配置热更：ttl 改为 900 秒
    monkeypatch.setattr(
        proactive_reservation_module,
        "get_config",
        lambda: SimpleNamespace(
            proactive_cooldown=300,
            proactive_max_per_hour=10,
            proactive_reservation_ttl_seconds=900,
        ),
    )
    fake.now_ms += 10_000

    assert asyncio.run(reservation.renew()) is True
    assert (
        fake.zsets[_slots_key("group-1")]["pending:message-1"]
        == fake.now_ms + 30_000
    )


def test_confirm_marks_delivered_and_starts_cooldown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """confirm 把预占转为已送达记录（一小时窗口），冷却值归属 confirmed。"""
    service, fake = _build_service(monkeypatch)

    reservation = asyncio.run(service.reserve("group-1", "message-1"))
    assert isinstance(reservation, Reservation)
    asyncio.run(
        service.confirm("group-1", "message-1", cooldown_seconds=300),
    )

    slots = fake.zsets[_slots_key("group-1")]
    assert "pending:message-1" not in slots
    assert slots["confirmed:message-1"] == fake.now_ms + _RATE_WINDOW_MS
    assert fake.values[_cooldown_key("group-1")] == "confirmed:message-1"
    # 冷却期内同群拒绝新预占
    assert asyncio.run(service.reserve("group-1", "message-2")) == "cooldown"


def test_confirm_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, fake = _build_service(monkeypatch)

    reservation = asyncio.run(service.reserve("group-1", "message-1"))
    assert isinstance(reservation, Reservation)
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

    reservation = asyncio.run(service.reserve("group-1", "message-1"))
    assert isinstance(reservation, Reservation)
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
    assert isinstance(first, Reservation)
    fake.values.pop(_cooldown_key("group-1"), None)
    fake.now_ms += 31_000

    second = asyncio.run(service.reserve("group-1", "message-2"))

    assert isinstance(second, Reservation)
    assert list(fake.zsets[_slots_key("group-1")]) == ["pending:message-2"]


def test_confirmed_slot_counts_against_rate_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """已送达名额在一小时窗口内计入防重计数。"""
    service, fake = _build_service(monkeypatch, proactive_max_per_hour=1)

    reservation = asyncio.run(service.reserve("group-1", "message-1"))
    assert isinstance(reservation, Reservation)
    asyncio.run(
        service.confirm("group-1", "message-1", cooldown_seconds=300),
    )
    # 模拟冷却结束（cooldown PX 过期），但一小时窗口未过期
    fake.values.pop(_cooldown_key("group-1"), None)

    assert asyncio.run(service.reserve("group-1", "message-2")) == "rate_limited"
