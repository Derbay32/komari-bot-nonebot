"""TSK-279 Stage-B recovery scan and retention cleanup workers.

Both workers drive the **existing** deep entry points - they never open a
second transaction boundary, never create a receipt, never touch a fulfillment
and never send anything to QQ.  That is deliberate: a background worker has no
legal inbound message id to answer, so the only legal effect is the roulette
aggregate transition itself.

Correctness boundaries:

* the group scope advisory lock is the only ordering primitive.  It is taken
  *before* any row lock (never the other way round) by delegating to
  ``RouletteCommandService.advance_expired`` / ``lock_group_scope``;
* every admission / retention boundary is re-evaluated against the PostgreSQL
  clock *after* that lock is held, so a decision that changed while the worker
  queued cannot leak through;
* APScheduler's ``max_instances=1`` purely throttles; it is never relied on for
  correctness.

The maintenance path imports no Redis client and holds no process lock.
"""

# This module deliberately keeps its operator-facing errors short.

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import text

from komari_bot.db.group_transaction_locks import lock_group_scope

from .command_service import EFFECT_CHECK_REJECTED
from .domain import GroupRef

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from sqlalchemy.ext.asyncio import AsyncSession

    from .command_service import RouletteCommandService, SessionFactory

#: Fixed job identifiers; re-registering replaces the same job, never adds one.
RECOVERY_JOB_ID = "komari_roulette_recovery"
CLEANUP_JOB_ID = "komari_roulette_retention_cleanup"

#: Recovery throttles only; a missed run is swallowed by ``coalesce``.
RECOVERY_INTERVAL_SECONDS = 60
RECOVERY_BATCH_SIZE = 100
CLEANUP_BATCH_SIZE = 100
#: Daily retention sweep, evaluated in the *scheduler's* deployment timezone.
CLEANUP_HOUR = 4
CLEANUP_MINUTE = 0

#: Retention boundaries, measured in PostgreSQL time (the storage is UTC).
RECEIPT_RETENTION_DAYS = 7
TERMINAL_RETENTION_DAYS = 30

#: The four legal terminal lifecycles.  ``completed`` is permanent (its result
#: is the win evidence) and ``waiting``/``active`` are never cleaned at all.
_NON_WIN_TERMINAL_LIFECYCLES: tuple[str, ...] = ("cancelled", "expired", "failed")

