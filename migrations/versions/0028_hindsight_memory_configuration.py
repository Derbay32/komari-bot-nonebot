"""Hindsight shared memory configuration (TSK-193).

Revision ID: 0028
Revises: 0027

Adds the caller-side Hindsight connection settings to the existing
``komari_memory_config`` singleton.  Every column is NOT NULL with the exact
schema default so populated databases upgrade without a data rewrite.  The
migration does not touch formal-memory tables, prompts or any remote service.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "0028"
down_revision: str | Sequence[str] | None = "0027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "komari_memory_config"

#: New columns: (name, SQL type, SQL default literal).
_HINDSIGHT_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("hindsight_base_url", "VARCHAR", "''"),
    ("hindsight_api_key", "VARCHAR", "''"),
    ("hindsight_bank_id", "VARCHAR", "''"),
    ("hindsight_request_timeout_seconds", "FLOAT", "10.0"),
    ("hindsight_recovery_interval_seconds", "FLOAT", "30.0"),
)


def upgrade(name: str = "") -> None:
    if name:
        return

    for column, column_type, default in _HINDSIGHT_COLUMNS:
        op.execute(
            f"ALTER TABLE {_TABLE} ADD COLUMN {column} "
            f"{column_type} NOT NULL DEFAULT {default}"
        )


def downgrade(name: str = "") -> None:
    if name:
        return

    for column, _column_type, _default in reversed(_HINDSIGHT_COLUMNS):
        op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN {column}")
