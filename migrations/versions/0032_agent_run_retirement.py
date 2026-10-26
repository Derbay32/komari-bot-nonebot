"""Retire the Agent Run logger storage (TSK-200).

Revision ID: 0032
Revises: 0031

The ``agent_run_logger`` plugin is deleted completely (JSONL storage,
PostgreSQL UNLOGGED index, index reconciliation/retention jobs, query API and
managed config resource).  This revision retires the two tables it owned:

- ``komari_agent_run_log_index`` (UNLOGGED, metadata-less: autogenerate can
  never see it, so the drop is handwritten);
- ``komari_agent_run_logger_config`` (typed config singleton; its SQLModel is
  deleted in the same change, keeping ``alembic check`` drift-free).

Persisted management credentials may still carry the retired
``agent_run_logs:*`` permission scopes.  They are filtered out here while
every other permission and credential value is preserved exactly; credentials
that would be left without any permission are removed rather than broadened to
``*`` or left as an invalid permission list.  The config revision/timestamp
advance only when filtering actually changed the persisted value.  Legacy
``llm_logs`` strings are intentionally preserved (no startup data cleanup).

Downgrade is refused: recreating storage for a deleted plugin would fabricate
an index/config surface with no producer or reader.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, NoReturn

from alembic import op
from sqlalchemy import text

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy import Connection

revision: str = "0032"
down_revision: str | Sequence[str] | None = "0031"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CONFIG_TABLE = "komari_agent_run_logger_config"
_INDEX_TABLE = "komari_agent_run_log_index"
_MANAGEMENT_TABLE = "komari_management_config"
_RETIRED_PERMISSION_PREFIX = "agent_run_logs:"
_DOWNGRADE_CODE = "0032_TSK200_IS_IRREVERSIBLE"


def upgrade(name: str = "") -> None:
    if name:
        return
    _drop_retired_tables()
    _filter_retired_management_permissions()


def _drop_retired_tables() -> None:
    """Drop exactly the two retired Agent Run storage tables."""
    op.execute('DROP TABLE "komari_agent_run_log_index"')
    op.execute('DROP TABLE "komari_agent_run_logger_config"')


def _decode_json(value: object) -> object:
    """Decode JSONB exactly once (asyncpg text codec or decoded object)."""
    if isinstance(value, str):
        return json.loads(value)
    return value


def _is_retired_permission(value: object) -> bool:
    return isinstance(value, str) and value.startswith(_RETIRED_PERMISSION_PREFIX)


def _filter_credentials(raw: object) -> list[Any] | None:
    """Filter retired scopes; ``None`` means "not a credential list, keep as is"."""
    credentials = _decode_json(raw)
    if not isinstance(credentials, list):
        return None
    filtered: list[Any] = []
    for credential in credentials:
        if not isinstance(credential, dict):
            filtered.append(credential)
            continue
        permissions = credential.get("permissions")
        if not isinstance(permissions, list):
            filtered.append(credential)
            continue
        kept = [
            permission
            for permission in permissions
            if not _is_retired_permission(permission)
        ]
        if not kept:
            # No residual permission: remove the credential instead of
            # broadening it to '*' or persisting an invalid empty list.
            continue
        updated = dict(credential)
        updated["permissions"] = kept
        filtered.append(updated)
    return filtered


def _filter_retired_management_permissions() -> None:
    """Filter retired scopes from persisted credentials without touching others."""
    connection: Connection = op.get_bind()
    row = connection.execute(
        text(f"SELECT api_credentials FROM {_MANAGEMENT_TABLE} WHERE id = 1 FOR UPDATE")
    ).first()
    if row is None:
        return
    original = row[0]
    filtered = _filter_credentials(original)
    if filtered is None or filtered == _decode_json(original):
        return
    connection.execute(
        text(
            f"UPDATE {_MANAGEMENT_TABLE} "
            "SET api_credentials = CAST(:credentials AS jsonb), "
            "revision = revision + 1, updated_at = now() "
            "WHERE id = 1"
        ),
        {"credentials": json.dumps(filtered, ensure_ascii=False)},
    )


def downgrade(name: str = "") -> None:
    if name:
        return
    _reject_downgrade()


def _reject_downgrade() -> NoReturn:
    msg = (
        f"{_DOWNGRADE_CODE}: TSK-200 已完整退役 Agent Run 日志插件的存储与查询"
        "能力，0032 拒绝降级；重建一个没有生产者和读取方的索引/配置面只会"
        "制造虚假存储。请停写当前版本并在新版本上向前修复。"
    )
    raise RuntimeError(msg)
