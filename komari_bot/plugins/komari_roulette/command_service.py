# ruff: noqa: B904,FBT003,PLR0911,TRY003,TRY300,TRY301

"""Transactional command boundary for the roulette game.

The command service is the only owner of a command transaction.  It combines
the canonical binding read, roulette aggregate transition, immutable receipt,
and the first fulfillment state on one ``AsyncSession`` before one explicit
commit.  Platform delivery is intentionally outside this module.
"""

from __future__ import annotations

import json
import random
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, suppress
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING, cast
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from komari_bot.db.group_transaction_locks import lock_group_scope
from komari_bot.plugins.character_binding import (
    BindingConflictError,
    BindingPersistenceError,
    BindingTransaction,
)

if TYPE_CHECKING:
    from komari_bot.plugins.character_binding import GroupBindingRecord

from .domain import (
    Action,
    ActionResult,
    ChamberKind,
    GroupRef,
    ItemType,
    PlayerRef,
    RandomSource,
    apply_action,
    initial_state,
)
from .mapper import (
    GameSnapshot,
    LeaderboardEntry,
    TerminalProjection,
    game_state_to_snapshot,
    transition_from_action_result,
)
from .storage import (
    AggregateCorruptError,
    PostgresRouletteStorage,
    StorageUnavailableError,
)

Scalar = str | int | bool | None
PublicValue = Scalar | tuple[str, ...]
SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]
#: Intents whose active-game writes are guarded by a caller observation.
OBSERVED_ACTIVE_WRITES = frozenset(
    {
        "shoot",
        "forfeit",
        "end_turn",
        "reload",
        "use_item",
        "discard_item",
        "choose_item",
    }
)


class IdempotencyKeyConflictError(RuntimeError):
    """The same inbound message key was reused with another request."""


class CommitOutcomeUnknownError(RuntimeError):
    """The database did not confirm whether the transaction committed."""


class StateConflictError(RuntimeError):
    """A mutating command used an obsolete observation."""


class FulfillmentConflictError(RuntimeError):
    """A fulfillment claim was used with an incompatible terminal state."""


class FulfillmentState(StrEnum):
    """Durable one-to-one reply fulfillment lifecycle."""

    NOT_STARTED = "NOT_STARTED"
    PENDING_CONFIRMATION = "PENDING_CONFIRMATION"
    DELIVERED = "DELIVERED"
    NOT_DELIVERED = "NOT_DELIVERED"


#: Age after which a receipt's delivery credential is expired.  The window is
#: enforced atomically inside :meth:`RouletteCommandService.claim_fulfillment`
#: against the PostgreSQL clock; the TSK-279 scheduler reuses this same constant
#: and the same ``komari_roulette_command_receipts.created_at`` column.
FULFILLMENT_CREDENTIAL_WINDOW_SECONDS: int = 300


def _age_seconds(value: object) -> float | None:
    """Coerce a driver-reported credential age, or ``None`` when unusable.

    ``EXTRACT(EPOCH FROM ...)`` is ``numeric`` so asyncpg hands back a
    :class:`~decimal.Decimal`, but the value is validated before use so an
    unexpected driver type fails closed instead of raising mid-claim.
    """

    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal, str)):
        return None
    try:
        return float(value)
    except ValueError:
        return None


@dataclass(frozen=True, slots=True)
class CanonicalCommand:
    """Typed intent produced by the command parser boundary."""

    intent: str
    target_player_seq: int | None = None
    item: ItemType | None = None
    decision: str | None = None
    replace_item: ItemType | None = None
    syntax_code: str | None = None

    @classmethod
    def create(cls) -> CanonicalCommand:
        return cls("create")

    @classmethod
    def join(cls) -> CanonicalCommand:
        return cls("join")

    @classmethod
    def leave(cls) -> CanonicalCommand:
        return cls("leave")

    @classmethod
    def cancel(cls) -> CanonicalCommand:
        return cls("cancel")

    @classmethod
    def start(cls) -> CanonicalCommand:
        return cls("start")

    @classmethod
    def shoot(cls) -> CanonicalCommand:
        return cls("shoot")

    @classmethod
    def forfeit(cls) -> CanonicalCommand:
        return cls("forfeit")

    @classmethod
    def end_turn(cls) -> CanonicalCommand:
        return cls("end_turn")

    @classmethod
    def reload(cls) -> CanonicalCommand:
        return cls("reload")

    @classmethod
    def use_item(
        cls,
        item: ItemType | str,
        target_player_seq: int | None = None,
    ) -> CanonicalCommand:
        return cls(
            "use_item",
            target_player_seq=target_player_seq,
            item=ItemType(item),
        )

    @classmethod
    def discard_item(cls, item: ItemType | str) -> CanonicalCommand:
        return cls("discard_item", item=ItemType(item))

    @classmethod
    def choose_item(
        cls,
        *,
        decision: str,
        replace_item: ItemType | str | None = None,
    ) -> CanonicalCommand:
        return cls(
            "choose_item",
            decision=decision,
            replace_item=(ItemType(replace_item) if replace_item is not None else None),
        )

    @classmethod
    def transfer(cls, *, target_player_seq: int) -> CanonicalCommand:
        return cls("transfer", target_player_seq=target_player_seq)

    @classmethod
    def open_item_panel(cls) -> CanonicalCommand:
        return cls("open_item_panel")

    @classmethod
    def leaderboard(cls) -> CanonicalCommand:
        return cls("leaderboard")

    @classmethod
    def syntax_failure(cls, *, code: str) -> CanonicalCommand:
        return cls("syntax_failure", syntax_code=code)

    def __post_init__(self) -> None:
        if not self.intent.strip():
            raise ValueError("command intent must not be empty")
        if self.target_player_seq is not None and self.target_player_seq <= 0:
            raise ValueError("target player sequence must be positive")
        if self.intent == "syntax_failure" and not (self.syntax_code or "").strip():
            raise ValueError("syntax failure code must not be empty")

    def to_action(self, player: PlayerRef) -> Action:
        """Translate a canonical command to the pure domain action seam."""

        match self.intent:
            case "create":
                return Action.create(player)
            case "join":
                return Action.join(player)
            case "leave":
                return Action.leave(player)
            case "cancel":
                return Action.cancel(player)
            case "start":
                return Action.start(player)
            case "shoot":
                return Action.shoot(player)
            case "forfeit":
                return Action.forfeit(player)
            case "end_turn":
                return Action.end_turn(player)
            case "reload":
                return Action.reload(player)
            case "use_item":
                if self.item is None:
                    raise ValueError("item is required")
                return Action.use_item(player, self.item, self.target_player_seq)
            case "discard_item":
                if self.item is None:
                    raise ValueError("item is required")
                return Action.discard_item(player, self.item)
            case "choose_item":
                if self.decision is None:
                    raise ValueError("item choice decision is required")
                return Action.choose_item(player, self.decision, self.replace_item)
            case "transfer":
                return Action.transfer(player, cast("int", self.target_player_seq))
            case "open_item_panel":
                return Action.open_item_panel(player)
            case _:
                raise ValueError(f"unsupported domain command: {self.intent}")

    def fingerprint_fields(self) -> dict[str, Scalar | dict[str, Scalar]]:
        """Return only stable typed command parameters for idempotency."""

        fields: dict[str, Scalar | dict[str, Scalar]] = {"intent": self.intent}
        if self.target_player_seq is not None:
            fields["target_player_seq"] = self.target_player_seq
        if self.item is not None:
            fields["item"] = self.item.value
        if self.decision is not None:
            fields["decision"] = self.decision
        if self.replace_item is not None:
            fields["replace_item"] = self.replace_item.value
        if self.syntax_code is not None:
            fields["syntax_code"] = self.syntax_code
        return fields


