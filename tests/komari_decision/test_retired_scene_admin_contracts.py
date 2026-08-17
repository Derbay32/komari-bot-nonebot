"""场景运维收窄后的公开接口与架构契约。"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import komari_bot.plugins.komari_decision as decision_plugin
from komari_bot.plugins.komari_decision import services as decision_services
from komari_bot.plugins.komari_decision.repositories.scene_repository import (
    SceneRepository,
)
from komari_bot.plugins.komari_decision.services import scene_admin_service
from komari_bot.plugins.komari_decision.services.scene_admin_service import (
    SceneAdminService,
)
from komari_bot.plugins.komari_management import scene_api

_REQUIRED_FIXED_SCENE_KEYS = frozenset(
    {"NOISE", "MEANINGFUL", "CALL_DIRECT", "CALL_MENTION"}
)


def _required_fixed_literal_locations() -> list[str]:
    decision_dir = Path(decision_plugin.__file__).resolve().parent
    source_files = [*decision_dir.rglob("*.py"), Path(scene_api.__file__).resolve()]
    locations: list[str] = []
    for source_file in source_files:
        tree = ast.parse(source_file.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Set | ast.Tuple | ast.List):
                continue
            values = {
                element.value
                for element in node.elts
                if isinstance(element, ast.Constant) and isinstance(element.value, str)
            }
            if len(values) != len(node.elts):
                continue
            if frozenset(values) == _REQUIRED_FIXED_SCENE_KEYS:
                locations.append(f"{source_file}:{node.lineno}")
    return locations


def test_required_fixed_scenes_have_one_internal_definition() -> None:
    locations = _required_fixed_literal_locations()

    assert len(locations) == 1, f"required-fixed 定义位置应唯一，实际为: {locations}"
    assert "komari_decision" in locations[0]


def test_required_fixed_scenes_are_not_in_plugin_top_level_exports() -> None:
    exported_names = set(decision_plugin.__all__)

    assert not {
        name
        for name in exported_names
        if "required" in name.lower() and "scene" in name.lower()
    }


def test_scene_admin_constructor_requires_repository_and_sync_service() -> None:
    """TSK-179: 构造签名固定为 (repository, sync_service) 双参。"""
    assert list(inspect.signature(SceneAdminService).parameters) == [
        "repository",
        "sync_service",
    ]


def test_scene_admin_interface_has_no_retired_manual_operations() -> None:
    retired_operations = {
        "activate_ready_set",
        "rollback_to_previous_ready",
        "retry_failed_set",
    }

    assert not retired_operations.intersection(dir(SceneAdminService))


def test_scene_retry_result_model_is_removed_instead_of_reexported() -> None:
    assert not hasattr(scene_admin_service, "SceneRetryResult")
    assert not hasattr(decision_services, "SceneRetryResult")
    assert "SceneRetryResult" not in decision_services.__all__


def test_scene_repository_has_no_delete_scene_interface() -> None:
    assert not hasattr(SceneRepository, "delete_scene")


def test_scene_repository_keeps_reopen_failed_set_interface() -> None:
    assert hasattr(SceneRepository, "reopen_failed_set")
