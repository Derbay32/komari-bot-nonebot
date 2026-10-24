"""TSK-189：旧场景运行时退役的删除性验收。

这些用例只穿过当前生产暴露面：插件顶层、``komari_bot.decision`` 共享包、
``services`` / ``repositories`` 包、真实 ``jev_lifecycle`` 装配点与真实
``DecisionRuntime``。它们证明旧运行时不是被禁用，而是物理删除且不能在任何
故障/刷新路径上复活。
"""

from __future__ import annotations

import ast
import asyncio
import importlib.util
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import komari_bot.plugins.komari_decision as decision_plugin
import komari_bot.plugins.komari_decision.repositories as decision_repositories
import komari_bot.plugins.komari_decision.services as decision_services
from komari_bot import decision as decision_contracts
from komari_bot.decision.dispatch import (
    DecisionDispatch,
    DecisionDispatchUnavailableError,
)
from komari_bot.decision.engine_protocol import DecisionEngineProtocol
from komari_bot.plugins.komari_decision.services import jev_lifecycle
from komari_bot.plugins.komari_decision.services.jev_runtime import (
    PRUNE_JOB_ID,
    REFRESH_JOB_ID,
    DecisionRuntime,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DECISION_ROOT = PROJECT_ROOT / "komari_bot" / "decision"
PLUGIN_ROOT = PROJECT_ROOT / "komari_bot" / "plugins" / "komari_decision"
MANAGEMENT_ROOT = PROJECT_ROOT / "komari_bot" / "plugins" / "komari_management"

LEGACY_SCENE_JOB_ID = "komari_decision_scene_sync_worker"

RETIRED_FILES = (
    "komari_bot/decision/decision_engine.py",
    "komari_bot/decision/message_filter.py",
    "komari_bot/decision/runtime_state.py",
    "komari_bot/decision/social_timing_service.py",
    "komari_bot/decision/summary_request_classification.py",
    "komari_bot/plugins/komari_decision/services/decision_engine.py",
    "komari_bot/plugins/komari_decision/services/message_filter.py",
    "komari_bot/plugins/komari_decision/services/runtime_state.py",
    "komari_bot/plugins/komari_decision/services/social_timing_service.py",
    "komari_bot/plugins/komari_decision/services/scene_classification.py",
    "komari_bot/plugins/komari_decision/services/summary_rerank_failure_budget.py",
    "komari_bot/plugins/komari_decision/services/_fixed_scene_rules.py",
    "komari_bot/plugins/komari_decision/services/scene_admin_service.py",
    "komari_bot/plugins/komari_decision/services/scene_embedding_worker.py",
    "komari_bot/plugins/komari_decision/services/scene_runtime_service.py",
    "komari_bot/plugins/komari_decision/services/scene_sync_service.py",
    "komari_bot/plugins/komari_decision/services/scene_template_loader.py",
    "komari_bot/plugins/komari_decision/repositories/scene_repository.py",
    "komari_bot/plugins/komari_decision/handlers/scene_sync_worker.py",
    "komari_bot/plugins/komari_decision/handlers/__init__.py",
    "komari_bot/plugins/komari_management/scene_api.py",
    "scripts/migrate_komari_decision_scenes_to_pg.py",
)

RETIRED_MODULES = tuple(
    path.removesuffix(".py").replace("/", ".") for path in RETIRED_FILES
)

RETIRED_MODULE_LEAVES = frozenset(
    path.rsplit("/", 1)[-1].removesuffix(".py") for path in RETIRED_FILES
)

RETIRED_PLUGIN_SYMBOLS = (
    "DecisionRuntimeState",
    "DecisionRuntimeStatus",
    "PluginManager",
    "_get_runtime_state",
    "classify_summary_request",
    "get_plugin_manager",
    "get_scene_admin_service",
)

RETIRED_CONTRACT_SYMBOLS = (
    "CallIntent",
    "DecisionOutcome",
    "FilterResult",
    "MemoryAction",
    "ReplyReason",
    "SummaryRequestClassificationResult",
    "SummaryRequestClassificationStatus",
    "SummaryRequestUnavailableReason",
    "TimingScoreBreakdown",
)

_EXPECTED_PLUGIN_ALL = {
    "get_decision_dispatch",
    "get_decision_engine",
    "observe_delivered_reply",
    "register_decision_api",
}

_EXPECTED_CONTRACT_ALL = {
    "DecisionDispatch",
    "DecisionDispatchUnavailableError",
    "DecisionEngineProtocol",
}


def _module_spec(module_name: str) -> object | None:
    try:
        return importlib.util.find_spec(module_name)
    except ModuleNotFoundError:
        return None


def _iter_sources() -> Iterator[Path]:
    for root in (PROJECT_ROOT / "komari_bot", PROJECT_ROOT / "scripts"):
        for path in sorted(root.rglob("*.py")):
            if "__pycache__" not in path.parts:
                yield path


def _imported_names(path: Path) -> tuple[set[str], set[str]]:
    """返回（绝对 import 模块名，相对 import 的最后一段）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    absolute: set[str] = set()
    relative_leaves: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            absolute.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.level:
                relative_leaves.add(node.module.split(".")[-1])
            else:
                absolute.add(node.module)
    return absolute, relative_leaves


def _assert_retired_surface_absent() -> None:
    assert set(decision_plugin.__all__) == _EXPECTED_PLUGIN_ALL
    for symbol in RETIRED_PLUGIN_SYMBOLS:
        assert not hasattr(decision_plugin, symbol), f"插件顶层仍暴露 {symbol}"

    assert set(decision_contracts.__all__) == _EXPECTED_CONTRACT_ALL
    for symbol in RETIRED_CONTRACT_SYMBOLS:
        assert not hasattr(decision_contracts, symbol), f"共享包仍暴露 {symbol}"

    assert decision_services.__all__ == []
    assert decision_repositories.__all__ == []


def test_retired_scene_files_are_physically_deleted() -> None:
    present = [
        relative for relative in RETIRED_FILES if (PROJECT_ROOT / relative).exists()
    ]

    assert present == [], f"旧场景实现仍物理存在: {present}"


def test_retired_modules_cannot_be_imported() -> None:
    still_importable = [
        module_name
        for module_name in RETIRED_MODULES
        if _module_spec(module_name) is not None
    ]

    assert still_importable == [], f"旧场景模块仍可导入: {still_importable}"


def test_public_surfaces_expose_only_current_decision_contracts() -> None:
    _assert_retired_surface_absent()
    assert decision_contracts.DecisionDispatch is DecisionDispatch
    assert (
        decision_contracts.DecisionDispatchUnavailableError
        is DecisionDispatchUnavailableError
    )
    assert decision_contracts.DecisionEngineProtocol is DecisionEngineProtocol
    assert "get_decision_runtime" not in decision_plugin.__all__
    assert "get_decision_engine" in decision_plugin.__all__


def test_production_and_scripts_do_not_import_retired_modules() -> None:
    offenders: list[str] = []
    for path in _iter_sources():
        absolute, relative_leaves = _imported_names(path)
        retired_absolute = absolute & set(RETIRED_MODULES)
        offenders.extend(
            f"{path.relative_to(PROJECT_ROOT)} imports {module_name}"
            for module_name in sorted(retired_absolute)
        )
        if (
            not path.is_relative_to(DECISION_ROOT)
            and not path.is_relative_to(PLUGIN_ROOT)
            and not path.is_relative_to(MANAGEMENT_ROOT)
        ):
            continue
        retired_relative = relative_leaves & RETIRED_MODULE_LEAVES
        offenders.extend(
            f"{path.relative_to(PROJECT_ROOT)} imports .{module_name}"
            for module_name in sorted(retired_relative)
        )

    assert offenders == [], f"生产代码仍引用旧场景实现: {offenders}"


class _RecordingScheduler:
    def __init__(self) -> None:
        self.jobs: dict[str, dict[str, Any]] = {}
        self.removed: list[str] = []

    def add_job(self, func: object, *args: object, **kwargs: object) -> None:
        del args
        job_id = str(kwargs["id"])
        self.jobs[job_id] = {"func": func, **kwargs}

    def remove_job(self, job_id: str) -> None:
        self.removed.append(job_id)
        self.jobs.pop(job_id, None)


class _StubRuntime:
    async def startup(self) -> None:
        return None

    async def close(self) -> None:
        return None

    @staticmethod
    def report_lifecycle_failure(_reason: str) -> None:
        return None

    @staticmethod
    def clear_lifecycle_failure() -> None:
        return None


def test_lifecycle_registers_only_current_decision_jobs(
    monkeypatch: Any,
) -> None:
    scheduler = _RecordingScheduler()
    monkeypatch.setattr(jev_lifecycle, "scheduler", scheduler)
    monkeypatch.setattr(jev_lifecycle, "get_decision_runtime", _StubRuntime)

    asyncio.run(jev_lifecycle._startup())
    try:
        assert set(scheduler.jobs) == {REFRESH_JOB_ID, PRUNE_JOB_ID}
        assert LEGACY_SCENE_JOB_ID not in scheduler.jobs
    finally:
        asyncio.run(jev_lifecycle._shutdown())

    assert set(scheduler.removed) >= {REFRESH_JOB_ID, PRUNE_JOB_ID}


class _UnavailableConfigManager:
    async def get_async(self) -> object:
        msg = "synthetic config outage"
        raise ConnectionError(msg)

    @staticmethod
    def get_cached_versioned_snapshot() -> object:
        msg = "config unavailable"
        raise RuntimeError(msg)


def _broken_redis() -> Any:
    msg = "synthetic redis outage"
    raise ConnectionError(msg)


async def test_failed_config_refresh_cannot_resurrect_retired_runtime() -> None:
    runtime = DecisionRuntime(
        config_manager=cast("Any", _UnavailableConfigManager()),
        redis_factory=_broken_redis,
        bot_resolver=lambda _account_id: None,
    )
    try:
        await runtime.startup()

        health = runtime.get_health()
        assert health.runtime_status == "failed"
        assert health.reason_code in {"storage_unavailable", "configuration_invalid"}
        _assert_retired_surface_absent()
    finally:
        await runtime.close()
