"""Reply fulfillment Hindsight memory commitment (TSK-195).

Revision ID: 0029
Revises: 0028

The delivered-memory commitment replaces the retired interaction-history
commitment. Old event/result/emotion payloads are never read at runtime and
are never converted into the new serialized request, so this revision fails
closed while any old parent is still unfinished:

- ``NOT_STARTED`` and ``PENDING_CONFIRMATION`` may still produce a platform
  send or carry an unknown delivery result;
- ``DELIVERED`` with ``completed_at IS NULL`` may still own unexecuted
  commitments, including a legacy interaction-history payload.

The check runs under transaction-scoped table locks so a concurrent storage
transition cannot pass between validation and the constraint change. A hit
raises a fixed actionable message and modifies no source row. Terminal
history keeps its literal ``interaction_history`` rows with ``payload NULL``
as historical facts; the new constraint permits that literal only without a
payload and never registers it as an active domain or operator-resume type.
Downgrade is refused rather than reviving the retired output protocol.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NoReturn

from alembic import op
from sqlalchemy import text

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy import Connection

revision: str = "0029"
down_revision: str | Sequence[str] | None = "0028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PARENT_TABLE = "komari_chat_reply_fulfillments"
_CHILD_TABLE = "komari_chat_reply_fulfillment_commitments"
_UNFINISHED_CODE = "TSK195_UNFINISHED_REPLY_FULFILLMENT"
_DOWNGRADE_CODE = "0029_TSK195_IS_IRREVERSIBLE"

_ACTIVE_COMMITMENT_TYPES = (
    "proactive_reply_confirmation",
    "favorability_adjustment",
    "assistant_reply_history",
    "hindsight_memory",
)
_HISTORICAL_COMMITMENT_TYPE = "interaction_history"


def _lock_fulfillment_tables(connection: Connection) -> None:
    """Block parent/child writes for the rest of this migration transaction."""
    connection.execute(
        text(f"LOCK TABLE {_PARENT_TABLE}, {_CHILD_TABLE} IN ACCESS EXCLUSIVE MODE")
    )


def _require_no_unfinished_fulfillment(connection: Connection) -> None:
    """Fail closed on every old state that may still own a live obligation."""
    row = connection.execute(
        text(
            f"SELECT fulfillment_id "
            f"FROM {_PARENT_TABLE} "
            "WHERE delivery_state IN ('NOT_STARTED', 'PENDING_CONFIRMATION') "
            "   OR (delivery_state = 'DELIVERED' AND completed_at IS NULL) "
            "ORDER BY fulfillment_id "
            "LIMIT 1 "
            "FOR UPDATE"
        )
    ).first()
    if row is None:
        return
    # Fixed message only: no fulfillment ID, group, user or content is emitted.
    msg = (
        f"{_UNFINISHED_CODE}: 回复履约存在未完成父记录（NOT_STARTED / "
        "PENDING_CONFIRMATION / DELIVERED 且 completed_at 为空），0029 "
        "拒绝切换送达记忆协议。请先停止新事件、完成已送达义务、经既有"
        "授权运维对账未知送达，并确认无未完成父记录后重试；本迁移未修改"
        "任何源行。"
    )
    raise RuntimeError(msg)


def _commitment_type_constraint(connection: Connection) -> str:
    """Locate the 0005-era commitment-type CHECK by its definition."""
    name = connection.execute(
        text(
            "SELECT con.conname "
            "FROM pg_constraint AS con "
            "JOIN pg_class AS rel ON rel.oid = con.conrelid "
            "WHERE rel.relname = :table "
            "  AND con.contype = 'c' "
            "  AND pg_get_constraintdef(con.oid) "
            "      LIKE '%interaction_history%' "
            "ORDER BY con.conname "
            "LIMIT 1"
        ),
        {"table": _CHILD_TABLE},
    ).scalar_one_or_none()
    if not isinstance(name, str):
        msg = "0029 未找到既有承诺类型约束，拒绝在未知 schema 上继续"
        raise TypeError(msg)
    return name


def _replace_commitment_type_constraint(connection: Connection) -> None:
    """Allow the new active type and keep history only without a payload."""
    existing = _commitment_type_constraint(connection)
    quoted = '"' + existing.replace('"', '""') + '"'
    connection.execute(text(f"ALTER TABLE {_CHILD_TABLE} DROP CONSTRAINT {quoted}"))
    active = ", ".join(f"'{value}'" for value in _ACTIVE_COMMITMENT_TYPES)
    historical = f"'{_HISTORICAL_COMMITMENT_TYPE}'"
    connection.execute(
        text(
            f"ALTER TABLE {_CHILD_TABLE} "
            "ADD CONSTRAINT ck_reply_fulfillment_commitment_type CHECK ("
            f"commitment_type IN ({active}, {historical})"
            ")"
        )
    )
    connection.execute(
        text(
            f"ALTER TABLE {_CHILD_TABLE} "
            "ADD CONSTRAINT ck_reply_fulfillment_historical_payload CHECK ("
            f"commitment_type <> {historical} OR payload IS NULL"
            ")"
        )
    )


def _refuse_downgrade() -> NoReturn:
    msg = (
        f"{_DOWNGRADE_CODE}: 0029 已切换为 Hindsight 送达记忆承诺并拒绝"
        "恢复旧互动历史输出协议；旧历史行只作为无载荷历史事实保留，"
        "不提供回退别名、旧运行时读取器或兼容双写"
    )
    raise RuntimeError(msg)


def upgrade(name: str = "") -> None:
    if name:
        return

    connection = op.get_bind()
    _lock_fulfillment_tables(connection)
    _require_no_unfinished_fulfillment(connection)
    _replace_commitment_type_constraint(connection)


def downgrade(name: str = "") -> None:
    if name:
        return

    _refuse_downgrade()
