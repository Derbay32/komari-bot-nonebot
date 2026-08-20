"""TSK-189 ``seed_bootstrap`` 模块级单元测试（无数据库）。

已接受的公开契约测试（``test_seed_data_contract.py``）以子进程方式覆盖 CLI
外部行为；本文件直接锁定契约未完全覆盖的生产语义：

- 文件校验细节：version 缺失、根节点非对象、空内容、缺失键 / 重复键点名；
- 播种后数据库最终状态校验（冷启动充分性）：缺必需固定场景、必需场景不可用、
  无启用一般场景时明确失败。

测试数据只使用测试自有的字面量，不从生产私有常量导入 required-fixed 键
（与 ``tests/komari_decision/required_fixed_scene_keys.py`` oracle 保持一致）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import yaml

from komari_bot.db.seed_bootstrap import (
    SeedValidationError,
    SeedVerificationError,
    load_seed_file,
    verify_cold_start_state,
)

if TYPE_CHECKING:
    from pathlib import Path

REQUIRED_FIXED_SCENE_KEYS = ("NOISE", "MEANINGFUL", "CALL_DIRECT", "CALL_MENTION")


def _seed_text(
    *,
    version: str = "1",
    fixed_keys: tuple[str, ...] = REQUIRED_FIXED_SCENE_KEYS,
    general_scenes: list[dict[str, str]] | None = None,
) -> str:
    if general_scenes is None:
        general_scenes = [{"id": "scene_test_general", "text": "测试一般场景内容"}]
    lines = [f'version: "{version}"', "fixed_candidates:"]
    lines.extend(f'  {key}: "固定内容 {key}"' for key in fixed_keys)
    lines.append("general_scenes:")
    for scene in general_scenes:
        lines.append(f'  - id: "{scene["id"]}"')
        lines.append(f'    text: "{scene["text"]}"')
    return "\n".join(lines) + "\n"


def _write(tmp_path: Path, name: str, body: str) -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


def test_load_seed_file_accepts_well_formed_seed(tmp_path: Path) -> None:
    path = _write(tmp_path, "ok.yaml", _seed_text())
    payload = load_seed_file(path)

    assert payload.version == "1"
    assert set(payload.fixed_candidates) == set(REQUIRED_FIXED_SCENE_KEYS)
    assert payload.general_scenes == [
        {"id": "scene_test_general", "text": "测试一般场景内容"}
    ]
    assert [item.scene_key for item in payload.items[:4]] == list(
        REQUIRED_FIXED_SCENE_KEYS
    )
    assert all(item.enabled for item in payload.items)
    assert payload.items[4].scene_key == "scene_test_general"
    assert payload.items[4].scene_type == "general"
    assert payload.items[4].content_hash == load_seed_file(path).items[4].content_hash


@pytest.mark.parametrize(
    ("body", "message_fragment"),
    [
        ("- just\n- a list\n", "根节点必须是对象"),
        ("fixed_candidates: [unclosed\n", "YAML 解析失败"),
    ],
)
def test_load_seed_file_rejects_invalid_format(
    tmp_path: Path,
    body: str,
    message_fragment: str,
) -> None:
    path = _write(tmp_path, "bad-format.yaml", body)
    with pytest.raises(SeedValidationError, match=message_fragment):
        load_seed_file(path)


def test_load_seed_file_rejects_missing_version(tmp_path: Path) -> None:
    body = (
        "fixed_candidates:\n"
        "  NOISE: 'x'\n"
        "  MEANINGFUL: 'x'\n"
        "  CALL_DIRECT: 'x'\n"
        "  CALL_MENTION: 'x'\n"
        "general_scenes:\n"
        "  - id: 'scene_test_general'\n"
        "    text: '内容'\n"
    )
    path = _write(tmp_path, "no-version.yaml", body)
    with pytest.raises(SeedValidationError, match="version"):
        load_seed_file(path)


@pytest.mark.parametrize("missing_key", REQUIRED_FIXED_SCENE_KEYS)
def test_load_seed_file_rejects_missing_required_fixed_key(
    tmp_path: Path,
    missing_key: str,
) -> None:
    fixed_keys = tuple(k for k in REQUIRED_FIXED_SCENE_KEYS if k != missing_key)
    path = _write(tmp_path, "missing-fixed.yaml", _seed_text(fixed_keys=fixed_keys))
    with pytest.raises(SeedValidationError) as exc_info:
        load_seed_file(path)
    assert missing_key in str(exc_info.value)


def test_load_seed_file_rejects_empty_general_scenes(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "no-general.yaml",
        _seed_text(general_scenes=[]).replace("general_scenes:\n", "general_scenes: []\n"),
    )
    with pytest.raises(SeedValidationError, match="general_scenes 不能为空"):
        load_seed_file(path)


@pytest.mark.parametrize(
    ("general_scenes", "duplicated_key"),
    [
        ([{"id": "NOISE", "text": "与 fixed 候选重复"}], "NOISE"),
        (
            [
                {"id": "scene_dup", "text": "第一个"},
                {"id": "scene_dup", "text": "第二个"},
            ],
            "scene_dup",
        ),
    ],
    ids=["fixed-general-duplicate", "within-general-duplicate"],
)
def test_load_seed_file_rejects_duplicate_scene_keys(
    tmp_path: Path,
    general_scenes: list[dict[str, str]],
    duplicated_key: str,
) -> None:
    path = _write(
        tmp_path,
        "duplicate-keys.yaml",
        _seed_text(general_scenes=general_scenes),
    )
    with pytest.raises(SeedValidationError) as exc_info:
        load_seed_file(path)
    assert duplicated_key in str(exc_info.value)


def test_load_seed_file_rejects_blank_fixed_content(tmp_path: Path) -> None:
    body = (
        "version: '1'\n"
        "fixed_candidates:\n"
        "  NOISE: '  '\n"
        "  MEANINGFUL: '内容'\n"
        "  CALL_DIRECT: '内容'\n"
        "  CALL_MENTION: '内容'\n"
        "general_scenes:\n"
        "  - id: 'scene_test_general'\n"
        "    text: '内容'\n"
    )
    path = _write(tmp_path, "blank-fixed.yaml", body)
    with pytest.raises(SeedValidationError, match="NOISE"):
        load_seed_file(path)


def _rows_with(
    *,
    missing: tuple[str, ...] = (),
    disabled: tuple[str, ...] = (),
    general_enabled: int = 1,
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    rows.extend(
        {
            "scene_key": key,
            "scene_type": "fixed",
            "content_text": f"内容 {key}",
            "enabled": key not in disabled,
        }
        for key in REQUIRED_FIXED_SCENE_KEYS
        if key not in missing
    )
    rows.extend(
        {
            "scene_key": f"scene_general_{index}",
            "scene_type": "general",
            "content_text": "一般场景内容",
            "enabled": True,
        }
        for index in range(general_enabled)
    )
    return rows


def test_verify_cold_start_state_accepts_sufficient_database_state() -> None:
    result = verify_cold_start_state(_rows_with())
    assert result.fixed_count == len(REQUIRED_FIXED_SCENE_KEYS)
    assert result.general_count == 1


@pytest.mark.parametrize("missing_key", REQUIRED_FIXED_SCENE_KEYS)
def test_verify_cold_start_state_rejects_missing_required_key(
    missing_key: str,
) -> None:
    with pytest.raises(SeedVerificationError) as exc_info:
        verify_cold_start_state(_rows_with(missing=(missing_key,)))
    assert "缺少必需固定场景" in str(exc_info.value)
    assert missing_key in str(exc_info.value)


def test_verify_cold_start_state_rejects_disabled_required_key() -> None:
    with pytest.raises(SeedVerificationError) as exc_info:
        verify_cold_start_state(_rows_with(disabled=("NOISE",)))
    assert "NOISE" in str(exc_info.value)


def test_verify_cold_start_state_rejects_zero_enabled_general() -> None:
    with pytest.raises(SeedVerificationError) as exc_info:
        verify_cold_start_state(
            [dict(row) for row in _rows_with() if row["scene_type"] == "fixed"]
        )
    assert "一般场景" in str(exc_info.value)


def test_verify_cold_start_state_ignores_disabled_general() -> None:
    rows = [
        *[dict(row) for row in _rows_with() if row["scene_type"] == "fixed"],
        {
            "scene_key": "scene_disabled_general",
            "scene_type": "general",
            "content_text": "已禁用一般场景",
            "enabled": False,
        },
    ]
    with pytest.raises(SeedVerificationError, match="一般场景"):
        verify_cold_start_state(rows)


def test_default_seed_file_satisfies_validation() -> None:
    """默认版本化资产必须通过自身校验（防资产与校验逻辑漂移）。"""
    from komari_bot.db.seed_bootstrap import DEFAULT_SEED_FILE

    assert DEFAULT_SEED_FILE.is_file()
    payload = load_seed_file(DEFAULT_SEED_FILE)
    assert set(payload.fixed_candidates) == set(REQUIRED_FIXED_SCENE_KEYS)
    assert payload.general_scenes
    raw = yaml.safe_load(DEFAULT_SEED_FILE.read_text(encoding="utf-8")) or {}
    assert raw.get("version")
