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

from pathlib import Path

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
    files = [path for path in DOC_PATHS if path.is_file()]
    files.extend(sorted((PROJECT_ROOT / "docs").rglob("*.md")))
    return files


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_ai_context_documents_new_chat_contract() -> None:
    """AC10：AI 上下文把新预算/约束模式/图片模式/图片预算记为 komari_chat 契约。"""
    text = _read(PROJECT_ROOT / "AGENTS.md")

    for field in (
        "agent_max_rounds",
        "agent_max_tool_calls_per_round",
        "agent_max_total_tool_calls",
        "agent_tool_call_mode",
        "image_understanding_mode",
        "vision_image_download_",
    ):
        assert field in text, f"AGENTS.md 必须记录新契约字段: {field}"
    assert "komari_chat_config" in text, "AGENTS.md 必须点名新契约所在配置表"


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
    adr = _read(PROJECT_ROOT / "docs" / "adr" / "0009-configurable-chat-agent-tool-protocol.md")
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
