"""TSK-276 cutover guard for the single 0020 migration head."""

from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_tsk276_adds_one_head_after_0019_without_parallel_branch() -> None:
    config = Config()
    config.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    config.set_main_option("version_path_separator", "os")
    script = ScriptDirectory.from_config(config)
    assert script.get_heads() == ["0020"]
    assert script.get_revision("0019").down_revision == "0018"  # type: ignore[union-attr]
    assert script.get_revision("0020").down_revision == "0019"  # type: ignore[union-attr]
