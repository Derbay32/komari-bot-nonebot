"""TSK-224 event gate census verification.

1. ONEBOT_EVENT_CENSUS == 22 V11 Event descendants (excl root Event), category counts exact, family/attribution_source match class defs; new adapter class fails.
2. MATCHER_ENTRY_CENSUS == 25 matcher registrations (4 on_message, 1 on_regex, 1 on_notice, 19 on_command); AST scan; added/deleted fails.
3. AST event_preprocessor scan: event_gate.py expected 1 entry.
4. All census anchors collectable by pytest --collect-only.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from tests.group_admission.entry_gate_census import (
    MATCHER_ENTRY_CENSUS,
    ONEBOT_EVENT_CENSUS,
    QQ_EVENT_CENSUS,
)

pytestmark = pytest.mark.group_admission_acceptance
PROJECT_ROOT = Path(__file__).resolve().parents[2]
_ADAPTER_MOD = "nonebot.adapters.onebot.v11.event"
_KNOWN_FACTORIES = frozenset({"on_message", "on_regex", "on_notice", "on_command", "on_request", "on_metaevent"})
_IGNORED = ("docs/", "tests/", "PIL/")


def _v11_classes() -> dict[str, type[Any]]:
    import importlib
    mod = importlib.import_module(_ADAPTER_MOD)
    event_cls = mod.Event

    def walk(cls: type[Any], seen: set[str]) -> dict[str, type[Any]]:
        r: dict[str, type[Any]] = {}
        for sub in cls.__subclasses__():
            if sub.__module__ == _ADAPTER_MOD and sub.__name__ not in seen:
                seen.add(sub.__name__)
                r[sub.__name__] = sub
                r.update(walk(sub, seen))
        return r

    return walk(event_cls, set())


def _scan_matchers() -> dict[str, dict[str, object]]:
    """AST scan of matcher assignments, import-aware for nonebot factory aliases.

    Recognises:
    - ``from nonebot import on_message as alias`` → matches local alias
    - ``import nonebot as alias; alias.on_*`` → matches attribute access on module alias
    - ``nonebot.on_*`` → direct attribute access
    """
    plugins_dir = PROJECT_ROOT / "komari_bot" / "plugins"
    result: dict[str, dict[str, object]] = {}
    for py_file in sorted(plugins_dir.rglob("*.py")):
        rel = py_file.relative_to(PROJECT_ROOT).as_posix()
        if any(rel.startswith(p) for p in _IGNORED):
            continue
        try:
            tree = ast.parse(py_file.read_text("utf-8"), filename=str(py_file))
        except SyntaxError:
            continue

        # Import-aware: collect local aliases for nonebot factories
        factory_aliases: dict[str, str] = {}  # local_name -> original_factory
        module_aliases: dict[str, str] = {}  # local_name -> module_name (import nonebot as nb)
        for n in ast.walk(tree):
            if isinstance(n, ast.ImportFrom) and n.module in ("nonebot", "nonebot.message"):
                for alias in n.names:
                    if alias.name in _KNOWN_FACTORIES:
                        local = alias.asname or alias.name
                        factory_aliases[local] = alias.name
            if isinstance(n, ast.Import):
                for alias in n.names:
                    if alias.name in ("nonebot", "nonebot.message"):
                        module_aliases[alias.asname or alias.name] = alias.name

        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign) or len(node.targets) != 1:
                continue
            target = node.targets[0]
            if not isinstance(target, ast.Name) or not isinstance(node.value, ast.Call):
                continue
            func = node.value.func
            factory: str | None = None
            if isinstance(func, ast.Name):
                if func.id in _KNOWN_FACTORIES:
                    factory = func.id
                elif func.id in factory_aliases:
                    factory = factory_aliases[func.id]
            elif isinstance(func, ast.Attribute) and func.attr in _KNOWN_FACTORIES:
                # Matches nonebot.on_*, alias.on_*, and permissive attribute access
                factory = func.attr
            if factory is None:
                continue
            plugin_rel = rel.removeprefix("komari_bot/plugins/")
            eid = f"matcher.{plugin_rel.replace('/', '.')[:-3]}.{target.id}"
            result[eid] = {"source_path": rel, "source_symbol": target.id, "factory": factory}
    return result


def _scan_preprocessors() -> dict[str, str]:
    """AST scan of event_preprocessor-decorated async/sync functions, import-aware.

    Recognises:
    - ``from nonebot.message import event_preprocessor as alias`` → matches local alias
    - ``import nonebot.message as alias; @alias.event_preprocessor`` → attribute on module alias
    - ``nonebot.message.event_preprocessor`` → direct attribute access
    """
    plugins_dir = PROJECT_ROOT / "komari_bot" / "plugins"
    result: dict[str, str] = {}
    for py_file in sorted(plugins_dir.rglob("*.py")):
        rel = py_file.relative_to(PROJECT_ROOT).as_posix()
        if any(rel.startswith(p) for p in _IGNORED):
            continue
        try:
            tree = ast.parse(py_file.read_text("utf-8"), filename=str(py_file))
        except SyntaxError:
            continue

        # Import-aware: collect local aliases for event_preprocessor
        ep_aliases: set[str] = set()
        module_ep_aliases: set[str] = set()  # module aliases (import nonebot.message as msg)
        for n in ast.walk(tree):
            if isinstance(n, ast.ImportFrom) and n.module in ("nonebot.message", "nonebot"):
                for alias in n.names:
                    if alias.name == "event_preprocessor":
                        ep_aliases.add(alias.asname or alias.name)
            if isinstance(n, ast.Import):
                for alias in n.names:
                    if alias.name in ("nonebot.message", "nonebot"):
                        module_ep_aliases.add(alias.asname or alias.name)

        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for deco in node.decorator_list:
                if (isinstance(deco, ast.Attribute) and deco.attr == "event_preprocessor") or (isinstance(deco, ast.Name) and deco.id in ep_aliases):
                    # ast.Attribute catches @msg.event_preprocessor and @nonebot.message.event_preprocessor;
                    # ast.Name catches @alias where alias is from ``from X import event_preprocessor as alias``.
                    result[rel] = node.name
    return result


# ---------------------------------------------------------------------------
# 1. Event census
# ---------------------------------------------------------------------------


def test_event_census_exact_descendants() -> None:
    actual = set(_v11_classes().keys())
    census = {row.event_class for row in ONEBOT_EVENT_CENSUS}
    assert actual == census, f"extra={actual - census}, missing={census - actual}"


def test_event_census_unique_rows() -> None:
    names = [row.event_class for row in ONEBOT_EVENT_CENSUS]
    assert len(names) == len(set(names))


def test_event_census_exact_count() -> None:
    assert len(ONEBOT_EVENT_CENSUS) == 22


def test_qq_event_census_has_one_exact_allowed_event() -> None:
    """QQ gate has one native @ entry; callback and other protocols stay closed."""
    from nonebot.adapters.qq import event as qq_event

    actual_names = {
        row.event_class
        for row in QQ_EVENT_CENSUS
        if row.event_class != "ForgedGroupAtMessageCreateEvent"
    }
    adapter_names = {
        "GroupAtMessageCreateEvent",
        "GroupMessageCreateEvent",
        "C2CMessageCreateEvent",
        "MessageCreateEvent",
        "DirectMessageCreateEvent",
        "InteractionCreateEvent",
    }
    assert actual_names == adapter_names

    allowed = {
        row.event_class for row in QQ_EVENT_CENSUS if row.qualification == "allowed"
    }
    assert allowed == {"GroupAtMessageCreateEvent"}
    assert isinstance(qq_event.GroupAtMessageCreateEvent, type)

    rejected = {
        row.event_class
        for row in QQ_EVENT_CENSUS
        if row.qualification == "rejected"
    }
    assert rejected == adapter_names - allowed | {"ForgedGroupAtMessageCreateEvent"}


def test_event_census_category_counts() -> None:
    counts = Counter(row.category for row in ONEBOT_EVENT_CENSUS)
    assert counts == {"group_business": 12, "private_input": 1, "system_meta": 3, "unsupported_business_fail_closed": 6}


def test_event_census_family_matches_definitions() -> None:
    import importlib
    mod = importlib.import_module(_ADAPTER_MOD)
    actual = _v11_classes()
    family_map = {
        "MessageEvent": "message", "PrivateMessageEvent": "message", "GroupMessageEvent": "message",
        "FriendAddNoticeEvent": "notice", "FriendRecallNoticeEvent": "notice",
        "GroupUploadNoticeEvent": "notice", "GroupAdminNoticeEvent": "notice",
        "GroupDecreaseNoticeEvent": "notice", "GroupIncreaseNoticeEvent": "notice",
        "GroupBanNoticeEvent": "notice", "GroupRecallNoticeEvent": "notice",
        "NotifyEvent": "notice", "PokeNotifyEvent": "notice", "LuckyKingNotifyEvent": "notice",
        "HonorNotifyEvent": "notice", "NoticeEvent": "notice",
        "FriendRequestEvent": "request", "GroupRequestEvent": "request", "RequestEvent": "request",
        "MetaEvent": "meta_event", "LifecycleMetaEvent": "meta_event", "HeartbeatMetaEvent": "meta_event",
    }
    for row in ONEBOT_EVENT_CENSUS:
        cls = actual.get(row.event_class)
        assert cls is not None, f"unknown {row.event_class}"
        if issubclass(cls, mod.MetaEvent):
            assert row.family == "meta_event", f"{row.event_class}: {row.family}"
        else:
            assert row.family == family_map.get(row.event_class, "unknown"), f"{row.event_class}: {row.family}"


def test_event_census_attribution_source_matches_field() -> None:
    actual = _v11_classes()
    for row in ONEBOT_EVENT_CENSUS:
        cls = actual.get(row.event_class)
        assert cls is not None
        has_group = "group_id" in cls.model_fields
        if row.category == "group_business":
            if row.event_class == "PokeNotifyEvent":
                assert row.attribution_source == "optional_positive_group_id"
            else:
                assert row.attribution_source == "group_id_field"
        else:
            assert row.attribution_source == "none"
            assert not has_group, f"{row.event_class} has group_id"


# ---------------------------------------------------------------------------
# 2. Matcher census
# ---------------------------------------------------------------------------


def test_matcher_census_exact_registrations() -> None:
    actual = set(_scan_matchers().keys())
    census = {row.entry_id for row in MATCHER_ENTRY_CENSUS}
    assert actual == census, f"extra={actual - census}, missing={census - actual}"


def test_matcher_census_unique_rows() -> None:
    ids = [row.entry_id for row in MATCHER_ENTRY_CENSUS]
    assert len(ids) == len(set(ids))


def test_matcher_census_exact_count() -> None:
    assert len(MATCHER_ENTRY_CENSUS) == 25


def test_matcher_census_factory_totals() -> None:
    counts = Counter(row.factory for row in MATCHER_ENTRY_CENSUS)
    assert counts == {"on_message": 4, "on_regex": 1, "on_notice": 1, "on_command": 19}


def test_matcher_census_effect_ids_always_singleton() -> None:
    expected = ("group_admission.effect.inbound_matcher_dispatch",)
    for row in MATCHER_ENTRY_CENSUS:
        assert row.effect_ids == expected, f"{row.entry_id}"


def test_matcher_census_source_paths_match_ast_scan() -> None:
    actual = _scan_matchers()
    for row in MATCHER_ENTRY_CENSUS:
        entry = actual.get(row.entry_id)
        assert entry is not None, f"{row.entry_id} not in AST scan"
        assert entry["source_path"] == row.source_path
        assert entry["source_symbol"] == row.source_symbol
        assert entry["factory"] == row.factory


# ---------------------------------------------------------------------------
# 3. event_preprocessor AST scan (expected 1 entry from event_gate.py)
# ---------------------------------------------------------------------------


def test_event_preprocessor_census() -> None:
    actual = _scan_preprocessors()
    expected = {"komari_bot/plugins/group_admission/event_gate.py": "_admission_event_gate"}
    assert actual == expected, f"preprocessors={actual}"


def test_no_other_event_preprocessors() -> None:
    actual = _scan_preprocessors()
    assert len(actual) == 1, f"expected 1, got {len(actual)}: {actual}"


# ---------------------------------------------------------------------------
# 4. Anchor collectability
# ---------------------------------------------------------------------------


def test_all_census_anchors_collectable() -> None:
    anchors = {row.acceptance_anchor for row in ONEBOT_EVENT_CENSUS} | {row.acceptance_anchor for row in MATCHER_ENTRY_CENSUS}
    proc = subprocess.run([sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider", *anchors],
                          cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=180, check=False)
    collected = {line.strip() for line in proc.stdout.splitlines()}
    missing = [a for a in anchors if a not in collected]
    assert proc.returncode == 0 and not missing, f"missing={missing}"
