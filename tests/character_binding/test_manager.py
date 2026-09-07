"""角色绑定管理器的公开输入与生命周期契约。

群、成员和角色名的数据库事务由真实 PostgreSQL 集成测试覆盖；这里保留
纯输入规则与旧全局接口消失的快速基线，避免通过内部仓储替身掩盖群作用域
实现问题。
"""

from __future__ import annotations

import pytest

from komari_bot.plugins.character_binding.manager import (
    MAX_CHARACTER_NAME_LENGTH,
    CharacterBindingManager,
    CharacterNameValidationError,
    validate_character_name,
)


@pytest.mark.parametrize(
    ("raw_name", "normalized_name"),
    [
        ("  Alice   Smith  ", "Alice Smith"),
        ("\u00a0花火\u00a0", "花火"),
        ("  A\u00a0\u00a0B  ", "A B"),
    ],
)
def test_character_name_normalizes_outer_and_allowed_whitespace(
    raw_name: str,
    normalized_name: str,
) -> None:
    assert validate_character_name(raw_name) == normalized_name


@pytest.mark.parametrize("character_name", ["角色\u200b名", "角色\u2060名"])
def test_character_name_rejects_format_characters(character_name: str) -> None:
    with pytest.raises(CharacterNameValidationError, match="控制字符"):
        validate_character_name(character_name)


def test_character_name_counts_unicode_code_points_and_accepts_boundary() -> None:
    boundary_name = "🙂" * MAX_CHARACTER_NAME_LENGTH
    assert validate_character_name(boundary_name) == boundary_name

    with pytest.raises(CharacterNameValidationError, match="不能超过"):
        validate_character_name("🙂" * (MAX_CHARACTER_NAME_LENGTH + 1))


def test_character_name_rejects_empty_after_normalization() -> None:
    with pytest.raises(CharacterNameValidationError, match="不能为空"):
        validate_character_name(" \u00a0 ")


@pytest.mark.parametrize(
    "character_name",
    ["角色\n名", "角色\t名", "角色\u2028名", "角色\u2029名"],
)
def test_character_name_rejects_line_breaks_and_control_characters(
    character_name: str,
) -> None:
    with pytest.raises(CharacterNameValidationError, match="换行或控制字符"):
        validate_character_name(character_name)


def test_manager_does_not_expose_legacy_global_binding_apis() -> None:
    manager = CharacterBindingManager()
    for method_name in (
        "set_character_name",
        "remove_character_name",
        "list_bindings",
        "has_binding",
    ):
        assert not hasattr(manager, method_name), method_name
