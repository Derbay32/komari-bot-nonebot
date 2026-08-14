"""Komari Chat 主动回复预占（proactive reservation）内部 module。

四个预占 Lua 原语从 komari_memory 的 RedisManager 随迁（KOMARIBOT-8），
键前缀改为新所有权 komari_chat:proactive:*。纯内部 module：不进
komari_chat 顶层 __all__，不允许其他插件 import。

TSK-132 起为租约形态：``reserve`` 返回 ``ProactiveLease | ReservationDenied``
联合；生成期经 ``async with`` 激活内部续租、``handoff()`` 内联一次续租
裁决后移交窄凭据（``ReservationHandoff``）。频控拒绝以领域对象暴露，
不再暴露裸字面量字符串。Redis 键布局与四个 Lua 脚本（含行首标记
注释）逐字保留，滑动窗口 / 惰性剪枝 / 预占即写冷却键 / 续租延长 /
孤儿预占纯 TTL 淘汰 / confirm 过期补记语义全部不变。
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Literal, Protocol, Self, cast

from nonebot import logger

from .config_interface import get_config

ReservationDeniedReason = Literal["cooldown", "rate_limited", "duplicate"]

# 一小时滑动窗口与 slots TTL 宽限，语义与旧实现（redis_manager.py）一致
_PROACTIVE_RATE_WINDOW_MS = 3_600_000
_PROACTIVE_SLOTS_TTL_GRACE_MS = 60_000

# 键前缀为新所有权 komari_chat:proactive:*
_PROACTIVE_COOLDOWN_KEY_PREFIX = "komari_chat:proactive:cd:"
_PROACTIVE_SLOTS_KEY_PREFIX = "komari_chat:proactive:slots:"

# 脚本与 komari_memory.services.redis_manager 的 _PROACTIVE_* 一一对应，
# 文本原样保留（脚本内不含键名，仅归属改变）；行首标记注释
# （-- proactive_reserve 等）为测试 fake 客户端的分发约定，必须保留。
_PROACTIVE_RESERVE_SCRIPT = """
-- proactive_reserve
local cooldown_key = KEYS[1]
local slots_key = KEYS[2]
local reservation_id = ARGV[1]
local max_slots = tonumber(ARGV[2])
local reservation_ttl_ms = tonumber(ARGV[3])
local slots_ttl_ms = tonumber(ARGV[4])
local pending_member = "pending:" .. reservation_id
local confirmed_member = "confirmed:" .. reservation_id
local redis_time = redis.call("TIME")
local now_ms = tonumber(redis_time[1]) * 1000
    + math.floor(tonumber(redis_time[2]) / 1000)
local pending_until_ms = now_ms + reservation_ttl_ms

redis.call("ZREMRANGEBYSCORE", slots_key, "-inf", now_ms)
if redis.call("ZSCORE", slots_key, pending_member)
    or redis.call("ZSCORE", slots_key, confirmed_member) then
    return 3
end
if redis.call("EXISTS", cooldown_key) == 1 then
    return 1
end
if redis.call("ZCARD", slots_key) >= max_slots then
    return 2
end

redis.call("ZADD", slots_key, pending_until_ms, pending_member)
redis.call("PEXPIRE", slots_key, slots_ttl_ms)
redis.call("SET", cooldown_key, reservation_id, "PX", reservation_ttl_ms)
return 0
"""
_PROACTIVE_CONFIRM_SCRIPT = """
-- proactive_confirm
local cooldown_key = KEYS[1]
local slots_key = KEYS[2]
local reservation_id = ARGV[1]
local cooldown_ttl_ms = tonumber(ARGV[2])
local slots_ttl_ms = tonumber(ARGV[3])
local rate_window_ms = tonumber(ARGV[4])
local pending_member = "pending:" .. reservation_id
local confirmed_member = "confirmed:" .. reservation_id
local redis_time = redis.call("TIME")
local now_ms = tonumber(redis_time[1]) * 1000
    + math.floor(tonumber(redis_time[2]) / 1000)
local confirmed_until_ms = now_ms + rate_window_ms

redis.call("ZREMRANGEBYSCORE", slots_key, "-inf", now_ms)
if redis.call("ZSCORE", slots_key, confirmed_member) then
    return 2
end

local had_pending = redis.call("ZREM", slots_key, pending_member)
redis.call("ZADD", slots_key, confirmed_until_ms, confirmed_member)
redis.call("PEXPIRE", slots_key, slots_ttl_ms)

