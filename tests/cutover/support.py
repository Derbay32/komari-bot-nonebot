"""TSK-232 轮 B 离线 cutover 验收红基线共享测试支持（test-support，不收集用例）。

骨架面验收（CLI 六命令、gate 表 phase CAS、policy canonicalization/fingerprint、
audit/status 投影、advisory lock 排他）共用的一次性基础设施：

- 门控环境解析与 skip 标记（``KOMARI_TEST_POSTGRES_URL`` /
  ``SQLALCHEMY_DATABASE_URL`` / ``KOMARI_TEST_REDIS_URL``；无门控整文件
  skip，沿用 ``tests/db/tsk197_gate_support.py`` 的既有约定）；
- 一次性隔离库：库名 = 门控库名 + ``_tsk232b1`` 后缀族（各测试文件再带
  短标签防并行冲突），先 DROP IF EXISTS 再 CREATE，于该 DSN 运行
  ``python -m komari_bot.db.orm_bootstrap upgrade head``（子进程经
  SQLALCHEMY_DATABASE_URL 注入），用例结束 DROP；绝不触碰共享门控库；
- CLI 公共入口驱动 helper：一律从 ``komari_bot.cutover.cli.main(argv)``
  全路径调用（含参数解析），不 subprocess 起 Python、不 mock 内部逻辑；
- gate / 准入配置行的只读快照与前置状态编排（fixture arrangement 用直连
  SQL，仅用于编排前置相位与断言落库事实，绝不替代被测命令本身）；
- legacy ``komari_plugin_configs`` 金丝雀夹具与聚合投影 oracle。

隔离纪律：测试自建 key 一律使用 ``test:cutover:`` 前缀并 teardown 清理，
绝不写生产 Redis namespace；所有 canary 均为明显非敏感合成值。
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import importlib
import json
import os
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import asyncpg
import pytest

from tests.db.tsk197_gate_support import (
    POSTGRES_URL,
    drop_scratch_database,
    recreate_scratch_database,
    run_bootstrap,
    same_database,
    scratch_url,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]

POSTGRES_GATE_URL = POSTGRES_URL
REDIS_URL = os.getenv("KOMARI_TEST_REDIS_URL", "")

#: 无门控 PostgreSQL 时跳过（与 tests/db 既有约定同一守卫语义）。
SKIP_NO_POSTGRES = pytest.mark.skipif(
    not POSTGRES_GATE_URL,
    reason="未设置 KOMARI_TEST_POSTGRES_URL，跳过集成测试",
)

#: 无门控 Redis 时跳过（capture-evidence 等 Redis 只读扫描路径专用）。
SKIP_NO_REDIS = pytest.mark.skipif(
    not REDIS_URL,
    reason="未设置 KOMARI_TEST_REDIS_URL，跳过集成测试",
)

# ---------------------------------------------------------------------------
# 设计契约常量（总调度裁定，测试以字面量锁定）
# ---------------------------------------------------------------------------

#: phase 顺序闭集：只准前进，CAS 拒绝乱序/跳步。
PHASE_SEQUENCE = (
    "EXPANDED",
    "POLICY_PREPARED",
    "REDIS_EVIDENCE_CAPTURED",
    "POSTGRES_BACKFILLED",
    "REDIS_FINALIZED",
)

GATE_TABLE = "komari_group_admission_gate"
ADMISSION_CONFIG_TABLE = "komari_group_admission_config"
LEGACY_CONFIG_TABLE = "komari_plugin_configs"

#: audit 聚合的八个 legacy 资源闭集。
LEGACY_RESOURCES = (
    "komari_knowledge",
    "komari_search",
    "komari_help",
    "group_history_summary",
    "komari_decision",
    "sr",
    "komari_memory",
    "komari_custom",
)

#: reply_outbox 投影统一键名闭集。
OUTBOX_COUNT_KEYS = (
    "not_started_count",
    "pending_confirmation_count",
    "delivered_count",
    "not_delivered_count",
)

#: closed code 闭集（错误码字面量断言依据）。
CLOSED_ERROR_CODES = frozenset(
    {
        "POLICY_FILE_INVALID",
        "POLICY_FINGERPRINT_MISMATCH",
        "POLICY_DRIFT",
        "POLICY_FROZEN",
        "PHASE_OUT_OF_ORDER",
        "ABORT_FORBIDDEN",
        "GATE_MISSING",
        "POLICY_MISSING",
        "BACKUP_CHECKPOINT_MISSING",
        "LOCK_BUSY",
        "REDIS_UNAVAILABLE",
    }
)

#: 所有写命令与 0012/0013 迁移共用的稳定 advisory lock 键。
CUTOVER_LOCK_KEY = int.from_bytes(
    hashlib.blake2b(b"komari_group_admission:cutover-gate", digest_size=8).digest(),
    signed=True,
)

# 契约常量不变量（收集期即校验，防止测试支持层自身漂移）。
assert len(set(PHASE_SEQUENCE)) == 5 and PHASE_SEQUENCE[0] == "EXPANDED"
assert PHASE_SEQUENCE[-1] == "REDIS_FINALIZED"

#: Redis 测试专用 namespace（teardown 清理；绝不写生产 namespace）。
TEST_REDIS_NAMESPACE = "test:cutover:"

# ---------------------------------------------------------------------------
# 合成 canary（明显非敏感；群号一律正整数）
# ---------------------------------------------------------------------------

CANARY_GROUP_A = 123456789
CANARY_GROUP_B = 987654321
CANARY_GROUP_C = 555000111
CANARY_USER_ID = 888777666
CANARY_USER_TOKEN_A = "canary-user-alpha-01"
CANARY_BODY_TOKEN = "canary-body-leak-probe-7d21"
CANARY_LEAK_MARKER = "canary-invalid-policy-marker-9f4e"

#: 验收基线合法策略（canonical 形态：升序去重正整数）。
VALID_POLICY: dict[str, Any] = {
    "mode": "blacklist",
    "group_ids": [CANARY_GROUP_B, CANARY_GROUP_A],
}
VALID_POLICY_FINGERPRINT = ""

#: fresh 隔离库经 0010 播种的缺省策略（与迁移链一致）。
DEFAULT_SEEDED_POLICY: dict[str, Any] = {"mode": "blacklist", "group_ids": []}
DEFAULT_SEEDED_POLICY_FINGERPRINT = ""


def oracle_fingerprint(policy: Mapping[str, Any]) -> str:
    """独立 fingerprint oracle：canonical JSON（sort_keys+紧凑分隔符）SHA-256。

    与 ``reply_fulfillment_domain.build_reply_fulfillment_payload_hash`` 的
    规范化范式一致；输入必须是已 canonical 形态的 dict。
    """
    canonical = json.dumps(policy, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def oracle_list_fingerprint(group_ids: list[int]) -> str:
    """legacy 名单 canonical fingerprint oracle：升序去重正整数数组的 SHA-256。"""
    canonical = json.dumps(sorted(set(group_ids)), separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


VALID_POLICY_FINGERPRINT = oracle_fingerprint(VALID_POLICY)
DEFAULT_SEEDED_POLICY_FINGERPRINT = oracle_fingerprint(DEFAULT_SEEDED_POLICY)


def write_policy_file(tmp_path: Path, payload: object) -> str:
    """把 policy 载荷写成 JSON 文件并返回路径字符串。"""
    file_path = tmp_path / "policy.json"
    file_path.write_text(json.dumps(payload), encoding="utf-8")
    return str(file_path)


# ---------------------------------------------------------------------------
# CLI 公共入口驱动
# ---------------------------------------------------------------------------


def run_cli(
    capsys: pytest.CaptureFixture[str],
    *args: str,
) -> tuple[int, dict[str, Any] | None]:
    """从 ``komari_bot.cutover.cli.main(argv)`` 全路径驱动 CLI 并解析 stdout JSON。

    返回 ``(退出码, 解析后的单行 JSON)``；stdout 为空时 payload 为 None。
    目标模块/命令缺失时让 ModuleNotFoundError 原样上抛——这正是红基线的
    预期失败原因。
    """
    cli_main = importlib.import_module("komari_bot.cutover.cli").main

    exit_code = int(cli_main(list(args)))
    captured = capsys.readouterr()
    stdout = captured.out.strip()
    payload = json.loads(stdout) if stdout else None
    return exit_code, payload


def run_cli_raw(
    capsys: pytest.CaptureFixture[str],
    *args: str,
) -> tuple[int, str, str]:
    """驱动 CLI 并返回 ``(退出码, 原始 stdout 文本, 原始 stderr 文本)``。

    供 canary 泄漏递归检查使用：不假设输出是合法 JSON。
    """
    cli_main = importlib.import_module("komari_bot.cutover.cli").main

    exit_code = int(cli_main(list(args)))
    captured = capsys.readouterr()
    return exit_code, captured.out, captured.err


# ---------------------------------------------------------------------------
# 一次性隔离库
# ---------------------------------------------------------------------------


def require_same_gate_database() -> None:
    """门控 DSN 与 SQLALCHEMY_DATABASE_URL 必须同库，否则按约定 skip。"""
    from tests.db.tsk197_gate_support import SQLALCHEMY_URL

    if not same_database(POSTGRES_GATE_URL, SQLALCHEMY_URL):
        pytest.skip("KOMARI_TEST_POSTGRES_URL 与 SQLALCHEMY_DATABASE_URL 不一致")


@contextlib.contextmanager
def cutover_scratch_database(
    tag: str,
    revision: str = "0010",
) -> Iterator[tuple[dict[str, Any], str]]:
    """重建 ``_tsk232b1<tag>`` 一次性隔离库并 upgrade 到指定 revision。

    骨架面 CLI 命令（prepare-policy/capture-evidence/abort/status 等）只存在于
    0010→0013 的停机窗口内：0013 coordinated contract 会按契约物理删除
    migration-only 的 gate 表，因此 head 库上这些命令只能观察到 GATE_MISSING。
    默认停链在 0010（gate 表与准入配置表就位、0013 未执行），与生产
    runbook 中 CLI 的真实操作窗口一致；fresh 直通守卫等需要完整链的用例
    显式传 ``revision="head"``。
    """
    require_same_gate_database()
    params = asyncio.run(recreate_scratch_database(f"_tsk232b1{tag}"))
    database_url = scratch_url(str(params["database"]))
    try:
        result = run_bootstrap(database_url, "upgrade", revision)
        assert result.returncode == 0, result.stderr
        yield params, database_url
    finally:
        asyncio.run(drop_scratch_database(str(params["database"])))


# ---------------------------------------------------------------------------
# gate / 准入配置行直连访问（只读快照 + 前置编排）
# ---------------------------------------------------------------------------

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


async def _fetch_gate_row(params: dict[str, Any]) -> dict[str, Any] | None:
    connection = await asyncpg.connect(**params)
    try:
        row = await connection.fetchrow(
            f"SELECT {', '.join(GATE_COLUMNS)} FROM {GATE_TABLE} WHERE id = 1"
        )
    finally:
        await connection.close()
    return None if row is None else dict(row)


def fetch_gate_row(params: dict[str, Any]) -> dict[str, Any] | None:
    """读取 gate 单行全量快照；行缺失返回 None。"""
    return asyncio.run(_fetch_gate_row(params))


async def _update_gate_row(
    params: dict[str, Any],
    assignments: dict[str, object],
) -> None:
    connection = await asyncpg.connect(**params)
    try:
        columns = ", ".join(
            f"{name} = ${index}" for index, name in enumerate(assignments, 1)
        )
        await connection.execute(
            f"UPDATE {GATE_TABLE} SET {columns} WHERE id = 1",
            *assignments.values(),
        )
    finally:
        await connection.close()


def stage_gate_phase(
    params: dict[str, Any],
    *,
    phase: str,
    policy_revision: int | None = None,
    policy_fingerprint: str | None = None,
    backup_checkpoint: str | None = None,
    redis_evidence_digest: str | None = None,
    redis_finalizer_digest: str | None = None,
) -> None:
    """直连 SQL 编排 gate 前置相位（仅 fixture arrangement，不替代被测命令）。"""
    asyncio.run(
        _update_gate_row(
            params,
            {
                "phase": phase,
                "policy_revision": policy_revision,
                "policy_fingerprint": policy_fingerprint,
                "backup_checkpoint": backup_checkpoint,
                "redis_evidence_digest": redis_evidence_digest,
                "redis_finalizer_digest": redis_finalizer_digest,
            },
        )
    )


def delete_gate_rows(params: dict[str, Any]) -> None:
    """清空 gate 行（模拟 gate 行缺失场景）。"""
    asyncio.run(_delete_gate_rows(params))


async def _delete_gate_rows(params: dict[str, Any]) -> None:
    connection = await asyncpg.connect(**params)
    try:
        await connection.execute(f"DELETE FROM {GATE_TABLE}")
    finally:
        await connection.close()


async def _fetch_admission_config(params: dict[str, Any]) -> dict[str, Any] | None:
    connection = await asyncpg.connect(**params)
    try:
        row = await connection.fetchrow(
            f"SELECT revision, policy FROM {ADMISSION_CONFIG_TABLE} WHERE id = 1"
        )
    finally:
        await connection.close()
    return None if row is None else dict(row)


def fetch_admission_config(params: dict[str, Any]) -> dict[str, Any] | None:
    """读取准入配置单行（revision 与 policy JSON 文本）。"""
    return asyncio.run(_fetch_admission_config(params))


def overwrite_admission_policy(
    params: dict[str, Any],
    policy: Mapping[str, Any],
) -> None:
    """直连改写准入策略内容（漂移注入专用 fixture arrangement）。"""
    asyncio.run(_overwrite_admission_policy(params, policy))


async def _overwrite_admission_policy(
    params: dict[str, Any],
    policy: Mapping[str, Any],
) -> None:
    connection = await asyncpg.connect(**params)
    try:
        await connection.execute(
            f"UPDATE {ADMISSION_CONFIG_TABLE} SET policy = $1 WHERE id = 1",
            json.dumps(policy),
        )
    finally:
        await connection.close()


# ---------------------------------------------------------------------------
# legacy 夹具播种（audit 聚合投影 + canary 泄漏面）
# ---------------------------------------------------------------------------


async def _seed_legacy_configs(
    params: dict[str, Any],
    rows: list[tuple[str, dict[str, Any]]],
) -> None:
    connection = await asyncpg.connect(**params)
    try:
        await connection.execute(
            f"CREATE TABLE IF NOT EXISTS {LEGACY_CONFIG_TABLE} ("
            " resource TEXT PRIMARY KEY,"
            " config_data JSONB NOT NULL)"
        )
        for resource, config_data in rows:
            await connection.execute(
                f"INSERT INTO {LEGACY_CONFIG_TABLE} (resource, config_data)"
                " VALUES ($1, $2)"
                " ON CONFLICT (resource) DO UPDATE"
                " SET config_data = EXCLUDED.config_data",
                resource,
                json.dumps(config_data),
            )
    finally:
        await connection.close()


def seed_legacy_configs(
    params: dict[str, Any],
    rows: list[tuple[str, dict[str, Any]]],
) -> None:
    """手工建旧 komari_plugin_configs 宽表并塞入资源 canary 配置。"""
    asyncio.run(_seed_legacy_configs(params, rows))


DELIVERY_STATE_ROWS: tuple[tuple[str, int], ...] = (
    ("NOT_STARTED", 2),
    ("PENDING_CONFIRMATION", 1),
    ("DELIVERED", 3),
    ("NOT_DELIVERED", 1),
)


async def _seed_reply_fulfillments(params: dict[str, Any]) -> None:
    connection = await asyncpg.connect(**params)
    try:
        serial = 0
        for delivery_state, count in DELIVERY_STATE_ROWS:
            for _ in range(count):
                serial += 1
                # 原始 CHECK（0005 起）要求非 NOT_STARTED 行携带对应计时事实
                send_started_at = (
                    datetime.now(UTC) if delivery_state != "NOT_STARTED" else None
                )
                delivered_at = (
                    datetime.now(UTC) if delivery_state == "DELIVERED" else None
                )
                not_delivered_at = (
                    datetime.now(UTC) if delivery_state == "NOT_DELIVERED" else None
                )
                await connection.execute(
                    """
                    INSERT INTO komari_chat_reply_fulfillments (
                        fulfillment_id, payload_hash, request_trace_id,
                        trigger_message_id, trigger_user_id, group_id,
                        bot_self_id, adapter_name, reply_target_message_id,
                        reply_content, delivery_state,
                        send_started_at, delivered_at, not_delivered_at
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11,
                              $12, $13, $14)
                    """,
                    f"fulfill-canary-seed-{serial}",
                    f"canary-payload-hash-{serial}",
                    f"trace-canary-{serial}",
                    f"msg-canary-{serial}",
                    str(CANARY_USER_ID),
                    str(CANARY_GROUP_A),
                    "canary-self",
                    "test",
                    f"target-canary-{serial}",
                    f"{CANARY_BODY_TOKEN}-{serial}",
                    delivery_state,
                    send_started_at,
                    delivered_at,
                    not_delivered_at,
                )
    finally:
        await connection.close()


def seed_reply_fulfillments(params: dict[str, Any]) -> None:
    """按四个 delivery state 播种 reply outbox 聚合计数行。"""
    asyncio.run(_seed_reply_fulfillments(params))


async def _seed_proposals(params: dict[str, Any]) -> None:
    connection = await asyncpg.connect(**params)
    try:
        for index, status in enumerate(("voting", "voting", "approved"), 1):
            await connection.execute(
                """
                INSERT INTO komari_custom_proposals (
                    id, group_id, proposer_id, title, content,
                    status, publication_key, required_votes
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                """,
                index,
                CANARY_GROUP_A,
                CANARY_USER_ID,
                f"{CANARY_BODY_TOKEN}-title-{index}",
                f"{CANARY_BODY_TOKEN}-content-{index}",
                status,
                f"pub-key-canary-{index}",
                3,
            )
    finally:
        await connection.close()


def seed_proposals(params: dict[str, Any]) -> None:
    """按 status 播种提案聚合计数行（votingx2、approvedx1）。"""
    asyncio.run(_seed_proposals(params))


async def _seed_announcements(params: dict[str, Any]) -> None:
    connection = await asyncpg.connect(**params)
    try:
        for request_id, status in (
            ("req-canary-processing-1", "processing"),
            ("req-canary-processing-2", "processing"),
            ("req-canary-done-1", "done"),
        ):
            await connection.execute(
                """
                INSERT INTO komari_announcement_dispatches (
                    request_id, payload_hash, status
                ) VALUES ($1, $2, $3)
                """,
                request_id,
                f"hash-{request_id}",
                status,
            )
    finally:
        await connection.close()


def seed_announcements(params: dict[str, Any]) -> None:
    """播种公告 processing 计数行（processingx2、donex1）。"""
    asyncio.run(_seed_announcements(params))


async def _seed_memory_jobs(params: dict[str, Any]) -> None:
    connection = await asyncpg.connect(**params)
    try:
        await connection.execute(
            """
            INSERT INTO komari_memory_jobs (
                job_name, run_date, owner_token, lease_until, stage
            )
            VALUES ('forgetting_canary', CURRENT_DATE, 'owner-canary',
                    NOW() + INTERVAL '1 hour', 'claimed')
            """
        )
        await connection.execute(
            """
            INSERT INTO komari_memory_jobs (
                job_name, run_date, owner_token, lease_until, stage,
                completed_at
            )
            VALUES ('forgetting_canary_done', CURRENT_DATE, 'owner-canary',
                    NOW() + INTERVAL '1 hour', 'succeeded', NOW())
            """
        )
    finally:
        await connection.close()


def seed_memory_jobs(params: dict[str, Any]) -> None:
    """播种 memory job 计数行（未完成x1、已完成x1）。"""
    asyncio.run(_seed_memory_jobs(params))


# ---------------------------------------------------------------------------
# Redis 测试 namespace（test:cutover: 前缀；teardown 清理）
# ---------------------------------------------------------------------------


def _redis_client() -> Any:
    import redis.asyncio as aioredis

    return aioredis.from_url(REDIS_URL, decode_responses=True)


async def _seed_test_namespace(entries: dict[str, str]) -> None:
    client = _redis_client()
    try:
        for key, value in entries.items():
            await client.set(f"{TEST_REDIS_NAMESPACE}{key}", value)
    finally:
        await client.aclose()


async def _cleanup_test_namespace() -> None:
    client = _redis_client()
    try:
        keys = [key async for key in client.scan_iter(f"{TEST_REDIS_NAMESPACE}*")]
        if keys:
            await client.delete(*keys)
    finally:
        await client.aclose()


@contextlib.contextmanager
def redis_test_namespace(entries: dict[str, str]) -> Iterator[None]:
    """在 test:cutover: 前缀下播种条目并在退出时全量清理。"""
    asyncio.run(_seed_test_namespace(entries))
    try:
        yield
    finally:
        asyncio.run(_cleanup_test_namespace())


# ---------------------------------------------------------------------------
# 结构无关投影定位 helper
# ---------------------------------------------------------------------------


def find_key(payload: Any, key: str) -> Any:
    """在任意嵌套结构中递归查找第一个匹配键的值；找不到返回 None。"""
    if isinstance(payload, Mapping):
        if key in payload:
            return payload[key]
        for value in payload.values():
            found = find_key(value, key)
            if found is not None:
                return found
    elif isinstance(payload, list):
        for item in payload:
            found = find_key(item, key)
            if found is not None:
                return found
    return None


def collect_resource_entries(payload: Any) -> list[dict[str, Any]]:
    """收集投影中所有带字符串 ``resource`` 字段的条目（legacy_resources）。"""
    found: list[dict[str, Any]] = []
    if isinstance(payload, Mapping):
        resource = payload.get("resource")
        if isinstance(resource, str):
            found.append(dict(payload))
        for value in payload.values():
            found.extend(collect_resource_entries(value))
    elif isinstance(payload, list):
        for item in payload:
            found.extend(collect_resource_entries(item))
    return found


def collect_key_values(payload: Any, key: str) -> list[Any]:
    """递归收集结构中所有名为 ``key`` 的值（用于可能重复出现的聚合键）。"""
    values: list[Any] = []
    if isinstance(payload, Mapping):
        if key in payload:
            values.append(payload[key])
        for value in payload.values():
            values.extend(collect_key_values(value, key))
    elif isinstance(payload, list):
        for item in payload:
            values.extend(collect_key_values(item, key))
    return values
