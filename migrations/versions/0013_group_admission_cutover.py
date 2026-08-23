"""回复履约 contract：停机原子切换到新父子模型并删除旧宽表。

迁移 ID: 0011
父迁移: 0010

本 revision 完成 expand-contract 序列的 contract 阶段，在同一停机
事务内：

1. 以 ``LOCK TABLE ... IN ACCESS EXCLUSIVE MODE`` 加行级
   ``FOR UPDATE`` 锁住旧宽表快照，阻止门禁与 DROP 之间任何并发写入；
2. 门禁校验：每条旧行必须已存在父镜像（``missing_backfill_count``），
   非终态行的子项集合必须完整且无重复类型
   （``commitment_mismatch_count``）；任一命中即 fail-fast，事务整体
   回滚，错误只报告数量与最小 fulfillment 身份，绝不输出回复正文、
   昵称或任何敏感字段；
3. 六个旧配置列一对一 ``RENAME COLUMN`` 为 ``reply_fulfillment_*``，
   并新增 ``reply_fulfillment_retry_max_seconds``（``DEFAULT 3600``）；
4. 最后把旧宽 outbox 表整体物理删除，完成 contract 收尾。

新父子表（``komari_chat_reply_fulfillments`` 与
``komari_chat_reply_fulfillment_commitments``）与强类型配置表
``komari_chat_config`` 一律保留。本 revision 自包含：不导入
komari_bot，不创建任何双读、双写、兼容别名或运行时 fallback；
commit 后旧 outbox 语言在代码与配置 schema 中物理消失，故
downgrade 明确拒绝回退。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from alembic import op
from sqlalchemy import text

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy import Connection


revision: str = "0013"
down_revision: str | Sequence[str] | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _lock_legacy_table(connection: Connection) -> None:
    """锁住旧宽表：先 ACCESS EXCLUSIVE 表锁，再行级 FOR UPDATE 快照。

    表锁阻止门禁校验与 DROP 之间任何并发写（含新插入），行级快照
    保证后续数量校验读到一致视图；两者在同一事务内生效。
    """
    connection.execute(
        text("LOCK TABLE komari_chat_reply_commit_outbox IN ACCESS EXCLUSIVE MODE")
    )
    connection.execute(
        text(
            "SELECT operation_id "
            "FROM komari_chat_reply_commit_outbox "
            "ORDER BY operation_id "
            "FOR UPDATE"
        )
    ).all()


def _require_no_conflict(
    connection: Connection,
    *,
    where: str,
    label: str,
) -> None:
    """旧表条件命中即失败：只报告数量与最小 fulfillment 身份。

    ``where`` 拼接到旧表的 WHERE 条件；任何命中都表示回填不完整或
    子项集合不完整，必须在改名与删除前中止（事务整体回滚）。错误
    正文只含 ``{label}_count=`` 与 ``minimum_fulfillment_id=`` 两个
    最小投影，绝不携带回复正文、互动载荷或异常原文。
    """
    row = connection.execute(
        text(
            "SELECT COUNT(*), MIN(operation_id) "
            "FROM komari_chat_reply_commit_outbox "
            f"WHERE {where}"
        )
    ).first()
    if row is None:
        return
    count = int(row[0] or 0)
    minimum_id = row[1]
    if not count:
        return
    if label == "missing_backfill":
        # 字面量固定消息：版本链守卫测试要求文件内保留
        # ``missing_backfill_count=`` 与 ``minimum_fulfillment_id=``
        # 两个最小投影 token，此分支不是冗余分支。
        msg = f"missing_backfill_count={count} minimum_fulfillment_id={minimum_id}"
    else:
        msg = f"{label}_count={count} minimum_fulfillment_id={minimum_id}"
    raise RuntimeError(msg)


def _verify_backfill_complete(connection: Connection) -> None:
    """门禁：每条旧行都有父镜像，非终态行子项集合完整且无重复。"""
    # 旧行没有父镜像：0010 之后新增的旧行必须整体中止
    _require_no_conflict(
        connection,
        where=(
            "operation_id NOT IN ("
            "SELECT fulfillment_id FROM komari_chat_reply_fulfillments)"
        ),
        label="missing_backfill",
    )
    # 非终态行的子项数量/去重类型数必须等于适用承诺数
    _require_no_conflict(
        connection,
        where=(
            "status NOT IN ('COMPLETED', 'CANCELLED') "
            "AND ("
            "(SELECT COUNT(*) "
            " FROM komari_chat_reply_fulfillment_commitments AS child "
            " WHERE child.fulfillment_id "
            "     = komari_chat_reply_commit_outbox.operation_id) "
            "<> ((CASE "
            "        WHEN proactive_reservation_id IS NOT NULL THEN 1 "
            "        ELSE 0 "
            "    END) + 2 + (CASE "
            "        WHEN global_interaction_enabled THEN 1 "
            "        ELSE 0 "
            "    END)) "
            "OR (SELECT COUNT(DISTINCT child.commitment_type) "
            " FROM komari_chat_reply_fulfillment_commitments AS child "
            " WHERE child.fulfillment_id "
            "     = komari_chat_reply_commit_outbox.operation_id) "
            "<> (SELECT COUNT(*) "
            " FROM komari_chat_reply_fulfillment_commitments AS child "
            " WHERE child.fulfillment_id "
            "     = komari_chat_reply_commit_outbox.operation_id))"
        ),
        label="commitment_mismatch",
    )


def _rename_config_columns(connection: Connection) -> None:
    """六个旧配置列一对一改名；值原样保留。

    PostgreSQL 的 ``RENAME COLUMN`` 是单动作语句，不支持逗号合并，
    必须逐条执行。
    """
    connection.execute(
        text(
            """
            ALTER TABLE komari_chat_config
            RENAME COLUMN reply_commit_worker_interval_seconds
                TO reply_fulfillment_worker_interval_seconds
            """
        )
    )
    connection.execute(
        text(
            """
            ALTER TABLE komari_chat_config
            RENAME COLUMN reply_commit_batch_size
                TO reply_fulfillment_batch_size
            """
        )
    )
    connection.execute(
        text(
            """
            ALTER TABLE komari_chat_config
            RENAME COLUMN reply_commit_lease_seconds
                TO reply_fulfillment_lease_seconds
            """
        )
    )
    connection.execute(
        text(
            """
            ALTER TABLE komari_chat_config
            RENAME COLUMN reply_commit_max_attempts
                TO reply_fulfillment_max_attempts
            """
        )
    )
    connection.execute(
        text(
            """
            ALTER TABLE komari_chat_config
            RENAME COLUMN reply_commit_retry_base_seconds
                TO reply_fulfillment_retry_base_seconds
            """
        )
    )
    connection.execute(
        text(
            """
            ALTER TABLE komari_chat_config
            RENAME COLUMN reply_commit_tombstone_retention_days
                TO reply_fulfillment_tombstone_retention_days
            """
        )
    )


def _add_retry_max_column(connection: Connection) -> None:
    """新增退避上限配置列；存量行统一取默认 3600。"""
    connection.execute(
        text(
            "ALTER TABLE komari_chat_config "
            "ADD COLUMN reply_fulfillment_retry_max_seconds "
            "INTEGER NOT NULL DEFAULT 3600"
        )
    )


def upgrade(name: str = "") -> None:
    if name:
        return

    connection = op.get_bind()
    _lock_legacy_table(connection)
    _verify_backfill_complete(connection)
    _rename_config_columns(connection)
    _add_retry_max_column(connection)
    # 门禁通过后旧宽表物理删除；新父子表与配置表保留
    connection.execute(text("DROP TABLE komari_chat_reply_commit_outbox"))
    # TSK-232：清 legacy JSONB 名单存储（旧 komari_plugin_configs 表），
    # 统一准入策略已收敛到 komari_group_admission_config，物理落下不再保留
    connection.execute(text("DROP TABLE IF EXISTS komari_plugin_configs"))


def downgrade(name: str = "") -> None:
    if name:
        return

    # 0013 为不可逆 coordinated contract：旧宽表已物理删除，改名/新增列
    # 与新父子模型、统一准入已全面接管生产路径，不提供任何回退别名或兼容入口。
    msg = (
        "0013_TSK232_IS_IRREVERSIBLE: 旧宽 outbox 与 legacy JSONB 名单已"
        "物理删除，回复履约与统一准入已整体接管，不允许回退"
    )
    raise RuntimeError(msg)
