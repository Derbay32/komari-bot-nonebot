"""TSK-224 slice4: event gate dependency & registration boundary.

1. event_gate.py module must exist, __init__ must import it via AST, __all__ exact
   public group/QQ contract, no install_hook.
2. Gate exactly one @event_preprocessor async _admission_event_gate; forbidden: run_preprocessor, on_*, Matcher, cancel/create_task, AgentRun, cross-plugin imports. IgnoredException required.
3. Runtime registry: event_gate_context loads exactly one preprocessor whose .call.__module__ is gate module.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from tests.group_admission.entry_gate_support import event_gate_context

pytestmark = pytest.mark.group_admission_acceptance
PROJECT_ROOT = Path(__file__).resolve().parents[2]
PKG = PROJECT_ROOT / "komari_bot" / "plugins" / "group_admission"
GATE = PKG / "event_gate.py"
INIT = PKG / "__init__.py"

# AST-based forbidden patterns: names, import roots, calls (not docstring text)
_FORBIDDEN_NAMES = frozenset({
    "run_preprocessor", "on_message", "on_notice", "on_request",
    "on_command", "on_regex", "on_metaevent", "Matcher", "AgentRun", "agent_run",
})
_FORBIDDEN_CALLS = frozenset({"cancel", "create_task"})
_FORBIDDEN_IMPORT_ROOTS = frozenset({
    "komari_bot.plugins.",
    "redis",
    "asyncpg",
    "sqlalchemy",
    "sqlmodel",
    "nonebot_plugin_orm",
    "komari_bot.db",
})


def _read_gate() -> str | None:
    return GATE.read_text("utf-8") if GATE.is_file() else None


def _check_forbidden_ast(source: str) -> list[str]:
    """Check for forbidden patterns using AST (names, import roots, calls).

    Docstrings and string constants are naturally excluded because AST
    ``ast.Name`` / ``ast.Call`` / ``ast.Import`` nodes do not appear inside
    string literals.
    """
    violations: list[str] = []
    try:
        tree = ast.parse(source, filename=str(GATE))
    except SyntaxError as e:
        return [f"<syntax_error: {e}>"]

    for node in ast.walk(tree):
        # Check name references (not in docstrings/strings)
        if isinstance(node, ast.Name) and node.id in _FORBIDDEN_NAMES:
            violations.append(f"name:{node.id}")

        # Check imports
        if isinstance(node, ast.Import):
            violations.extend(
                f"import:{alias.name}"
                for alias in node.names
                if any(alias.name.startswith(root) for root in _FORBIDDEN_IMPORT_ROOTS)
            )
        if isinstance(node, ast.ImportFrom) and node.module and any(node.module.startswith(root) for root in _FORBIDDEN_IMPORT_ROOTS):
            violations.append(f"import:{node.module}")

        # Check calls
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in _FORBIDDEN_CALLS:
                violations.append(f"call:{node.func.id}")
            if isinstance(node.func, ast.Attribute) and node.func.attr in _FORBIDDEN_CALLS:
                violations.append(f"call:{node.func.attr}")

    return violations


# 1. Production event_gate.py existence


def test_event_gate_module_exists() -> None:
    assert GATE.is_file(), "event_gate module must exist"


def test_package_init_imports_event_gate() -> None:
    assert INIT.is_file()
    tree = ast.parse(INIT.read_text("utf-8"), filename=str(INIT))
    found = False
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                if "event_gate" in a.name:
                    found = True
        if isinstance(n, ast.ImportFrom):
            if n.module and "event_gate" in n.module:
                found = True
            for a in n.names:
                if "event_gate" in a.name:
                    found = True
    assert found, "package must import event_gate"


def test_package_all_exact_group_and_qq_symbols() -> None:
    assert INIT.is_file()
    tree = ast.parse(INIT.read_text("utf-8"), filename=str(INIT))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets):
            assert isinstance(node.value, ast.List)
            all_syms = {elt.value for elt in node.value.elts if isinstance(elt, ast.Constant) and isinstance(elt.value, str)}
            expected = {
                "AdmissionIntent", "AdmissionQualification", "AdmissionResult",
                "AdmissionRuntimeState", "AdmissionRuntimeStatus", "adjudicate",
                "get_runtime_state", "register_group_admission_api",
                "QQ_ADMISSION_STATE_KEY", "QQAdmissionToken", "QQBindClaim",
                "QQInitialBindRequest", "QQVerifiedBindingSession",
                "QQEffectDecision", "qualify_qq_event", "get_qq_admission_token",
                "register_qq_group_resolver",
                "register_qq_initial_bind_claimer",
                "register_qq_binding_session_resolver", "register_qq_ban_checker",
                "recheck_qq_effect",
            }
            assert all_syms == expected, f"extra={all_syms - expected}, missing={expected - all_syms}"
            assert "install_event_gate" not in all_syms
            return
    pytest.fail("no __all__")


# 2. event_gate.py content contract


def test_event_gate_has_exactly_one_event_preprocessor() -> None:
    src = _read_gate()
    if src is None:
        pytest.fail("event_gate.py missing")
    tree = ast.parse(src, filename=str(GATE))
    names = [
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        for deco in node.decorator_list
        if (isinstance(deco, ast.Attribute) and deco.attr == "event_preprocessor") or (isinstance(deco, ast.Name) and deco.id == "event_preprocessor")
    ]
    assert len(names) == 1, f"expected 1, got {names}"
    assert names[0] == "_admission_event_gate"


def test_event_gate_gate_function_is_async() -> None:
    src = _read_gate()
    if src is None:
        pytest.fail("event_gate.py missing")
    tree = ast.parse(src, filename=str(GATE))
    for n in ast.walk(tree):
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "_admission_event_gate":
            return
    pytest.fail("_admission_event_gate not async")


def test_event_gate_no_forbidden_ast_patterns() -> None:
    src = _read_gate()
    if src is None:
        pytest.fail("event_gate.py missing")
    violations = _check_forbidden_ast(src)
    assert not violations, f"forbidden AST patterns: {violations}"


def test_event_gate_no_forbidden_imports() -> None:
    src = _read_gate()
    if src is None:
        pytest.fail("event_gate.py missing")
    tree = ast.parse(src, filename=str(GATE))
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                assert not any(a.name.startswith(root) for root in _FORBIDDEN_IMPORT_ROOTS), f"forbidden import: {a.name}"
        if isinstance(n, ast.ImportFrom) and n.module:
            assert not any(n.module.startswith(root) for root in _FORBIDDEN_IMPORT_ROOTS), f"forbidden import: {n.module}"


def test_event_gate_has_ignored_exception() -> None:
    src = _read_gate()
    if src is None:
        pytest.fail("event_gate.py missing")
    assert "IgnoredException" in src


# 3. Runtime registry


async def test_event_gate_registers_exactly_one_preprocessor() -> None:
    import nonebot.message as _msg_mod

    import komari_bot.plugins.group_admission  # noqa: F401
    async with event_gate_context():
        pre = list(_msg_mod._event_preprocessors)
        assert len(pre) == 1, f"expected 1, got {len(pre)}"
        assert pre[0].call.__module__ == "komari_bot.plugins.group_admission.event_gate", (
            f"expected gate module, got {pre[0].call.__module__}"
        )


async def test_no_duplicate_event_gate_registration() -> None:
    import importlib as _il

    import nonebot.message as _msg_mod

    import komari_bot.plugins.group_admission as _pkg
    async with event_gate_context():
        pre = list(_msg_mod._event_preprocessors)
        assert len(pre) == 1, f"expected 1, got {len(pre)}"
        assert pre[0].call.__module__ == "komari_bot.plugins.group_admission.event_gate"

        # Reload within the same context — registration must remain idempotent
        _il.reload(_pkg)
        pre2 = list(_msg_mod._event_preprocessors)
        assert len(pre2) == 1, f"expected 1 after reload, got {len(pre2)}"
        assert pre2[0].call.__module__ == "komari_bot.plugins.group_admission.event_gate", (
            f"expected gate module after reload, got {pre2[0].call.__module__}"
        )
