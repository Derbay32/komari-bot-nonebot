"""Komari Chat 主动回复预占（proactive reservation）内部 module。

四个预占 Lua 原语从 komari_memory 的 RedisManager 随迁（KOMARIBOT-8），
键前缀改为新所有权 komari_chat:proactive:*。纯内部 module：不进
komari_chat 顶层 __all__，不允许其他插件 import；本票无生产调用方，
生产行为零变化（含滑动窗口 / 惰性剪枝 / 预占即写冷却键 / 续租延长 /
孤儿预占纯 TTL 淘汰 / confirm 过期补记语义）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol, cast

from nonebot import logger

from .config_interface import get_config

ProactiveReservationStatus = Literal["cooldown", "rate_limited", "duplicate"]

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


@dataclass
class Reservation:
    """一次主动回复预占的句柄；后三个配置字段为 reserve 时冻结的快照。"""

    group_id: str
    reservation_id: str
    reservation_ttl_seconds: int
    cooldown_seconds: int
    max_per_hour: int
    _service: ProactiveReservationService

    async def renew(self) -> bool:
        """续租 pending 名额（含冷却键同步延长语义）。

        已确认记录视为成功（True）；预占过期/丢失返回 False。
        """
        return await self._service._renew_reservation(self)

    async def release(self) -> bool:
        """幂等释放 pending 名额并清除自己持有的冷却键。

        Returns:
            是否移除了 pending 名额；永不撤销 confirmed 名额。
        """
        return await self._service._release_reservation(self)


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
    ) -> Reservation | ProactiveReservationStatus:
        """读取 get_config() 冻结快照并原子预占一个主动回复名额。

        Returns:
            成功返回 Reservation 句柄（配置快照冻结在句柄上）；失败返回
            "cooldown" / "rate_limited" / "duplicate"。
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
                return Reservation(
                    group_id=group_id,
                    reservation_id=reservation_id,
                    reservation_ttl_seconds=reservation_ttl_seconds,
                    cooldown_seconds=cooldown_seconds,
                    max_per_hour=max_per_hour,
                    _service=self,
                )
            case 1:
                return "cooldown"
            case 2:
                return "rate_limited"
            case 3:
                return "duplicate"
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

    async def _renew_reservation(self, reservation: Reservation) -> bool:
        """续期生成中的主动回复预占；已确认记录同样视为成功。"""
        reservation_ttl_ms = max(
            1, int(reservation.reservation_ttl_seconds * 1000)
        )
        slots_ttl_ms = (
            max(_PROACTIVE_RATE_WINDOW_MS, reservation_ttl_ms)
            + _PROACTIVE_SLOTS_TTL_GRACE_MS
        )
        result = await self._redis.execute_command(
            "EVAL",
            _PROACTIVE_RENEW_SCRIPT,
            2,
            self._cooldown_key(reservation.group_id),
            self._slots_key(reservation.group_id),
            reservation.reservation_id,
            reservation_ttl_ms,
            slots_ttl_ms,
        )
        code = int(cast("int | str | bytes", result))
        if code not in {0, 1, 2}:
            msg = f"Redis 返回未知的主动回复续期状态: {code}"
            raise RuntimeError(msg)
        return code in {1, 2}

    async def _release_reservation(self, reservation: Reservation) -> bool:
        """释放尚未确认送达的主动回复预占；重复释放安全。"""
        result = await self._redis.execute_command(
            "EVAL",
            _PROACTIVE_RELEASE_SCRIPT,
            2,
            self._cooldown_key(reservation.group_id),
            self._slots_key(reservation.group_id),
            reservation.reservation_id,
        )
        return int(cast("int | str | bytes", result)) > 0