@dataclass(frozen=True, slots=True)
class CommandRequest:
    """Validated protocol-scoped request handed over by the parser."""

    app_id: str
    group_openid: str
    inbound_msg_id: str
    member_openid: str
    command: CanonicalCommand
    target_mention_count: int = 0

    def __post_init__(self) -> None:
        if not self.app_id.strip() or not self.group_openid.strip():
            raise ValueError("request group identity must not be empty")
        if not self.inbound_msg_id.strip() or not self.member_openid.strip():
            raise ValueError("request identity must not be empty")
        if self.target_mention_count < 0:
            raise ValueError("target mention count must not be negative")

    @property
    def group(self) -> GroupRef:
        return GroupRef(self.app_id, self.group_openid)

    @property
    def fingerprint(self) -> dict[str, object]:
        """Stable structured fingerprint; raw message text never enters it."""

        return {
            "version": 1,
            "app_id": self.app_id,
            "group_openid": self.group_openid,
            "member_openid": self.member_openid,
            "target_mention_count": self.target_mention_count,
            "command": self.command.fingerprint_fields(),
        }


@dataclass(frozen=True, slots=True)
class Observation:
    """Client observation used for optimistic version checks."""

    game_id: str
    state_revision: int
    turn_seq: int


@dataclass(frozen=True, slots=True)
class ReplyProjection:
    """Frozen reply payload safe for the platform sender."""

    body: str
    metadata: Mapping[str, Scalar]

    def __post_init__(self) -> None:
        if not isinstance(self.body, str):
            raise TypeError("reply body must be text")
        normalized: dict[str, Scalar] = {}
        for key, value in self.metadata.items():
            if not isinstance(key, str) or (
                not isinstance(value, (str, int, bool)) and value is not None
            ):
                raise TypeError("reply metadata must contain scalar values")
            normalized[str(key)] = cast("Scalar", value)
        object.__setattr__(self, "metadata", MappingProxyType(normalized))


@dataclass(frozen=True, slots=True)
class ReplyPlayer:
    """Safe frozen roster entry for a reply projector."""

    join_seq: int
    display_name: str
    alive: bool
    inventory_counts: tuple[tuple[str, int], ...] = ()
    # This is an internal sending identity. Projectors may use it to create
    # one explicit mention, but it must never be interpolated into body text.
    member_openid: str = ""
    pending_lock: bool = False


@dataclass(frozen=True, slots=True)
class ReplyGameView:
    """Public game facts frozen with a command receipt."""

    host_seq: int | None = None
    chamber_remaining_total: int = 0
    chamber_remaining_live: int = 0
    chamber_remaining_blank: int = 0
    hit_probability_percent: float | None = None
    pending_reward_count: int = 0
    pending_burst: bool = False
    pending_lock_player_seqs: tuple[int, ...] = ()
    pending_lock_players: tuple[ReplyPlayer, ...] = ()
    active_lock_player: ReplyPlayer | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "pending_lock_player_seqs",
            tuple(self.pending_lock_player_seqs),
        )
        object.__setattr__(
            self,
            "pending_lock_players",
            tuple(self.pending_lock_players),
        )


@dataclass(frozen=True, slots=True)
class ReplyProjectionContext:
    """Safe, structured view supplied to a reply projector."""

    result_code: str
    game_id: str | None
    lifecycle: str | None
    phase: str | None
    state_revision: int | None
    turn_seq: int | None
    actor_member_openid: str
    target_mention_count: int
    details: Mapping[str, PublicValue] = field(default_factory=dict)
    players: tuple[ReplyPlayer, ...] = ()
    current_player: ReplyPlayer | None = None
    winner: ReplyPlayer | None = None
    winner_group_wins: int | None = None
    target_player: ReplyPlayer | None = None
    target_member_openid: str | None = None
    reward_player: ReplyPlayer | None = None
    lock_target_player: ReplyPlayer | None = None
    mention_target: ReplyPlayer | None = None
    mention_reason: str | None = None
    game_view: ReplyGameView | None = None
    #: Protocol intent of the command that produced this projection (for
    #: phase-specific copy such as "leave" after a game has started).
    intent: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "details", MappingProxyType(dict(self.details)))
        object.__setattr__(self, "players", tuple(self.players))

    @property
    def host_seq(self) -> int | None:
        return self.game_view.host_seq if self.game_view is not None else None

    @property
    def chamber_remaining_total(self) -> int:
        return self.game_view.chamber_remaining_total if self.game_view is not None else 0

    @property
    def chamber_remaining_live(self) -> int:
        return self.game_view.chamber_remaining_live if self.game_view is not None else 0

    @property
    def chamber_remaining_blank(self) -> int:
        return self.game_view.chamber_remaining_blank if self.game_view is not None else 0

    @property
    def hit_probability_percent(self) -> float | None:
        return (
            self.game_view.hit_probability_percent
            if self.game_view is not None
            else None
        )

    @property
    def pending_reward_count(self) -> int:
        return self.game_view.pending_reward_count if self.game_view is not None else 0

    @property
    def pending_burst(self) -> bool:
        return self.game_view.pending_burst if self.game_view is not None else False

    @property
    def pending_lock_player_seqs(self) -> tuple[int, ...]:
        return (
            self.game_view.pending_lock_player_seqs
            if self.game_view is not None
            else ()
        )

    @property
    def pending_lock_players(self) -> tuple[ReplyPlayer, ...]:
        return (
            self.game_view.pending_lock_players
            if self.game_view is not None
            else ()
        )

    @property
    def active_lock_player(self) -> ReplyPlayer | None:
        return self.game_view.active_lock_player if self.game_view is not None else None


