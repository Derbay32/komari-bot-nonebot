"""QQ adapter seams for the Russian roulette plugin (TSK-278).

This phase ships the parser, keyboard, renderer and help-copy seams; the
handler and delivery seams land in the following delivery phase.
"""

from .keyboard import build_keyboard, keyboard_from_spec
from .parser import parse_command
from .renderer import render_reply

__all__ = ["build_keyboard", "keyboard_from_spec", "parse_command", "render_reply"]
