"""QQ adapter seams for the Russian roulette plugin (TSK-278).

This phase ships the parser, keyboard, renderer and help-copy seams; the
handler and delivery seams land in the following delivery phase.
"""

from .parser import parse_command

__all__ = ["parse_command"]
