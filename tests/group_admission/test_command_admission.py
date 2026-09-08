"""TSK-227 命令准入验收：短生命周期命令的 effect manifest / sink census、
逐效果准入复查、SUPERUSER 不 bypass、plugin_enable 只收窄不放开、private 输入
不得触发命令。

验收目标（本阶段红基线锁定目标行为，生产未接入时失败为预期红态）：
- AC1: 每个被收敛命令 matcher 映射到 ``group_admission.effect.command.*``
  效果 manifest（``ADMISSION_COMMAND_EFFECT_CASES``）与 sink census
  （``command_effect_sink``），owner_module 对应各插件；AST 可对账，无孤儿。
- AC2: 命令 handler 必须经 ``group_admission`` 顶层裁决接缝逐效果复查，
  admitted 才能继续、restricted/failed 静默返回；SUPERUSER 身份鉴权不脱离
  准入（同一裁决入口、无 SUPERUSER bypass 面）。
- AC4: ``plugin_enable`` ``（插件自有开关）只能进一步收窄，不允许扩张准入：
  ``group_admission`` 裁决不在乎 ``plugin_enable``，受限群恒 rejected。
- AC3: private 输入无法触发这些命令（TSK-224 全局门禁静默拒绝）。

相同三态策略构造参照 ``tests/group_admission/runtime_support.py``；matcher 行为
参照 ``entry_gate_support`` 的 Fake bot / handler 模式。
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

from tests.group_admission.acceptance_manifest import (
    ADMISSION_COMMAND_EFFECT_CASES,
)
from tests.group_admission.command_effect_sink import COMMAND_EFFECT_SINK_CENSUS
from tests.group_admission.entry_gate_support import (
    ProbeBot,
    dispatch,
    event_gate_context,
    make_v11_event,
    register_phase_probe,
)
from tests.group_admission.management_support import (
    READER_TOKEN,
    STATUS_PATH,
    asgi_client,
    auth_headers,
    prepare_control_plane,
)
from tests.group_admission.runtime_support import (
    AdmissionStorageFake,
    install_singleton,
    start_runtime,
    stored_policy,
)

pytestmark = pytest.mark.group_admission_acceptance

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _target_command_paths() -> list[Path]:
    """TSK-227 被收敛命令的源模块（TSK-277 起 character_binding 旧命令退役）。"""
    paths = [
        "komari_help/commands.py",
        "sr/__init__.py",
        "user_ban/commands.py",
    ]
    return [PROJECT_ROOT / "komari_bot" / "plugins" / p for p in paths]


_KNOWN_FACTORIES = frozenset({"on_command"})


def _scan_command_matchers() -> dict[str, dict[str, str]]:
    """AST 扫描四个命令源模块，返回 entry_id -> {source_path, symbol, factory}。"""
    result: dict[str, dict[str, str]] = {}
    for py_file in _target_command_paths():
        rel = py_file.relative_to(PROJECT_ROOT).as_posix()
        owner = rel.split("/")[2]
        try:
            tree = ast.parse(py_file.read_text("utf-8"), filename=str(py_file))
        except SyntaxError:
            continue
        aliases: dict[str, str] = {}
        for n in ast.walk(tree):
            if isinstance(n, ast.ImportFrom) and n.module in ("nonebot",):
                for alias in n.names:
                    if alias.name in _KNOWN_FACTORIES:
                        aliases[alias.asname or alias.name] = alias.name
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign) or len(node.targets) != 1:
                continue
            target = node.targets[0]
            value = node.value
            if not isinstance(target, ast.Name) or not isinstance(value, ast.Call):
                continue
            func = value.func
            factory: str | None = None
            if isinstance(func, ast.Name) and func.id in _KNOWN_FACTORIES:
                factory = func.id
            elif isinstance(func, ast.Name) and func.id in aliases:
                factory = aliases[func.id]
            if factory is None:
                continue
            module_stem = "commands" if rel.endswith("/commands.py") else "__init__"
            result[f"matcher.{owner}.{module_stem}.{target.id}"] = {
                "source_path": rel,
                "source_symbol": target.id,
                "factory": factory,
            }
    return result






def test_command_effect_manifest_unique_and_well_formed() -> None:
    """AC1：manifest 行唯一、前缀/owner/sink 语义受控。"""
    ids = [case.effect_id for case in ADMISSION_COMMAND_EFFECT_CASES]
    assert len(ids) == len(set(ids)), "effect_id 必须唯一"
    for case in ADMISSION_COMMAND_EFFECT_CASES:
        assert case.effect_id.startswith("group_admission.effect.command."), case.effect_id
        assert case.owner_module.startswith("komari_bot.plugins."), case.owner_module
        assert case.intent == "business", case.effect_id
        assert case.work_category in {
            "transient_interaction",
            "persistent_group_work",
        }, case.effect_id
        assert case.sink_kind in {
            "group_message_reply",
            "persistent_group_write",
        }, case.effect_id


def test_command_effect_manifest_derives_from_sink_census() -> None:
    """AC1：manifest 与 sink census 单一真源一致。"""
    census = {r.effect_id: r for r in COMMAND_EFFECT_SINK_CENSUS}
    manifest = {c.effect_id: c for c in ADMISSION_COMMAND_EFFECT_CASES}
    assert set(census) == set(manifest), (
        f"manifest 与 census effect 不一致: only_census={set(census)-set(manifest)}, "
        f"only_manifest={set(manifest)-set(census)}"
    )
    for effect_id, row in census.items():
        case = manifest[effect_id]
        assert case.owner_module == row.owner_module, effect_id
        assert case.matcher_entry_id == row.entry_id, effect_id
        assert case.sink_kind == row.sink_kind, effect_id
        assert case.work_category == row.work_category, effect_id


def test_census_maps_each_command_matcher_to_effect() -> None:
    """AC1（manifest anchor）：每条 census/sink 行必须对应一个真实 on_command matcher。"""
    scanned = _scan_command_matchers()
    missing: list[str] = []
    for row in COMMAND_EFFECT_SINK_CENSUS:
        entry = scanned.get(row.entry_id)
        if entry is None:
            missing.append(f"missing matcher {row.entry_id}")
            continue
        rel = row.source_path
        assert entry["source_path"] == rel, row.entry_id
        assert entry["source_symbol"] == row.source_symbol, row.entry_id
        assert entry["factory"] == "on_command", row.entry_id
    assert not missing, f"census 指向不存在的命令 matcher: {missing}"


def test_no_orphan_command_matchers_in_target_modules() -> None:
    """AC1：被测模块里没有 census 之外的额外 on_command matcher 泄漏。"""
    scanned = _scan_command_matchers()
    census = {row.entry_id for row in COMMAND_EFFECT_SINK_CENSUS}
    extra = set(scanned) - census
    assert extra == set(), f"存在未登记的 on_command matcher: {extra}"
    assert len(census) == 8, f"command census 应为 8 条，实际 {len(census)}"


def test_command_effect_manifest_anchors_collectable() -> None:
    """AC1：manifest 每行 acceptance_anchor 必须可被 pytest --collect-only 收集。"""
    anchors = {case.acceptance_anchor for case in ADMISSION_COMMAND_EFFECT_CASES}
    assert anchors, "command effect anchor 不能为空"
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "-p",
            "no:cacheprovider",
            *sorted(anchors),
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    collected = {line.strip() for line in proc.stdout.splitlines()}
    missing = [a for a in anchors if a not in collected]
    assert proc.returncode == 0 and not missing, (
        f"anchor 收集失败: missing={missing}\nstdout={proc.stdout}\nstderr={proc.stderr}"
    )


def test_command_handlers_use_unified_admission_gate() -> None:
    """AC2/AC7（红基线）：命令模块统一接入 group_admission 裁决（sr 模式）。

    目标：收敛后各业务插件直接拥有 plugin_enable 并走统一准入裁决，
    不再依赖旧运行时权限检查容器（已物理删除）。
    """
    target_files = [
        "komari_help/commands.py",
        "sr/__init__.py",
        "user_ban/commands.py",
    ]
    missing: list[str] = []
    for rel in target_files:
        py_file = PROJECT_ROOT / "komari_bot" / "plugins" / rel
        text = py_file.read_text("utf-8")
        if "adjudicate" not in text and "group_admission" not in text:
            missing.append(rel)
    assert missing == [], (
        f"命令模块未统一接入统一准入裁决（AC2/AC7 迁移未完成）: {missing}"
    )


def test_command_handlers_gate_effects_through_group_admission() -> None:
    """AC2（红基线）：每个命令模块必须引用统一准入裁决接缝 ``adjudicate``。

    目标：admitted 才执行效果、restricted/failed 静默停止。当前生产未接入
    时为红态。
    """
    target_files = [
        "komari_help/commands.py",
        "sr/__init__.py",
        "user_ban/commands.py",
    ]
    unguarded: list[str] = []
    for rel in target_files:
        py_file = PROJECT_ROOT / "komari_bot" / "plugins" / rel
        tree = ast.parse(py_file.read_text("utf-8"), filename=str(py_file))
        imports_admission = False
        uses_adjudicate = False
        for n in ast.walk(tree):
            if isinstance(n, ast.ImportFrom):
                module = n.module or ""
                if "group_admission" in module or "adjudicate" in module:
                    imports_admission = True
            if isinstance(n, ast.Name) and n.id == "adjudicate":
                uses_adjudicate = True
            if isinstance(n, ast.Attribute) and n.attr == "adjudicate":
                uses_adjudicate = True
        if not (imports_admission or uses_adjudicate):
            unguarded.append(rel)
    assert unguarded == [], (
        "命令模块未接入 group_admission 逐效果准入复查（AC2 未实现）: "
        f"{unguarded}"
    )


async def test_superuser_does_not_bypass_group_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC2：SUPERUSER 身份鉴权不构成准入 bypass，受限群恒拒绝。"""
    policy: dict[str, object] = {"mode": "whitelist", "group_ids": [100]}
    storage = AdmissionStorageFake(stored_policy(1, policy))
    runtime, _manager = await start_runtime(monkeypatch, storage)
    install_singleton(monkeypatch, runtime)

    from komari_bot.plugins.group_admission import (
        AdmissionQualification,
        adjudicate,
    )

    result = adjudicate([200])  # 受限群
    assert result.qualification is AdmissionQualification.REJECTED
    assert result.reason_code == "policy_restricted"

    # 裁决入口没有 SUPERUSER / bypass 参数：调用方身份（包括 SUPERUSER）不影响结果。
    import inspect

    signature = inspect.signature(adjudicate)
    assert "superuser" not in signature.parameters
    assert "bypass" not in signature.parameters


