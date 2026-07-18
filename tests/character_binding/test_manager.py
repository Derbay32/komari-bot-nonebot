"""角色绑定管理器的原子持久化与输入约束测试。"""

from __future__ import annotations

import asyncio
import json
import os
from typing import TYPE_CHECKING

import pytest

from komari_bot.plugins.character_binding import manager as manager_module
from komari_bot.plugins.character_binding.manager import (
    DEFAULT_BINDING_FILE,
    MAX_CHARACTER_NAME_LENGTH,
    BindingPersistenceError,
    CharacterBindingManager,
    CharacterNameValidationError,
)

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def binding_file(tmp_path: Path) -> Path:
    return tmp_path / "character_binding" / "bindings.json"


def test_default_binding_path_is_independent_of_working_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    temporary_default = tmp_path / "app-data" / "character_binding" / "bindings.json"
    monkeypatch.setattr(manager_module, "DEFAULT_BINDING_FILE", temporary_default)
    monkeypatch.chdir(tmp_path)

    manager = CharacterBindingManager()

    assert DEFAULT_BINDING_FILE.is_absolute()
    assert manager.binding_file == temporary_default
    assert manager.binding_file.is_absolute()


@pytest.mark.asyncio
async def test_set_uses_fsync_and_atomic_replace(
    binding_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = CharacterBindingManager(binding_file)
    real_fsync = os.fsync
    real_replace = os.replace
    fsync_calls = 0
    replace_calls: list[tuple[Path, Path]] = []

    def tracked_fsync(file_descriptor: int) -> None:
        nonlocal fsync_calls
        fsync_calls += 1
        real_fsync(file_descriptor)

    def tracked_replace(source: Path, target: Path) -> None:
        replace_calls.append((source, target))
        real_replace(source, target)

    monkeypatch.setattr(manager_module.os, "fsync", tracked_fsync)
    monkeypatch.setattr(manager_module.os, "replace", tracked_replace)

    await manager.set_character_name("42", "泉此方")

    assert fsync_calls == 1
    assert len(replace_calls) == 1
    assert replace_calls[0][1] == binding_file
    assert json.loads(binding_file.read_text(encoding="utf-8")) == {"42": "泉此方"}
    assert list(binding_file.parent.glob(".bindings.json.*.tmp")) == []


@pytest.mark.asyncio
async def test_set_failure_keeps_memory_and_file_unchanged(
    binding_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = CharacterBindingManager(binding_file)
    await manager.set_character_name("42", "泉此方")
    original_content = binding_file.read_text(encoding="utf-8")

    def fail_replace(_source: Path, _target: Path) -> None:
        raise OSError("模拟原子替换失败")

    monkeypatch.setattr(manager_module.os, "replace", fail_replace)

    with pytest.raises(BindingPersistenceError, match="角色绑定保存失败"):
        await manager.set_character_name("10086", "柊镜")

    assert manager.list_bindings() == {"42": "泉此方"}
    assert binding_file.read_text(encoding="utf-8") == original_content
    assert list(binding_file.parent.glob(".bindings.json.*.tmp")) == []


@pytest.mark.asyncio
async def test_remove_failure_keeps_existing_binding(
    binding_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = CharacterBindingManager(binding_file)
    await manager.set_character_name("42", "泉此方")

    def fail_replace(_source: Path, _target: Path) -> None:
        raise OSError("模拟原子替换失败")

    monkeypatch.setattr(manager_module.os, "replace", fail_replace)

    with pytest.raises(BindingPersistenceError, match="角色绑定保存失败"):
        await manager.remove_character_name("42")

    assert manager.get_character_name("42") == "泉此方"
    assert json.loads(binding_file.read_text(encoding="utf-8")) == {"42": "泉此方"}


@pytest.mark.asyncio
async def test_concurrent_updates_are_serialized(binding_file: Path) -> None:
    manager = CharacterBindingManager(binding_file)

    await asyncio.gather(
        manager.set_character_name("42", "泉此方"),
        manager.set_character_name("10086", "柊镜"),
    )

    assert manager.list_bindings() == {"42": "泉此方", "10086": "柊镜"}
    assert json.loads(binding_file.read_text(encoding="utf-8")) == {
        "42": "泉此方",
        "10086": "柊镜",
    }


@pytest.mark.asyncio
async def test_two_managers_reload_under_file_lock_without_lost_update(
    binding_file: Path,
) -> None:
    first = CharacterBindingManager(binding_file, refresh_interval_seconds=0)
    second = CharacterBindingManager(binding_file, refresh_interval_seconds=0)

    await asyncio.gather(
        first.set_character_name("42", "泉此方"),
        second.set_character_name("10086", "柊镜"),
    )

    expected = {"42": "泉此方", "10086": "柊镜"}
    assert json.loads(binding_file.read_text(encoding="utf-8")) == expected
    assert first.list_bindings() == expected
    assert second.list_bindings() == expected


@pytest.mark.asyncio
async def test_regular_reads_refresh_external_updates_with_bounded_interval(
    binding_file: Path,
) -> None:
    writer = CharacterBindingManager(binding_file, refresh_interval_seconds=0)
    reader = CharacterBindingManager(binding_file, refresh_interval_seconds=0)

    await writer.set_character_name("42", "泉此方")

    assert reader.get_character_name("42") == "泉此方"
    assert reader.has_binding("42") is True


@pytest.mark.asyncio
@pytest.mark.parametrize("character_name", ["角色\n名", "角色\t名", "角色\u2028名"])
async def test_rejects_line_breaks_and_control_characters(
    binding_file: Path,
    character_name: str,
) -> None:
    manager = CharacterBindingManager(binding_file)

    with pytest.raises(CharacterNameValidationError, match="换行或控制字符"):
        await manager.set_character_name("42", character_name)

    assert manager.list_bindings() == {}


@pytest.mark.asyncio
async def test_rejects_name_over_unicode_length_limit(binding_file: Path) -> None:
    manager = CharacterBindingManager(binding_file)

    with pytest.raises(CharacterNameValidationError, match="不能超过"):
        await manager.set_character_name("42", "鞠" * (MAX_CHARACTER_NAME_LENGTH + 1))

    assert manager.list_bindings() == {}
