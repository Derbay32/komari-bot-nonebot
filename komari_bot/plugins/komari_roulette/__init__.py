"""Public domain and PostgreSQL seams for the QQ Russian roulette game.

The persistence imports register SQLModel metadata only; they do not open a
connection or execute DDL.  Database/session ownership remains with callers.
"""

from nonebot.plugin import require

require("character_binding")

from .command_service import (
    CanonicalCommand,
    CommandReceipt,
    CommandRequest,
    CommitOutcomeUnknownError,
    ExpiryAdvance,
    FulfillmentClaim,
    FulfillmentConflictError,
    FulfillmentState,
    IdempotencyKeyConflictError,
    Observation,
    ReplyGameView,
    ReplyPlayer,
    ReplyProjection,
    ReplyProjectionContext,
    RouletteCommandService,
    StateConflictError,
)
from .domain import (
    Action,
    ActionResult,
    ChamberKind,
    GameState,
    GroupRef,
    ItemType,
    PlayerRef,
    apply_action,
    initial_state,
)
from .mapper import (
    EliminationRecord,
    GameSnapshot,
    LeaderboardEntry,
    ResultPlayer,
    RouletteResult,
    StateTransition,
    TerminalProjection,
    game_state_from_snapshot,
    game_state_to_snapshot,
    transition_from_action_result,
)
from .storage import (
    AggregateCorruptError,
    PostgresRouletteStorage,
    RevisionConflictError,
    StorageUnavailableError,
    TerminalProjectionRejectedError,
)

__all__ = [
    "Action",
    "ActionResult",
    "AggregateCorruptError",
    "CanonicalCommand",
    "ChamberKind",
    "CommandReceipt",
    "CommandRequest",
    "CommitOutcomeUnknownError",
    "EliminationRecord",
    "ExpiryAdvance",
    "FulfillmentClaim",
    "FulfillmentConflictError",
    "FulfillmentState",
    "GameSnapshot",
    "GameState",
    "GroupRef",
    "IdempotencyKeyConflictError",
    "ItemType",
    "LeaderboardEntry",
    "Observation",
    "PlayerRef",
    "PostgresRouletteStorage",
    "ReplyGameView",
    "ReplyPlayer",
    "ReplyProjection",
    "ReplyProjectionContext",
    "ResultPlayer",
    "RevisionConflictError",
    "RouletteCommandService",
    "RouletteResult",
    "StateConflictError",
    "StateTransition",
    "StorageUnavailableError",
    "TerminalProjection",
    "TerminalProjectionRejectedError",
    "apply_action",
    "game_state_from_snapshot",
    "game_state_to_snapshot",
    "initial_state",
    "transition_from_action_result",
]
