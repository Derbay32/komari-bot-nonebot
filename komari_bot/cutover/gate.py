"""cutover CLI 的 PostgreSQL 访问层（asyncpg 直连，无 ORM、无插件依赖）。

收敛 gate 单行快照、准入配置读取、advisory lock 键与聚合投影 helper；
全部语句只触碰 cutover 契约内的表，绝不写入业务数据。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import asyncpg

#: 所有写命令与 0012/0013 迁移共用的稳定 advisory lock 键（设计契约字面量）。
CUTOVER_LOCK_KEY = int.from_bytes(
    hashlib.blake2b(b"komari_group_admission:cutover-gate", digest_size=8).digest(),
    signed=True,
)

GATE_TABLE = "komari_group_admission_gate"
CONFIG_TABLE = "komari_group_admission_config"
_LEGACY_CONFIG_TABLE = "komari_plugin_configs"
_REPLY_PARENT_TABLE = "komari_chat_reply_fulfillments"

#: gate 单行全量列闭集（status 快照键名完备性的依据）。
GATE_COLUMNS = (
    "id",
    "phase",
    "is_fresh",
    "policy_revision",
    "policy_fingerprint",
    "backup_checkpoint",
    "redis_evidence_digest",
    "redis_finalizer_digest",
    "phase_updated_at",
)

#: reply outbox 聚合计数闭集：delivery_state → 投影键名。
_DELIVERY_STATE_COUNT_KEYS = {
    "NOT_STARTED": "not_started_count",
    "PENDING_CONFIRMATION": "pending_confirmation_count",
    "DELIVERED": "delivered_count",
    "NOT_DELIVERED": "not_delivered_count",
}


class CutoverCliError(Exception):
    """以 closed code 表达的 CLI 失败；details 只允许聚合 count。"""

    def __init__(self, error_code: str, **details: Any) -> None:
        super().__init__(error_code)
        self.error_code = error_code
        self.details = details


def to_asyncpg_dsn(database_url: str) -> str:
    """把 SQLAlchemy 风格 DSN 归一为 asyncpg 可直连的 DSN。"""
    return database_url.replace("postgresql+asyncpg://", "postgresql://")


async def connect(database_url: str) -> asyncpg.Connection:
    """建立 asyncpg 连接（调用方负责关闭）。"""
    return await asyncpg.connect(dsn=to_asyncpg_dsn(database_url))


async def fetch_gate_row(connection: asyncpg.Connection) -> dict[str, Any] | None:
    """读取 gate 单行全量快照；行缺失返回 None。"""
    row = await connection.fetchrow(
        f"SELECT {', '.join(GATE_COLUMNS)} FROM {GATE_TABLE} WHERE id = 1"
    )
    return None if row is None else dict(row)


async def fetch_admission_config(
    connection: asyncpg.Connection,
) -> dict[str, Any] | None:
    """读取准入配置单行（revision 与 policy JSON 文本）；缺失返回 None。"""
    row = await connection.fetchrow(
        f"SELECT revision, policy FROM {CONFIG_TABLE} WHERE id = 1"
    )
    return None if row is None else dict(row)


def stored_policy_fingerprint(policy_text: str) -> str:
    """对配置表内 policy 文本计算共享包指纹（非法内容原样上抛）。"""
    from komari_bot.admission_policy import policy_fingerprint

    return policy_fingerprint(json.loads(policy_text))


async def table_exists(connection: asyncpg.Connection, table: str) -> bool:
    """探测业务表是否存在（聚合投影对缺席表按零计数处理）。"""
    return (
        await connection.fetchval("SELECT to_regclass($1) IS NOT NULL", f"public.{table}")
    ) is True


async def reply_outbox_counts(connection: asyncpg.Connection) -> dict[str, int]:
    """按 delivery state 聚合 reply 履约父表计数（缺席表全零）。"""
    counts = dict.fromkeys(_DELIVERY_STATE_COUNT_KEYS.values(), 0)
    if not await table_exists(connection, _REPLY_PARENT_TABLE):
        return counts
    rows = await connection.fetch(
        f"SELECT delivery_state, count(*) AS n FROM {_REPLY_PARENT_TABLE}"
        " GROUP BY delivery_state"
    )
    for row in rows:
        key = _DELIVERY_STATE_COUNT_KEYS.get(str(row["delivery_state"]))
        if key is not None:
            counts[key] = int(row["n"] or 0)
    return counts


async def proposal_status_counts(connection: asyncpg.Connection) -> dict[str, int]:
    """proposals 按 status 聚合为 ``{"<status名>": count}`` map。"""
    if not await table_exists(connection, "komari_custom_proposals"):
        return {}
    rows = await connection.fetch(
        "SELECT status, count(*) AS n FROM komari_custom_proposals GROUP BY status"
    )
    return {str(row["status"]): int(row["n"]) for row in rows}


async def announcement_processing_count(connection: asyncpg.Connection) -> int:
    """公告 processing 聚合计数（缺席表为零）。"""
    if not await table_exists(connection, "komari_announcement_dispatches"):
        return 0
    processing = await connection.fetchval(
        "SELECT count(*) FROM komari_announcement_dispatches"
        " WHERE status = 'processing'"
    )
    return int(processing or 0)


async def memory_job_incomplete_count(connection: asyncpg.Connection) -> int:
    """memory 未完成 job 聚合计数（缺席表为零）。

    完成判定取双事实：终态 ``stage='completed'`` 或已落完成事实
    ``completed_at``；两者皆无才算未完成（legacy 终态行只带
    ``completed_at`` 也计为已完成）。
    """
    if not await table_exists(connection, "komari_memory_jobs"):
        return 0
    incomplete = await connection.fetchval(
        "SELECT count(*) FROM komari_memory_jobs"
        " WHERE stage <> 'completed' AND completed_at IS NULL"
    )
    return int(incomplete or 0)


async def aggregate_projection(connection: asyncpg.Connection) -> dict[str, Any]:
    """status/audit 共用的只读聚合投影（closed 键名 + 聚合 count）。"""
    projection: dict[str, Any] = {}
    projection.update(await reply_outbox_counts(connection))
    projection.update(await proposal_status_counts(connection))
    projection["processing_count"] = await announcement_processing_count(connection)
    projection["incomplete_count"] = await memory_job_incomplete_count(connection)
    return projection


async def fetch_legacy_config_rows(
    connection: asyncpg.Connection,
) -> dict[str, Any]:
    """读旧 komari_plugin_configs 宽表（resource → config 字典）；缺席为空。"""
    if not await table_exists(connection, _LEGACY_CONFIG_TABLE):
        return {}
    rows = await connection.fetch(
        f"SELECT resource, config_data FROM {_LEGACY_CONFIG_TABLE}"
    )
    parsed: dict[str, Any] = {}
    for row in rows:
        raw = row["config_data"]
        data: Any = raw
        if isinstance(raw, (str, bytes, bytearray)):
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                data = {}
        parsed[str(row["resource"])] = data if isinstance(data, dict) else {}
    return parsed


def canonical_list_fingerprint(group_ids: list[int]) -> str:
    """legacy 名单 canonical fingerprint：升序去重正整数数组的 SHA-256。"""
    serialized = json.dumps(sorted(set(group_ids)), separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()
