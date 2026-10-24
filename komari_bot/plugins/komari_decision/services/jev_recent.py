"""判定专属 Redis 近期上下文窗口。

本模块是 ``decision`` 自己的 Redis 布局，**不读也不写**回复生成历史与记忆
缓冲区：每群一份独立的哈希（记录正文）、两个有序集合（接收顺序与接收时刻）、
序号计数器与状态哈希（confirmed / provisional）。所有会影响「可见窗口」的
操作都是单个 Lua 脚本，因此「按截止点筛出前十条合格前文、再追加本次目标」
在同一次原子调用内完成，不会出现半写入窗口，也不会因为先取十条再过滤而
少看到合格前文。

保留策略（TSK-180 §4）：最近 10 条 confirmed 记录 + **全部**尚未定案的
provisional 记录。provisional 的定案**只依据持久事实**（同一串行化入口内读到
的执行状态与缓存处置布尔值），不依据时间、预算或猜测的分类：仍在排队/执行
的记录无论多旧都保持 provisional，只有持久行已终结或根本没有可运行所有者时
才按持久事实确认/移除。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Final, Literal, NamedTuple, cast

from komari_bot.decision.jev import DecisionMessage

if TYPE_CHECKING:
    from collections.abc import Awaitable, Mapping, Sequence

    from redis.asyncio import Redis

RECENT_WINDOW_SIZE: Final = 10
"""目标之前最多取用的合格近期消息数量。"""

_PROVISIONAL: Final = "p"
_CONFIRMED: Final = "c"

_KEY_PREFIX: Final = "komari:decision:recent:v1"

_PRUNE_STEP: Final = """
local ids = redis.call('ZREVRANGE', order, 0, -1)
local kept = 0
for _, id in ipairs(ids) do
  local body = redis.call('HGET', entries, id)
  if body then
    if redis.call('HGET', states, id) == 'c' then
      kept = kept + 1
      if kept > keep then
        redis.call('HDEL', entries, id)
        redis.call('ZREM', order, id)
        redis.call('HDEL', states, id)
        redis.call('ZREM', times, id)
        changed = changed + 1
      end
    end
  else
    redis.call('ZREM', order, id)
    redis.call('HDEL', states, id)
    redis.call('ZREM', times, id)
  end
end
"""
"""保留最新 keep 条 confirmed；provisional 一律保留，绝不按时间删除。"""

_SNAPSHOT_APPEND_SCRIPT: Final = (
    """
local entries = KEYS[1]
local order = KEYS[2]
local counter = KEYS[3]
local states = KEYS[4]
local times = KEYS[5]
local message_id = ARGV[1]
local payload = ARGV[2]
local limit = tonumber(ARGV[3])
local cutoff = tonumber(ARGV[4])
local received = tonumber(ARGV[5])
local keep = tonumber(ARGV[6])
local score = redis.call('ZSCORE', order, message_id)
if not score then
  score = redis.call('INCR', counter)
  redis.call('HSET', entries, message_id, payload)
  redis.call('ZADD', order, score, message_id)
  redis.call('ZADD', times, received, message_id)
  redis.call('HSET', states, message_id, 'p')