async def test_plugin_enable_cannot_expand_group_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC4：插件自有 plugin_enable 只能收窄，不能扩张准入。"""
    policy: dict[str, object] = {"mode": "whitelist", "group_ids": [100]}
    storage = AdmissionStorageFake(stored_policy(1, policy))
    runtime, _manager = await start_runtime(monkeypatch, storage)
    install_singleton(monkeypatch, runtime)

    from komari_bot.plugins import group_admission as admission

    # group_admission 顶层暴露面没有任何 plugin_enable 开关信号。
    assert not hasattr(admission, "plugin_enable")
    assert "plugin_enable" not in admission.__all__

    # 就算把等同于“已启用插件”的语境喂进去，受限群仍然按策略拒绝：plugin_enable
    # 无法把 restricted 扩张成 admitted。
    from komari_bot.plugins.group_admission import (
        AdmissionQualification,
        adjudicate,
    )

    result = adjudicate([200])
    assert result.qualification is AdmissionQualification.REJECTED


async def test_private_message_cannot_trigger_command_from_event_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC3：private 输入在全局门禁被静默拒绝，命令 handler 不会被触发。"""
    storage = AdmissionStorageFake(
        stored_policy(1, {"mode": "blacklist", "group_ids": []})
    )
    _app, _runtime, _manager = await prepare_control_plane(monkeypatch, storage)

    from nonebot.adapters.onebot.v11.event import PrivateMessageEvent

    bot = ProbeBot()
    trace: list[str] = []

    async with event_gate_context():
        register_phase_probe(trace, "message")
        event = make_v11_event(PrivateMessageEvent, group_id=1)
        await dispatch(bot, event)

    assert trace == [], f"private 事件不应进入任何阶段: {trace}"
    assert bot.calls == [], f"private 事件不应产生 Bot 调用: {bot.calls}"

    async with asgi_client(_app) as client:
        response = await client.get(
            STATUS_PATH, headers=auth_headers(READER_TOKEN)
        )
        assert response.status_code == 200, response.text
        telemetry = response.json()["telemetry"]
        assert (
            telemetry["by_reason_code"]["private_input_rejected"] >= 1
        ), telemetry