@dataclass(frozen=True, slots=True)
class CommandReceipt:
    """Immutable result and frozen projection for one inbound message."""

    receipt_id: str
    app_id: str
    group_openid: str
    inbound_msg_id: str
    fingerprint: Mapping[str, object]
    result_code: str
    game_id: str | None
    state_revision: int | None
    turn_seq: int | None
    reply: ReplyProjection


@dataclass(frozen=True, slots=True)
class ExpiryAdvance:
    """Result of the worker-only expiry entry point."""

    receipt_id: None
    game_id: str | None
    result_code: str
    changed: bool
    state_revision: int | None
    turn_seq: int | None


@dataclass(frozen=True, slots=True)
class FulfillmentClaim:
    """Exclusive typed claim for a reply delivery attempt."""

    receipt_id: str
    state: FulfillmentState


class _DefaultRandomSource:
    def chamber_order(
        self,
        live_count: int,
        blank_count: int,
    ) -> Sequence[ChamberKind]:
        chamber = [ChamberKind.LIVE] * live_count + [ChamberKind.BLANK] * blank_count
        random.shuffle(chamber)
        return tuple(chamber)

    def weighted_item(self, weights: Mapping[ItemType, int]) -> ItemType:
        items = tuple(weights)
        values = tuple(weights[item] for item in items)
        return random.choices(items, weights=values, k=1)[0]


