"""SQLModel metadata for the durable roulette aggregate.

The models in this module only describe the schema.  They deliberately do not
open a database connection, import NoneBot, or execute DDL; ``0019`` remains
the only owner of the physical schema.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import ClassVar

from sqlalchemy import (
    ARRAY,
    JSON,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    PrimaryKeyConstraint,
    Table,
    Text,
    UniqueConstraint,
    text,
)
from sqlmodel import Field, SQLModel


def _utcnow() -> datetime:
    """Provide a value for direct ORM construction in tests."""

    return datetime.now(UTC)


class _RouletteModelBase(SQLModel):
    """Non-table base with declarations that keep SQLModel type checkable."""

    __tablename__: ClassVar[str]  # pyright: ignore[reportIncompatibleVariableOverride]
    __table__: ClassVar[Table]


class RouletteGameRow(_RouletteModelBase, table=True):
    """Mutable game root and its current, consumable runtime state."""

    __tablename__ = "komari_roulette_games"

    game_id: str = Field(sa_column=Column(Text, primary_key=True, nullable=False))
    app_id: str = Field(sa_column=Column(Text, nullable=False))
    group_openid: str = Field(sa_column=Column(Text, nullable=False))
    lifecycle: str = Field(
        default="waiting",
        sa_column=Column(
            Text,
            nullable=False,
            server_default=text("'waiting'"),
        ),
    )
    host_seq: int | None = Field(default=None, sa_column=Column(Integer))
    created_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(
            DateTime(timezone=True),
            nullable=False,
            server_default=text("CURRENT_TIMESTAMP"),
        ),
    )
    started_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True)),
    )
    ended_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True)),
    )
    waiting_expires_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True)),
    )
    turn_deadline_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True)),
    )
    state_revision: int = Field(
        default=1,
        sa_column=Column(Integer, nullable=False, server_default=text("1")),
    )
    chamber_revision: int = Field(
        default=0,
        sa_column=Column(Integer, nullable=False, server_default=text("0")),
    )
    turn_seq: int = Field(
        default=0,
        sa_column=Column(Integer, nullable=False, server_default=text("0")),
    )
    current_player_seq: int | None = Field(
        default=None,
        sa_column=Column(Integer),
    )
    phase: str | None = Field(default=None, sa_column=Column(Text))
    ordered_chamber: list[str] = Field(
        default_factory=list,
        sa_column=Column(
            ARRAY(Text),
            nullable=False,
            server_default=text("'{}'::text[]"),
        ),
    )
    pending_rewards: list[str] = Field(
        default_factory=list,
        sa_column=Column(
            ARRAY(Text),
            nullable=False,
            server_default=text("'{}'::text[]"),
        ),
    )
    pending_burst: bool = Field(
        default=False,
        sa_column=Column(Boolean, nullable=False, server_default=text("false")),
    )
    pending_locks: list[int] = Field(
        default_factory=list,
        sa_column=Column(
            ARRAY(Integer),
            nullable=False,
            server_default=text("'{}'::integer[]"),
        ),
    )
    item_weights: dict[str, int] = Field(
        default_factory=dict,
        sa_column=Column(
            JSON,
            nullable=False,
            server_default=text("'{}'::json"),
        ),
    )
    next_join_seq: int = Field(
        default=1,
        sa_column=Column(Integer, nullable=False, server_default=text("1")),
    )
    updated_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(
            DateTime(timezone=True),
            nullable=False,
            server_default=text("CURRENT_TIMESTAMP"),
        ),
    )

    __table_args__ = (
        CheckConstraint(
            "lifecycle IN ('waiting', 'active', 'completed', 'cancelled', "
            "'expired', 'failed')",
            name="ck_komari_roulette_games_lifecycle",
        ),
        CheckConstraint(
            "state_revision >= 0 AND chamber_revision >= 0 AND turn_seq >= 0",
            name="ck_komari_roulette_games_revisions_nonnegative",
        ),
        CheckConstraint(
            "next_join_seq > 0",
            name="ck_komari_roulette_games_next_join_seq_positive",
        ),
        CheckConstraint(
            "cardinality(ordered_chamber) <= 6 AND ordered_chamber <@ "
            "ARRAY['live', 'blank']::text[]",
            name="ck_komari_roulette_games_ordered_chamber",
        ),
        CheckConstraint(
            "cardinality(pending_rewards) <= 4 AND pending_rewards <@ "
            "ARRAY['magnifier', 'beer', 'burst', 'lock']::text[]",
            name="ck_komari_roulette_games_pending_rewards",
        ),
        Index(
            "uq_komari_roulette_games_active_slot",
            "app_id",
            "group_openid",
            unique=True,
            postgresql_where=text("lifecycle IN ('waiting', 'active')"),
        ),
        Index(
            "idx_komari_roulette_games_scope_lifecycle",
            "app_id",
            "group_openid",
            "lifecycle",
        ),
    )


class RoulettePlayerRow(_RouletteModelBase, table=True):
    """Current frozen seat and its mutable runtime inventory."""

    __tablename__ = "komari_roulette_players"

    game_id: str = Field(
        sa_column=Column(
            Text,
            ForeignKey(
                "komari_roulette_games.game_id",
                ondelete="CASCADE",
                name="fk_komari_roulette_players_game",
            ),
            nullable=False,
        )
    )
    join_seq: int = Field(sa_column=Column(Integer, nullable=False))
    member_openid: str = Field(sa_column=Column(Text, nullable=False))
    display_name: str = Field(sa_column=Column(Text, nullable=False))
    alive: bool = Field(
        default=True,
        sa_column=Column(Boolean, nullable=False, server_default=text("true")),
    )
    magnifier_count: int = Field(
        default=0,
        sa_column=Column(Integer, nullable=False, server_default=text("0")),
    )
    beer_count: int = Field(
        default=0,
        sa_column=Column(Integer, nullable=False, server_default=text("0")),
    )
    burst_count: int = Field(
        default=0,
        sa_column=Column(Integer, nullable=False, server_default=text("0")),
    )
    lock_count: int = Field(
        default=0,
        sa_column=Column(Integer, nullable=False, server_default=text("0")),
    )
    eliminated_order: int | None = Field(
        default=None,
        sa_column=Column(Integer),
    )
    eliminated_reason: str | None = Field(default=None, sa_column=Column(Text))
    eliminated_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True)),
    )

    __table_args__ = (
        PrimaryKeyConstraint("game_id", "join_seq", name="pk_komari_roulette_players"),
        UniqueConstraint(
            "game_id",
            "member_openid",
            name="uq_komari_roulette_players_member",
        ),
        CheckConstraint(
            "magnifier_count >= 0 AND beer_count >= 0 AND burst_count >= 0 "
            "AND lock_count >= 0",
            name="ck_komari_roulette_players_inventory_nonnegative",
        ),
        CheckConstraint(
            "magnifier_count + beer_count + burst_count + lock_count <= 4",
            name="ck_komari_roulette_players_inventory_capacity",
        ),
        CheckConstraint(
            "join_seq > 0",
            name="ck_komari_roulette_players_join_seq_positive",
        ),
    )


class RouletteResultRow(_RouletteModelBase, table=True):
    """Immutable terminal proof header."""

    __tablename__ = "komari_roulette_results"

    game_id: str = Field(
        sa_column=Column(
            Text,
            ForeignKey(
                "komari_roulette_games.game_id",
                ondelete="RESTRICT",
                name="fk_komari_roulette_results_game",
            ),
            primary_key=True,
            nullable=False,
        )
    )
    app_id: str = Field(sa_column=Column(Text, nullable=False))
    group_openid: str = Field(sa_column=Column(Text, nullable=False))
    lifecycle: str = Field(sa_column=Column(Text, nullable=False))
    reason: str = Field(sa_column=Column(Text, nullable=False))
    created_at: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False)
    )
    started_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True)),
    )
    ended_at: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False)
    )
    terminal_revision: int = Field(sa_column=Column(Integer, nullable=False))
    winner_seq: int | None = Field(default=None, sa_column=Column(Integer))
    winner_member_openid: str | None = Field(
        default=None,
        sa_column=Column(Text),
    )
    winner_display_name: str | None = Field(
        default=None,
        sa_column=Column(Text),
    )

    __table_args__ = (
        UniqueConstraint("game_id", name="uq_komari_roulette_results_game"),
        CheckConstraint(
            "lifecycle IN ('completed', 'cancelled', 'expired', 'failed')",
            name="ck_komari_roulette_results_lifecycle",
        ),
    )


class RouletteResultPlayerRow(_RouletteModelBase, table=True):
    """Immutable terminal seat proof."""

    __tablename__ = "komari_roulette_result_players"

    game_id: str = Field(
        sa_column=Column(
            Text,
            ForeignKey(
                "komari_roulette_results.game_id",
                ondelete="CASCADE",
                name="fk_komari_roulette_result_players_result",
            ),
            nullable=False,
        )
    )
    join_seq: int = Field(sa_column=Column(Integer, nullable=False))
    member_openid: str = Field(sa_column=Column(Text, nullable=False))
    display_name: str = Field(sa_column=Column(Text, nullable=False))
    alive: bool = Field(sa_column=Column(Boolean, nullable=False))
    eliminated_order: int | None = Field(
        default=None,
        sa_column=Column(Integer),
    )
    eliminated_reason: str | None = Field(default=None, sa_column=Column(Text))
    eliminated_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True)),
    )

    __table_args__ = (
        PrimaryKeyConstraint(
            "game_id",
            "join_seq",
            name="pk_komari_roulette_result_players",
        ),
        UniqueConstraint(
            "game_id",
            "member_openid",
            name="uq_komari_roulette_result_players_member",
        ),
        CheckConstraint(
            "join_seq > 0",
            name="ck_komari_roulette_result_players_join_seq_positive",
        ),
    )


class RouletteLeaderboardRow(_RouletteModelBase, table=True):
    """Rebuildable per-group wins projection."""

    __tablename__ = "komari_roulette_leaderboard"

    app_id: str = Field(sa_column=Column(Text, nullable=False))
    group_openid: str = Field(sa_column=Column(Text, nullable=False))
    member_openid: str = Field(sa_column=Column(Text, nullable=False))
    display_name: str = Field(sa_column=Column(Text, nullable=False))
    wins: int = Field(
        default=1,
        sa_column=Column(Integer, nullable=False, server_default=text("1")),
    )
    last_won_at: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False)
    )

    __table_args__ = (
        PrimaryKeyConstraint(
            "app_id",
            "group_openid",
            "member_openid",
            name="pk_komari_roulette_leaderboard",
        ),
        CheckConstraint(
            "wins >= 1",
            name="ck_komari_roulette_leaderboard_wins_positive",
        ),
    )


__all__ = [
    "RouletteGameRow",
    "RouletteLeaderboardRow",
    "RoulettePlayerRow",
    "RouletteResultPlayerRow",
    "RouletteResultRow",
]