end
-- 先按接收截止与身份筛选，再取最近 limit 条：过滤发生在计数之前。
local candidates = redis.call('ZREVRANGEBYSCORE', order, '(' .. score, '-inf')
local out = {tonumber(score)}
for _, id in ipairs(candidates) do
  if #out > limit then
    break
  end
  local at = tonumber(redis.call('ZSCORE', times, id))
  if at ~= nil and at <= cutoff and id ~= message_id then
    local body = redis.call('HGET', entries, id)
    if body then
      out[#out + 1] = body
    end
  end
end
local changed = 0
"""
    + _PRUNE_STEP
    + """
return out
"""
)

_SETTLE_SCRIPT: Final = (
    """
local entries = KEYS[1]
local order = KEYS[2]
local states = KEYS[3]
local times = KEYS[4]
local keep = tonumber(ARGV[1])
local changed = 0
for index = 2, #ARGV, 2 do
  local message_id = ARGV[index]
  local action = ARGV[index + 1]
  if redis.call('HEXISTS', entries, message_id) == 1 then
    if action == 'remove' then
      -- 只撤销 provisional；已确认的送达事实没有持久依据被删除。
      if redis.call('HGET', states, message_id) ~= 'c' then
        redis.call('HDEL', entries, message_id)
        redis.call('ZREM', order, message_id)
        redis.call('HDEL', states, message_id)
        redis.call('ZREM', times, message_id)
        changed = changed + 1
      end
    elseif action == 'confirm' then
      if redis.call('HGET', states, message_id) ~= 'c' then
        redis.call('HSET', states, message_id, 'c')
        changed = changed + 1
      end
    end
  end
end
"""
    + _PRUNE_STEP
    + """
return changed
"""
)

_OBSERVE_SCRIPT: Final = (
    """
local entries = KEYS[1]
local order = KEYS[2]
local counter = KEYS[3]
local states = KEYS[4]
local times = KEYS[5]
local message_id = ARGV[1]
local payload = ARGV[2]
local keep = tonumber(ARGV[3])
local received = tonumber(ARGV[4])
local changed = 0
if redis.call('HEXISTS', entries, message_id) == 1 then
  -- 幂等：同一平台消息身份只吸收一次，重复送达只补确认状态。
  if redis.call('HGET', states, message_id) ~= 'c' then
    redis.call('HSET', states, message_id, 'c')
    changed = changed + 1
  end
else
  local seq = redis.call('INCR', counter)
  redis.call('HSET', entries, message_id, payload)
  redis.call('ZADD', order, seq, message_id)
  redis.call('ZADD', times, received, message_id)
  redis.call('HSET', states, message_id, 'c')
  changed = changed + 1
end
"""
    + _PRUNE_STEP
    + """
return changed
"""
)

ResolveAction = Literal["confirm", "remove"]


def _text(value: object) -> str:
    """把 Redis 返回值统一成 ``str``（客户端不要求 decode_responses）。"""
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return str(value)


async def _eval(
    redis: Redis,
    script: str,
    keys: Sequence[str],
    args: Sequence[str],
) -> object:
    """执行 Lua 脚本（redis-py 的异步 ``eval`` 返回可等待对象）。"""
    return await cast(
        "Awaitable[object]", redis.eval(script, len(keys), *keys, *args)
    )


def _scope_digest(protocol: str, account_id: str, group_id: str) -> str:
    """返回与身份一一对应的稳定键片段（不把原始平台身份写进键名）。"""
    digest = hashlib.blake2b(digest_size=16)
    for part in (protocol, account_id, group_id):
        encoded = part.encode("utf-8")
        digest.update(len(encoded).to_bytes(4, "big"))
        digest.update(encoded)
    return digest.hexdigest()


class ScopeKeys(NamedTuple):
    """一个群作用域的窗口键（按脚本需要逐项取用，绝不靠位置切片）。"""

    entries: str
    order: str
    seq: str
    states: str
    times: str

    def all(self) -> tuple[str, ...]:
        """该作用域的全部键（供诊断与显式清理）。"""
        return (self.entries, self.order, self.seq, self.states, self.times)


def scope_keys(protocol: str, account_id: str, group_id: str) -> ScopeKeys:
    """返回一个群作用域的窗口键名（内部布局工厂，生产路径与清理共用）。"""
    base = f"{_KEY_PREFIX}:{_scope_digest(protocol, account_id, group_id)}"
    return ScopeKeys(
        entries=f"{base}:entries",
        order=f"{base}:order",
        seq=f"{base}:seq",
        states=f"{base}:states",
        times=f"{base}:times",
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class RecentRecord:
    """窗口中一条已接收的合格消息（判定自有事实，不含过滤原文）。"""

    message: DecisionMessage

    @classmethod
    def from_message(cls, message: DecisionMessage) -> RecentRecord:
        return cls(message=message)

    def encode(self) -> str:
        """序列化为窗口载荷；缺省的可选字段不写成 JSON null。"""
        message = self.message
        payload: dict[str, Any] = {
            "message_id": message.message_id,
            "group_id": message.group_id,
            "sender_id": message.sender_id,
            "display_name": message.display_name,
            "text": message.text,
            "timestamp": message.timestamp.isoformat(),
            "is_bot": message.is_bot,
            "reference_ids": list(message.reference_ids),
            "mention_ids": list(message.mention_ids),
            "image_present": message.image_present,
        }
        if message.image_description is not None:
            payload["image_description"] = message.image_description
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)

    @classmethod
    def decode(cls, raw: str) -> RecentRecord:
        payload = json.loads(raw)
        return cls(
            message=DecisionMessage(
                message_id=str(payload["message_id"]),
                group_id=str(payload["group_id"]),
                sender_id=str(payload["sender_id"]),
                display_name=str(payload.get("display_name") or ""),
                text=str(payload.get("text") or ""),
                timestamp=datetime.fromisoformat(str(payload["timestamp"])),
                is_bot=bool(payload.get("is_bot", False)),
                reference_ids=tuple(
                    str(item) for item in payload.get("reference_ids") or ()
                ),
                mention_ids=tuple(
                    str(item) for item in payload.get("mention_ids") or ()
                ),
                image_present=bool(payload.get("image_present", False)),
                image_description=payload.get("image_description"),
            )
        )


class DecisionRecentStore:
    """判定专属近期上下文窗口（Redis，原子快照后追加 + 事实定案）。"""

    def __init__(
        self,
        redis: Redis,
        *,
        window_size: int = RECENT_WINDOW_SIZE,
    ) -> None:
        self._redis = redis
        self._window_size = window_size

    async def freeze_for_target(
        self,
        *,
        protocol: str,
        account_id: str,
        target: DecisionMessage,
        cutoff: datetime,
    ) -> tuple[DecisionMessage, ...]:
        """原子筛出目标之前的合格窗口，并把目标作为 provisional 追加。

        接收截止与身份筛选发生在「取最近十条」之前（脚本内完成），因此未来
        消息既不会进入旧快照，也不会挤掉合格前文。返回按接收顺序升序排列的
        最多 ``window_size`` 条前文。
        """
        keys = scope_keys(protocol, account_id, target.group_id)
        raw = await _eval(
            self._redis,
            _SNAPSHOT_APPEND_SCRIPT,
            (keys.entries, keys.order, keys.seq, keys.states, keys.times),
            (
                target.message_id,
                RecentRecord.from_message(target).encode(),
                str(self._window_size),
                str(cutoff.timestamp()),
                str(target.timestamp.timestamp()),
                str(self._window_size),
            ),
        )
        if not isinstance(raw, list):
            msg = "近期上下文窗口读取结果不是列表"
            raise TypeError(msg)
        rows = cast("list[object]", raw)
        selected: list[DecisionMessage] = []
        for item in reversed(rows[1:]):  # 脚本返回降序，这里恢复接收顺序
            record = RecentRecord.decode(_text(item)).message
            if record.message_id == target.message_id:
                continue
            if record.group_id != target.group_id:
                continue
            if record.timestamp > cutoff:
                continue
            selected.append(record)
        return tuple(selected[-self._window_size :])

    async def provisional_ids(
        self,
        *,
        protocol: str,
        account_id: str,
        group_id: str,
    ) -> tuple[str, ...]:
        """只读返回该作用域仍未定案的消息 id（不读取任何正文）。"""
        keys = scope_keys(protocol, account_id, group_id)
        members = cast(
            "list[object]",
            await cast(
                "Awaitable[list[object]]", self._redis.zrange(keys.order, 0, -1)
            ),
        )
        pending: list[str] = []
        for member in members:
            message_id = _text(member)
            state = await cast(
                "Awaitable[object]",
                self._redis.hget(keys.states, message_id),
            )
            if state is not None and _text(state) == _PROVISIONAL:
                pending.append(message_id)
        return tuple(pending)

    async def settle(
        self,
        *,
        protocol: str,
        account_id: str,
        group_id: str,
        decisions: Mapping[str, ResolveAction],
    ) -> None:
        """按持久事实定案已存在的窗口条目（绝不新建、绝不取回历史）。"""
        if not decisions:
            return
        keys = scope_keys(protocol, account_id, group_id)
        args: list[str] = [str(self._window_size)]
        for message_id, action in decisions.items():
            args.append(message_id)
            args.append(action)
        await _eval(
            self._redis,
            _SETTLE_SCRIPT,
            (keys.entries, keys.order, keys.states, keys.times),
            args,
        )

    async def resolve(
        self,
        *,
        protocol: str,
        account_id: str,
        group_id: str,
        message_id: str,
        action: ResolveAction,
    ) -> None:
        """定案单条已存在的条目（本次分发自己的 provisional）。"""
        await self.settle(
            protocol=protocol,
            account_id=account_id,
            group_id=group_id,
            decisions={message_id: action},
        )

    async def observe_confirmed(
        self,
        *,
        protocol: str,
        account_id: str,
        message: DecisionMessage,
    ) -> bool:
        """把一条已确认的合格消息写入窗口（按实际消息身份幂等）。"""
        keys = scope_keys(protocol, account_id, message.group_id)
        changed = await _eval(
            self._redis,
            _OBSERVE_SCRIPT,
            (keys.entries, keys.order, keys.seq, keys.states, keys.times),
            (
                message.message_id,
                RecentRecord.from_message(message).encode(),
                str(self._window_size),
                str(message.timestamp.timestamp()),
            ),
        )
        return bool(int(cast("int", changed) or 0))

    async def window(
        self,
        *,
        protocol: str,
        account_id: str,
        group_id: str,
    ) -> tuple[DecisionMessage, ...]:
        """只读返回当前窗口（确认记录 + 未决 provisional，按接收顺序）。"""
        keys = scope_keys(protocol, account_id, group_id)
        members = cast(
            "list[object]",
            await cast(
                "Awaitable[list[object]]", self._redis.zrange(keys.order, 0, -1)
            ),
        )
        messages: list[DecisionMessage] = []
        for member in members:
            payload = await cast(
                "Awaitable[object]", self._redis.hget(keys.entries, _text(member))
            )
            if payload is None:
                continue
            messages.append(RecentRecord.decode(_text(payload)).message)
        return tuple(messages)


def resolve_action_for_retention(*, retain_recent: bool | None) -> ResolveAction:
    """把保留结论映射为窗口定案动作（未分类时保留既有处理）。"""
    if retain_recent is None:
        return "confirm"
    return "confirm" if retain_recent else "remove"


__all__ = [
    "RECENT_WINDOW_SIZE",
    "DecisionRecentStore",
    "RecentRecord",
    "ResolveAction",
    "ScopeKeys",
    "resolve_action_for_retention",
    "scope_keys",
]
