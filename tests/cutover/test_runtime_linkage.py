"""TSK-232 轮 B —— 运行时 ORM/仓储联动红基线（门控 DB，真实仓储驱动）。

锁定数据面 cutover 在生产代码侧的联动契约：

- ``ProposalRow`` 新增四列（admission_state / execution_hold_code /
  admission_deferred_revision / admission_deferred_at）、
  ``AnnouncementDispatchRow`` 新增 reconciliation_code：SQLModel 元数据
  与迁移库 information_schema 双侧存在；
- ``python -m komari_bot.db.orm_bootstrap check`` 在隔离库零漂移；
- 普通认领路径（claim_for_approval / recover_publication）统一附加
  ``admission_state='ACTIVE' AND execution_hold_code IS NULL``：DEFERRED
  与 hold 行一律拒收且状态不被改写；
- 唤醒翻转：admitted 裁决后对 DEFERRED 行幂等 CAS DEFERRED→ACTIVE（同
  revision 只一次），翻转后普通认领即可成功。

仓储用例以子进程驱动真实 ``ProposalRepository``（子进程内经
``nonebot.init() + load_plugin("nonebot_plugin_orm")`` 把共享引擎绑定到
一次性隔离库，绝不触碰共享门控库）。隔离库 = 门控库名 +
``_tsk232b1rt``，用例结束 DROP。红基线纪律：目标列/方法未实现时以列不
存在或 AttributeError 红。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from tests.cutover.support import (
    CANARY_GROUP_A,
    PROJECT_ROOT,
    SKIP_NO_POSTGRES,
    drop_scratch_database,
    recreate_scratch_database,
    run_bootstrap,
    scratch_url,
)

if TYPE_CHECKING:
    from collections.abc import Iterator


pytestmark = [SKIP_NO_POSTGRES]


# ---------------------------------------------------------------------------
# 隔离库编排
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _head_database() -> Iterator[tuple[dict[str, Any], str]]:
    """重建 ``_tsk232b1rt`` 隔离库并 upgrade head，结束时 DROP。"""
    params = asyncio.run(recreate_scratch_database("_tsk232b1rt"))
    database_url = scratch_url(str(params["database"]))
    try:
        result = run_bootstrap(database_url, "upgrade", "head")
        assert result.returncode == 0, result.stderr
        yield params, database_url
    finally:
        asyncio.run(drop_scratch_database(str(params["database"])))


async def _connect(params: dict[str, Any]) -> Any:
    import asyncpg

    return await asyncpg.connect(**params)


async def _column_names(params: dict[str, Any], table: str) -> set[str]:
    connection = await _connect(params)
    try:
        rows = await connection.fetch(
            "SELECT column_name FROM information_schema.columns"
            " WHERE table_name = $1",
            table,
        )
    finally:
        await connection.close()
    return {str(row["column_name"]) for row in rows}


# ---------------------------------------------------------------------------
# 用例一：ORM 元数据与数据库双侧新列 + check 零漂移
# ---------------------------------------------------------------------------


def test_orm_models_declare_new_admission_columns() -> None:
    """SQLModel 元数据必须声明本轮全部新列（模型侧真源）。"""
    from komari_bot.plugins.komari_custom.orm_models import ProposalRow
    from komari_bot.plugins.komari_management.orm_models import (
        AnnouncementDispatchRow,
    )

    proposal_columns = {str(column.name) for column in ProposalRow.__table__.columns}
    for column in (
        "admission_state",
        "execution_hold_code",
        "admission_deferred_revision",
        "admission_deferred_at",
    ):
        assert column in proposal_columns, f"ProposalRow 缺少新列 {column}"

    announcement_columns = {
        str(column.name) for column in AnnouncementDispatchRow.__table__.columns
    }
    assert "reconciliation_code" in announcement_columns


def test_database_columns_exist_and_check_has_zero_drift() -> None:
    """隔离库 head 后新列落库，且 orm_bootstrap check 必须零漂移通过。"""
    with _head_database() as (params, database_url):
        proposal_columns = asyncio.run(_column_names(params, "komari_custom_proposals"))
        for column in (
            "admission_state",
            "execution_hold_code",
            "admission_deferred_revision",
            "admission_deferred_at",
        ):
            assert column in proposal_columns, f"库表缺少新列 {column}"
        announcement_columns = asyncio.run(
            _column_names(params, "komari_announcement_dispatches")
        )
        assert "reconciliation_code" in announcement_columns

        check = run_bootstrap(database_url, "check")
        assert check.returncode == 0, (
            f"orm_bootstrap check 出现漂移:\n{check.stdout}\n{check.stderr}"
        )


# ---------------------------------------------------------------------------
# 子进程驱动真实 ProposalRepository（引擎绑定到隔离库）
# ---------------------------------------------------------------------------

_DRIVER_SCRIPT = """
import asyncio
import json
import sys


