"""TSK-190 聊天 Prompt 版本化初始数据 —— 资产与公开 CLI 契约（无数据库）。

测试即规范（对齐 ``tests/komari_decision/test_seed_data_contract.py``）：

- 默认版本化 seed 文件（``komari_bot/db/initial_data/scenes.yaml``，经
  ``seed_bootstrap.DEFAULT_SEED_FILE`` 公开常量定位）必须包含完整聊天
  Prompt 初始值，字段集合与强类型 Schema 一一对应（AC1/AC2）；
- 聊天 Prompt 初始值保留角色/文风正文，但绝不含旧 XML 最终输出协议与
  ``output_instruction`` 字段（AC4）；
- 全部聊天 Prompt 初始值通过项目共享内容预算校验（AC9）；
- 公开 CLI 对缺失聊天 Prompt 字段的 seed 资产在校验阶段即失败，且不触碰
  数据库（AC7 的 CLI 侧 / TSK-188 决策 33）。

聊天 Prompt 块定位使用通用约定（``find_chat_prompt_mapping``），不锁定
YAML 布局；本文件断言从不复制生产默认正文。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

from komari_bot.db.seed_bootstrap import DEFAULT_SEED_FILE
from komari_bot.llm.content_budget import CONTENT_TEXT_BUDGET, validate_text_budget
from tests.config.chat_prompt_field_contract import (
    REMOVED_FIELD,
    chat_prompt_field_names,
    find_chat_prompt_mapping,
    resolve_behavior_field_names,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CLI_MODULE = "komari_bot.db.seed_bootstrap"

#: 故意不可达的数据库地址：seed 校验类用例必须在该地址被触碰前失败。
UNREACHABLE_DB_URL = (
    "postgresql+asyncpg://seed_test:seed_test@seed-unreachable.invalid:1/seed_test"
)

#: AC4 安全语义弱标记：新初始数据必须承载显式安全/不可信上下文约束。
#: 只做弱语义断言（稳定标记命中之一即可），不复制生产正文，不要求旧
#: XML 输出协议；provider 侧安全边界仍由代码承担（单一职责）。
SECURITY_MARKERS: tuple[str, ...] = (
    "不可信",
    "忽略",
    "不得遵循",
    "不要遵循",
    "不得执行",
)


def _load_default_seed() -> dict[str, Any]:
    assert DEFAULT_SEED_FILE.is_file(), (
        f"缺少版本化初始数据文件: {DEFAULT_SEED_FILE}"
    )
    raw = yaml.safe_load(DEFAULT_SEED_FILE.read_text(encoding="utf-8")) or {}
    assert isinstance(raw, dict), "seed 文件根节点必须是对象"
    return raw


def _require_chat_mapping(raw: dict[str, Any]) -> dict[str, Any]:
    """定位聊天 Prompt 块；缺失即视为 TSK-190 资产尚未实现（可解释 RED）。"""
    mapping = find_chat_prompt_mapping(raw)
    assert mapping is not None, (
        "默认 seed 资产缺少聊天 Prompt 初始数据块（TSK-190 尚未实现："
        "未找到同时包含 system_prompt 且不含 planning_system_prompt 的映射）"
    )
    return mapping


def _run_seed_cli(seed_file: str | Path) -> subprocess.CompletedProcess[str]:
    """以公开 CLI 形式运行播种命令（与 prestart / 本地 / CI 同一命令）。"""
    env = os.environ.copy()
    env["SQLALCHEMY_DATABASE_URL"] = UNREACHABLE_DB_URL
    env["PYTHONPATH"] = str(PROJECT_ROOT)
    return subprocess.run(
        [sys.executable, "-m", CLI_MODULE, "--seed-file", str(seed_file)],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )


def test_default_seed_file_contains_complete_chat_prompt_section() -> None:
    """AC1/AC2：默认 seed 资产携带完整聊天 Prompt 初始值且字段与 Schema 一致。"""
    raw = _load_default_seed()
    chat = _require_chat_mapping(raw)

    assert str(raw.get("version") or "").strip(), "seed 文件必须携带 version 标记"
    expected_fields = chat_prompt_field_names()
    missing = sorted(expected_fields - set(chat))
    assert missing == [], f"聊天 Prompt 初始数据缺少字段: {missing}"
    for field in sorted(expected_fields):
        value = chat[field]
        assert isinstance(value, str) and value.strip(), (
            f"聊天 Prompt 字段 {field} 的初始值必须是非空字符串"
        )


def test_behavior_fields_cover_ticket_responsibilities() -> None:
    """AC2：聊天 Prompt 行为字段覆盖工具调用、图片读取、画像、搜索、抓取等职责。"""
    expected_fields = chat_prompt_field_names()
    assert REMOVED_FIELD not in expected_fields

    resolved = resolve_behavior_field_names(expected_fields)
    assert len(resolved) == len(set(resolved.values())), "各职责必须由不同字段承担"


def test_chat_seed_values_do_not_contain_xml_output_protocol() -> None:
    """AC4：新初始数据不含旧 XML 最终输出协议与 output_instruction。"""
    raw = _load_default_seed()
    chat = _require_chat_mapping(raw)

    assert REMOVED_FIELD not in chat, "seed 不得再包含 output_instruction 字段"
    for field, value in chat.items():
        assert isinstance(value, str)
        assert "<content" not in value and "</content" not in value, (
            f"聊天 Prompt 字段 {field} 不得包含旧 XML 最终输出协议"
        )


def test_chat_seed_keeps_role_style_text() -> None:
    """AC4：初始数据保留角色正文（以角色名为弱标记，不复制生产正文）。"""
    raw = _load_default_seed()
    chat = _require_chat_mapping(raw)

    joined = "\n".join(str(value) for value in chat.values())
    assert "小鞠" in joined or "知花" in joined, (
        "聊天 Prompt 初始数据必须保留角色正文（TSK-188 决策 27）"
    )


def test_chat_seed_keeps_security_constraints() -> None:
    """AC4：初始数据保留明确的安全/不可信上下文约束。

    弱语义断言：聊天 Prompt 初始值必须命中一组稳定安全标记之一（不可信
    内容、忽略内嵌指令、不得遵循等）；不复制整段生产正文，也不要求旧
    XML 输出协议。当前实现（无聊天 Prompt 初始数据）下本用例是
    TSK-190 的可解释 RED。
    """
    raw = _load_default_seed()
    chat = _require_chat_mapping(raw)

    joined = "\n".join(str(value) for value in chat.values())
    assert any(marker in joined for marker in SECURITY_MARKERS), (
        "聊天 Prompt 初始数据必须包含明确的安全/不可信上下文约束"
        f"（期望命中稳定标记之一: {', '.join(SECURITY_MARKERS)}）"
    )


def test_chat_seed_values_pass_shared_content_budget() -> None:
    """AC9：全部聊天 Prompt 初始值通过项目共享内容预算校验。"""
    raw = _load_default_seed()
    chat = _require_chat_mapping(raw)

    for field, value in chat.items():
        validate_text_budget(
            str(value),
            label=f"聊天 Prompt 初始数据字段 {field}",
            budget=CONTENT_TEXT_BUDGET,
        )


def test_seed_cli_rejects_chat_prompt_with_missing_field(
    tmp_path: Path,
) -> None:
    """TSK-188 决策 33：聊天 Prompt 字段缺失/空白时 CLI 在数据库访问前失败。

    夹具 = 默认资产中把聊天块某字段值清空；产出文件仍能被 YAML 解析，
    校验层必须独立检出。当前实现（无聊天 Prompt 支持）会漏过校验并尝试
    连接数据库，因此本用例是 TSK-190 的可解释 RED。
    """
    raw = _load_default_seed()
    chat = _require_chat_mapping(raw)
    target_field = "system_prompt"
    chat[target_field] = "   "

    bad_file = tmp_path / "missing-chat-field.yaml"
    bad_file.write_text(
        yaml.safe_dump(raw, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )

    result = _run_seed_cli(bad_file)
    output = f"{result.stdout}\n{result.stderr}"
    assert result.returncode != 0, output
    assert str(bad_file) in output, "报错必须指明出错的 seed 文件"
    assert UNREACHABLE_DB_URL.split("@")[-1] not in output, (
        "聊天 Prompt 校验失败前不得尝试连接数据库"
    )
