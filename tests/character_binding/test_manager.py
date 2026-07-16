"""角色绑定管理器的原子持久化与输入约束测试。"""

from __future__ import annotations

import asyncio
import json
import os
from typing import TYPE_CHECKING

import pytest

from komari_bot.plugins.character_binding import manager as manager_module
from komari_bot.plugins.character_binding.manager import (
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