local current_cooldown = redis.call("GET", cooldown_key)
if not current_cooldown or current_cooldown == reservation_id then
    redis.call(
        "SET",
        cooldown_key,
        "confirmed:" .. reservation_id,
        "PX",
        cooldown_ttl_ms
    )
end
return had_pending
"""
_PROACTIVE_RENEW_SCRIPT = """
-- proactive_renew
local cooldown_key = KEYS[1]
local slots_key = KEYS[2]
local reservation_id = ARGV[1]
local reservation_ttl_ms = tonumber(ARGV[2])
local slots_ttl_ms = tonumber(ARGV[3])
local pending_member = "pending:" .. reservation_id
local confirmed_member = "confirmed:" .. reservation_id
local redis_time = redis.call("TIME")
local now_ms = tonumber(redis_time[1]) * 1000
    + math.floor(tonumber(redis_time[2]) / 1000)

redis.call("ZREMRANGEBYSCORE", slots_key, "-inf", now_ms)
if redis.call("ZSCORE", slots_key, confirmed_member) then
    return 2
end
if not redis.call("ZSCORE", slots_key, pending_member) then
    return 0
end

redis.call("ZADD", slots_key, now_ms + reservation_ttl_ms, pending_member)
redis.call("PEXPIRE", slots_key, slots_ttl_ms)
if redis.call("GET", cooldown_key) == reservation_id then
    redis.call("PEXPIRE", cooldown_key, reservation_ttl_ms)
end
return 1
"""
_PROACTIVE_RELEASE_SCRIPT = """
-- proactive_release
local cooldown_key = KEYS[1]
local slots_key = KEYS[2]
local reservation_id = ARGV[1]
local pending_member = "pending:" .. reservation_id

local removed = redis.call("ZREM", slots_key, pending_member)
if redis.call("GET", cooldown_key) == reservation_id then
    redis.call("DEL", cooldown_key)
