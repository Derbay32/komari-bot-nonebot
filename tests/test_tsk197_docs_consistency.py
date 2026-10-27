"""TSK-197 验收 10 —— 项目文档与最终实现保持一致（无数据库）。

机器可执行的文档一致性检查：项目上下文（AGENTS.md）、ADR、迁移说明、
部署说明与 AI 上下文必须指向唯一新契约，不残留已删除字段作为活动字段。

- AI 上下文（AGENTS.md）必须把回复 Agent 预算、工具调用约束模式、
  图片理解模式与 8 项图片下载预算明确记为 ``komari_chat`` 的新契约；
- 旧图片开关 ``vision_tool_enabled`` 已从代码与迁移中物理删除，任何
  tracked 文档不得再引用它；
- 旧聊天 Prompt 输出协议 ``output_instruction`` 不得作为活动字段出现在
  AI 上下文（AGENTS.md）中；ADR-0009 只在“已删除/不再作为真源”的
  移除语境中提及它；
- 部署说明（docker/prestart.sh）在 ``upgrade head`` 后执行播种命令。

只做稳定、面向文档内容的弱断言，不复制生产正文。
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent

#: 需要一致性的 tracked 文档集合。
DOC_PATHS: tuple[Path, ...] = (
    PROJECT_ROOT / "AGENTS.md",
    PROJECT_ROOT / "README.md",
    PROJECT_ROOT / "CHANGELOG",
    PROJECT_ROOT / "migrations" / "README.md",
)

#: 部署说明 seam（与既有 prestart 契约测试同一文件）。
PRE_START_SCRIPT = PROJECT_ROOT / "docker" / "prestart.sh"


def _tracked_doc_files() -> list[Path]:
    result = subprocess.run(
        [
            "git",
            "ls-files",
            "-z",
            "--",
            *(str(path.relative_to(PROJECT_ROOT)) for path in DOC_PATHS),
            "docs/",
        ],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    files = [PROJECT_ROOT / name for name in result.stdout.split("\0") if name]
    return [
        path
        for path in files
        if path.is_file() and (path in DOC_PATHS or path.suffix == ".md")
    ]


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_tracked_doc_inventory_excludes_ignored_and_untracked_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Historical local material must not enter the tracked-doc inventory."""
    tracked = tmp_path / "docs" / "adr" / "current.md"
    tracked.parent.mkdir(parents=True)
    tracked.write_text("Current decision\n", encoding="utf-8")
    ignored = tmp_path / "docs" / "handoff" / "old.md"
    ignored.parent.mkdir(parents=True)
    ignored.write_text("vision_tool_enabled\n", encoding="utf-8")
    (tmp_path / "docs" / "draft.md").write_text("Untracked draft\n", encoding="utf-8")
    (tmp_path / ".gitignore").write_text("docs/handoff/\n", encoding="utf-8")
    agents = tmp_path / "AGENTS.md"
    agents.write_text("Current instructions\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(
        ["git", "add", "AGENTS.md", "docs/adr/current.md"],
        cwd=tmp_path,
        check=True,
    )
    monkeypatch.setattr(f"{__name__}.PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(f"{__name__}.DOC_PATHS", (agents,))

    actual = _tracked_doc_files()

    assert ignored not in actual
    assert sorted(path.relative_to(tmp_path).as_posix() for path in actual) == [
        "AGENTS.md",
        "docs/adr/current.md",
    ]


def test_ai_context_documents_new_chat_contract() -> None:
    """当前契约保留任务与图片预算，DSH 替代已退役的双模式配置。"""
    text = _read(PROJECT_ROOT / "AGENTS.md")

    for field in (
        "agent_max_rounds",
        "agent_max_tool_calls_per_round",
        "agent_max_total_tool_calls",
        "vision_image_download_",
    ):
        assert field in text, f"AGENTS.md 必须记录新契约字段: {field}"
    assert "komari_chat_config" in text, "AGENTS.md 必须点名新契约所在配置表"
    assert "agent_tool_call_mode" not in text
    assert "image_understanding_mode" not in text
    assert "DSH" in text
    assert "ADR-0016" in text


def test_no_tracked_doc_references_removed_vision_switch() -> None:
    """AC10：``vision_tool_enabled`` 已删除，任何 tracked 文档不得引用。"""
    offenders = [
        str(path)
        for path in _tracked_doc_files()
        if "vision_tool_enabled" in _read(path)
    ]
    assert offenders == [], (
        f"已删除的旧图片开关 vision_tool_enabled 不得出现在文档中: {offenders}"
    )


def test_ai_context_does_not_present_removed_output_instruction() -> None:
    """AC10：AI 上下文不得把 output_instruction 当作活动聊天 Prompt 字段。"""
    text = _read(PROJECT_ROOT / "AGENTS.md")
    assert "output_instruction" not in text, (
        "AGENTS.md 不得引用已删除的聊天 Prompt 输出协议 output_instruction"
    )


def test_adr_0009_records_output_instruction_removal_context_only() -> None:
    """AC10：ADR-0009 只在移除语境中提及 output_instruction。"""
    adr = _read(
        PROJECT_ROOT / "docs" / "adr" / "0009-configurable-chat-agent-tool-protocol.md"
    )
    assert "output_instruction" in adr
    assert "不再作为" in adr and "删除" in adr, (
        "ADR-0009 必须把 output_instruction 记录为已删除/不再作为真源"
    )
    assert "Alembic 删除" in adr


def test_prestart_runs_seed_after_upgrade_in_deployment_docs() -> None:
    """AC10：部署说明按 upgrade head → seed_bootstrap 顺序执行初始化。"""
    script = _read(PRE_START_SCRIPT)
    migrate_index = script.index("upgrade head")
    seed_index = script.index("seed_bootstrap")
    assert 0 <= migrate_index < seed_index, (
        "prestart.sh 必须先 upgrade head 再执行 seed_bootstrap"
    )
