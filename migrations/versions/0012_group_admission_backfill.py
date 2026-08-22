"""group admission backfill 前置 barrier（ADR-0012 锚点3，forward-only）

统一准入策略由 operator 显式提交到 ``komari_group_admission_config`` 之后，
本 revision 作为 forward-only 门禁把 ``upgrade head`` 收敛到唯一 policy
事实。全新空库经 0010 幂等播种缺省策略后放行；非 fresh 库（已带
pre-admission schema、无策略数据）继续升级时中止在 head 之前，绝不静默
生成缺省策略或合并旧 JSONB 名单。downgrade 明确拒绝回退。
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from alembic import op
from sqlalchemy import text

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy import Connection

revision: str = "0012"
down_revision: str | Sequence[str] | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "komari_group_admission_config"
_MARKER = "0012_TSK232_IS_IRREVERSIBLE"

def _gate(connection: Connection) -> None:
    row = connection.execute(
        text(
            f"SELECT count(*) FROM {_TABLE} WHERE id = 1 "
            "AND policy IS NOT NULL AND jsonb_typeof(policy) = 'object'"
        )
    ).scalar_one_or_none()
    if row is None or int(row) < 1:
        msg = (
            f"{_MARKER}: 群聊准入策略未显式提交，无法继续 head 升级；"
            "绝不静默生成缺省策略或合并旧名单"
        )
        raise RuntimeError(msg)

def upgrade(name: str = "") -> None:
    if name:
        return
    _gate(op.get_bind())

def downgrade(name: str = "") -> None:
    if name:
        return
    msg = f"{_MARKER}: 群聊准入 backfill 为 forward-only barrier，不允许回退"
    raise RuntimeError(msg)
