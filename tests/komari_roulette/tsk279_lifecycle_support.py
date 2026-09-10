"""TSK-279 Stage-C1 lifecycle assembly support (test-only, no production semantics).

C1 pins the **real** production lifecycle: the roulette package registers its
driver startup/shutdown hooks at import time, the startup hook builds the real
application (config -> binding/admission -> storage recovery -> QQ install ->
scheduler jobs) and the shutdown hook tears it down in order.

This support drives that seam deterministically:

* :class:`FakeScheduler` replaces the ``nonebot_plugin_apscheduler`` singleton
  and records ``add_job`` / ``remove_job`` by job id, so a test can assert
  "registered exactly once" and call the *real* registered ``func`` objects;
* :func:`lifecycle_context` uses the real NoneBot ``Driver`` lifespan as the
  assembly source.  It snapshots the lifespan containers, swaps the scheduler
  for the recording fake, installs a recording ``get_config_manager`` getter
  (the sanctioned config resource seam), pops the reloadable roulette
  lifecycle/QQ modules, reloads the package and yields the newly registered
  hooks.  It never calls ``runtime.start()`` by hand.

Registry / lifespan isolation is delegated to the single authority
``tests.group_admission.registry_isolation_support.registry_isolation_context``
(TSK-253) - this module never re-implements container restore.
"""

from __future__ import annotations

import importlib
import sys
from contextlib import asynccontextmanager, suppress
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from nonebot import get_driver
from nonebot.adapters.qq import Bot as QQBot
from nonebot.adapters.qq.adapter import Adapter as QQAdapter
from nonebot.adapters.qq.config import BotInfo, Intents