#: ``rowcount`` is authoritative on every explicit DELETE we issue.
_SQL_DUE_GAMES = (
    "SELECT game_id, app_id, group_openid, "
    "COALESCE(waiting_expires_at, turn_deadline_at) AS due_at "
    "FROM komari_roulette_games "
    "WHERE lifecycle IN ('waiting', 'active') "
    "AND COALESCE(waiting_expires_at, turn_deadline_at) <= clock_timestamp() "
    "ORDER BY due_at ASC, game_id ASC "
    "LIMIT :limit"
)
_SQL_DUE_GAMES_AFTER_CURSOR = (
    "SELECT game_id, app_id, group_openid, "
    "COALESCE(waiting_expires_at, turn_deadline_at) AS due_at "
    "FROM komari_roulette_games "
    "WHERE lifecycle IN ('waiting', 'active') "
    "AND COALESCE(waiting_expires_at, turn_deadline_at) <= clock_timestamp() "
    "AND (COALESCE(waiting_expires_at, turn_deadline_at), game_id) "
    "> (CAST(:resume_due AS timestamptz), CAST(:resume_game AS text)) "
    "ORDER BY due_at ASC, game_id ASC "
    "LIMIT :limit"
)
_SQL_AGED_RECEIPT_GROUPS = (
    "SELECT app_id, group_openid FROM komari_roulette_command_receipts "
    "WHERE created_at < clock_timestamp() - make_interval(days => :retention_days) "
    "GROUP BY app_id, group_openid "
    "ORDER BY app_id ASC, group_openid ASC "
    "LIMIT :limit"
)
_SQL_DELETE_AGED_RECEIPTS = (
    "DELETE FROM komari_roulette_command_receipts "
    "WHERE receipt_id IN ("
    "SELECT receipt_id FROM komari_roulette_command_receipts "
    "WHERE app_id = :app_id AND group_openid = :group_openid "
    "AND created_at < clock_timestamp() - make_interval(days => :retention_days) "
    "ORDER BY created_at ASC, receipt_id ASC "
    "LIMIT :limit"
    ") RETURNING receipt_id"
)
_SQL_AGED_TERMINAL_GROUPS = (
    "SELECT app_id, group_openid FROM komari_roulette_games "
    "WHERE lifecycle IN ('cancelled', 'expired', 'failed') "
    "AND ended_at IS NOT NULL "
    "AND ended_at < clock_timestamp() - make_interval(days => :retention_days) "
    "GROUP BY app_id, group_openid "
    "ORDER BY app_id ASC, group_openid ASC "
    "LIMIT :limit"
)
_SQL_AGED_TERMINAL_GAMES = (
    "SELECT game_id FROM komari_roulette_games "
    "WHERE app_id = :app_id AND group_openid = :group_openid "
    "AND lifecycle IN ('cancelled', 'expired', 'failed') "
    "AND ended_at IS NOT NULL "
    "AND ended_at < clock_timestamp() - make_interval(days => :retention_days) "
    "ORDER BY ended_at ASC, game_id ASC "
    "LIMIT :limit"
)
#: ``komari_roulette_result_players`` is RESTRICTed by ``results`` and
#: ``results`` by ``games``, so the evidence has to be peeled off in that order.
#: ``komari_roulette_players`` cascades from ``games`` on its own.
_SQL_DELETE_RESULT_PLAYERS = (
    "DELETE FROM komari_roulette_result_players WHERE game_id = :game_id"
)
_SQL_DELETE_RESULT = (
    "DELETE FROM komari_roulette_results WHERE game_id = :game_id "
    "RETURNING game_id"
)
_SQL_DELETE_GAME = (
    "DELETE FROM komari_roulette_games WHERE game_id = :game_id RETURNING game_id"
)
_SQL_RECEIPTS_PENDING = (
    "SELECT EXISTS(SELECT 1 FROM komari_roulette_command_receipts "
    "WHERE created_at < clock_timestamp() - make_interval(days => :retention_days))"
)
_SQL_TERMINALS_PENDING = (
    "SELECT EXISTS(SELECT 1 FROM komari_roulette_games "
    "WHERE lifecycle IN ('cancelled', 'expired', 'failed') "
    "AND ended_at IS NOT NULL "
    "AND ended_at < clock_timestamp() - make_interval(days => :retention_days))"
)


@dataclass(frozen=True, slots=True)
class RecoveryTickResult:
    """Bounded, identity-free summary of one recovery scan.

    ``cursor`` is an opaque internal pagination token.  It must never be logged
    or copied into an observation; :meth:`RouletteObservability.note_scan` drops
    it precisely because it encodes scan position.
    """

    scanned: int
    advanced: int
    skipped_restricted: int
    failed: int
    cursor: str | None = None


@dataclass(frozen=True, slots=True)
class CleanupResult:
    """Summary of one bounded, reentrant retention cleanup batch."""

    receipts_deleted: int
    games_deleted: int
    results_deleted: int
    more_pending: bool


@dataclass(frozen=True, slots=True)
class _DueGame:
    """One due candidate root; the cursor key is ``(due_at, game_id)``."""

    game_id: str
    app_id: str
    group_openid: str
    due_at: datetime


def _cursor_token(due_at: datetime, game_id: str) -> str:
    return json.dumps(
        {"due": due_at.isoformat(), "game": game_id},
        separators=(",", ":"),
        sort_keys=True,
    )


