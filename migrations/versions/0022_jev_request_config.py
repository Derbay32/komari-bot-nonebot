"""新增 Jev 请求配置列（TSK-181）。

迁移 ID: 0022
父迁移: 0021

从 SQLModel 元数据自动生成 5 列，再补充存量行的临时默认值。
升级保留既有配置、修订号与更新时间。
升级完成后删除服务器默认值，使最终结构与模型一致。
本迁移不启用 Jev 业务路径。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "0022"
down_revision: str | Sequence[str] | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade(name: str = "") -> None:
    if name:
        return
    with op.batch_alter_table("komari_decision_config") as batch_op:
        batch_op.add_column(
            sa.Column("jev_base_url", sa.String(), nullable=False, server_default="")
        )
        batch_op.add_column(
            sa.Column("jev_api_key", sa.String(), nullable=False, server_default="")
        )
        batch_op.add_column(
            sa.Column(
                "jev_model", sa.String(), nullable=False, server_default="jev-1.14.0"
            )
        )
        batch_op.add_column(
            sa.Column(
                "jev_http_timeout_seconds",
                sa.Float(),
                nullable=False,
                server_default=sa.text("3.0"),
            )
        )
        batch_op.add_column(
            sa.Column(
                "jev_request_timeout_seconds",
                sa.Float(),
                nullable=False,
                server_default=sa.text("3.0"),
            )
        )

    with op.batch_alter_table("komari_decision_config") as batch_op:
        for column in (
            "jev_base_url",
            "jev_api_key",
            "jev_model",
            "jev_http_timeout_seconds",
            "jev_request_timeout_seconds",
        ):
            batch_op.alter_column(column, server_default=None)


def downgrade(name: str = "") -> None:
    if name:
        return
    with op.batch_alter_table("komari_decision_config") as batch_op:
        batch_op.drop_column("jev_request_timeout_seconds")
        batch_op.drop_column("jev_http_timeout_seconds")
        batch_op.drop_column("jev_model")
        batch_op.drop_column("jev_api_key")
        batch_op.drop_column("jev_base_url")
