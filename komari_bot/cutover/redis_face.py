"""cutover CLI 的 Redis 操作面（扫描、隔离、休眠 sidecar、预占释放）。

finalize-redis / capture-evidence 共用的只读扫描与处置原语：

- 扫描闭集：``komari_memory:global_interaction:*`` 全族摘除（RENAME 到
  quarantine 命名空间并 PERSIST，原字节保留）；可归因（payload 为含
  ``group_id`` 的 JSON 对象）的 staging/buffer 键写 dormancy sidecar 并
  PERSIST；归因损坏的一律走 quarantine；
- quarantine digest 公式（设计契约字面量）：``sha256(原key字节+payload字节)``
  无分隔符拼接；attestation 聚合 digest 按底册 ``entry_id`` 序确定性重算，
  与 0013 迁移内联实现逐字节一致（两侧同步维护，禁止漂移）；
- 预占释放与 ``proactive_reservation`` 的 release Lua 保持等价
  （ZREM slots pending 成员 + 自持冷却键 DEL），永不撤销 confirmed 名额。
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any

#: capture-evidence 证据扫描前缀闭集（quarantine/dormancy 命名空间排除）。
_EVIDENCE_SCAN_PREFIXES = ("komari_memory:", "komari_chat:proactive:")

#: finalize-redis 处置扫描的 active legacy 族前缀闭集。
ACTIVE_FAMILY_PREFIXES = (
    "komari_memory:global_interaction:",
    "komari_memory:staging:",
    "komari_memory:buffer:",
)

QUARANTINE_NAMESPACE = "komari_memory:quarantine:v1:"
DORMANCY_NAMESPACE = "komari_memory:dormancy:v1:"

#: 与 komari_chat/services/proactive_reservation.py 的
#: _PROACTIVE_RELEASE_SCRIPT 文本等价（脚本内不含键名；键经 KEYS 注入）：
#: ZREM slots 的 pending 成员 + 仅当冷却键仍归属本预占时 DEL，
#: 返回移除的 pending 数；confirmed 名额绝不撤销。
PROACTIVE_RELEASE_SCRIPT = """
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


def payload_digest(key: str, serialized_value: str) -> str:
    """quarantine 底册摘要：sha256(原key字节 + payload字节)，无分隔符。"""
    return hashlib.sha256(key.encode("utf-8") + serialized_value.encode("utf-8")).hexdigest()


def aggregate_attestation_digest(rows: list[tuple[str, str]]) -> str:
    """按底册行序聚合 attestation 摘要（64-hex）。

    公式为实现裁定契约：以 ``(key_family, payload_digest)`` 按 entry_id
    升序拼接 ``key_family + \\x1f + payload_digest + \\n`` 后取 SHA-256。
    finalize-redis 写入与 0013 迁移重算必须使用同一公式。
    """
    hasher = hashlib.sha256()
    for key_family, digest in rows:
        hasher.update(f"{key_family}\x1f{digest}\n".encode())
    return hasher.hexdigest()


async def _serialize_redis_value(client: Any, key: str) -> str:
    """按键类型确定性序列化值文本（digest 输入；绝不回显到输出面）。"""
    key_type = await client.type(key)
    if key_type == "string":
        value = await client.get(key)
        return "" if value is None else str(value)
    if key_type == "hash":
        mapping = await client.hgetall(key)
        return "\n".join(f"{field}={mapping[field]}" for field in sorted(mapping))
    if key_type == "set":
        members = list(await client.smembers(key))
        return "\n".join(sorted(members))
    if key_type == "zset":
        pairs = await client.zrange(key, 0, -1, withscores=True)
        return "\n".join(f"{member}:{score:.17g}" for member, score in pairs)
    if key_type == "list":
        items = await client.lrange(key, 0, -1)
        return "\n".join(items)
    return str(key_type)


async def _scan_family_keys(client: Any, prefix: str) -> list[str]:
    keys = [key async for key in client.scan_iter(f"{prefix}*")]
    return sorted(keys)


async def scan_evidence_entries(client: Any) -> list[str]:
    """capture-evidence：扫描证据面并返回排序的 per-key SHA-256 hex 清单。"""
    entries: list[str] = []
    seen: set[str] = set()
    for prefix in _EVIDENCE_SCAN_PREFIXES:
        for key in await _scan_family_keys(client, prefix):
            if key in seen or key.startswith(QUARANTINE_NAMESPACE):
                continue
            if key.startswith(DORMANCY_NAMESPACE):
                continue
            seen.add(key)
            serialized = await _serialize_redis_value(client, key)
            entries.append(payload_digest(key, serialized))
    return sorted(entries)


def evidence_aggregate_digest(entries: list[str]) -> str:
    """证据聚合 digest：排序条目换行拼接后取 SHA-256（空集同样有确定值）。"""
    hasher = hashlib.sha256()
    for entry in entries:
        hasher.update(f"{entry}\n".encode())
    return hasher.hexdigest()


