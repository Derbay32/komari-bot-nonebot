"""TSK-232 轮 B —— 0012 admission backfill reply 转换矩阵红基线（门控 DB）。

锁定 0012 数据面对回复履约新父子表（0011 镜像产物）的破坏性转换契约：

- 全部 ``NOT_STARTED`` 行不区分 policy（受限/获准群一视同仁）原子转为
  ``NOT_DELIVERED`` 并落 ``not_delivered_at``；正文清空、全部子承诺
  payload 清空（cutover-cancelled tombstone 最小化）；
- 带 proactive 预占身份的 NOT_STARTED 行按 ``(reservation_id, group_id)``
  投影进 ``komari_reply_reservation_cleanup_ledger``（ON CONFLICT DO
  NOTHING，重复预占身份只记一条）；
- ``PENDING_CONFIRMATION`` / ``DELIVERED`` / 既有 ``NOT_DELIVERED`` 控制行
  正文与 payload 原样保留，绝不进 cleanup ledger；
- legacy 宽表路线：旧 ``PREPARED`` 行经 0011 镜像为 PENDING_CONFIRMATION
  后在 0012 保持正文不动（镜像行不是 NOT_STARTED，绝不转换、不进
  ledger）；CANCELLED 镜像为既有 NOT_DELIVERED 亦不被二次改写。

隔离库 = 门控库名 + ``_tsk232b1reply``（中途停链重放 0011→0012），用例
结束 DROP；直连 SQL 仅用于前置编排与结果断言。红基线纪律：目标列/表/
迁移行为未实现时以断言失败或表不存在红。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import asyncpg

from tests.cutover.support import (
    CANARY_BODY_TOKEN,
    CANARY_GROUP_A,
    CANARY_GROUP_C,
    SKIP_NO_POSTGRES,
    VALID_POLICY,
    VALID_POLICY_FINGERPRINT,
    drop_scratch_database,
    fetch_gate_row,
    overwrite_admission_policy,
    recreate_scratch_database,
    run_bootstrap,
    scratch_url,
    stage_gate_phase,
)

pytestmark = [SKIP_NO_POSTGRES]

if TYPE_CHECKING:
    from collections.abc import Iterator

_SHARED_RESERVATION_ID = "resv-canary-shared"


# ---------------------------------------------------------------------------
# 隔离库与前置编排 helper（中途停链：upgrade 到指定 revision 而非 head）
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _scratch_at_revision(
    revision: str,
) -> Iterator[tuple[dict[str, Any], str]]:
    """重建 ``_tsk232b1reply`` 隔离库并 upgrade 到指定 revision。"""
    params = asyncio.run(recreate_scratch_database("_tsk232b1reply"))
    database_url = scratch_url(str(params["database"]))
    try:
        result = run_bootstrap(database_url, "upgrade", revision)
        assert result.returncode == 0, result.stderr
        yield params, database_url
    finally:
        asyncio.run(drop_scratch_database(str(params["database"])))


def _arrange_policy_and_evidence_phase(params: dict[str, Any]) -> None:
    """编排 operator 路径前置：policy 内容 + gate 相位 REDIS_EVIDENCE_CAPTURED。"""
    overwrite_admission_policy(params, VALID_POLICY)
    stage_gate_phase(
        params,
        phase="REDIS_EVIDENCE_CAPTURED",
        policy_revision=1,
        policy_fingerprint=VALID_POLICY_FINGERPRINT,
    )


async def _seed_mirrored_families(params: dict[str, Any]) -> dict[str, str]:
    """直接向新父子表播种四状态家族（fixture 编排；满足父表 CHECK）。

    返回语义键 → fulfillment_id 索引：
    - ``ns_restricted`` / ``ns_admitted``：受限/获准群的 NOT_STARTED 行
      （含正文与承诺 payload；受限行带共享 reservation 身份）；
    - ``ns_dup``：复用同一 reservation 身份的第三条 NOT_STARTED；
    - ``ctl_pending`` / ``ctl_delivered`` / ``ctl_cancelled``：控制行。
    """
    connection = await _connect(params)
    try:
        ids: dict[str, str] = {}
        rows: list[tuple[str, str, str | None, int]] = [
            ("ns_restricted", "NOT_STARTED", str(CANARY_GROUP_A), 1),
            ("ns_admitted", "NOT_STARTED", str(CANARY_GROUP_C), 2),
            ("ns_dup", "NOT_STARTED", str(CANARY_GROUP_A), 3),
            ("ctl_pending", "PENDING_CONFIRMATION", str(CANARY_GROUP_A), 4),
            ("ctl_delivered", "DELIVERED", str(CANARY_GROUP_C), 5),
            ("ctl_cancelled", "NOT_DELIVERED", str(CANARY_GROUP_A), 6),
        ]
        for key, state, group_id, index in rows:
            fid = f"fulfill-b1reply-{index:02d}-{key}"
            ids[key] = fid
            has_reservation = key in {"ns_restricted", "ns_dup"}
            await connection.execute(
                """
                INSERT INTO komari_chat_reply_fulfillments (
                    fulfillment_id, payload_hash, request_trace_id,
                    trigger_message_id, trigger_user_id, group_id,
                    bot_self_id, adapter_name, reply_target_message_id,
                    reply_content, delivery_state,
                    send_started_at, delivered_at, not_delivered_at
                ) VALUES (
                    $1, $2, $3, $4, '888777666', $5, 'canary-self', 'test',
                    $6, $7, $8, $9, $10, $11
                )
                """,
                fid,
                f"hash-{fid}",
                f"trace-{fid}",
                f"trigger-{fid}",
                group_id,
                f"target-{fid}",
                f"{CANARY_BODY_TOKEN}-{index}",
                state,
                _send_started_expr(state),
                _delivered_expr(state),
                _not_delivered_expr(state),
            )
            if state != "NOT_DELIVERED":
                await connection.execute(
                    """
                    INSERT INTO komari_chat_reply_fulfillment_commitments (
                        fulfillment_id, commitment_type, state, payload
                    ) VALUES ($1, 'favorability_adjustment', 'PENDING', $2)
                    """,
                    fid,
                    json.dumps({"user_id": "888777666", "delta": 3}),
                )
            if has_reservation:
                await connection.execute(
                    """
                    INSERT INTO komari_chat_reply_fulfillment_commitments (
                        fulfillment_id, commitment_type, state, payload
                    ) VALUES (
                        $1, 'proactive_reply_confirmation', 'PENDING', $2
                    )
                    """,
                    fid,
                    json.dumps({
                        "group_id": group_id,
                        "reservation_id": _SHARED_RESERVATION_ID,
                        "cooldown_seconds": 120,
                    }),
                )
        return ids
    finally:
        await connection.close()


def _send_started_expr(state: str) -> object:
    """按父表 CHECK 约束给 send_started_at 取值。"""
    return datetime.now(UTC) if state != "NOT_STARTED" else None


def _delivered_expr(state: str) -> object:
    """DELIVERED 行必须带送达时间戳（父表 CHECK）。"""
    return datetime.now(UTC) if state == "DELIVERED" else None


def _not_delivered_expr(state: str) -> object:
    """NOT_DELIVERED 行必须带未送达时间戳（父表 CHECK）。"""
    return datetime.now(UTC) if state == "NOT_DELIVERED" else None


async def _connect(params: dict[str, Any]) -> Any:
    """打开隔离库 asyncpg 连接（测试编排/断言专用）。"""
    return await asyncpg.connect(**params)


async def _fetch_parent(params: dict[str, Any], fid: str) -> dict[str, Any]:
    connection = await _connect(params)
    try:
        row = await connection.fetchrow(
            """
            SELECT delivery_state, reply_content, not_delivered_at
            FROM komari_chat_reply_fulfillments
            WHERE fulfillment_id = $1
            """,
            fid,
        )
    finally:
        await connection.close()
    assert row is not None, f"父行缺失: {fid}"
    return dict(row)


async def _fetch_children(params: dict[str, Any], fid: str) -> list[dict[str, Any]]:
    connection = await _connect(params)
    try:
        rows = await connection.fetch(
            """
            SELECT commitment_type, state, payload
            FROM komari_chat_reply_fulfillment_commitments
            WHERE fulfillment_id = $1
            ORDER BY commitment_type
            """,
            fid,
        )
    finally:
        await connection.close()
    return [dict(row) for row in rows]


async def _fetch_ledger_pairs(params: dict[str, Any]) -> list[tuple[str, int]]:
    connection = await _connect(params)
    try:
        rows = await connection.fetch(
            "SELECT reservation_id, group_id"
            " FROM komari_reply_reservation_cleanup_ledger"
            " ORDER BY reservation_id"
        )
    finally:
        await connection.close()
    return [(str(row["reservation_id"]), int(row["group_id"])) for row in rows]


async def _insert_legacy_outbox_rows(params: dict[str, Any]) -> None:
    """向旧宽 outbox 播种 PREPARED（带预占）与 CANCELLED 两行。"""
    connection = await _connect(params)
    try:
        await connection.execute(
            """
            INSERT INTO komari_chat_reply_commit_outbox (
                operation_id, payload_hash, request_trace_id,
                source_message_id, group_id, user_id, bot_self_id,
                adapter_name, reply_target_message_id, reply_content,
                reply_timestamp, favorability_delta, favorability_reason,
                proactive_reservation_id, proactive_cooldown_seconds,
                global_interaction_enabled, global_interaction_trigger_size,
                status, prepared_at, updated_at, bot_nickname
            ) VALUES (
                'legacy-prepared-canary-01',
                'a0000000000000000000000000000000'
                '00000000000000000000000000000000',
                'trace-legacy-01', 'src-legacy-01', '123456789', '888777666',
                'canary-self', 'test', 'target-legacy-01',
                'legacy-canary-body-keep-me', 100.0, 1, '互动',
                'resv-legacy-canary-01', 120, FALSE, 3,
                'PREPARED', NOW(), NOW(), '小鞠'
            )
            """,
        )
        await connection.execute(
            """
            INSERT INTO komari_chat_reply_commit_outbox (
                operation_id, payload_hash, request_trace_id,
                source_message_id, group_id, user_id, bot_self_id,
                adapter_name, reply_target_message_id, reply_content,
                reply_timestamp, favorability_delta, favorability_reason,
                global_interaction_enabled, global_interaction_trigger_size,
                status, prepared_at, delivered_at, not_delivered_at,
                updated_at, bot_nickname
            ) VALUES (
                'legacy-cancelled-canary-02',
                'b0000000000000000000000000000000'
                '00000000000000000000000000000000',
                'trace-legacy-02', 'src-legacy-02', '555000111', '888777666',
                'canary-self', 'test', 'target-legacy-02',
                'legacy-cancelled-body', 100.0, 1, '互动',
                FALSE, 3, 'CANCELLED', NOW(), NOW(), NOW(), NOW(), '小鞠'
            )
            """,
        )
    finally:
        await connection.close()


# ---------------------------------------------------------------------------
# 用例
# ---------------------------------------------------------------------------


def test_0012_converts_all_not_started_rows_regardless_of_policy() -> None:
    """NOT_STARTED 全转 NOT_DELIVERED：正文/payload 清空、ledger 投影去重。"""
    with _scratch_at_revision("0011") as (params, database_url):
        ids = asyncio.run(_seed_mirrored_families(params))
        _arrange_policy_and_evidence_phase(params)

        result = run_bootstrap(database_url, "upgrade", "0012")
        assert result.returncode == 0, result.stderr

        # 受限群与获准群的 NOT_STARTED 一律转为 NOT_DELIVERED
        for key in ("ns_restricted", "ns_admitted", "ns_dup"):
            parent = asyncio.run(_fetch_parent(params, ids[key]))
            assert parent["delivery_state"] == "NOT_DELIVERED"
            assert parent["not_delivered_at"] is not None
            assert parent["reply_content"] is None, "tombstone 行必须清空正文"
            children = asyncio.run(_fetch_children(params, ids[key]))
            assert children, "原 NOT_STARTED 行应有子承诺记录"
            for child in children:
                assert child["payload"] is None, "子承诺 payload 必须全部清空"

        # ledger 投影：共享 reservation 身份只记一条（ON CONFLICT DO NOTHING）
        pairs = asyncio.run(_fetch_ledger_pairs(params))
        assert pairs == [(_SHARED_RESERVATION_ID, CANARY_GROUP_A)]

        gate = fetch_gate_row(params)
        assert gate is not None
        assert gate["phase"] == "POSTGRES_BACKFILLED"


def test_0012_preserves_pending_delivered_and_existing_not_delivered() -> None:
    """控制行矩阵：PENDING/DELIVERED 正文与 payload 保留，既有未送达行不动。"""
    with _scratch_at_revision("0011") as (params, database_url):
        ids = asyncio.run(_seed_mirrored_families(params))
        _arrange_policy_and_evidence_phase(params)

        result = run_bootstrap(database_url, "upgrade", "0012")
        assert result.returncode == 0, result.stderr

        pending = asyncio.run(_fetch_parent(params, ids["ctl_pending"]))
        assert pending["delivery_state"] == "PENDING_CONFIRMATION"
        assert pending["reply_content"] == f"{CANARY_BODY_TOKEN}-4"

        delivered = asyncio.run(_fetch_parent(params, ids["ctl_delivered"]))
        assert delivered["delivery_state"] == "DELIVERED"
        assert delivered["reply_content"] == f"{CANARY_BODY_TOKEN}-5"
        children = asyncio.run(_fetch_children(params, ids["ctl_delivered"]))
        payloads = {
            str(row["commitment_type"]): row["payload"] for row in children
        }
        assert payloads["favorability_adjustment"] == {
            "user_id": "888777666",
            "delta": 3,
        }
        proactive_payload = payloads["proactive_reply_confirmation"]
        assert proactive_payload is not None
        assert json.loads(json.dumps(proactive_payload))["reservation_id"] == (
            _SHARED_RESERVATION_ID
        )

        cancelled = asyncio.run(_fetch_parent(params, ids["ctl_cancelled"]))
        assert cancelled["delivery_state"] == "NOT_DELIVERED"

        # 控制行不产生任何 ledger 条目：ledger 只含 NOT_STARTED 投影
        pairs = asyncio.run(_fetch_ledger_pairs(params))
        assert pairs == [(_SHARED_RESERVATION_ID, CANARY_GROUP_A)]


def test_0012_legacy_mirror_route_keeps_prepared_body_intact() -> None:
    """legacy 路线：镜像 PENDING_CONFIRMATION 在 0012 不被转换、不进 ledger。"""
    with _scratch_at_revision("0010") as (params, database_url):
        asyncio.run(_insert_legacy_outbox_rows(params))

        mirror = run_bootstrap(database_url, "upgrade", "0011")
        assert mirror.returncode == 0, mirror.stderr

        prepared_parent = asyncio.run(
            _fetch_parent(params, "legacy-prepared-canary-01")
        )
        assert prepared_parent["delivery_state"] == "PENDING_CONFIRMATION"
        assert prepared_parent["reply_content"] == "legacy-canary-body-keep-me"
        cancelled_parent = asyncio.run(
            _fetch_parent(params, "legacy-cancelled-canary-02")
        )
        assert cancelled_parent["delivery_state"] == "NOT_DELIVERED"

        _arrange_policy_and_evidence_phase(params)
        result = run_bootstrap(database_url, "upgrade", "0012")
        assert result.returncode == 0, result.stderr

        still_pending = asyncio.run(
            _fetch_parent(params, "legacy-prepared-canary-01")
        )
        assert still_pending["delivery_state"] == "PENDING_CONFIRMATION"
        assert still_pending["reply_content"] == "legacy-canary-body-keep-me"
        # 镜像行没有 NOT_STARTED 来源：cleanup ledger 必须保持为空
        assert asyncio.run(_fetch_ledger_pairs(params)) == []
        for child in asyncio.run(
            _fetch_children(params, "legacy-prepared-canary-01")
        ):
            assert child["payload"] is not None
