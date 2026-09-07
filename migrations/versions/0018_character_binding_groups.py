"""按 QQ 应用隔离的群成员角色绑定关系（TSK-271）。

旧的 ``komari_character_bindings`` 表刻意保留为显式 /bind 迁移候选；本
revision 不自动搬运或删除旧全局值。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "0018"
down_revision: str | Sequence[str] | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade(name: str = "") -> None:
    if name:
        return

    op.execute(
        """
        CREATE TABLE komari_character_binding_groups (
            app_id TEXT NOT NULL,
            group_openid TEXT NOT NULL,
            group_id TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (app_id, group_openid),
            CONSTRAINT uq_komari_character_binding_groups_app_group_id
                UNIQUE (app_id, group_id)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE komari_character_binding_members (
            app_id TEXT NOT NULL,
            group_openid TEXT NOT NULL,
            member_openid TEXT NOT NULL,
            member_qq TEXT NOT NULL,
            character_name TEXT,
            character_name_key TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (app_id, group_openid, member_openid),
            CONSTRAINT fk_komari_character_binding_members_group
                FOREIGN KEY (app_id, group_openid)
                REFERENCES komari_character_binding_groups (app_id, group_openid)
                ON DELETE CASCADE,
            CONSTRAINT uq_komari_character_binding_members_qq
                UNIQUE (app_id, group_openid, member_qq),
            CONSTRAINT uq_komari_character_binding_members_name
                UNIQUE (app_id, group_openid, character_name_key)
        )
        """
    )


def downgrade(name: str = "") -> None:
    if name:
        return

    op.execute("DROP TABLE komari_character_binding_members")
    op.execute("DROP TABLE komari_character_binding_groups")
