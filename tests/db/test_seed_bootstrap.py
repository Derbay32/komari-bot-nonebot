"""TSK-189/TSK-190 ``seed_bootstrap`` 模块级单元测试（无数据库）。

已接受的公开契约测试（``test_seed_data_contract.py``）以子进程方式覆盖 CLI
外部行为；本文件直接锁定契约未完全覆盖的生产语义：

- 文件校验细节：version 缺失、根节点非对象、YAML 解析失败；
- 版本化 manifest 只要求两个 Prompt 资源块：完全不含场景块的 manifest
  必须校验通过（TSK-190 退役场景初始化）；
- 旧场景键是**无关顶层键**：校验层不因此新增拒绝规则，读取时直接忽略；
- 载荷与报告只统计 Prompt：不再携带场景条目或场景计数；
- 默认版本化资产只携带两个 Prompt 资源块，不再携带 ``fixed_candidates`` /
  ``general_scenes``。

测试数据只使用测试自有的 marker，Prompt 字段名一律从
``tests/config/prompt_field_contract.py`` oracle 派生，不复制生产默认正文。
"""

from __future__ import annotations

import json
from dataclasses import fields
from typing import TYPE_CHECKING

import pytest
import yaml

from komari_bot.db.seed_bootstrap import (
    DEFAULT_SEED_FILE,
    SeedPayload,
    SeedReport,
    SeedValidationError,
    load_seed_file,
)
from tests.config.prompt_field_contract import (
    PROMPT_RESOURCE_IDS,
    prompt_marker_values,
)

if TYPE_CHECKING:
    from pathlib import Path

#: 两个必需 Prompt 资源块在 manifest 中的节名（只影响可读性，不参与定位）。
_SECTION_BY_RESOURCE: dict[str, str] = {
    "komari_chat": "chat_prompt",
    "group_history_summary": "group_history_summary_prompt",
}

#: 已退役的场景键：只作为“无关顶层键”输入，校验层必须忽略而不拒绝。
_RETIRED_SCENE_BLOCKS: tuple[str, ...] = (
    "fixed_candidates:",
    '  NOISE: "退役场景内容"',
    "general_scenes:",
    '  - id: "scene_retired"',
    '    text: "退役场景内容"',
)


def _prompt_seed_text(
    *,
    version: str | None = "1",
    retired_scene_blocks: bool = False,
) -> str:
    """构造测试自有的只带 Prompt 的 manifest 文本。"""
    lines: list[str] = []
    if version is not None:
        lines.append(f'version: "{version}"')
    if retired_scene_blocks:
        lines.extend(_RETIRED_SCENE_BLOCKS)
    for resource_id in PROMPT_RESOURCE_IDS:
        lines.append(f"{_SECTION_BY_RESOURCE[resource_id]}:")
        values = prompt_marker_values(resource_id)
        lines.extend(
            f"  {field}: {json.dumps(values[field], ensure_ascii=False)}"
            for field in sorted(values)
        )
    return "\n".join(lines) + "\n"


def _write(tmp_path: Path, name: str, body: str) -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


def test_load_seed_file_accepts_prompt_only_seed(tmp_path: Path) -> None:
    """场景块退役后，只带两个必需 Prompt 块的 manifest 是完整合法载荷。"""
    path = _write(tmp_path, "prompt-only.yaml", _prompt_seed_text())

    payload = load_seed_file(path)

    assert payload.version == "1"
    assert set(payload.prompts) == set(PROMPT_RESOURCE_IDS)
    for resource_id in PROMPT_RESOURCE_IDS:
        assert payload.prompts[resource_id] == prompt_marker_values(resource_id)


def test_seed_payload_and_report_carry_prompt_data_only() -> None:
    """载荷只剩 version 与 prompts；报告只剩 Prompt 新建行与补齐字段数。"""
    assert {field.name for field in fields(SeedPayload)} == {"version", "prompts"}
    assert {field.name for field in fields(SeedReport)} == {
        "prompt_rows_inserted",
        "prompt_fields_filled",
    }


def test_load_seed_file_ignores_retired_scene_blocks(tmp_path: Path) -> None:
    """旧场景键是无关顶层键：不新增拒绝规则，读取结果与只带 Prompt 时一致。"""
    path = _write(
        tmp_path,
        "legacy-scene-keys.yaml",
        _prompt_seed_text(retired_scene_blocks=True),
    )

    payload = load_seed_file(path)

    assert payload.version == "1"
    assert set(payload.prompts) == set(PROMPT_RESOURCE_IDS)
    for resource_id in PROMPT_RESOURCE_IDS:
        assert payload.prompts[resource_id] == prompt_marker_values(resource_id)


@pytest.mark.parametrize(
    ("body", "message_fragment"),
    [
        ("- just\n- a list\n", "根节点必须是对象"),
        ("version: [unclosed\n", "YAML 解析失败"),
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
    path = _write(
        tmp_path,
        "no-version.yaml",
        _prompt_seed_text(version=None),
    )
    with pytest.raises(SeedValidationError, match="version"):
        load_seed_file(path)


def test_default_seed_file_satisfies_validation() -> None:
    """默认版本化资产必须通过自身校验：只有 Prompt 块，没有场景块。"""
    assert DEFAULT_SEED_FILE.is_file()
    payload = load_seed_file(DEFAULT_SEED_FILE)
    assert set(payload.prompts) == set(PROMPT_RESOURCE_IDS)

    raw = yaml.safe_load(DEFAULT_SEED_FILE.read_text(encoding="utf-8")) or {}
    assert raw.get("version")
    assert "fixed_candidates" not in raw
    assert "general_scenes" not in raw
