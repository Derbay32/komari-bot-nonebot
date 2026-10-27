"""TSK-182 判定 Prompt 可选初始数据 —— 辅助函数与公开 CLI 契约（无数据库）。

测试即规范（对齐 ``tests/db/test_prompt_seed_contract.py`` 的既有惯例）：

- 打包资产 ``komari_bot/db/initial_data/decision_prompt.yaml``
  （``seed_bootstrap.DEFAULT_DECISION_SEED_FILE``）固定形状为
  ``{version: 非空字符串, decision_prompt: {四个正文字段}}``，四个字段
  一一对应 ``komari_decision`` 强类型 Schema 的正文列、全部非空并通过
  共享内容预算（用真实 Schema 与真实预算校验，不复制字段名或正文）；
- 该固定形状是**严格**的：``version`` 必须是非空字符串（整数、布尔、
  序列、映射一律拒绝，绝不做 ``str()`` 强转），根键集合恰为
  ``{version, decision_prompt}``（未知顶层键一律拒绝）；
- 公开辅助 ``load_decision_prompt_seed`` 独立校验该可选资产；
- 公开 CLI ``--decision-seed-file`` 独立读取可选资产：资产缺失、畸形、
  重复键、version 非字符串、未知顶层键、字段缺失/空白/非字符串/多余、
  超预算时只输出一条固定安全诊断，不回显正文/未知字段值/原始异常/
  回溯，且不阻止独立服务启动；
- 旧聊天与总结 manifest 和 --seed-file 参数已退役。

断言不复制生产判定正文：期望值一律来自打包资产本身或测试自有字面量。
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from komari_bot.config.typed_config import ensure_typed_prompt_model
from komari_bot.db.seed_bootstrap import (
    DEFAULT_DECISION_SEED_FILE,
    SeedValidationError,
    load_decision_prompt_seed,
)
from komari_bot.llm.content_budget import CONTENT_TEXT_BUDGET, validate_text_budget

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CLI_MODULE = "komari_bot.db.seed_bootstrap"

#: 故意不可达的数据库地址：可选资产校验类用例必须在该地址被触碰前完成。
UNREACHABLE_DB_URL = (
    "postgresql+asyncpg://seed_test:seed_test@seed-unreachable.invalid:1/seed_test"
)

DECISION_RESOURCE_ID = "komari_decision"

#: 可选资产不可用/未能确认完成时的唯一固定安全诊断。
#:
#: 只陈述“未能确认完成”，不含正文、路径或原始异常，也不宣称回滚或
#: 运行时健康状态（提交后发生的归还失败不等于写入被撤销）。
OPTIONAL_UNAVAILABLE_MESSAGE = "判定 Prompt 初始化未能确认完成。"

#: Prompt 强类型表继承自 ``TypedPromptModel`` 的存储专用字段（非正文）。
PROMPT_STORAGE_FIELDS = frozenset({"id", "revision", "updated_at"})

#: 放进可选资产内容里的哨兵：新诊断与回溯绝不能回显它。
SENTINEL = "TSK182-OPTIONAL-SENTINEL-7f3c"

#: 必需 fixed 场景键（测试自有字面量，不导入生产私有常量）。
REQUIRED_FIXED_SCENE_KEYS = ("NOISE", "MEANINGFUL", "CALL_DIRECT", "CALL_MENTION")


def _decision_schema_fields() -> set[str]:
    """``komari_decision`` Prompt 正文列（真实 Schema，非复制 oracle）。"""
    model = ensure_typed_prompt_model(DECISION_RESOURCE_ID)
    assert model is not None, "无法加载 komari_decision Prompt 强类型 Schema"
    return set(model.model_fields) - PROMPT_STORAGE_FIELDS


def _load_asset_document() -> dict[str, Any]:
    """读取打包的可选判定资产文档（断言读写资产本身，不复制正文）。"""
    assert DEFAULT_DECISION_SEED_FILE.is_file(), (
        f"缺少可选判定 Prompt 初始数据文件: {DEFAULT_DECISION_SEED_FILE}"
    )
    raw = yaml.safe_load(
        DEFAULT_DECISION_SEED_FILE.read_text(encoding="utf-8")
    ) or {}
    assert isinstance(raw, dict), "判定初始数据根节点必须是对象"
    return raw


def _asset_values() -> dict[str, str]:
    """打包资产中的 ``decision_prompt`` 正文（固定形状见契约）。"""
    section = _load_asset_document().get("decision_prompt")
    assert isinstance(section, dict), (
        "判定初始数据必须包含 decision_prompt 对象块"
    )
    return {str(field): str(value) for field, value in section.items()}


def _decision_text(
    values: dict[str, str],
    *,
    version: str | None = "1",
    version_raw: str | None = None,
    raw_overrides: dict[str, str] | None = None,
    extra_root: dict[str, str] | None = None,
) -> str:
    """按固定形状生成可选资产文本。

    ``raw_overrides`` 提供 ``decision_prompt`` 块内的原始 YAML 片段；
    ``version_raw`` 提供 ``version`` 值的原始 YAML 片段（用于非字符串
    version）；``extra_root`` 追加额外的顶层键（原始 YAML 片段）。
    """
    lines: list[str] = []
    if version_raw is not None:
        lines.append(f"version: {version_raw}")
    elif version is not None:
        lines.append(f'version: "{version}"')
    lines.append("decision_prompt:")
    overrides = raw_overrides or {}
    for field, text in values.items():
        raw = overrides.get(field, json.dumps(text, ensure_ascii=False))
        lines.append(f"  {field}: {raw}")
    for field, raw in overrides.items():
        if field not in values:
            lines.append(f"  {field}: {raw}")
    for key, raw in (extra_root or {}).items():
        lines.append(f"{key}: {raw}")
    return "\n".join(lines) + "\n"


def _write(tmp_path: Path, name: str, body: str) -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


def _run_seed_cli(*args: str) -> subprocess.CompletedProcess[str]:
    """以公开 CLI 形式运行播种命令（与 prestart / 本地 / CI 同一命令）。"""
    env = os.environ.copy()
    env["SQLALCHEMY_DATABASE_URL"] = UNREACHABLE_DB_URL
    env["PYTHONPATH"] = str(PROJECT_ROOT)
    return subprocess.run(
        [sys.executable, "-m", CLI_MODULE, *args],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=180,
    )


def _combined(result: subprocess.CompletedProcess[str]) -> str:
    return f"{result.stdout}\n{result.stderr}"


def _sentinel_values() -> dict[str, str]:
    """测试自有的完整正文 marker（含哨兵，不复制生产正文）。"""
    return {
        field: f"marker-{field}-{SENTINEL}"
        for field in sorted(_decision_schema_fields())
    }


def _invalid_asset_bodies(values: dict[str, str]) -> dict[str, str]:
    """可选资产的全部非法形状（键即用例 id，值是资产文本）。"""
    fields = sorted(values)
    return {
        "root-list": "- just\n- a list\n",
        "malformed-yaml": f'version: "1"\ndecision_prompt: ["{SENTINEL}", unclosed\n',
        "duplicate-key": _decision_text(values)
        + f'  {fields[0]}: "[{SENTINEL}-duplicate]"\n',
        "missing-version": _decision_text(values, version=None),
        "blank-version": _decision_text(values, version="   "),
        "integer-version": _decision_text(values, version_raw="1"),
        "boolean-version": _decision_text(values, version_raw="true"),
        "sequence-version": _decision_text(values, version_raw="[1]"),
        "mapping-version": _decision_text(
            values,
            version_raw=f'{{major: "[{SENTINEL}]-version"}}',
        ),
        "extra-root-key": _decision_text(
            values,
            extra_root={f"unexpected_root_{SENTINEL}": '"x"'},
        ),
        "missing-block": 'version: "1"\nfixed_candidates: {}\n',
        "block-not-mapping": f'version: "1"\ndecision_prompt: "[{SENTINEL}]"\n',
        "missing-field": _decision_text(
            {field: text for field, text in values.items() if field != fields[0]}
        ),
        "blank-field": _decision_text(
            values,
            raw_overrides={fields[0]: '"   "'},
        ),
        "non-string-field": _decision_text(
            values,
            raw_overrides={fields[0]: "123"},
        ),
        "extra-field": _decision_text(
            values,
            raw_overrides={f"unexpected_{SENTINEL}": '"x"'},
        ),
        "overbudget-field": _decision_text(
            values,
            raw_overrides={fields[0]: json.dumps("x" * (12_000 + 1))},
        ),
    }


#: 可选资产的非法形状用例 id（辅助函数与 CLI 共用同一份矩阵）。
INVALID_ASSET_CASES = (
    "root-list",
    "malformed-yaml",
    "duplicate-key",
    "missing-version",
    "blank-version",
    "integer-version",
    "boolean-version",
    "sequence-version",
    "mapping-version",
    "extra-root-key",
    "missing-block",
    "block-not-mapping",
    "missing-field",
    "blank-field",
    "non-string-field",
    "extra-field",
    "overbudget-field",
)


def test_bundled_decision_asset_is_complete_and_budget_valid() -> None:
    """资产契约：非空 version + 与 Schema 一一对应的四个非空预算内正文。"""
    raw = _load_asset_document()
    assert str(raw.get("version") or "").strip(), (
        "判定初始数据必须携带非空 version 标记"
    )

    values = _asset_values()
    assert set(values) == _decision_schema_fields(), (
        "decision_prompt 字段集必须与 komari_decision 强类型 Schema 一一对应"
    )
    for field, text in values.items():
        assert isinstance(text, str) and text.strip(), (
            f"判定 Prompt 字段 {field} 的初始值必须是非空字符串"
        )
        validate_text_budget(
            text,
            label=f"判定 Prompt 初始数据字段 {field}",
            budget=CONTENT_TEXT_BUDGET,
        )


def test_load_decision_prompt_seed_accepts_valid_asset() -> None:
    """公开辅助：打包资产校验通过并返回四个正文字段。"""
    values = load_decision_prompt_seed(DEFAULT_DECISION_SEED_FILE)
    assert set(values) == _decision_schema_fields()
    assert values == _asset_values()


def test_load_decision_prompt_seed_accepts_explicit_valid_file(
    tmp_path: Path,
) -> None:
    """公开辅助：显式路径的合法资产同样通过。"""
    values = _sentinel_values()
    path = _write(tmp_path, "decision-ok.yaml", _decision_text(values))
    assert load_decision_prompt_seed(path) == values


def test_load_decision_prompt_seed_rejects_missing_file(tmp_path: Path) -> None:
    """公开辅助：文件缺失是校验失败并指出路径，而不是未捕获异常。"""
    path = tmp_path / "absent-decision.yaml"
    with pytest.raises(SeedValidationError) as exc_info:
        load_decision_prompt_seed(path)
    assert str(path) in str(exc_info.value)


@pytest.mark.parametrize("case", INVALID_ASSET_CASES)
def test_load_decision_prompt_seed_rejects_invalid_assets(
    tmp_path: Path,
    case: str,
) -> None:
    """公开辅助：非法形状一律 ``SeedValidationError`` 且点名资产路径。"""
    body = _invalid_asset_bodies(_sentinel_values())[case]
    path = _write(tmp_path, f"decision-{case}.yaml", body)
    with pytest.raises(SeedValidationError) as exc_info:
        load_decision_prompt_seed(path)
    assert str(path) in str(exc_info.value), "校验失败必须指明出错的资产路径"


def test_seed_cli_accepts_explicit_valid_decision_seed_file(
    tmp_path: Path,
) -> None:
    """CLI：显式合法可选资产被接受，不产生跳过诊断，必需流程照常进入数据库。"""
    path = _write(
        tmp_path,
        "decision-ok.yaml",
        _decision_text(_sentinel_values()),
    )
    result = _run_seed_cli("--decision-seed-file", str(path))
    output = _combined(result)

    assert result.returncode == 0, output
    assert output.count(OPTIONAL_UNAVAILABLE_MESSAGE) == 1, output
    assert "初始数据校验失败" not in output, output



def test_seed_cli_skips_missing_decision_seed_file_and_continues(
    tmp_path: Path,
) -> None:
    """CLI：可选资产缺失只输出一条固定诊断，必需初始化继续（进入数据库阶段）。"""
    path = tmp_path / "absent-decision.yaml"
    result = _run_seed_cli("--decision-seed-file", str(path))
    output = _combined(result)

    assert result.returncode == 0, output
    assert output.count(OPTIONAL_UNAVAILABLE_MESSAGE) == 1, output
    assert "初始数据校验失败" not in output, output
    assert "Traceback" not in output, output


@pytest.mark.parametrize("case", INVALID_ASSET_CASES)
def test_seed_cli_isolates_invalid_decision_seed_file(
    tmp_path: Path,
    case: str,
) -> None:
    """CLI：非法可选资产只输出固定诊断，既不阻断必需初始化也不回显内容。"""
    body = _invalid_asset_bodies(_sentinel_values())[case]
    path = _write(tmp_path, f"decision-{case}.yaml", body)
    result = _run_seed_cli("--decision-seed-file", str(path))
    output = _combined(result)

    assert result.returncode == 0, output
    assert output.count(OPTIONAL_UNAVAILABLE_MESSAGE) == 1, output
    assert "初始数据校验失败" not in output, output
    assert SENTINEL not in output, "新诊断不得回显可选资产内容或未知字段值"
    assert "Traceback" not in output, output



def test_optional_seed_cancellation_propagates_without_success_diagnostic(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    from komari_bot.db import seed_bootstrap

    monkeypatch.setattr(seed_bootstrap, "_bootstrap_nonebot", lambda: None)

    async def cancelled(_values: dict[str, str]) -> tuple[int, int]:
        raise asyncio.CancelledError

    monkeypatch.setattr(seed_bootstrap, "_seed_database", cancelled)
    with pytest.raises(asyncio.CancelledError):
        seed_bootstrap.main(["--decision-seed-file", str(DEFAULT_DECISION_SEED_FILE)])
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
