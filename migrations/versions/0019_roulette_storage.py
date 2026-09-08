"""TSK-275 durable PostgreSQL storage for roulette games and results.

The aggregate is intentionally relational.  Runtime rows are mutable until a
terminal proof is projected; result rows are retained and the leaderboard is a
rebuildable projection of completed results.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "0019"
down_revision: str | Sequence[str] | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade(name: str = "") -> None:
    if name:
        return

    op.execute(
        """
        CREATE TABLE komari_roulette_games (
            game_id TEXT PRIMARY KEY,
            app_id TEXT NOT NULL,
            group_openid TEXT NOT NULL,
            lifecycle TEXT NOT NULL DEFAULT 'waiting',
            host_seq INTEGER,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            started_at TIMESTAMPTZ,
            ended_at TIMESTAMPTZ,
            waiting_expires_at TIMESTAMPTZ,
            turn_deadline_at TIMESTAMPTZ,
            state_revision INTEGER NOT NULL DEFAULT 1,
            chamber_revision INTEGER NOT NULL DEFAULT 0,
            turn_seq INTEGER NOT NULL DEFAULT 0,
            current_player_seq INTEGER,
            phase TEXT,
            ordered_chamber TEXT[] NOT NULL DEFAULT '{}'::text[],
            pending_rewards TEXT[] NOT NULL DEFAULT '{}'::text[],
            pending_burst BOOLEAN NOT NULL DEFAULT FALSE,
            pending_locks INTEGER[] NOT NULL DEFAULT '{}'::integer[],
            item_weights JSON NOT NULL DEFAULT '{}'::json,
            next_join_seq INTEGER NOT NULL DEFAULT 1,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT ck_komari_roulette_games_lifecycle
                CHECK (lifecycle IN ('waiting', 'active', 'completed',
                                     'cancelled', 'expired', 'failed')),
            CONSTRAINT ck_komari_roulette_games_revisions_nonnegative
                CHECK (state_revision >= 0 AND chamber_revision >= 0
                       AND turn_seq >= 0),
            CONSTRAINT ck_komari_roulette_games_next_join_seq_positive
                CHECK (next_join_seq > 0),
            CONSTRAINT ck_komari_roulette_games_ordered_chamber
                CHECK (cardinality(ordered_chamber) <= 6
                       AND ordered_chamber <@ ARRAY['live', 'blank']::text[]),
            CONSTRAINT ck_komari_roulette_games_pending_rewards
                CHECK (cardinality(pending_rewards) <= 4
                       AND pending_rewards <@ ARRAY[
                           'magnifier', 'beer', 'burst', 'lock'
                       ]::text[])
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_komari_roulette_games_active_slot
            ON komari_roulette_games (app_id, group_openid)
            WHERE lifecycle IN ('waiting', 'active')
        """
    )
    op.execute(
        """
        CREATE INDEX idx_komari_roulette_games_scope_lifecycle
            ON komari_roulette_games (app_id, group_openid, lifecycle)
        """
    )
    op.execute(
        """
        CREATE TABLE komari_roulette_players (
            game_id TEXT NOT NULL,
            join_seq INTEGER NOT NULL,
            member_openid TEXT NOT NULL,
            display_name TEXT NOT NULL,
            alive BOOLEAN NOT NULL DEFAULT TRUE,
            magnifier_count INTEGER NOT NULL DEFAULT 0,
            beer_count INTEGER NOT NULL DEFAULT 0,
            burst_count INTEGER NOT NULL DEFAULT 0,
            lock_count INTEGER NOT NULL DEFAULT 0,
            eliminated_order INTEGER,
            eliminated_reason TEXT,
            eliminated_at TIMESTAMPTZ,
            CONSTRAINT pk_komari_roulette_players
                PRIMARY KEY (game_id, join_seq),
            CONSTRAINT fk_komari_roulette_players_game
                FOREIGN KEY (game_id)
                REFERENCES komari_roulette_games (game_id)
                ON DELETE CASCADE,
            CONSTRAINT uq_komari_roulette_players_member
                UNIQUE (game_id, member_openid),
            CONSTRAINT ck_komari_roulette_players_inventory_nonnegative
                CHECK (magnifier_count >= 0 AND beer_count >= 0
                       AND burst_count >= 0 AND lock_count >= 0),
            CONSTRAINT ck_komari_roulette_players_inventory_capacity
                CHECK (magnifier_count + beer_count + burst_count + lock_count
                       <= 4),
            CONSTRAINT ck_komari_roulette_players_join_seq_positive
                CHECK (join_seq > 0)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE komari_roulette_results (
            game_id TEXT NOT NULL,
            app_id TEXT NOT NULL,
            group_openid TEXT NOT NULL,
            lifecycle TEXT NOT NULL,
            reason TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL,
            started_at TIMESTAMPTZ,
            ended_at TIMESTAMPTZ NOT NULL,
            terminal_revision INTEGER NOT NULL,
            winner_seq INTEGER,
            winner_member_openid TEXT,
            winner_display_name TEXT,
            CONSTRAINT pk_komari_roulette_results
                PRIMARY KEY (game_id),
            CONSTRAINT fk_komari_roulette_results_game
                FOREIGN KEY (game_id)
                REFERENCES komari_roulette_games (game_id)
                ON DELETE RESTRICT,
            CONSTRAINT uq_komari_roulette_results_scope
                UNIQUE (game_id, app_id, group_openid),
            CONSTRAINT ck_komari_roulette_results_lifecycle
                CHECK (lifecycle IN ('completed', 'cancelled', 'expired',
                                     'failed'))
        )
        """
    )
    op.execute(
        """
        CREATE TABLE komari_roulette_result_players (
            game_id TEXT NOT NULL,
            join_seq INTEGER NOT NULL,
            member_openid TEXT NOT NULL,
            display_name TEXT NOT NULL,
            alive BOOLEAN NOT NULL,
            eliminated_order INTEGER,
            eliminated_reason TEXT,
            eliminated_at TIMESTAMPTZ,
            CONSTRAINT pk_komari_roulette_result_players
                PRIMARY KEY (game_id, join_seq),
            CONSTRAINT fk_komari_roulette_result_players_result
                FOREIGN KEY (game_id)
                REFERENCES komari_roulette_results (game_id)
                ON DELETE RESTRICT,
            CONSTRAINT uq_komari_roulette_result_players_member
                UNIQUE (game_id, member_openid),
            CONSTRAINT ck_komari_roulette_result_players_join_seq_positive
                CHECK (join_seq > 0)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE komari_roulette_leaderboard (
            app_id TEXT NOT NULL,
            group_openid TEXT NOT NULL,
            member_openid TEXT NOT NULL,
            display_name TEXT NOT NULL,
            wins INTEGER NOT NULL DEFAULT 1,
            last_won_at TIMESTAMPTZ NOT NULL,
            CONSTRAINT pk_komari_roulette_leaderboard
                PRIMARY KEY (app_id, group_openid, member_openid),
            CONSTRAINT ck_komari_roulette_leaderboard_wins_positive
                CHECK (wins >= 1)
        )
        """
    )


def downgrade(name: str = "") -> None:
    if name:
        return

    op.execute("DROP TABLE komari_roulette_leaderboard")
    op.execute("DROP TABLE komari_roulette_result_players")
    op.execute("DROP TABLE komari_roulette_results")
    op.execute("DROP TABLE komari_roulette_players")
    op.execute("DROP TABLE komari_roulette_games")