def main() -> None:
    import nonebot

    nonebot.init()
    nonebot.load_plugin("nonebot_plugin_orm")

    from komari_bot.plugins.komari_custom.proposal_repository import (
        ProposalRepository,
    )

    spec = json.loads(sys.argv[1])

    async def run() -> dict[str, object]:
        repo = ProposalRepository()
        await repo.initialize()
        outcome: dict[str, object] = {}
        if spec["scenario"] == "reject":
            deferred = await repo.claim_for_approval(
                spec["deferred_voting_id"], "tok-c13-deferred",
                lease_seconds=60,
            )
            outcome["claim_deferred_none"] = deferred is None
            held = await repo.claim_for_approval(
                spec["held_approving_id"], "tok-c13-held", lease_seconds=60
            )
            outcome["claim_held_none"] = held is None
            recovered = await repo.recover_publication(
                spec["deferred_publishing_key"], 555001
            )
            outcome["recover_deferred_none"] = recovered is None
        elif spec["scenario"] == "wake":
            first = await repo.wake_deferred(
                spec["target_id"], policy_revision=spec["policy_revision"]
            )
            outcome["wake_returned"] = first is not None
            second = await repo.wake_deferred(
                spec["target_id"], policy_revision=spec["policy_revision"]
            )
            outcome["rewake_returned"] = second is not None
            claimed = await repo.claim_for_approval(
                spec["target_id"], "tok-c13-wake", lease_seconds=60
            )
            outcome["claim_status"] = None if claimed is None else claimed.status
        return outcome

    print("RESULT_JSON:" + json.dumps(asyncio.run(run())))


