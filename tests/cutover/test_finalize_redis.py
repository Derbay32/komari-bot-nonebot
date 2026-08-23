"""TSK-232 轮 B —— ``finalize-redis`` 数据面行为矩阵红基线（门控 PG+Redis）。

锁定 finalize-redis 在真实 Redis 上（phase 前置 POSTGRES_BACKFILLED、
``--apply`` + ``--expected-fingerprint``）的契约：

- cleanup ledger 逐条释放：按 reservation 身份执行与 proactive release
  Lua 等价的释放（slots ZREM pending 成员 + 自持冷却键 DEL），成功后落
  ``release_finished_at``；已完成的条目断点续跑跳过、时间戳不被改写；
  confirmed 名额绝不撤销。
- legacy ``komari_memory:global_interaction*`` 全族摘除：原 key RENAME 到
  ``komari_memory:quarantine:v1:<原key>`` 并 PERSIST（无 TTL）、原字节
  保留；active 族复扫归零；quarantine ledger 记录 (key_family,
  sha256(原key+payload字节))；输出只含 closed code 与聚合 count，绝不回显
  键名/正文 canary。
- 受限群可归因 staging/buffer-processing key：写 dormancy sidecar hash
  （state/effective_revision/deferred_at/resume_not_before/original_pttl_ms/
  migration_version='tsk232'）并 PERSIST；归因损坏的走 quarantine。
- attestation：aggregate SHA-256 写 ``gate.redis_finalizer_digest`` 且 CAS
  到 REDIS_FINALIZED；重跑为 no-op 幂等；active 非零拒绝
  （LEGACY_ACTIVE_NONZERO）。

Redis 隔离：一律经 ``--redis-url`` 指向测试专用逻辑库（与生产库物理隔
离），teardown 仅清理本前缀族键。隔离库 = 门控库名 + ``_tsk232b1fin<tag>``，
用例结束 DROP。
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit, urlunsplit

from tests.cutover.support import (
    CANARY_BODY_TOKEN,
    CANARY_GROUP_A,
    CANARY_GROUP_B,
    REDIS_URL,
    SKIP_NO_POSTGRES,
    SKIP_NO_REDIS,
    VALID_POLICY,
    VALID_POLICY_FINGERPRINT,
    drop_scratch_database,
    fetch_gate_row,
    overwrite_admission_policy,
    recreate_scratch_database,
    run_bootstrap,
    run_cli,
    run_cli_raw,
    scratch_url,
    stage_gate_phase,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    import pytest

pytestmark = [SKIP_NO_POSTGRES]

_REDIS_TEST_DB = 15
_QUARANTINE_PREFIX = "komari_memory:quarantine:v1:"
_DORMANCY_PREFIX = "komari_memory:dormancy:v1:"
_ACTIVE_LEGACY_PREFIXES = (
    "komari_memory:global_interaction:",
    "komari_memory:staging:",
    "komari_memory:buffer:",
)
_CLEANUP_KEY_PREFIXES = (
    "komari_chat:proactive:",
    "komari_memory:",
)


# ---------------------------------------------------------------------------
# 隔离库 / Redis 编排 helper
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _backfilled_database(
    tag: str,
) -> Iterator[tuple[dict[str, Any], str]]:
    """重建 ``_tsk232b1fin<tag>`` 库并编排到 POSTGRES_BACKFILLED 前置态。

    policy 内容 = VALID_POLICY（受限群 A/B）；gate 相位直接 SQL 编排，
    使本文件聚焦 finalize-redis 自身行为而非 0012 迁移本体。
    """
    params = asyncio.run(recreate_scratch_database(f"_tsk232b1fin{tag}"))
    database_url = scratch_url(str(params["database"]))
    try:
        # 停在 0012：finalize-redis 运行期 gate 表必须仍存在（0013 才删）
        result = run_bootstrap(database_url, "upgrade", "0012")
        assert result.returncode == 0, result.stderr
        overwrite_admission_policy(params, VALID_POLICY)
        stage_gate_phase(
            params,
            phase="POSTGRES_BACKFILLED",
            policy_revision=1,
            policy_fingerprint=VALID_POLICY_FINGERPRINT,
        )
        yield params, database_url
    finally:
        asyncio.run(drop_scratch_database(str(params["database"])))


def _redis_test_url() -> str:
    """把门控 Redis DSN 指到测试专用逻辑库（与生产库物理隔离）。"""
    if not REDIS_URL:
        return ""
    parts = urlsplit(REDIS_URL)
    return urlunsplit(
        (parts.scheme, parts.netloc, f"/{_REDIS_TEST_DB}", parts.query, "")
    )


def redis_run(handler: Any) -> None:
    """在测试逻辑库连接上执行异步 handler（teardown 安全）。"""
    import redis.asyncio as aioredis

    async def _run() -> None:
        client = aioredis.from_url(_redis_test_url(), decode_responses=True)
        try:
            await handler(client)
        finally:
            await client.aclose()

    asyncio.run(_run())


@contextlib.contextmanager
def _redis_guard() -> Iterator[None]:
    """进入前清空测试逻辑库前缀族，退出时再清理一次。"""

    def _cleanup_sync(client: Any) -> None:
        del client

    async def _cleanup(client: Any) -> None:
        keys: list[str] = []
        for prefix in _CLEANUP_KEY_PREFIXES:
            keys.extend([key async for key in client.scan_iter(f"{prefix}*")])
        if keys:
            await client.delete(*keys)

    del _cleanup_sync
    redis_run(_cleanup)
    try:
        yield
    finally:
        redis_run(_cleanup)


def _finalize_argv(database_url: str) -> list[str]:
    return [
        "finalize-redis",
        "--database-url",
        database_url,
        "--redis-url",
        _redis_test_url(),
        "--expected-fingerprint",
        VALID_POLICY_FINGERPRINT,
        "--apply",
    ]


async def _connect(params: dict[str, Any]) -> Any:
    import asyncpg

    return await asyncpg.connect(**params)


async def _seed_ledger(
    params: dict[str, Any],
    entries: list[tuple[str, int]],
) -> None:
    connection = await _connect(params)
    try:
        for reservation_id, group_id in entries:
            await connection.execute(
                """
                INSERT INTO komari_reply_reservation_cleanup_ledger (
                    reservation_id, group_id
                ) VALUES ($1, $2)
                """,
                reservation_id,
                group_id,
            )
    finally:
        await connection.close()


async def _preset_ledger_finished(
    params: dict[str, Any],
    reservation_id: str,
    stamp: datetime,
) -> None:
    connection = await _connect(params)
    try:
        await connection.execute(
            "UPDATE komari_reply_reservation_cleanup_ledger"
            " SET release_finished_at = $2 WHERE reservation_id = $1",
            reservation_id,
            stamp,
        )
    finally:
        await connection.close()


async def _fetch_ledger(params: dict[str, Any]) -> list[dict[str, Any]]:
    connection = await _connect(params)
    try:
        rows = await connection.fetch(
            "SELECT reservation_id, group_id, release_finished_at"
            " FROM komari_reply_reservation_cleanup_ledger"
            " ORDER BY reservation_id"
        )
    finally:
        await connection.close()
    return [dict(row) for row in rows]


async def _fetch_quarantine_rows(
    params: dict[str, Any],
) -> list[dict[str, Any]]:
    connection = await _connect(params)
    try:
        rows = await connection.fetch(
            "SELECT key_family, payload_digest"
            " FROM komari_admission_quarantine_ledger ORDER BY entry_id"
        )
    finally:
        await connection.close()
    return [dict(row) for row in rows]


def _oracle_digest(key: str, value: str) -> str:
    """sha256(原key字节 + payload字节)，无分隔符拼接（设计契约字面量）。"""
    return hashlib.sha256(
        key.encode("utf-8") + value.encode("utf-8")
    ).hexdigest()


# ---------------------------------------------------------------------------
# 用例：cleanup ledger 释放与断点续跑
# ---------------------------------------------------------------------------


@SKIP_NO_REDIS
def test_release_pending_reservations_and_mark_finished(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """ledger 条目按预占身份释放：ZREM pending、DEL 冷却键、confirmed 不动。"""
    slots_key = f"komari_chat:proactive:slots:{CANARY_GROUP_A}"
    cooldown_key = f"komari_chat:proactive:cd:{CANARY_GROUP_A}"
    future_score = int(datetime.now(UTC).timestamp() * 1000) + 600_000

    async def _seed(client: Any) -> None:
        await client.zadd(
            slots_key,
            {
                "pending:resv-fin-1": future_score,
                "confirmed:resv-keep": future_score,
            },
        )
        await client.set(cooldown_key, "resv-fin-1")

    async def _assert_released(client: Any) -> None:
        assert await client.zscore(slots_key, "pending:resv-fin-1") is None
        assert (
            await client.zscore(slots_key, "confirmed:resv-keep") is not None
        ), "confirmed 名额绝不因释放撤销"
        assert await client.get(cooldown_key) is None

    with _backfilled_database("a") as (params, database_url), _redis_guard():
        redis_run(_seed)
        asyncio.run(_seed_ledger(params, [("resv-fin-1", CANARY_GROUP_A)]))

        exit_code, payload = run_cli(
            capsys, *_finalize_argv(database_url)
        )
        assert exit_code == 0, payload
        assert payload is not None
        assert payload["status"] == "ok"

        gate = fetch_gate_row(params)
        assert gate is not None
        assert gate["phase"] == "REDIS_FINALIZED"
        digest = gate["redis_finalizer_digest"]
        assert isinstance(digest, str) and len(digest) == 64
        int(digest, 16)

        redis_run(_assert_released)
        ledger = asyncio.run(_fetch_ledger(params))
        assert ledger[0]["reservation_id"] == "resv-fin-1"
        assert ledger[0]["release_finished_at"] is not None


@SKIP_NO_REDIS
def test_resume_skips_already_finished_entries(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """断点续跑：已完成条目时间戳不动、不重复处理；未完成条目补跑成功。"""
    preset_stamp = datetime.now(UTC) - timedelta(hours=1)
    slots_b = f"komari_chat:proactive:slots:{CANARY_GROUP_B}"
    cooldown_b = f"komari_chat:proactive:cd:{CANARY_GROUP_B}"
    future_score = int(datetime.now(UTC).timestamp() * 1000) + 600_000

    async def _seed(client: Any) -> None:
        await client.zadd(slots_b, {"pending:resv-open": future_score})
        await client.set(cooldown_b, "resv-open")

    async def _assert_released(client: Any) -> None:
        assert await client.zscore(slots_b, "pending:resv-open") is None
        assert await client.get(cooldown_b) is None

    with _backfilled_database("b") as (params, database_url), _redis_guard():
        redis_run(_seed)
        asyncio.run(
            _seed_ledger(
                params,
                [
                    ("resv-done", CANARY_GROUP_A),
                    ("resv-open", CANARY_GROUP_B),
                ],
            )
        )
        asyncio.run(
            _preset_ledger_finished(params, "resv-done", preset_stamp)
        )

        exit_code, payload = run_cli(
            capsys, *_finalize_argv(database_url)
        )
        assert exit_code == 0, payload

        ledger = {
            str(row["reservation_id"]): row
            for row in asyncio.run(_fetch_ledger(params))
        }
        assert ledger["resv-done"]["release_finished_at"] == preset_stamp, (
            "已完成条目不得被重复处理"
        )
        assert ledger["resv-open"]["release_finished_at"] is not None
        redis_run(_assert_released)


# ---------------------------------------------------------------------------
# 用例：legacy global_interaction 全族 quarantine
# ---------------------------------------------------------------------------


async def _seed_legacy_family(client: Any) -> dict[str, str]:
    """播种 legacy global_interaction 族键，返回字符串键 {key: value}。"""
    string_entries = {
        "komari_memory:global_interaction:user-canary-1": (
            f"{CANARY_BODY_TOKEN}-buffer-bytes"
        ),
        "komari_memory:global_interaction:processing:user-canary-1:tok": (
            f"{CANARY_BODY_TOKEN}-processing-bytes"
        ),
    }
    for key, value in string_entries.items():
        await client.set(key, value)
    await client.sadd("komari_memory:global_interaction:pending", "user-canary-1")
    await client.hset(
        "komari_memory:global_interaction:leases", "user-canary-1", "lease-1"
    )
    await client.hset(
        "komari_memory:global_interaction:lease_owners",
        "user-canary-1",
        "own-1",
    )
    await client.hset(
        "komari_memory:global_interaction:snapshots", "user-canary-1", '{"a":1}'
    )
    return string_entries


@SKIP_NO_REDIS
def test_legacy_global_interaction_family_quarantined(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """全族 RENAME+PERSIST+字节保留；active 归零；ledger 记 family+digest。"""
    seeded: dict[str, str] = {}

    async def _flow(client: Any) -> None:
        seeded.update(await _seed_legacy_family(client))

    async def _assert(client: Any) -> None:
        # 原 key 全部消失；quarantine 镜像保留原字节且无 TTL
        for key, value in seeded.items():
            assert await client.get(key) is None, key
            quarantined = f"{_QUARANTINE_PREFIX}{key}"
            assert await client.get(quarantined) == value, quarantined
            assert await client.pttl(quarantined) == -1
        for base in (
            "komari_memory:global_interaction:pending",
            "komari_memory:global_interaction:leases",
            "komari_memory:global_interaction:lease_owners",
            "komari_memory:global_interaction:snapshots",
        ):
            assert await client.exists(base) == 0, base
            mirrored_type = await client.type(f"{_QUARANTINE_PREFIX}{base}")
            assert mirrored_type in {"set", "hash"}, (
                f"{_QUARANTINE_PREFIX}{base}: {mirrored_type}"
            )
        # active 族复扫归零（quarantine 命名空间不计入）
        active: list[str] = []
        for prefix in _ACTIVE_LEGACY_PREFIXES:
            active.extend([
                key
                async for key in client.scan_iter(f"{prefix}*")
                if not key.startswith(_QUARANTINE_PREFIX)
            ])
        assert active == [], f"active legacy 残留: {active}"

    with _backfilled_database("c") as (params, database_url), _redis_guard():
        redis_run(_flow)
        exit_code, raw_out, raw_err = run_cli_raw(
            capsys, *_finalize_argv(database_url)
        )
        assert exit_code == 0, f"{raw_out}\n{raw_err}"

        redis_run(_assert)

        rows = asyncio.run(_fetch_quarantine_rows(params))
        assert len(rows) == 6, "六个族键都应入账"
        digests = {str(row["payload_digest"]) for row in rows}
        for row in rows:
            int(str(row["payload_digest"]), 16)
        for key, value in seeded.items():
            assert _oracle_digest(key, value) in digests, key

        # 内容安全：输出只含 closed code/count，不出现键名或正文 canary
        combined = f"{raw_out}\n{raw_err}"
        assert "minimum_fulfillment_id" not in combined
        assert CANARY_BODY_TOKEN not in combined
        assert "komari_memory:global_interaction" not in combined


# ---------------------------------------------------------------------------
# 用例：受限群休眠 sidecar 与归因损坏 quarantine
# ---------------------------------------------------------------------------


@SKIP_NO_REDIS
def test_dormancy_sidecar_records_original_pttl(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """受限群可归因 staging/buffer-processing key：sidecar 字段完整并 PERSIST。"""
    staging_key = "komari_memory:staging:profile:sess-canary-1"
    processing_key = "komari_memory:buffer:processing:123456789:tokcanary"
    restricted_payload = json.dumps({"group_id": str(CANARY_GROUP_A)})

    async def _seed(client: Any) -> None:
        await client.set(staging_key, restricted_payload, px=300_000)
        await client.set(processing_key, restricted_payload, px=120_000)

    async def _assert(client: Any) -> None:
        for original in (staging_key, processing_key):
            sidecar = f"{_DORMANCY_PREFIX}{original}"
            fields = await client.hgetall(sidecar)
            assert fields, f"缺少休眠 sidecar: {sidecar}"
            assert fields.get("state") == "DEFERRED"
            assert fields.get("migration_version") == "tsk232"
            assert fields.get("effective_revision", "").isdigit()
            assert fields.get("deferred_at")
            assert fields.get("resume_not_before")
            original_pttl = int(fields.get("original_pttl_ms", "0"))
            assert 100_000 <= original_pttl <= 300_000, (
                f"original_pttl_ms 未记录原始 TTL: {original_pttl}"
            )
            # 原 key PERSIST：保留且无 TTL
            assert await client.get(original) == restricted_payload
            assert await client.pttl(original) == -1

    with _backfilled_database("d") as (_params, database_url), _redis_guard():
        redis_run(_seed)
        exit_code, payload = run_cli(
            capsys, *_finalize_argv(database_url)
        )
        assert exit_code == 0, payload
        redis_run(_assert)


@SKIP_NO_REDIS
def test_unattributable_staging_profile_quarantined(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """payload 归因损坏的 staging key 走 quarantine，绝不做 dormancy sidecar。"""
    corrupt_key = "komari_memory:staging:profile:sess-corrupt"

    async def _seed(client: Any) -> None:
        await client.set(corrupt_key, "not-json{{", px=60_000)

    async def _assert(client: Any) -> None:
        assert await client.get(corrupt_key) is None
        quarantined = f"{_QUARANTINE_PREFIX}{corrupt_key}"
        assert await client.get(quarantined) == "not-json{{"
        assert await client.hgetall(f"{_DORMANCY_PREFIX}{corrupt_key}") == {}

    with _backfilled_database("e") as (params, database_url), _redis_guard():
        redis_run(_seed)
        exit_code, payload = run_cli(
            capsys, *_finalize_argv(database_url)
        )
        assert exit_code == 0, payload
        redis_run(_assert)
        rows = asyncio.run(_fetch_quarantine_rows(params))
        assert any(
            "staging" in str(row["key_family"]) for row in rows
        ), "损坏 staging key 必须记入 quarantine ledger"


# ---------------------------------------------------------------------------
# 用例：attestation 幂等与 active 非零拒绝
# ---------------------------------------------------------------------------


@SKIP_NO_REDIS
def test_rerun_after_finalized_is_noop(capsys: pytest.CaptureFixture[str]) -> None:
    """REDIS_FINALIZED 后重复 finalize 为 no-op：digest/phase/数据不变。"""
    slots_a = f"komari_chat:proactive:slots:{CANARY_GROUP_A}"
    future_score = int(datetime.now(UTC).timestamp() * 1000) + 600_000

    async def _seed(client: Any) -> None:
        await client.zadd(slots_a, {"pending:resv-noop": future_score})

    with _backfilled_database("f") as (params, database_url), _redis_guard():
        redis_run(_seed)
        asyncio.run(_seed_ledger(params, [("resv-noop", CANARY_GROUP_A)]))

        first_code, _first = run_cli(capsys, *_finalize_argv(database_url))
        assert first_code == 0
        gate_first = fetch_gate_row(params)
        assert gate_first is not None
        first_digest = gate_first["redis_finalizer_digest"]
        first_ledger = asyncio.run(_fetch_ledger(params))

        second_code, second_payload = run_cli(
            capsys, *_finalize_argv(database_url)
        )
        assert second_code == 0
        assert second_payload is not None

        gate_second = fetch_gate_row(params)
        assert gate_second is not None
        assert gate_second["phase"] == "REDIS_FINALIZED"
        assert gate_second["redis_finalizer_digest"] == first_digest
        assert asyncio.run(_fetch_ledger(params)) == first_ledger


@SKIP_NO_REDIS
def test_active_legacy_nonzero_rejected_after_finalized(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """复扫发现 active legacy 残留 → LEGACY_ACTIVE_NONZERO 拒绝、phase 不前进。"""
    late_key = "komari_memory:global_interaction:late-canary"

    async def _inject(client: Any) -> None:
        await client.set(late_key, "late-arrival")

    with _backfilled_database("g") as (params, database_url), _redis_guard():
        first_code, _first = run_cli(
            capsys, *_finalize_argv(database_url)
        )
        assert first_code == 0
        gate = fetch_gate_row(params)
        assert gate is not None
        digest = gate["redis_finalizer_digest"]

        redis_run(_inject)
        stage_gate_phase(
            params,
            phase="POSTGRES_BACKFILLED",
            policy_revision=1,
            policy_fingerprint=VALID_POLICY_FINGERPRINT,
            redis_finalizer_digest=str(digest),
        )

        exit_code, payload = run_cli(
            capsys, *_finalize_argv(database_url)
        )
        assert exit_code != 0
        assert payload is not None
        assert payload["status"] == "error"
        assert payload["error_code"] == "LEGACY_ACTIVE_NONZERO"

        gate_after = fetch_gate_row(params)
        assert gate_after is not None
        assert gate_after["phase"] == "POSTGRES_BACKFILLED"