class RouletteCommandService:
    """Own the single transaction boundary for roulette commands."""

    def __init__(
        self,
        *,
        session_factory: SessionFactory,
        reply_projector: Callable[[ReplyProjectionContext], ReplyProjection],
        random_source: RandomSource | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._reply_projector = reply_projector
        self._random_source = random_source or _DefaultRandomSource()

    async def execute_group_command(
        self,
        request: CommandRequest,
        *,
        observation: Observation | None = None,
    ) -> CommandReceipt:
        """Execute one parsed command and commit its receipt atomically."""

        try:
            async with self._session_factory() as session:
                try:
                    receipt = await self._execute_in_session(
                        session,
                        request,
                        observation=observation,
                    )
                    try:
                        await session.commit()
                    except Exception as error:
                        raise CommitOutcomeUnknownError(
                            "command commit outcome is unknown"
                        ) from error
                    return receipt
                except CommitOutcomeUnknownError:
                    with suppress(Exception):
                        await session.rollback()
                    raise
                except Exception:
                    with suppress(Exception):
                        await session.rollback()
                    raise
        except (
            IdempotencyKeyConflictError,
            CommitOutcomeUnknownError,
            BindingConflictError,
            BindingPersistenceError,
            StorageUnavailableError,
        ):
            raise
        except (DBAPIError, SQLAlchemyError, ConnectionError, OSError, TimeoutError) as error:
            raise StorageUnavailableError("roulette command storage is unavailable") from error

    async def observe_current(self, group: GroupRef) -> Observation | None:
        """Read the latest validated game snapshot for a group."""

        try:
            async with self._session_factory() as session:
                snapshot = await PostgresRouletteStorage(session).load_current(group)
                if snapshot is None:
                    return None
                return Observation(
                    game_id=snapshot.game_id,
                    state_revision=snapshot.state_revision,
                    turn_seq=snapshot.turn_seq,
                )
        except StorageUnavailableError:
            raise
        except (DBAPIError, SQLAlchemyError, ConnectionError, OSError, TimeoutError) as error:
            raise StorageUnavailableError("roulette command storage is unavailable") from error

    async def advance_expired(
        self,
        group: GroupRef,
        *,
        observation: Observation | None = None,
    ) -> ExpiryAdvance:
        """Advance one expired game without creating a receipt or sending."""

        del observation
        try:
            async with self._session_factory() as session:
                storage = PostgresRouletteStorage(session)
                await lock_group_scope(
                    session,
                    app_id=group.app_id,
                    group_openid=group.group_openid,
                )
                try:
                    current = await storage.load_current(group, for_update=True)
                except AggregateCorruptError:
                    now = await self._pg_now(session)
                    failed = await storage.fail_corrupt_current(group, ended_at=now)
                    if failed is None:
                        raise StorageUnavailableError("corrupt game disappeared")
                    await session.commit()
                    return ExpiryAdvance(
                        receipt_id=None,
                        game_id=failed.game_id,
                        result_code="aggregate_corrupt",
                        changed=True,
                        state_revision=failed.terminal_revision,
                        turn_seq=None,
                    )
                if current is None:
                    await session.rollback()
                    return ExpiryAdvance(None, None, "no_active_game", False, None, None)
                now = await self._pg_now(session)
                if current.deadline is None or now < current.deadline:
                    await session.rollback()
                    return ExpiryAdvance(
                        None,
                        current.game_id,
                        "not_expired",
                        False,
                        current.state_revision,
                        current.turn_seq,
                    )
                result = apply_action(
                    current.state,
                    Action.expire(),
                    now=now,
                    random_source=self._random_source,
                )
                if result.state == current.state:
                    await session.rollback()
                    return ExpiryAdvance(
                        None,
                        current.game_id,
                        result.code,
                        False,
                        current.state_revision,
                        current.turn_seq,
                    )
                saved, _ = await self._persist_result(
                    session,
                    storage,
                    current,
                    result,
                    now=now,
                )
                await session.commit()
                return ExpiryAdvance(
                    receipt_id=None,
                    game_id=saved.game_id,
                    result_code=result.code,
                    changed=True,
                    state_revision=saved.state_revision,
                    turn_seq=saved.turn_seq,
                )
        except StorageUnavailableError:
            raise
        except (DBAPIError, SQLAlchemyError, ConnectionError, OSError, TimeoutError) as error:
            raise StorageUnavailableError("roulette expiry storage is unavailable") from error

    async def claim_fulfillment(self, receipt_id: str) -> FulfillmentClaim | None:
        """Atomically claim the one allowed platform send attempt.

        The 5-minute credential window is enforced *inside* the claim while the
        row lock is held, comparing the receipt's ``created_at`` against the
        PostgreSQL ``clock_timestamp()`` (never a local wall clock).  An expired
        ``NOT_STARTED`` row converges to ``NOT_DELIVERED`` and the returned
        claim carries that state, so the sender never starts a network call; a
        receipt still in the window transitions ``NOT_STARTED`` to
        ``PENDING_CONFIRMATION``.  Any other state is left untouched and yields
        no claim.
        """

        try:
            async with self._session_factory() as session:
                row = await self._fulfillment_window_row(session, receipt_id)
                if row is None:
                    await session.rollback()
                    return None
                state = FulfillmentState(str(row["state"]))
                if state is not FulfillmentState.NOT_STARTED:
                    await session.rollback()
                    return None
                age_seconds = _age_seconds(row["age_seconds"])
                if age_seconds is None or (
                    age_seconds >= FULFILLMENT_CREDENTIAL_WINDOW_SECONDS
                ):
                    await session.execute(
                        text(
                            "UPDATE komari_roulette_fulfillments "
                            "SET state = 'NOT_DELIVERED', "
                            "updated_at = clock_timestamp() "
                            "WHERE receipt_id = :receipt_id"
                        ),
                        {"receipt_id": receipt_id},
                    )
                    await session.commit()
                    return FulfillmentClaim(
                        receipt_id=receipt_id,
                        state=FulfillmentState.NOT_DELIVERED,
                    )
                await session.execute(
                    text(
                        "UPDATE komari_roulette_fulfillments "
                        "SET state = 'PENDING_CONFIRMATION', "
                        "updated_at = clock_timestamp() "
                        "WHERE receipt_id = :receipt_id"
                    ),
                    {"receipt_id": receipt_id},
                )
                await session.commit()
                return FulfillmentClaim(
                    receipt_id=receipt_id,
                    state=FulfillmentState.PENDING_CONFIRMATION,
                )
        except CommitOutcomeUnknownError:
            raise
        except (DBAPIError, SQLAlchemyError, ConnectionError, OSError, TimeoutError) as error:
            raise StorageUnavailableError("fulfillment storage is unavailable") from error

    async def check_fulfillment_window(self, claim: FulfillmentClaim) -> bool:
        """Re-read the credential window from the DB clock before the send.

        The claim is a positive send authorization only for
        ``PENDING_CONFIRMATION``; the receipt's ``created_at`` is compared to
        the PostgreSQL ``clock_timestamp()`` so a slow pre-send recheck cannot
        deliver on an expired credential.  Store errors propagate unfiltered so
        the delivery boundary fails closed without a network call.
        """

        try:
            async with self._session_factory() as session:
                row = await self._fulfillment_window_row(session, claim.receipt_id)
                await session.rollback()
        except (DBAPIError, SQLAlchemyError, ConnectionError, OSError, TimeoutError) as error:
            raise StorageUnavailableError("fulfillment storage is unavailable") from error
        if row is None:
            return False
        state = FulfillmentState(str(row["state"]))
        if state is not FulfillmentState.PENDING_CONFIRMATION:
            return False
        age_seconds = _age_seconds(row["age_seconds"])
        return (
            age_seconds is not None
            and age_seconds < FULFILLMENT_CREDENTIAL_WINDOW_SECONDS
        )

    async def mark_delivered(
        self,
        claim: FulfillmentClaim,
        *,
        platform_message_id: str,
    ) -> None:
        """Record a confirmed platform message id exactly once."""

        if not platform_message_id.strip():
            raise ValueError("platform message id must not be empty")
        try:
            async with self._session_factory() as session:
                row = await self._fulfillment_row(
                    session,
                    claim.receipt_id,
                    for_update=True,
                )
                if row is None:
                    raise FulfillmentConflictError("fulfillment claim is unknown")
                state = FulfillmentState(str(row["state"]))
                current_id = row["platform_message_id"]
                if state is FulfillmentState.DELIVERED:
                    if current_id == platform_message_id:
                        await session.rollback()
                        return
                    raise FulfillmentConflictError(
                        "fulfillment already has another platform message"
                    )
                if state is not FulfillmentState.PENDING_CONFIRMATION:
                    raise FulfillmentConflictError("fulfillment claim is no longer pending")
                await session.execute(
                    text(
                        "UPDATE komari_roulette_fulfillments SET "
                        "state = 'DELIVERED', platform_message_id = :message_id, "
                        "updated_at = clock_timestamp() "
                        "WHERE receipt_id = :receipt_id"
                    ),
                    {
                        "receipt_id": claim.receipt_id,
                        "message_id": platform_message_id,
                    },
                )
                await session.commit()
        except FulfillmentConflictError:
            raise
        except (DBAPIError, SQLAlchemyError, ConnectionError, OSError, TimeoutError) as error:
            raise StorageUnavailableError("fulfillment storage is unavailable") from error

    async def mark_not_delivered(self, claim: FulfillmentClaim) -> None:
        """Record an explicit pre-send failure without making a new claim."""

        try:
            async with self._session_factory() as session:
                row = await self._fulfillment_row(
                    session,
                    claim.receipt_id,
                    for_update=True,
                )
                if row is None:
                    raise FulfillmentConflictError("fulfillment claim is unknown")
                state = FulfillmentState(str(row["state"]))
                if state is FulfillmentState.NOT_DELIVERED:
                    await session.rollback()
                    return
                if state is not FulfillmentState.PENDING_CONFIRMATION:
                    raise FulfillmentConflictError("fulfillment claim is no longer pending")
                await session.execute(
                    text(
                        "UPDATE komari_roulette_fulfillments SET "
                        "state = 'NOT_DELIVERED', updated_at = clock_timestamp() "
                        "WHERE receipt_id = :receipt_id"
                    ),
                    {"receipt_id": claim.receipt_id},
                )
                await session.commit()
        except FulfillmentConflictError:
            raise
        except (DBAPIError, SQLAlchemyError, ConnectionError, OSError, TimeoutError) as error:
            raise StorageUnavailableError("fulfillment storage is unavailable") from error

    async def _execute_in_session(
        self,
        session: AsyncSession,
        request: CommandRequest,
        *,
        observation: Observation | None,
    ) -> CommandReceipt:
        """Run one command while retaining the caller's transaction."""

        group = request.group
        fingerprint = request.fingerprint

        # The initial lookup is deliberately before observation, config, clock,
        # random, or projector work.  The second lookup after the scope lock
        # closes the same-key race without creating a second effect.
        existing = await self._load_receipt(
            session,
            app_id=request.app_id,
            group_openid=request.group_openid,
            inbound_msg_id=request.inbound_msg_id,
            for_update=False,
        )
        if existing is not None:
            return self._replay_or_conflict(existing, fingerprint)
        await lock_group_scope(
            session,
            app_id=request.app_id,
            group_openid=request.group_openid,
        )
        existing = await self._load_receipt(
            session,
            app_id=request.app_id,
            group_openid=request.group_openid,
            inbound_msg_id=request.inbound_msg_id,
            for_update=True,
        )
        if existing is not None:
            return self._replay_or_conflict(existing, fingerprint)

        if request.command.intent == "syntax_failure":
            code = request.command.syntax_code or "invalid_syntax"
            projection = self._project_reply(
                request,
                result_code=code,
                snapshot=None,
                details={},
            )
            return await self._insert_receipt(
                session,
                request,
                fingerprint=fingerprint,
                result_code=code,
                game_id=None,
                state_revision=None,
                turn_seq=None,
                reply=projection,
            )

        binding = BindingTransaction(session)
        member_record = None
        if request.command.intent in {"create", "join"}:
            member_record = await binding.resolve_member(
                app_id=request.app_id,
                group_openid=request.group_openid,
                member_openid=request.member_openid,
            )
            if member_record is None or member_record.character_name is None:
                return await self._insert_failure_receipt(
                    session,
                    request,
                    fingerprint=fingerprint,
                    result_code="binding_required",
                    snapshot=None,
                    details={},
                )

        storage = PostgresRouletteStorage(session)
        try:
            current = await storage.load_current(group, for_update=True)
        except AggregateCorruptError:
            now = await self._pg_now(session)
            failed = await storage.fail_corrupt_current(group, ended_at=now)
            if failed is None:
                raise StorageUnavailableError("corrupt game disappeared")
            return await self._insert_failure_receipt(
                session,
                request,
                fingerprint=fingerprint,
                result_code="aggregate_corrupt",
                snapshot=None,
                game_id=failed.game_id,
                state_revision=failed.terminal_revision,
                details={},
            )

        now = await self._pg_now(session)
        if (
            request.command.intent == "join"
            and current is not None
            and member_record is not None
            and member_record.character_name is not None
            and any(
                seat.member_openid != request.member_openid
                and _name_key(seat.display_name)
                == _name_key(member_record.character_name)
                for seat in current.players
            )
        ):
            return await self._insert_failure_receipt(
                session,
                request,
                fingerprint=fingerprint,
                result_code="character_name_taken",
                snapshot=current,
                details={},
            )

        requires_observation = (
            current is not None
            and current.lifecycle == "active"
            and current.phase in {"first_shot", "follow_up", "locked_turn", "item_choice"}
            and request.command.intent in OBSERVED_ACTIVE_WRITES
        )
        if requires_observation and not _observation_matches(current, observation):
            return await self._insert_failure_receipt(
                session,
                request,
                fingerprint=fingerprint,
                result_code="state_conflict",
                snapshot=current,
                details={},
            )

        winner_group_wins: int | None = None
        if request.command.intent == "leaderboard":
            if current is not None and current.deadline is not None and now >= current.deadline:
                expiring_seq = current.current_player_seq
                expiry_result = apply_action(
                    current.state,
                    Action.expire(),
                    now=now,
                    random_source=self._random_source,
                )
                if expiry_result.state != current.state:
                    current, winner_group_wins = await self._persist_result(
                        session,
                        storage,
                        current,
                        expiry_result,
                        now=now,
                    )
                    # TSK-266 10.2: a leaderboard query that lazily advanced the
                    # deadline reports the timeout it caused to the very player
                    # it eliminated; the leaderboard is never appended there.
                    if _member_for_seq(current, expiring_seq) == request.member_openid:
                        return await self._insert_failure_receipt(
                            session,
                            request,
                            fingerprint=fingerprint,
                            result_code=expiry_result.code,
                            snapshot=current,
                            details=expiry_result.reply,
                            winner_group_wins=winner_group_wins,
                        )
            entries = await storage.list_leaderboard(group)
            details = {"leaderboard": _leaderboard_values(entries)}
            return await self._insert_failure_receipt(
                session,
                request,
                fingerprint=fingerprint,
                result_code="leaderboard",
                snapshot=current,
                details=details,
                winner_group_wins=winner_group_wins,
            )

        state = current.state if current is not None else initial_state(group)
        player = self._player_for_request(
            request,
            current=current,
            binding_record=member_record,
        )
        action = request.command.to_action(player)
        result = apply_action(
            state,
            action,
            now=now,
            random_source=self._random_source,
        )
        if result.code == "invalid_game_state" and current is not None:
            failed = await storage.fail_corrupt_current(group, ended_at=now)
            if failed is None:
                raise StorageUnavailableError("corrupt game disappeared")
            return await self._insert_failure_receipt(
                session,
                request,
                fingerprint=fingerprint,
                result_code="aggregate_corrupt",
                snapshot=None,
                game_id=failed.game_id,
                state_revision=failed.terminal_revision,
                details={},
            )

        saved = current
        if current is None and result.state != state:
            if result.state.lifecycle == "waiting" and result.code == "created":
                saved = await storage.create_waiting(
                    game_state_to_snapshot(result.state, game_id=str(uuid4()))
                )
        elif current is not None and result.state != state:
            saved, winner_group_wins = await self._persist_result(
                session,
                storage,
                current,
                result,
                now=now,
            )

        projection = self._project_reply(
            request,
            result_code=result.code,
            snapshot=saved,
            details=_waiting_end_details(result, request, current),
            winner_group_wins=winner_group_wins,
        )
        return await self._insert_receipt(
            session,
            request,
            fingerprint=fingerprint,
            result_code=result.code,
            game_id=saved.game_id if saved is not None else None,
            state_revision=saved.state_revision if saved is not None else result.state.state_revision,
            turn_seq=saved.turn_seq if saved is not None else result.state.turn_seq,
            reply=projection,
        )

    async def _persist_result(
        self,
        session: AsyncSession,
        storage: PostgresRouletteStorage,
        current: GameSnapshot,
        result: ActionResult,
        *,
        now: datetime,
    ) -> tuple[GameSnapshot, int | None]:
        transition = transition_from_action_result(
            current,
            result,
            action_kind=result.code,
            occurred_at=now,
        )
        saved = await storage.save_transition(
            transition,
            expected_revision=current.state_revision,
        )
        winner_group_wins: int | None = None
        if saved.lifecycle in {"completed", "cancelled", "expired"}:
            raw_reason = result.reply.get("completion_reason") or result.code
            reason = str(raw_reason)
            winner_value = result.reply.get("winner_seq")
            winner_seq = winner_value if isinstance(winner_value, int) else None
            winner_group_wins = await storage.project_terminal_with_wins(
                TerminalProjection.from_state(
                    saved,
                    lifecycle=saved.lifecycle,
                    reason=reason,
                    ended_at=now,
                    winner_seq=winner_seq,
                )
            )
        del session
        return saved, winner_group_wins

    async def _insert_failure_receipt(
        self,
        session: AsyncSession,
        request: CommandRequest,
        *,
        fingerprint: Mapping[str, object],
        result_code: str,
        snapshot: GameSnapshot | None,
        details: Mapping[str, object],
        game_id: str | None = None,
        state_revision: int | None = None,
        turn_seq: int | None = None,
        winner_group_wins: int | None = None,
    ) -> CommandReceipt:
        projection = self._project_reply(
            request,
            result_code=result_code,
            snapshot=snapshot,
            details=details,
            winner_group_wins=winner_group_wins,
        )
        return await self._insert_receipt(
            session,
            request,
            fingerprint=fingerprint,
            result_code=result_code,
            game_id=game_id if game_id is not None else (snapshot.game_id if snapshot else None),
            state_revision=(
                state_revision
                if state_revision is not None
                else (snapshot.state_revision if snapshot else None)
            ),
            turn_seq=(turn_seq if turn_seq is not None else (snapshot.turn_seq if snapshot else None)),
            reply=projection,
        )

    def _project_reply(
        self,
        request: CommandRequest,
        *,
        result_code: str,
        snapshot: GameSnapshot | None,
        details: Mapping[str, object],
        winner_group_wins: int | None = None,
    ) -> ReplyProjection:
        safe_details = _safe_details(details)
        players = _reply_players(snapshot)
        current_player = (
            _reply_player(players, snapshot.current_player_seq)
            if snapshot is not None
            else None
        )
        winner = _reply_winner(snapshot, players)
        target_player = (
            _reply_player(players, request.command.target_player_seq)
            if snapshot is not None
            else None
        )
        reward_player = _reply_reward_player(snapshot, result_code, players)
        lock_target_player = _reply_lock_target(
            snapshot,
            result_code,
            safe_details,
            players,
        )
        context = ReplyProjectionContext(
            result_code=result_code,
            game_id=snapshot.game_id if snapshot is not None else None,
            lifecycle=snapshot.lifecycle if snapshot is not None else None,
            phase=snapshot.phase if snapshot is not None else None,
            state_revision=snapshot.state_revision if snapshot is not None else None,
            turn_seq=snapshot.turn_seq if snapshot is not None else None,
            actor_member_openid=request.member_openid,
            target_mention_count=request.target_mention_count,
            details=safe_details,
            players=players,
            current_player=current_player,
            winner=winner,
            winner_group_wins=winner_group_wins,
            target_player=target_player,
            target_member_openid=(
                _member_for_seq(snapshot, request.command.target_player_seq)
                if snapshot is not None
                else None
            ),
            reward_player=reward_player,
            lock_target_player=lock_target_player,
            mention_target=_reply_mention_target(
                request,
                result_code=result_code,
                snapshot=snapshot,
                current_player=current_player,
                winner=winner,
                reward_player=reward_player,
                lock_target_player=lock_target_player,
            ),
            mention_reason=_reply_mention_reason(
                request,
                result_code=result_code,
                snapshot=snapshot,
                current_player=current_player,
                winner=winner,
                reward_player=reward_player,
                lock_target_player=lock_target_player,
            ),
            game_view=_reply_game_view(snapshot, players),
            intent=request.command.intent,
        )
        projection = self._reply_projector(context)
        if not isinstance(projection, ReplyProjection):
            raise TypeError("reply projector must return ReplyProjection")
        return projection

    @staticmethod
    def _player_for_request(
        request: CommandRequest,
        *,
        current: GameSnapshot | None,
        binding_record: GroupBindingRecord | None,
    ) -> PlayerRef:
        display_name: str | None = None
        if binding_record is not None:
            display_name = binding_record.character_name
        if current is not None:
            for seat in current.players:
                if seat.member_openid == request.member_openid:
                    display_name = seat.display_name
                    break
        return PlayerRef(
            app_id=request.app_id,
            group_openid=request.group_openid,
            member_openid=request.member_openid,
            display_name=display_name or request.member_openid,
        )

    @staticmethod
    async def _load_receipt(
        session: AsyncSession,
        *,
        app_id: str,
        group_openid: str,
        inbound_msg_id: str,
        for_update: bool,
    ) -> CommandReceipt | None:
        statement = text(
            "SELECT receipt_id, app_id, group_openid, inbound_msg_id, "
            "fingerprint, result_code, game_id, state_revision, turn_seq, "
            "reply_projection FROM komari_roulette_command_receipts "
            "WHERE app_id = :app_id AND group_openid = :group_openid "
            "AND inbound_msg_id = :inbound_msg_id"
            + (" FOR UPDATE" if for_update else "")
        )
        row = (
            await session.execute(
                statement,
                {
                    "app_id": app_id,
                    "group_openid": group_openid,
                    "inbound_msg_id": inbound_msg_id,
                },
            )
        ).mappings().one_or_none()
        return (
            _receipt_from_row(cast("Mapping[str, object]", row))
            if row is not None
            else None
        )

    @staticmethod
    async def _fulfillment_row(
        session: AsyncSession,
        receipt_id: str,
        *,
        for_update: bool,
    ) -> Mapping[str, object] | None:
        row = (
            await session.execute(
                text(
                    "SELECT receipt_id, state, platform_message_id "
                    "FROM komari_roulette_fulfillments WHERE receipt_id = :receipt_id"
                    + (" FOR UPDATE" if for_update else "")
                ),
                {"receipt_id": receipt_id},
            )
        ).mappings().one_or_none()
        return cast("Mapping[str, object] | None", row)

    @staticmethod
    async def _fulfillment_window_row(
        session: AsyncSession,
        receipt_id: str,
    ) -> Mapping[str, object] | None:
        """Lock the fulfillment row, then read its credential age from the DB clock.

        The lock and the age read are deliberately *two* statements in one
        transaction.  PostgreSQL evaluates a ``LockRows`` node's target list
        below the lock, so computing ``clock_timestamp() - r.created_at`` in the
        locking statement would freeze the age at statement start: a claim that
        blocked for seconds on another connection's row lock would still see the
        pre-wait age and could admit an already-expired credential (TSK-278).

        The lock is taken by a statement whose target list only reads columns,
        and the age is then read by a second statement that is sent strictly
        after the lock is held, against the same
        ``komari_roulette_command_receipts.created_at`` column.  A missing
        fulfillment row returns ``None`` before any age is read, so callers
        keep their existing missing-row handling.
        """

        locked = (
            await session.execute(
                text(
                    "SELECT receipt_id, state "
                    "FROM komari_roulette_fulfillments "
                    "WHERE receipt_id = :receipt_id "
                    "FOR UPDATE"
                ),
                {"receipt_id": receipt_id},
            )
        ).mappings().one_or_none()
        if locked is None:
            return None
        age_seconds = await session.scalar(
            text(
                "SELECT EXTRACT(EPOCH FROM (clock_timestamp() - created_at)) "
                "FROM komari_roulette_command_receipts "
                "WHERE receipt_id = :receipt_id"
            ),
            {"receipt_id": receipt_id},
        )
        if age_seconds is None:
            return None
        return {
            "receipt_id": locked["receipt_id"],
            "state": locked["state"],
            "age_seconds": age_seconds,
        }

    async def _insert_receipt(
        self,
        session: AsyncSession,
        request: CommandRequest,
        *,
        fingerprint: Mapping[str, object],
        result_code: str,
        game_id: str | None,
        state_revision: int | None,
        turn_seq: int | None,
        reply: ReplyProjection,
    ) -> CommandReceipt:
        receipt_id = str(uuid4())
        projection_json = json.dumps(
            {"body": reply.body, "metadata": dict(reply.metadata)},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        fingerprint_json = json.dumps(
            dict(fingerprint),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        await session.execute(
            text(
                "INSERT INTO komari_roulette_command_receipts "
                "(receipt_id, app_id, group_openid, inbound_msg_id, fingerprint, "
                "result_code, game_id, state_revision, turn_seq, reply_projection) "
                "VALUES (:receipt_id, :app_id, :group_openid, :inbound_msg_id, "
                "CAST(:fingerprint AS JSONB), :result_code, :game_id, "
                ":state_revision, :turn_seq, CAST(:reply_projection AS JSONB))"
            ),
            {
                "receipt_id": receipt_id,
                "app_id": request.app_id,
                "group_openid": request.group_openid,
                "inbound_msg_id": request.inbound_msg_id,
                "fingerprint": fingerprint_json,
                "result_code": result_code,
                "game_id": game_id,
                "state_revision": state_revision,
                "turn_seq": turn_seq,
                "reply_projection": projection_json,
            },
        )
        await session.execute(
            text(
                "INSERT INTO komari_roulette_fulfillments "
                "(receipt_id, state, platform_message_id) "
                "VALUES (:receipt_id, 'NOT_STARTED', NULL)"
            ),
            {"receipt_id": receipt_id},
        )
        await session.flush()
        return CommandReceipt(
            receipt_id=receipt_id,
            app_id=request.app_id,
            group_openid=request.group_openid,
            inbound_msg_id=request.inbound_msg_id,
            fingerprint=dict(fingerprint),
            result_code=result_code,
            game_id=game_id,
            state_revision=state_revision,
            turn_seq=turn_seq,
            reply=reply,
        )

    @staticmethod
    def _replay_or_conflict(
        receipt: CommandReceipt,
        fingerprint: Mapping[str, object],
    ) -> CommandReceipt:
        if dict(receipt.fingerprint) != dict(fingerprint):
            raise IdempotencyKeyConflictError("inbound message key payload conflicts")
        return receipt

    @staticmethod
    async def _pg_now(session: AsyncSession) -> datetime:
        value = await session.scalar(text("SELECT clock_timestamp()"))
        if not isinstance(value, datetime):
            raise StorageUnavailableError("database clock is unavailable")
        return value


def _receipt_from_row(row: Mapping[str, object]) -> CommandReceipt:
    raw_projection = row["reply_projection"]
    if isinstance(raw_projection, str):
        raw_projection = json.loads(raw_projection)
    projection = cast("Mapping[str, object]", raw_projection)
    metadata = cast("Mapping[str, Scalar]", projection.get("metadata", {}))
    return CommandReceipt(
        receipt_id=str(row["receipt_id"]),
        app_id=str(row["app_id"]),
        group_openid=str(row["group_openid"]),
        inbound_msg_id=str(row["inbound_msg_id"]),
        fingerprint=cast("Mapping[str, object]", _json_value(row["fingerprint"])),
        result_code=str(row["result_code"]),
        game_id=str(row["game_id"]) if row["game_id"] is not None else None,
        state_revision=(
            _as_int(row["state_revision"])
            if row["state_revision"] is not None
            else None
        ),
        turn_seq=(
            _as_int(row["turn_seq"]) if row["turn_seq"] is not None else None
        ),
        reply=ReplyProjection(
            body=str(projection.get("body", "")),
            metadata=metadata,
        ),
    )


def _json_value(value: object) -> object:
    if isinstance(value, str):
        return json.loads(value)
    return value


def _as_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise TypeError("receipt revision must be an integer")
    return int(value)


def _observation_matches(
    snapshot: GameSnapshot | None,
    observation: Observation | None,
) -> bool:
    if snapshot is None or observation is None:
        return False
    return (
        snapshot.game_id == observation.game_id
        and snapshot.state_revision == observation.state_revision
        and snapshot.turn_seq == observation.turn_seq
    )


def _name_key(name: str) -> str:
    import unicodedata

    return unicodedata.normalize("NFKC", name).casefold()


def _projection_details(result: ActionResult) -> dict[str, object]:
    """Forward the safe reply payload plus the fixed domain reason code.

    TSK-278 renders reason-specific fixed errors (for example
    ``item_precondition_failed`` + ``burst_requires_two_rounds``); the reason is
    a closed domain identifier, never user input.
    """

    details: dict[str, object] = dict(result.reply)
    if result.reason is not None:
        details["reason"] = result.reason
    return details


def _waiting_end_details(
    result: ActionResult,
    request: CommandRequest,
    current: GameSnapshot | None,
) -> dict[str, object]:
    """Add the TSK-266 1G waiting-end actor facts for a cancelled game.

    A cancel commits an empty roster, so the post-transition snapshot can no
    longer resolve who ended the game: the actor name is captured from the
    pre-transition snapshot while it is still available.
    """

    details = _projection_details(result)
    if result.code != "cancelled":
        return details
    if request.command.intent == "cancel":
        details["waiting_end_reason"] = "host_cancelled"
    elif request.command.intent == "leave":
        details["waiting_end_reason"] = "last_player_left"
    else:
        return details
    if current is not None:
        for seat in current.players:
            if seat.member_openid == request.member_openid:
                details["waiting_end_actor_name"] = seat.display_name
                break
    return details


def _safe_details(details: Mapping[str, object]) -> dict[str, PublicValue]:
    safe: dict[str, PublicValue] = {}
    allowed = {
        "reason",
        "consumed_kind",
        "remaining_live",
        "remaining_blank",
        "auto_reloaded",
        "reload_reason",
        "reward_count",
        "rewards",
        "pending_item",
        "pending_item_count",
        "decision",
        "item",
        "observed_kind",
        "observation_chamber_revision",
        "target_seq",
        "before_live",
        "before_blank",
        "after_live",
        "after_blank",
        "turn_ends",
        "information_invalidated",
        "inventory",
        "completion_reason",
        "winner_seq",
        "eliminated_reason",
        "leaderboard",
        "waiting_end_reason",
        "waiting_end_actor_name",
    }
    for key, value in details.items():
        if key not in allowed:
            continue
        if isinstance(value, (str, int, bool)) or value is None:
            safe[key] = value
        elif isinstance(value, Mapping):
            safe[key] = tuple(
                f"{item}:{count}"
                for item, count in sorted(value.items(), key=lambda pair: str(pair[0]))
                if isinstance(item, str) and isinstance(count, int)
            )
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            values = tuple(str(item) for item in value if isinstance(item, (str, int)))
            safe[key] = values
    return safe


def _leaderboard_values(entries: Sequence[LeaderboardEntry]) -> tuple[str, ...]:
    return tuple(f"{entry.display_name}:{entry.wins}" for entry in entries)


def _reply_players(snapshot: GameSnapshot | None) -> tuple[ReplyPlayer, ...]:
    """Copy only the public roster facts into the projector context."""

    if snapshot is None:
        return ()
    pending_locks = frozenset(snapshot.pending_locks)
    return tuple(
        ReplyPlayer(
            join_seq=seat.join_seq,
            display_name=seat.display_name,
            alive=seat.alive,
            inventory_counts=tuple(
                sorted(
                    (item.value, count)
                    for item, count in seat.inventory.items()
                    if isinstance(item, ItemType) and isinstance(count, int) and count > 0
                )
            ),
            member_openid=seat.member_openid,
            pending_lock=seat.join_seq in pending_locks,
        )
        for seat in snapshot.players
    )


def _reply_player(
    players: Sequence[ReplyPlayer],
    join_seq: int | None,
) -> ReplyPlayer | None:
    if join_seq is None:
        return None
    for player in players:
        if player.join_seq == join_seq:
            return player
    return None


def _reply_winner(
    snapshot: GameSnapshot | None,
    players: Sequence[ReplyPlayer],
) -> ReplyPlayer | None:
    if snapshot is None or snapshot.lifecycle != "completed":
        return None
    alive_players = tuple(player for player in players if player.alive)
    return alive_players[0] if len(alive_players) == 1 else None


def _reply_game_view(
    snapshot: GameSnapshot | None,
    players: Sequence[ReplyPlayer],
) -> ReplyGameView | None:
    if snapshot is None:
        return None
    remaining_total = len(snapshot.ordered_chamber)
    remaining_live = snapshot.ordered_chamber.count(ChamberKind.LIVE)
    remaining_blank = snapshot.ordered_chamber.count(ChamberKind.BLANK)
    probability = (
        round(remaining_live * 100 / remaining_total, 1)
        if remaining_total
        else None
    )
    pending_lock_player_seqs = tuple(snapshot.pending_locks)
    pending_lock_players = tuple(
        player for player in players if player.join_seq in pending_lock_player_seqs
    )
    active_lock_player = (
        _reply_player(players, snapshot.current_player_seq)
        if snapshot.phase == "locked_turn"
        else None
    )
    return ReplyGameView(
        host_seq=snapshot.host_seq,
        chamber_remaining_total=remaining_total,
        chamber_remaining_live=remaining_live,
        chamber_remaining_blank=remaining_blank,
        hit_probability_percent=probability,
        pending_reward_count=len(snapshot.pending_rewards),
        pending_burst=snapshot.pending_burst,
        pending_lock_player_seqs=pending_lock_player_seqs,
        pending_lock_players=pending_lock_players,
        active_lock_player=active_lock_player,
    )


def _reply_reward_player(
    snapshot: GameSnapshot | None,
    result_code: str,
    players: Sequence[ReplyPlayer],
) -> ReplyPlayer | None:
    if (
        snapshot is None
        or snapshot.phase != "item_choice"
        or result_code not in {"item_choice_pending", "item_choice_updated"}
    ):
        return None
    return _reply_player(players, snapshot.current_player_seq)


def _reply_lock_target(
    snapshot: GameSnapshot | None,
    result_code: str,
    details: Mapping[str, PublicValue],
    players: Sequence[ReplyPlayer],
) -> ReplyPlayer | None:
    if snapshot is None or result_code != "item_used" or details.get("item") != ItemType.LOCK.value:
        return None
    target_seq = details.get("target_seq")
    if isinstance(target_seq, bool) or not isinstance(target_seq, int):
        return None
    return _reply_player(players, target_seq)


#: Rotation-family result codes: the reply names the *new* current player, so
#: the outbound reminder follows the rotated seat (a timeout that eliminated the
#: old current player triggers the same rotation mention).
_ROTATION_MENTION_RESULT_CODES: frozenset[str] = frozenset(
    {"started", "shot", "forfeited", "reloaded", "turn_ended", "turn_expired"}
)


def _reply_mention_target(
    request: CommandRequest,
    *,
    result_code: str,
    snapshot: GameSnapshot | None,
    current_player: ReplyPlayer | None,
    winner: ReplyPlayer | None,
    reward_player: ReplyPlayer | None,
    lock_target_player: ReplyPlayer | None,
) -> ReplyPlayer | None:
    """Select the single outbound reminder target for downstream sending."""

    if snapshot is None:
        return None
    actor_seq = _member_seq(snapshot, request.member_openid)
    if (
        current_player is not None
        and current_player.join_seq != actor_seq
        and result_code in _ROTATION_MENTION_RESULT_CODES
    ):
        return current_player
    if reward_player is not None:
        return reward_player
    if winner is not None:
        return winner
    return lock_target_player


def _reply_mention_reason(
    request: CommandRequest,
    *,
    result_code: str,
    snapshot: GameSnapshot | None,
    current_player: ReplyPlayer | None,
    winner: ReplyPlayer | None,
    reward_player: ReplyPlayer | None,
    lock_target_player: ReplyPlayer | None,
) -> str | None:
    target = _reply_mention_target(
        request,
        result_code=result_code,
        snapshot=snapshot,
        current_player=current_player,
        winner=winner,
        reward_player=reward_player,
        lock_target_player=lock_target_player,
    )
    if target is None:
        return None
    actor_seq = _member_seq(snapshot, request.member_openid) if snapshot else None
    if (
        target is current_player
        and target.join_seq != actor_seq
        and result_code in _ROTATION_MENTION_RESULT_CODES
    ):
        return "turn"
    if target is reward_player:
        return "reward"
    if target is winner:
        return "winner"
    if target is lock_target_player:
        return "lock_target"
    return None


def _member_seq(snapshot: GameSnapshot | None, member_openid: str) -> int | None:
    if snapshot is None:
        return None
    for seat in snapshot.players:
        if seat.member_openid == member_openid:
            return seat.join_seq
    return None


def _member_for_seq(snapshot: GameSnapshot | None, join_seq: int | None) -> str | None:
    if snapshot is None or join_seq is None:
        return None
    for seat in snapshot.players:
        if seat.join_seq == join_seq:
            return seat.member_openid
    return None


__all__ = [
    "FULFILLMENT_CREDENTIAL_WINDOW_SECONDS",
    "CanonicalCommand",
    "CommandReceipt",
    "CommandRequest",
    "CommitOutcomeUnknownError",
    "ExpiryAdvance",
    "FulfillmentClaim",
    "FulfillmentConflictError",
    "FulfillmentState",
    "IdempotencyKeyConflictError",
    "Observation",
    "ReplyGameView",
    "ReplyPlayer",
    "ReplyProjection",
    "ReplyProjectionContext",
    "RouletteCommandService",
    "StateConflictError",
]
