"""TSK-227 系统私聊通道与 helper/依赖静态边界验收（AC5/AC6/AC7/AC8）。

验收目标（红基线：helper 迁移与依赖收窄未接入时为预期红态）：
- AC5/AC6：系统私聊只允许冻结的 SUPERUSER 运维类别与用户权限/状态生命周期
  类别（user_ban 自然解封等）；普通插件便利通知、群任务结果、debug 便利结果、
  普通通知不得借私聊发送（不做“私聊绕过”）。本阶段把约束钉在被收敛命令源码：
  命令模块自身不得直接开辟私连，唯一允许的受影响用户生命周期私连 sink 是
  ``user_ban/notifications.py``。
- AC7：``get_user_nickname`` 类 nickname helper 迁到 OneBot utility
  （``komari_bot/onebot/``）；``sr`` 等业务插件改为自持 plugin_enable 并统一
  走 group_admission 裁决，不再依赖旧运行时权限辅助容器。
- AC8：不新增静态 matcher rule 权限捕获（``on_command(..., rule=/permission=)``）。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.group_admission_acceptance

PROJECT_ROOT = Path(__file__).resolve().parents[2]

_CMD_MODULES = [
    "komari_help/commands.py",
    "sr/__init__.py",
    "character_binding/commands.py",
    "user_ban/commands.py",
]

_ONLY_LIFECYCLE_PRIVATE_SINK = "user_ban/notifications.py"


def _module_path(rel: str) -> Path:
    return PROJECT_ROOT / "komari_bot" / "plugins" / rel


def _import_targets(path: Path) -> set[str]:
    tree = ast.parse(path.read_text("utf-8"), filename=str(path))
    targets: set[str] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom):
            if n.module:
                targets.add(n.module)
            targets.update(a.name for a in n.names)
        elif isinstance(n, ast.Import):
            targets.update(a.name for a in n.names)
    return targets


def _identifiers(path: Path, name: str) -> bool:
    tree = ast.parse(path.read_text("utf-8"), filename=str(path))
    for n in ast.walk(tree):
        if isinstance(n, ast.Name) and n.id == name:
            return True
        if isinstance(n, ast.Attribute) and n.attr == name:
            return True
    return False


# ---------------------------------------------------------------------------
# AC8：不新增静态 matcher rule 权限捕获
# ---------------------------------------------------------------------------


def _on_command_calls(path: Path) -> list[ast.Call]:
    tree = ast.parse(path.read_text("utf-8"), filename=str(path))
    calls: list[ast.Call] = []
    for n in ast.walk(tree):
        if not isinstance(n, ast.Call):
            continue
        func = n.func
        is_name = isinstance(func, ast.Name) and func.id == "on_command"
        is_attr = isinstance(func, ast.Attribute) and func.attr == "on_command"
        if is_name or is_attr:
            calls.append(n)
    return calls


def test_command_matchers_do_not_capture_rule_permission_statically() -> None:
    """AC8：on_command(...) 创建时不传 rule=/permission= 捕获权限。"""
    offenders: list[str] = []
    for rel in _CMD_MODULES:
        path = _module_path(rel)
        offenders.extend(
            f"{rel}: on_command(..., {kw.arg}=...)"
            for call in _on_command_calls(path)
            for kw in call.keywords
            if kw.arg in {"rule", "permission"}
        )
    assert offenders == [], f"发现静态 rule/permission 捕获: {offenders}"


def test_superuser_check_is_runtime_not_matcher_static() -> None:
    """AC8/AC5：SUPERUSER 鉴权必须发生在 handler 内，不能以 matcher 静态 permission 捕获。"""
    for rel in ("komari_help/commands.py", "sr/__init__.py", "user_ban/commands.py"):
        path = _module_path(rel)
        for call in _on_command_calls(path):
            for kw in call.keywords:
                assert kw.arg != "permission", (
                    f"{rel}: matcher 静态 permission 捕获违规"
                )


# ---------------------------------------------------------------------------
# AC5/AC6：系统私聊只允许冻结类别，命令不得私连绕过
# ---------------------------------------------------------------------------

def test_no_command_module_opens_private_bypass_channel() -> None:
    """AC6：四个命令模块不得直接 send_private_msg 私连，借私聊发便利/群任务结果。"""
    for rel in _CMD_MODULES:
        path = _module_path(rel)
        text = path.read_text("utf-8")
        assert "send_private_msg" not in text, (
            f"{rel} 直接私连，违反 AC6 无私聊绕过"
        )
        assert "send_private_notification" not in text, (
            f"{rel} 直接调用私连通知，违反 AC6"
        )


def test_private_lifecycle_sink_whitelist() -> None:
    """AC5：受影响用户生命周期私连（自然/手动解封等）只允许集中在 user_ban notifications。"""
    allowed = {_ONLY_LIFECYCLE_PRIVATE_SINK}
    offenders: list[str] = []
    for rel in _CMD_MODULES:
        path = _module_path(rel)
        if rel in allowed:
            continue
        for n, _line in enumerate(path.read_text("utf-8").splitlines(), 1):
            if "send_private" in _line:
                offenders.append(f"{rel}:{n}")
    assert offenders == [], (
        f"系统私聊绕过出现在: {offenders}（只允许 {allowed}）"
    )


# ---------------------------------------------------------------------------
# AC7：helper 迁移到 OneBot utility，sr 接入统一准入
# ---------------------------------------------------------------------------

_NICKNAME_SYMBOL = "get_user_nickname"


def _onebot_module() -> Path:
    return PROJECT_ROOT / "komari_bot" / "onebot" / "__init__.py"


def test_nickname_helper_lives_in_onebot_utility() -> None:
    """AC7（红基线）：nickname helper 必须位于 komari_bot/onebot/。"""
    onebot = _onebot_module()
    assert onebot.exists(), "komari_bot/onebot/__init__.py 缺失"
    tree = ast.parse(onebot.read_text("utf-8"), filename=str(onebot))
    promoted = {
        alias.name
        for n in ast.walk(tree)
        if isinstance(n, ast.ImportFrom)
        for alias in n.names
    }
    assert _NICKNAME_SYMBOL in promoted, (
        f"komari_bot/onebot 未导出 {_NICKNAME_SYMBOL}（AC7 helper 迁移未完成）"
    )


def test_sr_imports_nickname_from_onebot_utility() -> None:
    """AC7（红基线）：sr 的 nickname helper 来源必须是 komari_bot.onebot。"""
    sr_path = _module_path("sr/__init__.py")
    targets = _import_targets(sr_path)
    onebot_deps = {t for t in targets if t.startswith("komari_bot.onebot")}
    assert onebot_deps, "sr 未从 komari_bot.onebot 导入 helper（AC7 未实现）"


def test_help_uses_unified_admission_gate() -> None:
    """AC7：komari_help 命令统一走 group_admission 裁决（sr 模式）。"""
    path = _module_path("komari_help/commands.py")
    targets = _import_targets(path)
    assert any("group_admission" in t or "adjudicate" in t for t in targets), (
        "komari_help 命令未接入统一准入裁决（AC7 未实现）"
    )