main()
"""


def _run_repository_driver(
    database_url: str,
    spec: dict[str, Any],
) -> dict[str, Any]:
    """在绑定隔离库的子进程里驱动真实 ProposalRepository 并解析结果。"""
    env = os.environ.copy()
    env["SQLALCHEMY_DATABASE_URL"] = database_url
    env["PYTHONPATH"] = str(PROJECT_ROOT)
    result = subprocess.run(
        [sys.executable, "-c", _DRIVER_SCRIPT, json.dumps(spec)],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=180,
    )
    marker = "RESULT_JSON:"
    for line in result.stdout.splitlines():
        if line.startswith(marker):
            payload: dict[str, Any] = json.loads(line[len(marker) :])
            return payload
    msg = (
        "仓储驱动子进程未产出结果 JSON\n"
        f"returncode={result.returncode}\n{result.stdout}\n{result.stderr}"
    )
    raise AssertionError(msg)


async def _seed_linkage_proposals(params: dict[str, Any]) -> dict[str, int]:
    """播种 DEFERRED/hold 联动夹具，返回语义键 → id（publication_key 兜底）。"""
    connection = await _connect(params)
    try:
        ids: dict[str, int] = {}
        seeds: list[tuple[str, str, str, str | None, str]] = [
            # key, 状态, admission_state, hold code, publication_key
            ("deferred_voting", "voting", "DEFERRED", None,
             "pub-key-rt-deferred-voting"),
            ("held_approving", "approving", "ACTIVE",
             "PUBLICATION_DELIVERY_UNKNOWN", "pub-key-rt-held"),
            ("deferred_publishing", "failed", "DEFERRED", None,
             "pub-key-rt-deferred-pub"),
            ("wake_target", "voting", "DEFERRED", None,
             "pub-key-rt-wake"),
        ]
        for index, (key, status, admission_state, hold_code, pub_key) in enumerate(
            seeds, 1
        ):
            approval_started = (
                datetime.now(UTC) - timedelta(hours=2)
                if status == "approving"
                else None
            )
            row_id = await connection.fetchval(
                """
                INSERT INTO komari_custom_proposals (
                    group_id, proposer_id, title, content, status,
                    publication_key, required_votes, vote_count,
                    admission_state, execution_hold_code,
                    approval_started_at, expired_at
                ) VALUES ($1, 888777666, $2, $3, $4, $5, 3, 3, $6, $7,
                          $8, NOW() + INTERVAL '1 day')
                RETURNING id
                """,
                CANARY_GROUP_A,
                f"rt-canary-title-{index}",
                f"rt-canary-body-{index}",
                status,
                pub_key,
                admission_state,
                hold_code,
                approval_started,
            )
            ids[key] = int(row_id)
        return ids
    finally:
        await connection.close()


async def _delete_fixture_rows(params: dict[str, Any]) -> None:
    connection = await _connect(params)
    try:
        await connection.execute(
            "DELETE FROM komari_custom_proposals"
            " WHERE publication_key LIKE 'pub-key-rt-%'"
        )
    finally:
        await connection.close()


# ---------------------------------------------------------------------------
# 用例二：认领拒收与唤醒翻转
# ---------------------------------------------------------------------------


def test_claim_paths_reject_deferred_and_held_rows() -> None:
    """普通认领统一拒收 DEFERRED 与 hold 行，且不改写其状态。"""
    with _head_database() as (params, database_url):
        ids = asyncio.run(_seed_linkage_proposals(params))
        try:
            outcome = _run_repository_driver(
                database_url,
                {
                    "scenario": "reject",
                    "deferred_voting_id": ids["deferred_voting"],
                    "held_approving_id": ids["held_approving"],
                    "deferred_publishing_key": "pub-key-rt-deferred-pub",
                },
            )
            assert outcome["claim_deferred_none"] is True
            assert outcome["claim_held_none"] is True
            assert outcome["recover_deferred_none"] is True

            async def _states() -> tuple[str, str, str]:
                connection = await _connect(params)
                try:
                    rows = await connection.fetch(
                        "SELECT id, status, admission_state FROM"
                        " komari_custom_proposals WHERE id = ANY($1::int[])",
                        [
                            ids["deferred_voting"],
                            ids["held_approving"],
                            ids["deferred_publishing"],
                        ],
                    )
                finally:
                    await connection.close()
                by_id = {int(row["id"]): row for row in rows}
                deferred = by_id[ids["deferred_voting"]]
                held = by_id[ids["held_approving"]]
                publishing = by_id[ids["deferred_publishing"]]
                return (
                    str(deferred["status"]),
                    str(held["status"]),
                    str(publishing["status"]),
                )

            deferred_status, held_status, publishing_status = asyncio.run(_states())
            assert deferred_status == "voting", "DEFERRED 行状态不得被改写"
            assert held_status == "approving", "hold 行状态不得被改写"
            assert publishing_status == "failed", "恢复失败应保持原状态"
        finally:
            asyncio.run(_delete_fixture_rows(params))


def test_wake_flip_enables_claim_for_admitted_deferred_row() -> None:
    """admitted 裁决后的唤醒翻转：幂等 CAS DEFERRED→ACTIVE 且可被认领。"""
    with _head_database() as (params, database_url):
        ids = asyncio.run(_seed_linkage_proposals(params))
        try:
            outcome = _run_repository_driver(
                database_url,
                {
                    "scenario": "wake",
                    "target_id": ids["wake_target"],
                    "policy_revision": 7,
                },
            )
            assert outcome["wake_returned"] is True, "首次唤醒必须翻转成功"
            assert outcome["rewake_returned"] is True, "重复唤醒必须幂等"
            assert outcome["claim_status"] == "approving", (
                "唤醒后普通认领必须可成功"
            )

            async def _row_state() -> tuple[str, int]:
                connection = await _connect(params)
                try:
                    row = await connection.fetchrow(
                        "SELECT admission_state,"
                        " COALESCE(admission_deferred_revision, 0) AS rev"
                        " FROM komari_custom_proposals WHERE id = $1",
                        ids["wake_target"],
                    )
                finally:
                    await connection.close()
                assert row is not None
                return str(row["admission_state"]), int(row["rev"])

            state, revision = asyncio.run(_row_state())
            assert state == "ACTIVE", "唤醒后必须收敛为 ACTIVE"
            assert revision == 7, "同 revision 只翻转一次，保留触发 revision"
        finally:
            asyncio.run(_delete_fixture_rows(params))