def register_maintenance_jobs(
    scheduler: Any,
    maintenance: RouletteMaintenance,
) -> None:
    """Register the two fixed jobs on the caller's scheduler singleton.

    The triggers are passed as the string aliases ``"interval"`` / ``"cron"`` on
    purpose: APScheduler then injects the *scheduler's* timezone, so "04:00"
    means 04:00 in the deployment timezone instead of the host's or UTC's.
    """

    scheduler.add_job(
        maintenance.advance_due,
        trigger="interval",
        seconds=RECOVERY_INTERVAL_SECONDS,
        id=RECOVERY_JOB_ID,
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )
    scheduler.add_job(
        maintenance.cleanup_retention,
        trigger="cron",
        hour=CLEANUP_HOUR,
        minute=CLEANUP_MINUTE,
        id=CLEANUP_JOB_ID,
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )


def unregister_maintenance_jobs(scheduler: Any) -> None:
    """Remove the two fixed jobs if they are still registered."""

    for job_id in (RECOVERY_JOB_ID, CLEANUP_JOB_ID):
        if scheduler.get_job(job_id) is not None:
            scheduler.remove_job(job_id)


class RouletteMaintenance:
    """Recover due games and clean aged rows under the real group lock."""

    def __init__(
        self,
        *,
        session_factory: SessionFactory,
        service: RouletteCommandService,
        admission: Callable[[str, str], bool | Awaitable[bool]],
    ) -> None:
        self._session_factory = session_factory
        self._service = service
        self._admission = admission
        #: Internal keyset resume position; never exported except as ``cursor``.
        self._resume_after: tuple[datetime, str] | None = None

    async def advance_due(
        self,
        *,
        batch_size: int = RECOVERY_BATCH_SIZE,
    ) -> RecoveryTickResult:
        """Advance every due game in one bounded page, once per group.

        The scan is keyset-paginated on ``(due_at, game_id)`` so a full page of
        restricted groups can never permanently starve an allowed group that sits
        behind them.  Each candidate is coarse-checked before the (potentially
        slow) deep entry, then re-checked *inside* the group lock through
        ``effect_check``; a group that turns restricted while the worker waits
        settles as a skip, not as an advance.
        """

        if batch_size <= 0:
            return RecoveryTickResult(0, 0, 0, 0, None)
        async with self._session_factory() as session:
            candidates = await self._load_due_candidates(session, batch_size)
        advanced = 0
        skipped = 0
        failed = 0
        seen: set[tuple[str, str]] = set()
        for candidate in candidates:
            key = (candidate.app_id, candidate.group_openid)
            if key in seen:
                # One scan advances one current game per group; a duplicate row
                # must never advance the same group twice.
                skipped += 1
                continue
            seen.add(key)
            if not await self._resolve_admission(
                candidate.app_id,
                candidate.group_openid,
            ):
                skipped += 1
                continue
            try:
                outcome = await self._service.advance_expired(
                    GroupRef(candidate.app_id, candidate.group_openid),
                    effect_check=self._post_lock_gate(
                        candidate.app_id,
                        candidate.group_openid,
                    ),
                )
            except Exception:
                # Per-group failures are counted and aggregated by the caller;
                # one bad group must not abort the whole page.
                failed += 1
                continue
            if outcome.changed:
                advanced += 1
            elif outcome.result_code == EFFECT_CHECK_REJECTED:
                skipped += 1
        self._advance_cursor(candidates, batch_size)
        return RecoveryTickResult(
            scanned=len(candidates),
            advanced=advanced,
            skipped_restricted=skipped,
            failed=failed,
            cursor=self._cursor_token(),
        )

    async def cleanup_retention(
        self,
        *,
        batch_size: int = CLEANUP_BATCH_SIZE,
    ) -> CleanupResult:
        """Delete one bounded page of aged receipts and non-win terminals.

        Receipts + fulfillments older than seven days go as a whole group (the
        fulfillment cascades); cancelled/expired/failed terminals older than
        thirty days go as a whole game (result players, then the result, then the
        game root, whose runtime seats cascade).  ``completed`` results, their
        seats, the leaderboard and any ``waiting``/``active`` game are never
        touched, and the leaderboard is never rebuilt here.
        """

        receipts_deleted = 0
        games_deleted = 0
        results_deleted = 0
        if batch_size > 0:
            receipts_deleted, remaining = await self._cleanup_receipts(batch_size)
            if remaining > 0:
                games_deleted, results_deleted = await self._cleanup_terminals(
                    remaining
                )
        more_pending = await self._has_pending_retention()
        return CleanupResult(
            receipts_deleted=receipts_deleted,
            games_deleted=games_deleted,
            results_deleted=results_deleted,
            more_pending=more_pending,
        )

    # ------------------------------------------------------------------
    # Recovery internals
    # ------------------------------------------------------------------

    async def _load_due_candidates(
        self,
        session: AsyncSession,
        batch_size: int,
    ) -> list[_DueGame]:
        resume = self._resume_after
        statement = (
            _SQL_DUE_GAMES if resume is None else _SQL_DUE_GAMES_AFTER_CURSOR
        )
        params: dict[str, object] = {"limit": batch_size}
        if resume is not None:
            params["resume_due"] = resume[0]
            params["resume_game"] = resume[1]
        rows = (await session.execute(text(statement), params)).mappings().all()
        candidates: list[_DueGame] = []
        for row in rows:
            due_at = row["due_at"]
            if not isinstance(due_at, datetime):
                continue
            candidates.append(
                _DueGame(
                    game_id=str(row["game_id"]),
                    app_id=str(row["app_id"]),
                    group_openid=str(row["group_openid"]),
                    due_at=due_at,
                )
            )
        return candidates

    def _advance_cursor(
        self,
        candidates: list[_DueGame],
        batch_size: int,
    ) -> None:
        """Keep the keyset cursor only while a full page suggests more work."""

        if len(candidates) >= batch_size and candidates:
            last = candidates[-1]
            self._resume_after = (last.due_at, last.game_id)
            return
        self._resume_after = None

    def _cursor_token(self) -> str | None:
        resume = self._resume_after
        if resume is None:
            return None
        return _cursor_token(resume[0], resume[1])

    def _post_lock_gate(
        self,
        app_id: str,
        group_openid: str,
    ) -> Callable[[], Awaitable[bool]]:
        """Build the gate re-evaluated inside the group lock."""

        async def gate() -> bool:
            return await self._resolve_admission(app_id, group_openid)

        return gate

    async def _resolve_admission(self, app_id: str, group_openid: str) -> bool:
        """Resolve the group-level admission port, failing closed on error.

        The port is intentionally group-level only: maintenance never invents a
        member identity.  A port that raises (or an awaitable port that rejects)
        is a refusal - never silently "allowed".
        """

        try:
            outcome = self._admission(app_id, group_openid)
            if isinstance(outcome, bool):
                return outcome
            return bool(await outcome)
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Retention internals
    # ------------------------------------------------------------------

    async def _cleanup_receipts(self, batch_size: int) -> tuple[int, int]:
        """Delete up to ``batch_size`` aged receipts; return (deleted, remaining)."""

        async with self._session_factory() as session:
            groups = (
                (
                    await session.execute(
                        text(_SQL_AGED_RECEIPT_GROUPS),
                        {
                            "retention_days": RECEIPT_RETENTION_DAYS,
                            "limit": batch_size,
                        },
                    )
                )
                .mappings()
                .all()
            )
            remaining = batch_size
            deleted = 0
            for row in groups:
                if remaining <= 0:
                    break
                app_id = str(row["app_id"])
                group_openid = str(row["group_openid"])
                deleted_here = await self._delete_aged_receipts(
                    session,
                    app_id,
                    group_openid,
                    remaining,
                )
                if deleted_here:
                    await session.commit()
                else:
                    await session.rollback()
                deleted += deleted_here
                remaining -= deleted_here
        return deleted, remaining

    async def _delete_aged_receipts(
        self,
        session: AsyncSession,
        app_id: str,
        group_openid: str,
        limit: int,
    ) -> int:
        """Lock the group, re-check the PG boundary, then delete one bounded page.

        The advisory lock is taken *before* any receipt row lock so the lock
        order always matches the command path; the retention boundary is then
        re-read from the PostgreSQL clock inside the locked transaction.
        """

        await lock_group_scope(session, app_id=app_id, group_openid=group_openid)
        result = await session.execute(
            text(_SQL_DELETE_AGED_RECEIPTS),
            {
                "app_id": app_id,
                "group_openid": group_openid,
                "retention_days": RECEIPT_RETENTION_DAYS,
                "limit": limit,
            },
        )
        return len(result.scalars().all())

    async def _cleanup_terminals(self, batch_size: int) -> tuple[int, int]:
        """Delete up to ``batch_size`` aged terminals; return (games, results)."""

        async with self._session_factory() as session:
            groups = (
                (
                    await session.execute(
                        text(_SQL_AGED_TERMINAL_GROUPS),
                        {
                            "retention_days": TERMINAL_RETENTION_DAYS,
                            "limit": batch_size,
                        },
                    )
                )
                .mappings()
                .all()
            )
            remaining = batch_size
            games_deleted = 0
            results_deleted = 0
            for row in groups:
                if remaining <= 0:
                    break
                app_id = str(row["app_id"])
                group_openid = str(row["group_openid"])
                games, results = await self._delete_aged_terminals(
                    session,
                    app_id,
                    group_openid,
                    remaining,
                )
                if games or results:
                    await session.commit()
                else:
                    await session.rollback()
                games_deleted += games
                results_deleted += results
                remaining -= games
        return games_deleted, results_deleted

    async def _delete_aged_terminals(
        self,
        session: AsyncSession,
        app_id: str,
        group_openid: str,
        limit: int,
    ) -> tuple[int, int]:
        """Peel evidence off one bounded page of aged non-win terminals."""

        await lock_group_scope(session, app_id=app_id, group_openid=group_openid)
        game_ids = (
            (
                await session.execute(
                    text(_SQL_AGED_TERMINAL_GAMES),
                    {
                        "app_id": app_id,
                        "group_openid": group_openid,
                        "retention_days": TERMINAL_RETENTION_DAYS,
                        "limit": limit,
                    },
                )
            )
            .scalars()
            .all()
        )
        games_deleted = 0
        results_deleted = 0
        for game_id in game_ids:
            params = {"game_id": str(game_id)}
            await session.execute(text(_SQL_DELETE_RESULT_PLAYERS), params)
            deleted_results = await session.execute(
                text(_SQL_DELETE_RESULT),
                params,
            )
            results_deleted += len(deleted_results.scalars().all())
            deleted_games = await session.execute(
                text(_SQL_DELETE_GAME),
                params,
            )
            games_deleted += len(deleted_games.scalars().all())
        return games_deleted, results_deleted

    async def _has_pending_retention(self) -> bool:
        """Report whether either retention rule still has eligible rows left."""

        async with self._session_factory() as session:
            receipts = await session.scalar(
                text(_SQL_RECEIPTS_PENDING),
                {"retention_days": RECEIPT_RETENTION_DAYS},
            )
            terminals = await session.scalar(
                text(_SQL_TERMINALS_PENDING),
                {"retention_days": TERMINAL_RETENTION_DAYS},
            )
        return bool(receipts) or bool(terminals)


__all__ = [
    "CLEANUP_BATCH_SIZE",
    "CLEANUP_HOUR",
    "CLEANUP_JOB_ID",
    "CLEANUP_MINUTE",
    "RECEIPT_RETENTION_DAYS",
    "RECOVERY_BATCH_SIZE",
    "RECOVERY_INTERVAL_SECONDS",
    "RECOVERY_JOB_ID",
    "TERMINAL_RETENTION_DAYS",
    "CleanupResult",
    "RecoveryTickResult",
    "RouletteMaintenance",
    "register_maintenance_jobs",
    "unregister_maintenance_jobs",
]
