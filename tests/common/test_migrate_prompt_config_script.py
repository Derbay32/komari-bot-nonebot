"""Prompt YAML 迁移脚本测试。"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

from komari_bot.common import prompt_storage

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = PROJECT_ROOT / "scripts/migrate_prompt_config_to_pg.py"


def _load_script_module() -> Any:
    spec = importlib.util.spec_from_file_location(
        "tests.scripts.migrate_prompt_config_to_pg",
        SCRIPT_PATH,
    )
    if spec is None or spec.loader is None:
        raise AssertionError

    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _FakePromptStorage:
    def __init__(self) -> None:
        self.upserts: list[tuple[str, dict[str, str]]] = []

    def upsert(
        self,
        *,
        resource_id: str,
        display_name: str,
        prompt_data: dict[str, str],
        version: str = "1.0",
    ) -> object:
        del display_name, version
        self.upserts.append((resource_id, prompt_data))
        return object()


def test_migrate_prompts_dry_run_skips_missing_yaml(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    module = _load_script_module()
    resource = module.PromptMigrationResource(
        resource_id="komari_chat",
        display_name="Komari Chat Prompt",
        legacy_file_path=tmp_path / "missing.yaml",
        defaults={"system_prompt": "默认"},
    )
    monkeypatch.setattr(module, "_RESOURCES", (resource,))

    stats = module.migrate_prompts(dry_run=True)

    assert stats == {"success": 0, "skipped": 1, "failed": 0}


def test_migrate_prompts_imports_only_supported_string_fields(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    module = _load_script_module()
    prompt_file = tmp_path / "prompt.yaml"
    prompt_file.write_text(
        "system_prompt: 新系统提示词\nunknown: 忽略\nmemory_ack: 123\n",
        encoding="utf-8",
    )
    resource = module.PromptMigrationResource(
        resource_id="komari_chat",
        display_name="Komari Chat Prompt",
        legacy_file_path=prompt_file,
        defaults={"system_prompt": "默认", "memory_ack": "默认确认"},
    )
    fake_storage = _FakePromptStorage()
    monkeypatch.setattr(module, "_RESOURCES", (resource,))
    monkeypatch.setattr(prompt_storage, "get_prompt_storage", lambda: fake_storage)

    stats = module.migrate_prompts(dry_run=False)

    assert stats == {"success": 1, "skipped": 0, "failed": 0}
    assert fake_storage.upserts == [
        (
            "komari_chat",
            {"system_prompt": "新系统提示词", "memory_ack": "默认确认"},
        )
    ]
