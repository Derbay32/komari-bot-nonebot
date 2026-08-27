"""聊天 Prompt 行为字段迁移：删除 output_instruction 并新增独立行为列

迁移 ID: 0009
父迁移: 0011

TSK-188 决策 26/36 与 TSK-190 验收标准 3：聊天 Prompt 的最终输出协议
（``output_instruction``）与“最终回复提交协议”冲突，本 revision 在
``komari_prompt_komari_chat`` 上完成删除与拆分：

1. 显式删除 ``output_instruction`` 列。被删除的自定义内容永久丢弃，
   不并入任何新字段（TSK-190 契约：downgrade 明确无法恢复，不承诺还原）。
2. 新增 7 个互相独立的行为列：``tool_call_instruction`` /
   ``image_read_instruction``（TSK-188 决策 26 精确点名）与画像读取、
   联网搜索、网页抓取、委托图片理解、视觉描述各自的行为字段
   （``profile_read_instruction`` / ``search_web_instruction`` /
   ``fetch_page_instruction`` / ``delegated_vision_instruction`` /
   ``vision_description_prompt``）。

新列先以 ``DEFAULT ''`` 补齐存量行再移除默认值，保证旧库单行表在
0011 → head 升级时不为空行失败，最终 DDL 与强类型 Schema 零漂移。
新列正文由统一版本化初始数据（``seed_bootstrap``）写入——本迁移只
建列，不承载 Prompt 内容。

本 revision 自包含（不导入 komari_bot），不创建双读、双写、兼容别名
或运行时 fallback。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence


revision: str = "0009"
down_revision: str | Sequence[str] | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "komari_prompt_komari_chat"

_NEW_BEHAVIOR_COLUMNS: tuple[str, ...] = (
    "tool_call_instruction",
    "image_read_instruction",
    "profile_read_instruction",
    "search_web_instruction",
    "fetch_page_instruction",
    "delegated_vision_instruction",
    "vision_description_prompt",
)


def upgrade(name: str = "") -> None:
    if name:
        return

    # 显式删除旧列：被删除的自定义内容不并入任何新字段
    op.execute(
        f"ALTER TABLE {_TABLE} DROP COLUMN output_instruction"
    )
    # 新增独立行为列：先以空字符串默认补齐存量行，再移除默认值，
    # 使列定义与强类型 Schema 完全一致（零漂移）。
    for column in _NEW_BEHAVIOR_COLUMNS:
        op.execute(
            f"ALTER TABLE {_TABLE} "
            f"ADD COLUMN {column} TEXT NOT NULL DEFAULT ''"
        )
    for column in _NEW_BEHAVIOR_COLUMNS:
        op.execute(f"ALTER TABLE {_TABLE} ALTER COLUMN {column} DROP DEFAULT")


def downgrade(name: str = "") -> None:
    if name:
        return

    # 0012 为不可逆 contract：output_instruction 旧列已删除，自定义内容
    # 已按 TSK-190 契约永久丢弃，无法承诺还原；新行为列正文由 seed 拥有，
    # 回退需要重建旧列并重跑 seed 流程，明确拒绝在此处提供。
    msg = (
        "0009_TSK232_IS_IRREVERSIBLE: "
        "output_instruction 列已删除且自定义内容永久丢弃，"
        "不允许回退"
    )
    raise RuntimeError(msg)
