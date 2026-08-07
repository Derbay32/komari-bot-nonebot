"""Prompt YAML 迁移脚本测试。"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from typing import Any

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


class _FakePromptConnection:
    def __init__(self) -> None:
        self.execute_calls: list[tuple[str, tuple[object, ...]]] = []
        self.closed = False

    async def execute(self, query: str, *args: object) -> str:
        self.execute_calls.append((query, args))
        return "OK"

    async def close(self) -> None:
        self.closed = True


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
        default_file_name="komari_chat.yaml",
    )
    monkeypatch.setattr(module, "_RESOURCES", (resource,))

    stats = asyncio.run(
        module.migrate_prompts(
            dry_run=True,
            dotenv_path=tmp_path / ".env",
            prompt_paths=module.PromptPathConfig(
                prompt_dir=None,
                komari_chat=None,
                komari_memory_summary=None,
                group_history_summary=None,
            ),
        )
    )

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
        default_file_name="komari_chat.yaml",
    )
    fake_conn = _FakePromptConnection()

    async def _fake_connect(**_kwargs: object) -> _FakePromptConnection:
        return fake_conn

    monkeypatch.setattr(module, "_RESOURCES", (resource,))
    monkeypatch.setattr(module.asyncpg, "connect", _fake_connect)
    dotenv_path = tmp_path / ".env"
    dotenv_path.write_text(
        "SQLALCHEMY_DATABASE_URL=postgresql+asyncpg://user:pass@localhost:5432/komari_bot\n",
        encoding="utf-8",
    )

    stats = asyncio.run(
        module.migrate_prompts(
            dry_run=False,
            dotenv_path=dotenv_path,
            prompt_paths=module.PromptPathConfig(
                prompt_dir=None,
                komari_chat=None,
                komari_memory_summary=None,
                group_history_summary=None,
            ),
        )
    )

    assert stats == {"success": 1, "skipped": 0, "failed": 0}
    assert fake_conn.closed is True
    assert any("CREATE TABLE IF NOT EXISTS komari_prompt_configs" in call[0] for call in fake_conn.execute_calls)
    assert (
        "komari_chat",
        "Komari Chat Prompt",
        '{"system_prompt": "新系统提示词", "memory_ack": "默认确认"}',
        "1.0",
    ) in [call[1] for call in fake_conn.execute_calls]