async def connect_redis(redis_url: str) -> Any:
    """建立 Redis 连接（短连接超时保证不可达时快速失败）。"""
    import redis.asyncio as aioredis

    return aioredis.from_url(
        redis_url,
        decode_responses=True,
        socket_connect_timeout=5.0,
        socket_timeout=10.0,
    )


def _attributable_group_id(serialized_value: str) -> str | None:
    """归因解析：payload 是含 group_id 的 JSON 对象时返回该群号。"""
    try:
        parsed = json.loads(serialized_value)
    except json.JSONDecodeError:
        return None
    if isinstance(parsed, dict) and "group_id" in parsed:
        group_id = parsed["group_id"]
        if isinstance(group_id, (str, int)) and not isinstance(group_id, bool):
            return str(group_id)
    return None


class FinalizePlan:
    """一次 finalize 处置计划：隔离/休眠清单与聚合计数。"""

    def __init__(self) -> None:
        self.quarantine_keys: list[str] = []
        self.dormancy_keys: list[str] = []
        self.serialized_values: dict[str, str] = {}

    @property
    def quarantined_count(self) -> int:
        return len(self.quarantine_keys)

    @property
    def dormant_count(self) -> int:
        return len(self.dormancy_keys)


async def build_finalize_plan(client: Any) -> FinalizePlan:
    """扫描 active legacy 族并为每个键定处置：全族隔离或归因休眠/隔离。

    - ``global_interaction`` 族键一律 RENAME 进 quarantine 命名空间并
      PERSIST（set/hash 等结构键同样整键搬运，原字节保留）；
    - staging/buffer 键可归因（JSON 含 group_id）→ dormancy sidecar +
      原 key PERSIST；归因损坏 → quarantine；
    - 已带 dormancy sidecar 的 staging/buffer 键视为已处置，不再重复处理。
    """
    plan = FinalizePlan()
    for prefix in ACTIVE_FAMILY_PREFIXES:
        for key in await _scan_family_keys(client, prefix):
            if key.startswith((QUARANTINE_NAMESPACE, DORMANCY_NAMESPACE)):
                continue
            if prefix != "komari_memory:global_interaction:":
                if await client.exists(DORMANCY_NAMESPACE + key):
                    continue
                serialized = await _serialize_redis_value(client, key)
                if _attributable_group_id(serialized) is not None:
                    plan.dormancy_keys.append(key)
                    plan.serialized_values[key] = serialized
                    continue
                plan.quarantine_keys.append(key)
                plan.serialized_values[key] = serialized
                continue
            plan.quarantine_keys.append(key)
            plan.serialized_values[key] = await _serialize_redis_value(client, key)
    return plan


async def execute_quarantine(client: Any, key: str) -> tuple[str, str]:
    """把原键 RENAME 到 quarantine 命名空间并 PERSIST，返回 (原key, digest)。"""
    target = QUARANTINE_NAMESPACE + key
    await client.delete(target)
    await client.rename(key, target)
    await client.persist(target)
    return key, target


async def execute_dormancy(
    client: Any,
    key: str,
    *,
    policy_revision: int | None,
) -> None:
    """写 dormancy sidecar hash 并对原 key PERSIST（受限群可归因休眠）。"""
    original_pttl_ms = await client.pttl(key)
    deferred_at = datetime.now(UTC)
    resume_not_before = deferred_at + timedelta(hours=24)
    sidecar = DORMANCY_NAMESPACE + key
    await client.hset(
        sidecar,
        mapping={
            "state": "DEFERRED",
            "effective_revision": str(policy_revision or 0),
            "deferred_at": deferred_at.isoformat(),
            "resume_not_before": resume_not_before.isoformat(),
            "original_pttl_ms": str(max(original_pttl_ms, 0)),
            "migration_version": "tsk232",
        },
    )
    await client.persist(key)


async def count_active_legacy_keys(client: Any) -> int:
    """复扫 active legacy 残留：global_interaction 全族 + 无 sidecar 的
    staging/buffer 键（quarantine/dormancy 命名空间不计入）。"""
    active = 0
    for prefix in ACTIVE_FAMILY_PREFIXES:
        for key in await _scan_family_keys(client, prefix):
            if key.startswith((QUARANTINE_NAMESPACE, DORMANCY_NAMESPACE)):
                continue
            if prefix != "komari_memory:global_interaction:" and (
                await client.exists(DORMANCY_NAMESPACE + key)
            ):
                continue
            active += 1
    return active


async def release_reservation(
    client: Any,
    *,
    group_id: int,
    reservation_id: str,
) -> bool:
    """按预占身份执行与 proactive release Lua 等价的释放（幂等）。"""
    result = await client.eval(
        PROACTIVE_RELEASE_SCRIPT,
        2,
        f"komari_chat:proactive:cd:{group_id}",
        f"komari_chat:proactive:slots:{group_id}",
        reservation_id,
    )
    try:
        return int(result) > 0
    except (TypeError, ValueError):
        return False
