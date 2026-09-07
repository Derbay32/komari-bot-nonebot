"""Pure domain primitives for the QQ Russian roulette game.

The command and persistence adapters are deliberately kept outside this
package.  Importing the package therefore has no NoneBot or database side
effects.
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

__all__ = [
    "Action",
    "ActionResult",
    "ChamberKind",
    "GameState",
    "GroupRef",
    "ItemType",
    "PlayerRef",
    "apply_action",
    "initial_state",
]
