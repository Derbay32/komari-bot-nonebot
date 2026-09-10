"""Public domain and PostgreSQL seams for the QQ Russian roulette game.

The persistence imports register SQLModel metadata only; they do not open a
connection or execute DDL.  Database/session ownership remains with callers.
"""

from nonebot.plugin import PluginMetadata, require

require("character_binding")
require("config_manager")
require("nonebot_plugin_apscheduler")
# The QQ adapter layer consumes the group-admission handoff token written into
# the event state; declare the dependency explicitly and import its public
# top-level surface only.
require("group_admission")

# Importing the QQ subpackage registers its group-@ matcher.  The matcher stays
# inert until the composition root installs the runtime, so importing it has no
# side effect on a deployment where the roulette plugin is disabled.
# Importing the lifecycle module registers exactly one driver startup / shutdown
# hook; it is the single production composition root (TSK-279).
from . import (
    lifecycle,  # noqa: F401
    qq,  # noqa: F401
)
from .help_copy import help_usage

__plugin_meta__ = PluginMetadata(
    name="俄罗斯轮盘",
    description="QQ 群俄罗斯轮盘小游戏",
    usage=help_usage(),
)

from .command_service import (
    CanonicalCommand,
    CommandReceipt,
    CommandRequest,
    CommitOutcomeUnknownError,
    EffectCheckRejectedError,
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
    "EffectCheckRejectedError",
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