from tests.group_admission.registry_isolation_support import (
    registry_isolation_context,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    import pytest
    from pydantic import BaseModel

PACKAGE = "komari_bot.plugins.komari_roulette"
LIFECYCLE_MODULE = f"{PACKAGE}.lifecycle"
QQ_MODULE = f"{PACKAGE}.qq"

#: Public lifecycle functions the C1 seam must expose.
APPLICATION_FUNCTIONS: tuple[str, ...] = (
    "start_roulette_application",
    "stop_roulette_application",
    "get_roulette_application",
)

__all__ = [
    "APPLICATION_FUNCTIONS",
    "LIFECYCLE_MODULE",
    "PACKAGE",
    "QQ_MODULE",
    "FakeScheduler",
    "RecordingQQBot",
    "application_api",
    "delete_roulette_config",
    "invoke_hook",
    "lifecycle_context",
    "require_single_shutdown_hook",
    "require_single_startup_hook",
]


class RecordingQQBot(QQBot):
    """Real QQ ``Bot`` whose ``call_api`` records the SDK payload, never sends.

    The delivery's `sender.send_to_group` is the adapter method, so this keeps
    the whole production delivery path (message build, claim, recheck, window)
    intact while intercepting the transport: the platform is never touched and
    the exact ``post_group_messages`` payload is observable.
    """

    def __init__(
        self,
        self_id: str,
        *,
        platform_message_id: str | None = "qq-platform-1",
    ) -> None:
        adapter = QQAdapter.__new__(QQAdapter)
        super().__init__(
            adapter,
            self_id,
            BotInfo(
                id=self_id,
                token="test-token",
                secret="test-secret",
                intent=Intents(c2c_group_at_messages=True),
                use_websocket=False,
            ),
        )
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.platform_message_id = platform_message_id

    async def call_api(self, api: str, **data: Any) -> Any:
        self.calls.append((api, data))
        if self.platform_message_id is None:
            return None
        return SimpleNamespace(id=self.platform_message_id)


class FakeScheduler:
    """Recording replacement for the ``nonebot_plugin_apscheduler`` singleton.

    Records every ``add_job`` / ``remove_job`` call and keeps the current job
    set keyed by job id (``replace_existing=True`` replaces the same id).  The
    recorded ``func`` is the *real* registered callback, so tests can invoke it
    without sleeping.
    """

    def __init__(self) -> None:
        self.add_job_calls: list[dict[str, Any]] = []
        self.removed_job_ids: list[str] = []
        self.jobs: list[dict[str, Any]] = []

    @staticmethod
    def _job_id(record: dict[str, Any]) -> str | None:
        job_id = record["kwargs"].get("id")
        return job_id if isinstance(job_id, str) and job_id else None

    def add_job(
        self,
        func: Callable[..., Any],
        *args: Any,
        **kwargs: Any,
    ) -> dict[str, Any]:
        record: dict[str, Any] = {
            "func": func,
            "args": tuple(args),
            "kwargs": dict(kwargs),
        }
        self.add_job_calls.append(record)
        job_id = self._job_id(record)
        if job_id is not None and kwargs.get("replace_existing", False):
            self.jobs = [
                job for job in self.jobs if self._job_id(job) != job_id
            ]
        self.jobs.append(record)
        return record

    def remove_job(self, job_id: str, *args: Any, **kwargs: Any) -> None:
        del args, kwargs
        existing = [job for job in self.jobs if self._job_id(job) == job_id]
        if not existing:
            from apscheduler.jobstores.base import JobLookupError

            raise JobLookupError(job_id)
        self.removed_job_ids.append(job_id)
        self.jobs = [job for job in self.jobs if self._job_id(job) != job_id]

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        for job in self.jobs:
            if self._job_id(job) == job_id:
                return job
        return None

    def get_jobs(self) -> list[dict[str, Any]]:
        return list(self.jobs)

    @staticmethod
    def trigger_of(record: dict[str, Any]) -> str | None:
        trigger = record["kwargs"].get("trigger")
        if trigger is not None:
            return str(trigger)
        if record["args"]:
            return str(record["args"][0])
        return None


def application_api() -> dict[str, Any]:
    """Load the lifecycle public functions lazily (missing module → clear RED)."""

    module = importlib.import_module(LIFECYCLE_MODULE)
    missing = [name for name in APPLICATION_FUNCTIONS if not hasattr(module, name)]
    if missing:
        message = (
            f"{LIFECYCLE_MODULE} does not expose {missing} yet (TSK-279 C1 RED)"
        )
        raise AttributeError(message)
    return {name: getattr(module, name) for name in APPLICATION_FUNCTIONS}


def require_single_startup_hook(ctx: SimpleNamespace) -> Any:
    assert len(ctx.startup_hooks) == 1, (
        "生产未注册恰一个 komari_roulette driver startup hook，"
        f"实际 {len(ctx.startup_hooks)}"
    )
    return ctx.startup_hooks[0]


def require_single_shutdown_hook(ctx: SimpleNamespace) -> Any:
    assert len(ctx.shutdown_hooks) == 1, (
        "生产未注册恰一个 komari_roulette driver shutdown hook，"
        f"实际 {len(ctx.shutdown_hooks)}"
    )
    return ctx.shutdown_hooks[0]


async def invoke_hook(hook: Callable[..., Any]) -> None:
    """Call a driver lifespan hook (sync or async)."""

    result = hook()
    if result is not None and hasattr(result, "__await__"):
        await result


def _install_config_manager_getter_fake(
    monkeypatch: pytest.MonkeyPatch,
    *,
    acquisition_error: Exception | None = None,
    manager_factory: Callable[..., Any] | None = None,
) -> tuple[list[tuple[str, type[BaseModel]]], dict[str, Any], dict[str, Any]]:
    """Replace the top-level ``get_config_manager`` with a recording getter.

    Production must obtain the roulette config through the config_manager
    top-level registry (never construct ``ConfigManager`` itself).  The fake is
    registry-shaped: the same ``plugin_name`` reuses one manager (by default a
    real ``ConfigManager``).  ``acquisition_error`` injects the getter failure
    path; ``manager_factory`` injects an alternate manager implementation.

    Returns ``(calls, cache, state)`` so a test can flip ``state["error"]`` and
    reach the exact manager instance the lifecycle consumed.
    """

    from komari_bot.plugins.config_manager import manager as manager_module

    calls: list[tuple[str, type[BaseModel]]] = []
    cache: dict[str, Any] = {}
    state: dict[str, Any] = {"error": acquisition_error}

    def fake_get_config_manager(
        plugin_name: str,
        config_schema: type[BaseModel],
        *,
        env_config_schema: type[BaseModel] | None = None,
    ) -> Any:
        calls.append((plugin_name, config_schema))
        if state["error"] is not None:
            raise state["error"]
        manager = cache.get(plugin_name)
        if manager is None:
            if manager_factory is not None:
                manager = manager_factory(
                    plugin_name, config_schema, env_config_schema
                )
            else:
                manager = manager_module.ConfigManager(
                    plugin_name,
                    config_schema,
                    env_config_schema=env_config_schema,
                )
            cache[plugin_name] = manager
        return manager

    monkeypatch.setattr(
        manager_module, "get_config_manager", fake_get_config_manager
    )
    config_manager_pkg = sys.modules.get("komari_bot.plugins.config_manager")
    if config_manager_pkg is not None:
        monkeypatch.setattr(
            config_manager_pkg,
            "get_config_manager",
            fake_get_config_manager,
        )
    return calls, cache, state


def _pop_reloadable_roulette_modules() -> None:
    """Pop the modules that register hooks / install the QQ runtime.

    Pops ``lifecycle`` and ``qq`` (plus ``qq`` submodules) so reloading the
    package re-executes whichever module registers the driver hooks, and gives
    the QQ install state a clean module identity.
    """

    pkg = sys.modules.get(PACKAGE)
    popped = [LIFECYCLE_MODULE, QQ_MODULE]
    popped.extend(
        name for name in list(sys.modules) if name.startswith(QQ_MODULE + ".")
    )
    for name in popped:
        sys.modules.pop(name, None)
        if pkg is not None:
            top_level = name[len(PACKAGE) + 1 :].split(".", 1)[0]
            with suppress(AttributeError):
                delattr(pkg, top_level)


@asynccontextmanager
async def lifecycle_context(
    monkeypatch: pytest.MonkeyPatch,
    *,
    acquisition_error: Exception | None = None,
    manager_factory: Callable[..., Any] | None = None,
) -> AsyncIterator[SimpleNamespace]:
    """Assemble the roulette lifecycle from the real Driver + recording scheduler.

    Yields a namespace with ``startup_hooks`` / ``shutdown_hooks`` (the hooks
    registered during the reload), ``scheduler`` (the recording fake),
    ``jobs``, ``config_manager_calls``, ``config_managers`` (the manager cache)
    and ``config_getter_state`` (flip ``state["error"]`` to recover).
    """

    importlib.import_module(PACKAGE)
    driver = get_driver()
    with registry_isolation_context():
        apscheduler_mod: Any = sys.modules.get("nonebot_plugin_apscheduler")
        previous_scheduler = getattr(apscheduler_mod, "scheduler", None)
        scheduler = FakeScheduler()
        if apscheduler_mod is not None:
            apscheduler_mod.scheduler = scheduler

        (
            config_manager_calls,
            config_managers,
            config_getter_state,
        ) = _install_config_manager_getter_fake(
            monkeypatch,
            acquisition_error=acquisition_error,
            manager_factory=manager_factory,
        )
        _pop_reloadable_roulette_modules()
        try:
            importlib.reload(sys.modules[PACKAGE])
            startup_hooks = list(driver._lifespan._startup_funcs)
            shutdown_hooks = list(driver._lifespan._shutdown_funcs)
            yield SimpleNamespace(
                startup_hooks=startup_hooks,
                shutdown_hooks=shutdown_hooks,
                scheduler=scheduler,
                jobs=list(scheduler.jobs),
                config_manager_calls=config_manager_calls,
                config_managers=config_managers,
                config_getter_state=config_getter_state,
            )
        finally:
            # Best-effort teardown of an application left installed.
            with suppress(Exception):
                api = application_api()
                await api["stop_roulette_application"]()
            with suppress(Exception):
                importlib.import_module(QQ_MODULE).clear_roulette_qq_runtime()
            if apscheduler_mod is not None:
                apscheduler_mod.scheduler = previous_scheduler


async def delete_roulette_config(engine: Any) -> None:
    """Delete the single roulette typed-config row (case-owned cleanup)."""

    from sqlalchemy import text

    async with engine.begin() as connection:
        with suppress(Exception):
            await connection.execute(
                text("DELETE FROM komari_roulette_config WHERE id = 1")
            )