end
return removed
"""


class _RedisExecuteClient(Protocol):
    """具备 execute_command("EVAL", ...) 能力的 Redis 客户端（注入，不新建连接）。"""

    async def execute_command(self, command: str, *args: object) -> object: ...


@dataclass(frozen=True)
class ReservationDenied:
    """预占被拒绝的领域结果（cooldown / rate_limited / duplicate）。"""

    group_id: str
    reservation_id: str
    reason: ReservationDeniedReason


class ReservationLostError(RuntimeError):
    """预占丢失（handoff 裁决时抛出）。携带 group_id / reservation_id 属性。"""

    def __init__(self, *, group_id: str, reservation_id: str) -> None:
        self.group_id = group_id
        self.reservation_id = reservation_id
        super().__init__(
            f"主动回复预占已丢失: group={group_id} reservation={reservation_id}"
        )


class ReservationStateError(RuntimeError):
    """租约状态机非法迁移（重复 handoff、终态后操作）。"""


@dataclass(frozen=True)
class ReservationHandoff:
    """移交窄凭据：只保留释放能力与冻结快照。release() 幂等，永不撤销 confirmed 名额。"""

    group_id: str
    reservation_id: str
    cooldown_seconds: int  # reserve 时刻冻结快照
    _service: ProactiveReservationService = field(repr=False, compare=False)

    async def release(self) -> bool:
        """按冻结身份幂等释放 pending 名额并清除自持冷却键。

        Returns:
            是否移除了 pending 名额；永不撤销 confirmed 名额。
        """
        return await self._service.release(self.group_id, self.reservation_id)


class ProactiveLease:
    """生成期主动回复租约：async with 上下文管理器。

    __aenter__ 内部启动续租任务（节奏 ttl/3，收为内部实现细节）；
    __aexit__ 停止续租任务；未移交则尽力自动释放（释放失败只记日志等
    TTL，绝不抛出）；handoff() 内联一次续租裁决——pending 丢失抛
    ReservationLostError，已确认视为存活；成功则停止续租、迁移到
    handed_off 终态并返回 ReservationHandoff；重复 handoff 或终态后
    handoff 抛 ReservationStateError。无公开 release；不公开
    max_per_hour / reservation_ttl_seconds；不维护 lost event，内部
    续租失败只记日志（不误杀，handoff 时以续租结果为唯一裁决）。
    """

    def __init__(
        self,
        *,
        service: ProactiveReservationService,
        group_id: str,
        reservation_id: str,
        reservation_ttl_seconds: int,
        cooldown_seconds: int,
    ) -> None:
        self._service = service
        self.group_id = group_id
        self.reservation_id = reservation_id
        # reserve 时刻冻结的配置快照（续租延长量与移交凭据的来源）
        self._reservation_ttl_seconds = reservation_ttl_seconds
        self._cooldown_seconds = cooldown_seconds
        self._renew_task: asyncio.Task[None] | None = None
        self._handed_off = False
        self._released = False

    def _renew_interval_seconds(self) -> float:
        """内部续租节奏：TTL 三分之一，至少 1 秒。"""
        return max(1.0, self._reservation_ttl_seconds / 3)

    async def __aenter__(self) -> Self:
        self._renew_task = asyncio.create_task(self._renew_loop())
        return self

    async def _renew_loop(self) -> None:
        """内部续租循环：失败与丢失只记日志，不误杀租约。"""
        while True:
            await asyncio.sleep(self._renew_interval_seconds())
            try:
                alive = await self._service._renew_reservation(
                    group_id=self.group_id,
                    reservation_id=self.reservation_id,
                    reservation_ttl_seconds=self._reservation_ttl_seconds,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "[KomariChat] 主动回复预占续期失败: group={}",
                    self.group_id,
                )
                continue
            if not alive:
                logger.warning(
                    "[KomariChat] 主动回复预占已丢失，停止后台续租: group={}",
                    self.group_id,
                )
                return

    async def __aexit__(
        self,
        _exc_type: object,
        _exc: object,
        _tb: object,
    ) -> None:
        await self._stop_renewal()
        if self._handed_off or self._released:
            return
        self._released = True
        try:
            await self._service.release(self.group_id, self.reservation_id)
        except Exception:
            logger.exception(
                "[KomariChat] 主动回复预占释放失败，将等待 TTL 回收: group={}",
                self.group_id,
            )

    async def _stop_renewal(self) -> None:
        """取消并等待内部续租任务退出（幂等）。"""
        task = self._renew_task
        self._renew_task = None
        if task is None:
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    async def handoff(self) -> ReservationHandoff:
        """内联一次续租裁决；存活则停止续租并移交窄凭据。

        Raises:
            ReservationLostError: pending 已过期/丢失（含惰性剪枝后）。
            ReservationStateError: 重复 handoff 或终态（已释放）后调用。
        """
        if self._handed_off:
            msg = "主动回复租约已移交，禁止重复 handoff"
            raise ReservationStateError(msg)
        if self._released:
            msg = "主动回复租约已释放退出，终态后禁止 handoff"
            raise ReservationStateError(msg)
        alive = await self._service._renew_reservation(
            group_id=self.group_id,
            reservation_id=self.reservation_id,
            reservation_ttl_seconds=self._reservation_ttl_seconds,
        )
        if not alive:
            raise ReservationLostError(
                group_id=self.group_id,
                reservation_id=self.reservation_id,
            )
        self._handed_off = True
        await self._stop_renewal()
        return ReservationHandoff(
            group_id=self.group_id,
            reservation_id=self.reservation_id,
            cooldown_seconds=self._cooldown_seconds,
            _service=self._service,
        )


class ProactiveReservationService:
    """主动回复预占服务：注入 Redis 客户端，module 级扁平操作。"""

    def __init__(self, redis_client: _RedisExecuteClient) -> None:
        self._redis = redis_client

    @staticmethod
    def _cooldown_key(group_id: str) -> str:
        return f"{_PROACTIVE_COOLDOWN_KEY_PREFIX}{group_id}"

    @staticmethod
    def _slots_key(group_id: str) -> str:
        return f"{_PROACTIVE_SLOTS_KEY_PREFIX}{group_id}"

    async def reserve(
        self, group_id: str, reservation_id: str
    ) -> ProactiveLease | ReservationDenied:
        """读取 get_config() 冻结快照并原子预占一个主动回复名额。

        Returns:
            成功返回 ProactiveLease（配置快照冻结在租约上）；失败返回
            ReservationDenied（reason 区分 cooldown / rate_limited /
            duplicate）。
        """
        config = get_config()
        cooldown_seconds = int(config.proactive_cooldown)
        max_per_hour = int(config.proactive_max_per_hour)
        reservation_ttl_seconds = int(config.proactive_reservation_ttl_seconds)

        reservation_ttl_ms = max(1, int(reservation_ttl_seconds * 1000))
        slots_ttl_ms = (
            max(_PROACTIVE_RATE_WINDOW_MS, reservation_ttl_ms)
            + _PROACTIVE_SLOTS_TTL_GRACE_MS
        )
        result = await self._redis.execute_command(
            "EVAL",
            _PROACTIVE_RESERVE_SCRIPT,
            2,
            self._cooldown_key(group_id),
            self._slots_key(group_id),
            reservation_id,
            max(1, int(max_per_hour)),
            reservation_ttl_ms,
            slots_ttl_ms,
        )
        match int(cast("int | str | bytes", result)):
            case 0:
                return ProactiveLease(
                    service=self,
                    group_id=group_id,
                    reservation_id=reservation_id,
                    reservation_ttl_seconds=reservation_ttl_seconds,
                    cooldown_seconds=cooldown_seconds,
                )
            case 1:
                return ReservationDenied(
                    group_id=group_id,
                    reservation_id=reservation_id,
                    reason="cooldown",
                )
            case 2:
                return ReservationDenied(
                    group_id=group_id,
                    reservation_id=reservation_id,
                    reason="rate_limited",
                )
            case 3:
                return ReservationDenied(
                    group_id=group_id,
                    reservation_id=reservation_id,
                    reason="duplicate",
                )
            case code:
                msg = f"Redis 返回未知的主动回复预占状态: {code}"
                raise RuntimeError(msg)

    async def confirm(
        self,
        group_id: str,
        reservation_id: str,
        *,
        cooldown_seconds: int,
    ) -> None:
        """把预占名额原子转换为已送达记录，并开始正式冷却。

        幂等（重复确认无副作用）；预占已过期仍按已送达补记（写 confirmed
        成员与冷却，并保留旧实现的 warning 日志语义）。
        """
        result = await self._redis.execute_command(
            "EVAL",
            _PROACTIVE_CONFIRM_SCRIPT,
            2,
            self._cooldown_key(group_id),
            self._slots_key(group_id),
            reservation_id,
            max(1, int(cooldown_seconds * 1000)),
            _PROACTIVE_RATE_WINDOW_MS + _PROACTIVE_SLOTS_TTL_GRACE_MS,
            _PROACTIVE_RATE_WINDOW_MS,
        )
        code = int(cast("int | str | bytes", result))
        if code == 0:
            logger.warning(
                "[KomariChat] 主动回复预占已过期，按已送达补记: group={}",
                group_id,
            )
        elif code not in {1, 2}:
            msg = f"Redis 返回未知的主动回复确认状态: {code}"
            raise RuntimeError(msg)

    async def _renew_reservation(
        self,
        *,
        group_id: str,
        reservation_id: str,
        reservation_ttl_seconds: int,
    ) -> bool:
        """按冻结 TTL 续期生成中的主动回复预占；已确认记录同样视为存活。

        返回 1/2 存活（pending 续期成功 / confirmed 已送达），0 丢失。
        """
        reservation_ttl_ms = max(1, int(reservation_ttl_seconds * 1000))
        slots_ttl_ms = (
            max(_PROACTIVE_RATE_WINDOW_MS, reservation_ttl_ms)
            + _PROACTIVE_SLOTS_TTL_GRACE_MS
        )
        result = await self._redis.execute_command(
            "EVAL",
            _PROACTIVE_RENEW_SCRIPT,
            2,
            self._cooldown_key(group_id),
            self._slots_key(group_id),
            reservation_id,
            reservation_ttl_ms,
            slots_ttl_ms,
        )
        code = int(cast("int | str | bytes", result))
        if code not in {0, 1, 2}:
            msg = f"Redis 返回未知的主动回复续期状态: {code}"
            raise RuntimeError(msg)
        return code in {1, 2}

    async def release(self, group_id: str, reservation_id: str) -> bool:
        """按持久化的群与预占 ID 幂等释放 pending 名额（无需进程句柄）。

        崩溃恢复只凭冻结的群与预占 ID 也能释放；永不撤销 confirmed 名额。
        """
        result = await self._redis.execute_command(
            "EVAL",
            _PROACTIVE_RELEASE_SCRIPT,
            2,
            self._cooldown_key(group_id),
            self._slots_key(group_id),
            reservation_id,
        )
        return int(cast("int | str | bytes", result)) > 0
