"""Rename business capability switches and retire required-dependency gates.

Revision ID: 0035
Revises: 0034

ADR-0018 consolidates the overloaded plugin switches in three ways.

1. Eight business switches keep their stored value and default under a
   capability name (``plugin_enable`` -> ``<capability>_enabled``).
2. The web-capability master switch is merged into the two existing business
   switches *before* the old column is dropped:
   ``search_enabled = search_enabled AND plugin_enable`` and
   ``fetch_enabled = fetch_enabled AND plugin_enable``, so all eight legacy
   boolean combinations keep their effective state and a fresh installation
   starts with both disabled.
3. The three required-dependency gates (System One decision, shared memory,
   user data) and the merged search gate are dropped.  The disable-only
   decision trigger and its function are removed before the decision column,
   because PostgreSQL keeps the trigger body bound to that column.

Business rows, revisions, timestamps, decision logs and unrelated settings are
preserved.  A fresh database applies the same script without touching any row.

Downgrade is refused: the retired columns cannot be reconstructed without
inventing switch values.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NoReturn

from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "0035"
down_revision: str | Sequence[str] | None = "0034"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DOWNGRADE_CODE = "0035_TSK210_IS_IRREVERSIBLE"

_SEARCH_TABLE = "komari_search_config"

#: ``(table, old column, new column)``: stored value and default are unchanged.
_RENAMED_SWITCHES: tuple[tuple[str, str, str], ...] = (
    ("komari_chat_config", "plugin_enable", "chat_enabled"),
    (
        "komari_group_history_summary_config",
        "plugin_enable",
        "summary_enabled",
    ),
    ("komari_knowledge_config", "plugin_enable", "knowledge_enabled"),
    ("komari_help_config", "plugin_enable", "help_enabled"),
    ("komari_custom_config", "plugin_enable", "knowledge_proposals_enabled"),
    ("komari_sentry_config", "plugin_enable", "sentry_enabled"),
    ("komari_sr_config", "plugin_enable", "sr_enabled"),
    ("komari_roulette_config", "plugin_enable", "roulette_enabled"),
)

#: ``(table, column)``: required-dependency gates and the merged search gate.
_RETIRED_SWITCHES: tuple[tuple[str, str], ...] = (
    ("komari_decision_config", "plugin_enable"),
    ("komari_memory_config", "plugin_enable"),
    ("komari_user_data_config", "plugin_enable"),
    (_SEARCH_TABLE, "plugin_enable"),
)


def upgrade(name: str = "") -> None:
    if name:
        return
    # 先合并总开关与两个业务开关，再删除总开关列：顺序颠倒会丢失实际状态。
    op.execute(
        f"UPDATE {_SEARCH_TABLE} SET"
        " search_enabled = search_enabled AND plugin_enable,"
        " fetch_enabled = fetch_enabled AND plugin_enable"
    )
    # 触发器函数体引用 plugin_enable，必须先于删列移除。
    op.execute(
        "DROP TRIGGER komari_decision_disable_fence ON komari_decision_config"
    )
    op.execute("DROP FUNCTION komari_revoke_disabled_decisions()")
    for table, old_column, new_column in _RENAMED_SWITCHES:
        op.execute(
            f"ALTER TABLE {table} RENAME COLUMN {old_column} TO {new_column}"
        )
    for table, column in _RETIRED_SWITCHES:
        op.execute(f"ALTER TABLE {table} DROP COLUMN {column}")


def downgrade(name: str = "") -> None:
    if name:
        return
    _reject_downgrade()


def _reject_downgrade() -> NoReturn:
    msg = (
        f"{_DOWNGRADE_CODE}: ADR-0018 已删除三个必需依赖的停用开关并合并网页"
        "能力总开关，0035 拒绝降级；重建这些列只会制造与当前运行时不匹配的"
        "虚假配置面。请停写当前版本并在新版本上向前修复。"
    )
    raise RuntimeError(msg)
