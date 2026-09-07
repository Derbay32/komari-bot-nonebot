"""PostgreSQL adapter for the roulette aggregate.

Every method accepts the caller's ``AsyncSession`` and leaves transaction
ownership to that caller.  The adapter contains no Redis or process lock
dependency; PostgreSQL constraints, row locks, a scope advisory lock for
projection/rebuild, and revision checks are the correctness boundary.
"""

# The adapter translates stable domain/storage errors with useful context.
# Keep those messages visible instead of applying TRY003 line by line.
# ruff: noqa: TRY003
from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import delete, func, insert, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import DBAPIError, IntegrityError, SQLAlchemyError

from .domain import ChamberKind, GameState, GroupRef, ItemType, PlayerRef, PlayerSeat
from .mapper import (
    EliminationRecord,
    GameSnapshot,
    LeaderboardEntry,
    ResultPlayer,
    RouletteResult,
    StateTransition,
    TerminalProjection,
)
from .orm_models import (
    RouletteGameRow,
    RouletteLeaderboardRow,
    RoulettePlayerRow,
    RouletteResultPlayerRow,
    RouletteResultRow,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.engine import Result
    from sqlalchemy.ext.asyncio import AsyncSession


FAILED_REASONS = frozenset({"aggregate_corrupt", "storage_corrupt"})
TERMINAL_LIFECYCLES = frozenset({"completed", "cancelled", "expired", "failed"})

_G = RouletteGameRow.__table__
_P = RoulettePlayerRow.__table__
_R = RouletteResultRow.__table__
_RP = RouletteResultPlayerRow.__table__
_L = RouletteLeaderboardRow.__table__


class StorageUnavailableError(RuntimeError):
    """The database could not answer; no game fact was changed."""


class AggregateCorruptError(RuntimeError):
    """Rows were read successfully but cannot form a valid aggregate."""


class RevisionConflictError(RuntimeError):
    """A state transition was based on an old aggregate revision."""


class TerminalProjectionRejectedError(RuntimeError):
    """A terminal proof did not match the persisted runtime aggregate."""


class PostgresRouletteStorage:
    """Relational roulette storage with caller-owned transaction scope."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def load_current(
        self,
        group: GroupRef,
        *,
        for_update: bool = False,
    ) -> GameSnapshot | None:
        """Load the waiting/active game in a group, validating its aggregate."""

        if for_update:
            await self._lock_scope(group)
        statement = (
            select(RouletteGameRow, RoulettePlayerRow)
            .outerjoin(
                _P,
                _P.c.game_id == _G.c.game_id,
            )
            .where(
                _G.c.app_id == group.app_id,
                _G.c.group_openid == group.group_openid,
                _G.c.lifecycle.in_(("waiting", "active")),
            )
            .order_by(_G.c.created_at, _P.c.join_seq)
            .execution_options(populate_existing=True)
        )
        if for_update:
            statement = statement.with_for_update(of=RouletteGameRow)
        result = await self._execute(statement)
        pairs = list(result.all())
        roots = list({pair[0].game_id: pair[0] for pair in pairs}.values())
        if len(roots) > 1:
            raise AggregateCorruptError("multiple current games share one group slot")
        if not roots:
            return None
        root = roots[0]
        players = [pair[1] for pair in pairs if pair[1] is not None]
        return self._snapshot_from_rows(root, players)

    async def create_waiting(self, snapshot: GameSnapshot) -> GameSnapshot:
        """Insert a new waiting root and its initial frozen player seat."""

        if snapshot.lifecycle != "waiting" or len(snapshot.players) != 1:
            raise ValueError("a waiting game must start with one player")
        if (
            snapshot.state_revision != 1
            or snapshot.host_seq != snapshot.players[0].join_seq
        ):
            raise ValueError("waiting game must start at revision one")
        if snapshot.players[0].join_seq != 1:
            raise ValueError("initial player must use join sequence one")
        await self._lock_scope(snapshot.group)
        await self._execute(
            insert(RouletteGameRow).values(
                game_id=snapshot.game_id,
                app_id=snapshot.group.app_id,
                group_openid=snapshot.group.group_openid,
                lifecycle="waiting",
                host_seq=snapshot.host_seq,
                created_at=func.current_timestamp(),
                started_at=None,
                ended_at=None,
                waiting_expires_at=snapshot.deadline,
                turn_deadline_at=None,
                chamber_revision=snapshot.chamber_revision,
                state_revision=snapshot.state_revision,
                turn_seq=snapshot.turn_seq,
                current_player_seq=None,
                phase=None,
                ordered_chamber=[],
                pending_rewards=[],
                pending_burst=False,
                pending_locks=[],
                item_weights=_weights_to_json(snapshot.state),
                next_join_seq=snapshot.next_join_seq,
                updated_at=func.current_timestamp(),
            )
        )
        await self._flush()
        await self._insert_players(snapshot.game_id, snapshot.players)
        await self._flush()
        loaded = await self._load_root(snapshot.game_id, for_update=False)
        if loaded is None:
            raise StorageUnavailableError("created game could not be read back")
        players = await self._load_players(snapshot.game_id, for_update=False)
        return self._snapshot_from_rows(loaded, players)

    async def save_transition(
        self,
        transition: StateTransition,
        *,
        expected_revision: int,
    ) -> GameSnapshot:
        """CAS a transition and persist its history delta atomically."""

        before = transition.before
        after = transition.after
        if before.state_revision != expected_revision:
            raise RevisionConflictError("transition expected revision is stale")
        if (
            after.game_id != before.game_id
            or after.state_revision != expected_revision + 1
        ):
            raise RevisionConflictError("transition revision is not monotonic")
        _validate_transition_shape(before, after)
        _validate_eliminations(transition)

        await self._lock_scope(before.group)
        root = await self._load_root(before.game_id, for_update=True)
        if root is None or root.state_revision != expected_revision:
            raise RevisionConflictError("stored game revision is stale")
        if root.lifecycle != before.lifecycle:
            raise RevisionConflictError("stored game lifecycle is stale")
        if (root.app_id, root.group_openid) != (
            before.group.app_id,
            before.group.group_openid,
        ) or after.group != before.group:
            raise RevisionConflictError("transition group does not match stored game")
        existing_players = await self._load_players(before.game_id, for_update=True)
        try:
            _validate_runtime_players(existing_players)
            _validate_runtime_matches_state(existing_players, before.players)
        except (AttributeError, TypeError, ValueError) as exc:
            raise AggregateCorruptError("stored player history is invalid") from exc

        values: dict[str, Any] = {
            "lifecycle": after.lifecycle,
            "host_seq": after.host_seq,
            "state_revision": after.state_revision,
            "chamber_revision": after.chamber_revision,
            "turn_seq": after.turn_seq,
            "current_player_seq": after.current_player_seq,
            "phase": after.phase,
            "ordered_chamber": [item.value for item in after.ordered_chamber],
            "pending_rewards": [item.value for item in after.pending_rewards],
            "pending_burst": after.pending_burst,
            "pending_locks": list(after.pending_locks),
            "item_weights": _weights_to_json(after.state),
            "next_join_seq": after.next_join_seq,
            "updated_at": transition.occurred_at,
        }
        if after.lifecycle == "waiting":
            values["waiting_expires_at"] = after.deadline if after.players else None
            values["turn_deadline_at"] = None
        elif after.lifecycle == "active":
            values["waiting_expires_at"] = None
            values["turn_deadline_at"] = after.deadline
            if before.lifecycle != "active":
                values["started_at"] = transition.occurred_at
        else:
            values["waiting_expires_at"] = None
            values["turn_deadline_at"] = None
        await self._execute(
            update(RouletteGameRow)
            .where(_G.c.game_id == before.game_id)
            .values(**values)
        )
        await self._sync_players(
            before.game_id,
            existing_players,
            after.players,
            preserve_empty_terminal=after.lifecycle
            in {"cancelled", "expired", "failed"}
            and not after.players,
        )
        await self._record_eliminations(
            before.game_id,
            existing_players,
            transition.eliminations,
        )
        await self._flush()
        updated = await self._load_root(before.game_id, for_update=False)
        if updated is None:
            raise StorageUnavailableError("transition disappeared before readback")
        rows = await self._load_players(before.game_id, for_update=False)
        return self._snapshot_from_rows(updated, rows)

    async def project_terminal(self, projection: TerminalProjection) -> None:
        """Write an immutable result, update wins, and clear runtime state."""

        await self._lock_scope(projection.group)
        existing_result = await self._load_result_row(projection.game_id)
        if existing_result is not None:
            if (existing_result.app_id, existing_result.group_openid) != (
                projection.group.app_id,
                projection.group.group_openid,
            ):
                raise TerminalProjectionRejectedError(
                    "terminal projection group does not match immutable result"
                )
            return
        root = await self._load_root(projection.game_id, for_update=True)
        if root is None or (root.app_id, root.group_openid) != (
            projection.group.app_id,
            projection.group.group_openid,
        ):
            raise TerminalProjectionRejectedError(
                "terminal projection has no game root"
            )
        if (
            projection.lifecycle in {"completed", "cancelled", "expired"}
            and root.lifecycle != projection.lifecycle
        ):
            raise TerminalProjectionRejectedError(
                "root is not in the projected terminal state"
            )
        if projection.lifecycle == "failed" and root.lifecycle not in {
            "waiting",
            "active",
            "failed",
        }:
            raise TerminalProjectionRejectedError(
                "failed projection has invalid root state"
            )
        if projection.lifecycle == "failed" and projection.reason not in FAILED_REASONS:
            raise TerminalProjectionRejectedError("failed reason is not whitelisted")

        players = await self._load_players(projection.game_id, for_update=True)
        try:
            _validate_runtime_players(players)
        except (AttributeError, TypeError, ValueError) as exc:
            raise AggregateCorruptError("stored player history is invalid") from exc
        winner = self._validate_winner(projection, players)
        if projection.ended_at < root.created_at:
            raise TerminalProjectionRejectedError(
                "terminal time precedes game creation"
            )
        if root.started_at is not None and projection.ended_at < root.started_at:
            raise TerminalProjectionRejectedError("terminal time precedes game start")

        terminal_revision = (
            root.state_revision + 1
            if projection.lifecycle == "failed"
            else root.state_revision
        )
        result = RouletteResultRow(
            game_id=projection.game_id,
            app_id=root.app_id,
            group_openid=root.group_openid,
            lifecycle=projection.lifecycle,
            reason=projection.reason,
            created_at=root.created_at,
            started_at=root.started_at,
            ended_at=projection.ended_at,
            terminal_revision=terminal_revision,
            winner_seq=winner.join_seq if winner is not None else None,
            winner_member_openid=winner.member_openid if winner is not None else None,
            winner_display_name=winner.display_name if winner is not None else None,
        )
        self._session.add(result)
        await self._flush()
        for player in players:
            self._session.add(
                RouletteResultPlayerRow(
                    game_id=projection.game_id,
                    join_seq=player.join_seq,
                    member_openid=player.member_openid,
                    display_name=player.display_name,
                    alive=player.alive,
                    eliminated_order=player.eliminated_order,
                    eliminated_reason=player.eliminated_reason,
                    eliminated_at=player.eliminated_at,
                )
            )
        await self._flush()
        if winner is not None:
            await self._increment_leaderboard(
                root.app_id,
                root.group_openid,
                winner,
                projection.ended_at,
            )
        await self._execute(
            update(RouletteGameRow)
            .where(_G.c.game_id == projection.game_id)
            .values(
                lifecycle=projection.lifecycle,
                host_seq=None,
                current_player_seq=None,
                phase=None,
                waiting_expires_at=None,
                turn_deadline_at=None,
                ordered_chamber=[],
                pending_rewards=[],
                pending_burst=False,
                pending_locks=[],
                ended_at=projection.ended_at,
                state_revision=terminal_revision,
                updated_at=projection.ended_at,
            )
        )
        await self._execute(
            delete(RoulettePlayerRow).where(_P.c.game_id == projection.game_id)
        )
        await self._flush()

    async def get_result(
        self,
        group: GroupRef,
        game_id: str,
    ) -> RouletteResult | None:
        """Read one immutable result in the requested group scope."""

        result_row = await self._load_result_row(game_id)
        if result_row is None:
            return None
        if (result_row.app_id, result_row.group_openid) != (
            group.app_id,
            group.group_openid,
        ):
            return None
        try:
            _validate_result_header(result_row)
            player_result = await self._execute(
                select(RouletteResultPlayerRow)
                .where(_RP.c.game_id == game_id)
                .order_by(_RP.c.join_seq)
                .execution_options(populate_existing=True)
            )
            result_players = list(player_result.scalars().all())
            _validate_result_players(result_row, result_players)
            players = tuple(
                ResultPlayer(
                    join_seq=row.join_seq,
                    member_openid=row.member_openid,
                    display_name=row.display_name,
                    alive=row.alive,
                    eliminated_order=row.eliminated_order,
                    eliminated_reason=row.eliminated_reason,
                    eliminated_at=row.eliminated_at,
                )
                for row in result_players
            )
            return RouletteResult(
                game_id=result_row.game_id,
                app_id=result_row.app_id,
                group_openid=result_row.group_openid,
                lifecycle=result_row.lifecycle,
                reason=result_row.reason,
                created_at=result_row.created_at,
                started_at=result_row.started_at,
                ended_at=result_row.ended_at,
                terminal_revision=result_row.terminal_revision,
                winner_seq=result_row.winner_seq,
                winner_member_openid=result_row.winner_member_openid,
                winner_display_name=result_row.winner_display_name,
                players=players,
            )
        except AggregateCorruptError:
            raise
        except (AttributeError, TypeError, ValueError, KeyError) as exc:
            raise AggregateCorruptError("persisted terminal result is invalid") from exc

    async def list_leaderboard(self, group: GroupRef) -> tuple[LeaderboardEntry, ...]:
        """Return safe display fields in the stable wins/time order."""

        result = await self._execute(
            select(RouletteLeaderboardRow)
            .where(
                _L.c.app_id == group.app_id,
                _L.c.group_openid == group.group_openid,
            )
            .order_by(
                _L.c.wins.desc(),
                _L.c.last_won_at.asc(),
                _L.c.member_openid.asc(),
            )
            .execution_options(populate_existing=True)
        )
        entries: list[LeaderboardEntry] = []
        try:
            for row in result.scalars().all():
                _validate_leaderboard_row(row)
                entries.append(
                    LeaderboardEntry(
                        display_name=row.display_name,
                        wins=row.wins,
                        last_won_at=row.last_won_at,
                    )
                )
        except (AttributeError, TypeError, ValueError, KeyError) as exc:
            raise AggregateCorruptError("persisted leaderboard row is invalid") from exc
        return tuple(entries)

    async def rebuild_leaderboard(self, group: GroupRef) -> None:
        """Rebuild the projection solely from completed result proofs."""

        await self._lock_scope(group)
        result = await self._execute(
            select(RouletteResultRow)
            .where(
                _R.c.app_id == group.app_id,
                _R.c.group_openid == group.group_openid,
                _R.c.lifecycle == "completed",
            )
            .order_by(_R.c.ended_at.asc(), _R.c.game_id.asc())
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        aggregate: dict[str, tuple[int, str, Any]] = {}
        for row in result.scalars().all():
            try:
                _validate_result_header(row)
                player_result = await self._execute(
                    select(RouletteResultPlayerRow)
                    .where(_RP.c.game_id == row.game_id)
                    .order_by(_RP.c.join_seq)
                    .execution_options(populate_existing=True)
                )
                result_players = list(player_result.scalars().all())
                _validate_result_players(row, result_players)
            except AggregateCorruptError:
                raise
            except (AttributeError, TypeError, ValueError, KeyError) as exc:
                raise AggregateCorruptError(
                    "persisted terminal result is invalid"
                ) from exc
            previous = aggregate.get(row.winner_member_openid)
            if previous is None:
                aggregate[row.winner_member_openid] = (
                    1,
                    row.winner_display_name,
                    row.ended_at,
                )
            else:
                aggregate[row.winner_member_openid] = (
                    previous[0] + 1,
                    row.winner_display_name,
                    row.ended_at,
                )
        await self._execute(
            delete(RouletteLeaderboardRow).where(
                _L.c.app_id == group.app_id,
                _L.c.group_openid == group.group_openid,
            )
        )
        for member_openid, (wins, display_name, last_won_at) in aggregate.items():
            self._session.add(
                RouletteLeaderboardRow(
                    app_id=group.app_id,
                    group_openid=group.group_openid,
                    member_openid=member_openid,
                    display_name=display_name,
                    wins=wins,
                    last_won_at=last_won_at,
                )
            )
        await self._flush()

    async def _load_root(
        self,
        game_id: str,
        *,
        for_update: bool,
    ) -> RouletteGameRow | None:
        statement = (
            select(RouletteGameRow)
            .where(_G.c.game_id == game_id)
            .execution_options(populate_existing=True)
        )
        if for_update:
            statement = statement.with_for_update()
        result = await self._execute(statement)
        return result.scalars().one_or_none()

    async def _load_result_row(self, game_id: str) -> RouletteResultRow | None:
        result = await self._execute(
            select(RouletteResultRow)
            .where(_R.c.game_id == game_id)
            .execution_options(populate_existing=True)
        )
        return result.scalars().one_or_none()

    async def _load_players(
        self,
        game_id: str,
        *,
        for_update: bool,
    ) -> list[RoulettePlayerRow]:
        statement = (
            select(RoulettePlayerRow)
            .where(_P.c.game_id == game_id)
            .order_by(_P.c.join_seq)
            .execution_options(populate_existing=True)
        )
        if for_update:
            statement = statement.with_for_update()
        result = await self._execute(statement)
        return list(result.scalars().all())

    async def _insert_players(
        self,
        game_id: str,
        seats: Sequence[PlayerSeat],
    ) -> None:
        for seat in seats:
            self._session.add(_player_row(game_id, seat))

    async def _sync_players(
        self,
        game_id: str,
        existing: Sequence[RoulettePlayerRow],
        desired: Sequence[PlayerSeat],
        *,
        preserve_empty_terminal: bool,
    ) -> None:
        existing_by_seq = {row.join_seq: row for row in existing}
        desired_by_seq = {seat.join_seq: seat for seat in desired}
        if not preserve_empty_terminal:
            for seq in set(existing_by_seq) - set(desired_by_seq):
                await self._execute(
                    delete(RoulettePlayerRow).where(
                        _P.c.game_id == game_id,
                        _P.c.join_seq == seq,
                    )
                )
        for seq, seat in desired_by_seq.items():
            row = existing_by_seq.get(seq)
            values = _player_values(seat)
            if row is None:
                self._session.add(_player_row(game_id, seat))
            else:
                await self._execute(
                    update(RoulettePlayerRow)
                    .where(
                        _P.c.game_id == game_id,
                        _P.c.join_seq == seq,
                    )
                    .values(**values)
                )

    async def _record_eliminations(
        self,
        game_id: str,
        existing: Sequence[RoulettePlayerRow],
        eliminations: Sequence[EliminationRecord],
    ) -> None:
        if not eliminations:
            return
        next_order = max(
            (row.eliminated_order or 0 for row in existing),
            default=0,
        )
        for elimination in eliminations:
            next_order += 1
            await self._execute(
                update(RoulettePlayerRow)
                .where(
                    _P.c.game_id == game_id,
                    _P.c.join_seq == elimination.join_seq,
                )
                .values(
                    alive=False,
                    eliminated_order=next_order,
                    eliminated_reason=elimination.reason,
                    eliminated_at=elimination.occurred_at,
                )
            )

    async def _increment_leaderboard(
        self,
        app_id: str,
        group_openid: str,
        winner: RoulettePlayerRow,
        ended_at: Any,
    ) -> None:
        statement = pg_insert(RouletteLeaderboardRow).values(
            app_id=app_id,
            group_openid=group_openid,
            member_openid=winner.member_openid,
            display_name=winner.display_name,
            wins=1,
            last_won_at=ended_at,
        )
        statement = statement.on_conflict_do_update(
            index_elements=[
                _L.c.app_id,
                _L.c.group_openid,
                _L.c.member_openid,
            ],
            set_={
                "wins": _L.c.wins + 1,
                "display_name": winner.display_name,
                "last_won_at": ended_at,
            },
        )
        await self._execute(statement)

    async def _lock_scope(self, group: GroupRef) -> None:
        await self._execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:scope_key, 0))"),
            {"scope_key": f"komari-roulette:{group.app_id}:{group.group_openid}"},
        )

    def _snapshot_from_rows(
        self,
        root: RouletteGameRow,
        players: Sequence[RoulettePlayerRow],
    ) -> GameSnapshot:
        if root.lifecycle == "waiting" and not players:
            raise AggregateCorruptError("waiting game has no runtime players")
        try:
            _validate_runtime_players(players)
            state = _state_from_rows(root, players)
            return GameSnapshot(
                game_id=root.game_id,
                state=state,
                created_at=root.created_at,
                started_at=root.started_at,
                ended_at=root.ended_at,
                waiting_expires_at=root.waiting_expires_at,
                turn_deadline_at=root.turn_deadline_at,
            )
        except (AttributeError, TypeError, ValueError, KeyError) as exc:
            raise AggregateCorruptError(
                "persisted roulette aggregate is invalid"
            ) from exc

    async def _execute(
        self, statement: Any, params: dict[str, Any] | None = None
    ) -> Result[Any]:
        try:
            return await self._session.execute(statement, params or {})
        except IntegrityError:
            raise
        except (
            DBAPIError,
            SQLAlchemyError,
            ConnectionError,
            OSError,
            TimeoutError,
        ) as exc:
            raise StorageUnavailableError("roulette storage is unavailable") from exc

    async def _flush(self) -> None:
        try:
            await self._session.flush()
        except IntegrityError:
            raise
        except (
            DBAPIError,
            SQLAlchemyError,
            ConnectionError,
            OSError,
            TimeoutError,
        ) as exc:
            raise StorageUnavailableError("roulette storage is unavailable") from exc

    @staticmethod
    def _validate_winner(
        projection: TerminalProjection,
        players: Sequence[RoulettePlayerRow],
    ) -> RoulettePlayerRow | None:
        alive = [row for row in players if row.alive]
        if projection.lifecycle == "completed":
            if len(alive) != 1 or projection.winner_seq != alive[0].join_seq:
                raise TerminalProjectionRejectedError(
                    "completed projection does not prove one winner"
                )
            return alive[0]
        if projection.winner_seq is not None:
            raise TerminalProjectionRejectedError(
                "non-completed projection has a winner"
            )
        return None


def _weights_to_json(state: GameState) -> dict[str, int]:
    return {item.value: value for item, value in state.item_weights.items()}


def _validate_transition_shape(before: GameSnapshot, after: GameSnapshot) -> None:
    """Reject a caller transition that rewrites frozen roster facts."""

    if after.group != before.group:
        raise RevisionConflictError("transition group identity changed")
    if after.next_join_seq < before.next_join_seq:
        raise RevisionConflictError("transition rewound the next join sequence")
    before_by_seq = {seat.join_seq: seat for seat in before.players}
    after_by_seq = {seat.join_seq: seat for seat in after.players}
    for sequence, before_seat in before_by_seq.items():
        after_seat = after_by_seq.get(sequence)
        if after_seat is None:
            continue
        if (
            after_seat.member_openid != before_seat.member_openid
            or after_seat.display_name != before_seat.display_name
        ):
            raise RevisionConflictError("transition rewrote a frozen player")
        if before_seat.alive and not after_seat.alive:
            continue
        if not before_seat.alive and after_seat.alive:
            raise RevisionConflictError("transition resurrected an eliminated player")
    if (
        before.lifecycle == "active"
        and after.lifecycle in {"active", "completed"}
        and set(before_by_seq) != set(after_by_seq)
    ):
        raise RevisionConflictError("active transition changed the roster")
    if before.lifecycle == "waiting" and after.lifecycle == "waiting":
        new_sequences = set(after_by_seq) - set(before_by_seq)
        if any(sequence < before.next_join_seq for sequence in new_sequences):
            raise RevisionConflictError("transition reused a join sequence")


def _validate_eliminations(transition: StateTransition) -> None:
    before_by_seq = {seat.join_seq: seat for seat in transition.before.players}
    after_by_seq = {seat.join_seq: seat for seat in transition.after.players}
    seen: set[int] = set()
    for elimination in transition.eliminations:
        if elimination.join_seq in seen:
            raise RevisionConflictError("transition repeated an elimination")
        before_seat = before_by_seq.get(elimination.join_seq)
        after_seat = after_by_seq.get(elimination.join_seq)
        if (
            before_seat is None
            or not before_seat.alive
            or after_seat is None
            or after_seat.alive
        ):
            raise RevisionConflictError("transition elimination does not match state")
        seen.add(elimination.join_seq)


def _validate_runtime_players(players: Sequence[RoulettePlayerRow]) -> None:
    """Validate row-local history before reconstructing the current state."""

    seen_sequences: set[int] = set()
    seen_members: set[str] = set()
    seen_names: set[str] = set()
    elimination_orders: set[int] = set()
    for row in players:
        if type(row.join_seq) is not int or row.join_seq <= 0:
            raise ValueError("player join sequence is invalid")
        if row.join_seq in seen_sequences:
            raise ValueError("player join sequence is duplicated")
        seen_sequences.add(row.join_seq)
        if (
            type(row.member_openid) is not str
            or not row.member_openid.strip()
            or row.member_openid in seen_members
        ):
            raise ValueError("player identity is invalid or duplicated")
        seen_members.add(row.member_openid)
        if (
            type(row.display_name) is not str
            or not row.display_name.strip()
            or row.display_name != row.display_name.strip()
            or row.display_name in seen_names
        ):
            raise ValueError("player display name is invalid or duplicated")
        seen_names.add(row.display_name)
        if type(row.alive) is not bool:
            raise TypeError("player alive flag is invalid")
        counts = (
            row.magnifier_count,
            row.beer_count,
            row.burst_count,
            row.lock_count,
        )
        if any(type(count) is not int or count < 0 for count in counts):
            raise ValueError("player inventory is invalid")
        if sum(counts) > 4:
            raise ValueError("player inventory exceeds capacity")
        if row.alive:
            if (
                row.eliminated_order is not None
                or row.eliminated_reason is not None
                or row.eliminated_at is not None
            ):
                raise ValueError("alive player has elimination history")
            continue
        if (
            type(row.eliminated_order) is not int
            or row.eliminated_order <= 0
            or row.eliminated_order in elimination_orders
            or type(row.eliminated_reason) is not str
            or not row.eliminated_reason.strip()
        ):
            raise ValueError("eliminated player history is invalid")
        _validate_db_timestamp(row.eliminated_at)
        if row.eliminated_at is None:
            raise ValueError("eliminated player has no timestamp")
        elimination_orders.add(row.eliminated_order)
    if elimination_orders and elimination_orders != set(
        range(1, len(elimination_orders) + 1)
    ):
        raise ValueError("elimination order has a gap")


def _validate_runtime_matches_state(
    rows: Sequence[RoulettePlayerRow],
    seats: Sequence[PlayerSeat],
) -> None:
    if len(rows) != len(seats):
        raise ValueError("stored player count does not match transition base")
    for row, seat in zip(rows, seats, strict=True):
        expected = (
            seat.inventory.get(ItemType.MAGNIFIER, 0),
            seat.inventory.get(ItemType.BEER, 0),
            seat.inventory.get(ItemType.BURST, 0),
            seat.inventory.get(ItemType.LOCK, 0),
        )
        actual = (
            row.magnifier_count,
            row.beer_count,
            row.burst_count,
            row.lock_count,
        )
        if (
            row.join_seq != seat.join_seq
            or row.member_openid != seat.member_openid
            or row.display_name != seat.display_name
            or row.alive != seat.alive
            or actual != expected
        ):
            raise ValueError("stored player row does not match transition base")


def _validate_result_header(row: RouletteResultRow) -> None:
    if (
        type(row.game_id) is not str
        or not row.game_id.strip()
        or type(row.app_id) is not str
        or not row.app_id.strip()
        or type(row.group_openid) is not str
        or not row.group_openid.strip()
        or row.lifecycle not in TERMINAL_LIFECYCLES
        or type(row.reason) is not str
        or not row.reason.strip()
        or type(row.terminal_revision) is not int
        or row.terminal_revision <= 0
    ):
        raise ValueError("terminal result header is invalid")
    if row.created_at is None or row.ended_at is None:
        raise ValueError("terminal result has missing timestamps")
    _validate_db_timestamp(row.created_at)
    _validate_db_timestamp(row.started_at)
    _validate_db_timestamp(row.ended_at)
    if row.started_at is not None and row.started_at < row.created_at:
        raise ValueError("result start precedes creation")
    if row.ended_at < row.created_at or (
        row.started_at is not None and row.ended_at < row.started_at
    ):
        raise ValueError("result end precedes its timestamps")
    if row.lifecycle == "completed":
        if (
            type(row.winner_seq) is not int
            or row.winner_seq <= 0
            or type(row.winner_member_openid) is not str
            or not row.winner_member_openid.strip()
            or type(row.winner_display_name) is not str
            or not row.winner_display_name.strip()
        ):
            raise ValueError("completed result has no complete winner proof")
    elif row.lifecycle == "failed" and row.reason not in FAILED_REASONS:
        raise ValueError("failed result reason is not whitelisted")
    elif any(
        value is not None
        for value in (
            row.winner_seq,
            row.winner_member_openid,
            row.winner_display_name,
        )
    ):
        raise ValueError("non-completed result has a winner proof")


def _validate_leaderboard_row(row: RouletteLeaderboardRow) -> None:
    if (
        type(row.app_id) is not str
        or not row.app_id.strip()
        or type(row.group_openid) is not str
        or not row.group_openid.strip()
        or type(row.member_openid) is not str
        or not row.member_openid.strip()
        or type(row.display_name) is not str
        or not row.display_name.strip()
        or row.display_name != row.display_name.strip()
        or type(row.wins) is not int
        or row.wins < 1
    ):
        raise ValueError("leaderboard row is invalid")
    _validate_db_timestamp(row.last_won_at)


def _validate_result_players(
    result: RouletteResultRow,
    players: Sequence[RouletteResultPlayerRow],
) -> None:
    if not players:
        raise ValueError("terminal result has no result players")
    seen_sequences: set[int] = set()
    seen_members: set[str] = set()
    seen_names: set[str] = set()
    elimination_orders: set[int] = set()
    for row in players:
        if row.game_id != result.game_id:
            raise ValueError("result player references a different game")
        if (
            type(row.join_seq) is not int
            or row.join_seq <= 0
            or row.join_seq in seen_sequences
        ):
            raise ValueError("result player sequence is invalid")
        seen_sequences.add(row.join_seq)
        if (
            type(row.member_openid) is not str
            or not row.member_openid.strip()
            or row.member_openid in seen_members
            or type(row.display_name) is not str
            or not row.display_name.strip()
            or row.display_name != row.display_name.strip()
            or row.display_name in seen_names
        ):
            raise ValueError("result player identity is invalid")
        seen_members.add(row.member_openid)
        seen_names.add(row.display_name)
        if type(row.alive) is not bool:
            raise TypeError("result player alive flag is invalid")
        if row.alive:
            if any(
                value is not None
                for value in (
                    row.eliminated_order,
                    row.eliminated_reason,
                    row.eliminated_at,
                )
            ):
                raise ValueError("alive result player has elimination history")
            continue
        if (
            type(row.eliminated_order) is not int
            or row.eliminated_order <= 0
            or row.eliminated_order in elimination_orders
            or type(row.eliminated_reason) is not str
            or not row.eliminated_reason.strip()
        ):
            raise ValueError("result player elimination history is invalid")
        _validate_db_timestamp(row.eliminated_at)
        if row.eliminated_at is None:
            raise ValueError("result player has no elimination timestamp")
        elimination_orders.add(row.eliminated_order)
    if elimination_orders and elimination_orders != set(
        range(1, len(elimination_orders) + 1)
    ):
        raise ValueError("result player elimination order has a gap")
    if result.lifecycle == "completed":
        alive = [row for row in players if row.alive]
        if len(alive) != 1:
            raise ValueError("completed result has multiple or no winners")
        winner = alive[0]
        if (
            result.winner_seq != winner.join_seq
            or result.winner_member_openid != winner.member_openid
            or result.winner_display_name != winner.display_name
        ):
            raise ValueError("completed result winner does not match seats")


def _validate_db_timestamp(value: object) -> None:
    if value is None:
        return
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError("database timestamps must be timezone-aware")


def _state_from_rows(
    root: RouletteGameRow,
    players: Sequence[RoulettePlayerRow],
) -> GameState:
    runtime_players = tuple(
        PlayerSeat(
            player=PlayerRef(
                app_id=root.app_id,
                group_openid=root.group_openid,
                member_openid=row.member_openid,
                display_name=row.display_name,
            ),
            join_seq=row.join_seq,
            alive=row.alive,
            inventory={
                item: count
                for item, count in {
                    ItemType.MAGNIFIER: row.magnifier_count,
                    ItemType.BEER: row.beer_count,
                    ItemType.BURST: row.burst_count,
                    ItemType.LOCK: row.lock_count,
                }.items()
                if count > 0
            },
        )
        for row in players
    )
    weights = _weights_from_db(root.item_weights)
    deadline: datetime | None = None
    if root.lifecycle == "waiting":
        deadline = root.waiting_expires_at
    elif root.lifecycle == "active":
        deadline = root.turn_deadline_at
    return GameState(
        group=GroupRef(root.app_id, root.group_openid),
        lifecycle=root.lifecycle,
        phase=root.phase,
        state_revision=root.state_revision,
        chamber_revision=root.chamber_revision,
        turn_seq=root.turn_seq,
        current_player_seq=root.current_player_seq,
        deadline=deadline,
        host_seq=root.host_seq,
        players=runtime_players,
        ordered_chamber=tuple(ChamberKind(value) for value in root.ordered_chamber),
        pending_rewards=tuple(ItemType(value) for value in root.pending_rewards),
        pending_burst=root.pending_burst,
        pending_locks=tuple(root.pending_locks),
        item_weights=weights,
        next_join_seq=root.next_join_seq,
    )


def _player_row(game_id: str, seat: PlayerSeat) -> RoulettePlayerRow:
    return RoulettePlayerRow(game_id=game_id, **_player_values(seat))


def _player_values(seat: PlayerSeat) -> dict[str, Any]:
    return {
        "join_seq": seat.join_seq,
        "member_openid": seat.member_openid,
        "display_name": seat.display_name,
        "alive": seat.alive,
        "magnifier_count": seat.inventory.get(ItemType.MAGNIFIER, 0),
        "beer_count": seat.inventory.get(ItemType.BEER, 0),
        "burst_count": seat.inventory.get(ItemType.BURST, 0),
        "lock_count": seat.inventory.get(ItemType.LOCK, 0),
    }


def _weights_from_db(raw: object) -> dict[ItemType, int]:
    """Decode JSON weights without coercing malformed values into valid ones."""

    if not isinstance(raw, dict):
        raise TypeError("item weights are not an object")
    expected = {item.value for item in ItemType}
    if set(raw) != expected:
        raise ValueError("item weights have an unexpected key set")
    decoded: dict[ItemType, int] = {}
    for key, value in raw.items():
        if type(key) is not str or type(value) is not int or value < 0:
            raise ValueError("item weight has an invalid type or value")
        decoded[ItemType(key)] = value
    if sum(decoded.values()) <= 0:
        raise ValueError("item weights must have a positive total")
    return decoded


__all__ = [
    "AggregateCorruptError",
    "PostgresRouletteStorage",
    "RevisionConflictError",
    "StorageUnavailableError",
    "TerminalProjectionRejectedError",
]
