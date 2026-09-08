"""Public domain and PostgreSQL seams for the QQ Russian roulette game.

The persistence imports register SQLModel metadata only; they do not open a
connection or execute DDL.  Database/session ownership remains with callers.
"""

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
    "ChamberKind",
    "EliminationRecord",
    "GameSnapshot",
    "GameState",
    "GroupRef",
    "ItemType",
    "LeaderboardEntry",
    "PlayerRef",
    "PostgresRouletteStorage",
    "ResultPlayer",
    "RevisionConflictError",
    "RouletteResult",
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
