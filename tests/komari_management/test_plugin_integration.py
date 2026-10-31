"""Komari Management 与 NoneBot FastAPI 驱动集成测试。"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import nonebot
import nonebot.plugin
import pytest
from nonebot.config import Config, Env
from nonebot.drivers import combine_driver
from nonebot.drivers.fastapi import Driver as FastAPIDriver
from nonebot.drivers.httpx import Mixin as HttpxMixin
from nonebot.drivers.websockets import Mixin as WebsocketsMixin
from pydantic import BaseModel

from komari_bot.plugins.character_binding.management_api import (
    register_character_binding_repair_api,
)
from komari_bot.plugins.group_admission import register_group_admission_api
from komari_bot.plugins.komari_decision import register_decision_api
from komari_bot.plugins.komari_help.api import register_help_api
from komari_bot.plugins.komari_knowledge.api import register_knowledge_api
from komari_bot.plugins.komari_management.api_runtime import (
    ManagementApiComponents,
    register_management_api_for_driver,
)
from komari_bot.plugins.komari_management.config_schema import ManagementConfig
from komari_bot.plugins.komari_management.managed_resources import (
    ManagedConfigResource,
)
from komari_bot.plugins.komari_search.api import register_search_api
from komari_bot.plugins.user_ban.api import register_user_ban_api
from tests.config.prompt_field_contract import (
    make_managed_prompt_resource,
    prompt_display_name,
)


class _FakeLogger:
    def __init__(self) -> None:
        self.info_messages: list[str] = []
        self.warning_messages: list[str] = []

    def info(self, message: str, *args: object) -> None:
        self.info_messages.append(message % args if args else message)

    def warning(self, message: str, *args: object) -> None:
        self.warning_messages.append(message % args if args else message)


class _DummyConfigModel(BaseModel):
    plugin_enable: bool = True


class _DummyConfigManager:
    @property
    def config_source(self) -> str:
        return "postgres:komari_plugin_configs/komari_management"

    async def get_async(self) -> BaseModel:
        return _DummyConfigModel()

    async def update_field_async(self, field_name: str, value: object) -> BaseModel:
        del field_name, value
        return await self.get_async()

    async def reload_async(self) -> BaseModel:
        return await self.get_async()


def _noop_roulette_registrar(*_args: object, **_kwargs: object) -> None:
    """Dummy roulette management registrar for this integration fixture."""


def _noop_roulette_observation_getter() -> None:
    """Dummy roulette observation getter for this integration fixture."""


def _build_components() -> ManagementApiComponents:
    return ManagementApiComponents(
        register_group_admission_api=register_group_admission_api,
        register_knowledge_api=register_knowledge_api,
        knowledge_engine_getter=lambda: None,
        register_help_api=register_help_api,
        help_engine_getter=lambda: None,
        register_search_api=register_search_api,
        register_user_ban_api=register_user_ban_api,
        user_ban_service_getter=lambda: None,
        reply_fulfillment_service_getter=lambda: None,
        register_character_binding_repair_api=register_character_binding_repair_api,
        character_binding_repair_service_getter=lambda: None,
        register_roulette_management_api=_noop_roulette_registrar,
        roulette_observation_getter=_noop_roulette_observation_getter,
        register_decision_api=register_decision_api,
        config_resources=(
            ManagedConfigResource(
                resource_id="komari_management",
                display_name="Komari Management",
                manager_getter=lambda: _DummyConfigManager(),
            ),
        ),
        prompt_resources=(
            make_managed_prompt_resource(
                "komari_decision",
                prompt_display_name("komari_decision"),
            ),
        ),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("combined", [False, True], ids=["fastapi", "fastapi-httpx-websockets"])
async def test_nonebot_fastapi_driver_exposes_docs_and_management_routes(
    *,
    combined: bool,
) -> None:
    driver_class = (
        combine_driver(FastAPIDriver, HttpxMixin, WebsocketsMixin)
        if combined
        else FastAPIDriver
    )
    settings = {
        "fastapi_docs_url": "/api/docs",
        "fastapi_openapi_url": "/api/openapi.json",
        "fastapi_include_adapter_schema": False,
    }
    driver = driver_class(
        Env.model_construct(environment="dev"),
        Config(_env_file=None, **settings),
    )
    assert isinstance(driver, FastAPIDriver)
    assert driver.type == ("fastapi+httpx+websockets" if combined else "fastapi")
    logger = _FakeLogger()

    registered = register_management_api_for_driver(
        driver=driver,
        config=ManagementConfig.model_validate(
            {
                "komari_management_api_enabled": True,
                "komari_management_api_credentials": [
                    {
                        "credential_id": "test-operator",
                        "token": "secret-token-00000000",
                        "permissions": ["*"],
                    }
                ],
                "komari_management_api_allowed_origins": [],
            }
        ),
        component_loader=_build_components,
        logger=logger,
    )

    assert registered is True

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=driver.server_app), base_url="http://management.test",
    ) as client:
        docs = await client.get("/api/docs")
        schema_response = await client.get("/api/openapi.json")

    assert docs.status_code == 200
    assert schema_response.status_code == 200

    schema = schema_response.json()
    assert "/api/v2/komari-knowledge/knowledge" in schema["paths"]
    assert "/api/v2/komari-help/help" in schema["paths"]
    assert "/api/v2/komari-memory/conversations" not in schema["paths"]
    assert "/api/v2/agent-run-logs/runs" not in schema["paths"]
    assert "/api/v2/komari-search/provider-descriptors" in schema["paths"]
    assert "/api/v2/komari-management-config/resources" in schema["paths"]
    assert "/api/v2/komari-management-prompt/resources" in schema["paths"]
    assert "/api/v2/komari-agent/providers" in schema["paths"]
    assert "/api/v2/komari-agent/bindings/{kind}" in schema["paths"]
    assert "/api/v2/komari-agent/prompts/{kind}" in schema["paths"]
    assert "/api/v2/komari-user-bans/bans" in schema["paths"]
    assert "/api/v2/reply-fulfillments/fulfillments" in schema["paths"]
    assert "/api/v2/character-bindings/repair/diagnose" in schema["paths"]
    assert "/api/llm-provider/v1/reply-logs" not in schema["paths"]
    tag_names = {
        tag
        for operations in schema["paths"].values()
        for operation in operations.values()
        for tag in operation.get("tags", [])
    }
    assert {
        "komari-knowledge",
        "komari-help",
        "komari-search",
        "komari-management-config",
        "komari-management-prompt",
        "komari-agent",
        "komari-user-bans",
        "reply-fulfillments",
        "character-binding-repair",
    } <= tag_names
    assert "komari-memory" not in tag_names


# ---------------------------------------------------------------------------
# TSK-279 C2: the *production* loader must carry the real roulette symbols.
#
# The suite shims ``komari_bot.plugins.komari_management`` (``__file__`` is
# ``None``) so its module body cannot be imported normally.  The fixture below
# deliberately loads the real file so the assertions exercise the actual
# ``_load_management_components`` wiring instead of a hand-built component
# object.  It does **not** pin the ``from import`` shape.
# ---------------------------------------------------------------------------

_REAL_MANAGEMENT_INIT = (
    Path(__file__).resolve().parents[2]
    / "komari_bot"
    / "plugins"
    / "komari_management"
    / "__init__.py"
)

#: Symbols the production loader reads off plugin tops the suite shims.
_LOADER_DEPENDENCY_SYMBOLS: dict[str, tuple[str, ...]] = {
    "komari_knowledge": ("register_knowledge_api", "get_engine"),
    "user_ban": ("register_user_ban_api", "get_service"),
    "komari_chat": ("get_reply_fulfillment_ops_service",),
    "character_binding": (
        "register_character_binding_repair_api",
        "get_binding_repair_service",
    ),
}


def _loader_registrar_stub(*_args: object, **_kwargs: object) -> None:
    """Stand-in registrar for plugins the loader only stores, never calls here."""


def _loader_getter_stub() -> None:
    """Stand-in getter for plugins the loader only stores, never calls here."""


class _RecordingDriver:
    """Fake NoneBot driver that only records lifecycle-hook registration."""

    def __init__(self) -> None:
        self.config = Config(_env_file=None)
        self.startup_hooks: list[object] = []
        self.shutdown_hooks: list[object] = []

    def on_startup(self, func: object) -> object:
        self.startup_hooks.append(func)
        return func

    def on_shutdown(self, func: object) -> object:
        self.shutdown_hooks.append(func)
        return func


@pytest.fixture
def real_management_loader(
    monkeypatch: pytest.MonkeyPatch,
) -> SimpleNamespace:
    """Execute the production ``komari_management/__init__.py`` loader.

    Patches a recording driver and the plugin-top symbols the loader reads, then
    loads the real module file and hands back the *production*
    ``_load_management_components`` plus the recorded ``require`` declarations.
    """

    import komari_bot.plugins.config_manager as manager_plugin
    from komari_bot.plugins.komari_management import api_runtime

    monkeypatch.setenv("KOMARI_MANAGEMENT_API_ENABLED", "true")
    monkeypatch.setenv(
        "KOMARI_MANAGEMENT_API_CREDENTIALS",
        '[{"credential_id":"startup-test","token":"test-startup-token-0123456789","permissions":["*"]}]',
    )
    monkeypatch.setenv("KOMARI_MANAGEMENT_API_ALLOWED_ORIGINS", "[]")
    fake_driver = _RecordingDriver()
    monkeypatch.setattr(nonebot, "get_driver", lambda: fake_driver)
    monkeypatch.setattr(nonebot.plugin, "get_driver", lambda: fake_driver)
    manager_getter = manager_plugin.get_config_manager
    manager_requests: list[str] = []

    def guarded_manager_getter(name: str, schema: type[BaseModel]) -> object:
        manager_requests.append(name)
        assert name != "komari_management", (
            "startup must not read PostgreSQL management config"
        )
        return manager_getter(name, schema)

    monkeypatch.setattr(manager_plugin, "get_config_manager", guarded_manager_getter)
    registration: dict[str, object] = {}

    def capture_registration(**kwargs: object) -> bool:
        registration.update(kwargs)
        return True

    monkeypatch.setattr(
        api_runtime, "register_management_api_for_driver", capture_registration
    )

    for plugin_name, symbols in _LOADER_DEPENDENCY_SYMBOLS.items():
        module = sys.modules.get(f"komari_bot.plugins.{plugin_name}")
        if module is None:
            continue
        for symbol in symbols:
            stub: object = (
                _loader_registrar_stub
                if symbol.startswith("register_")
                else _loader_getter_stub
            )
            monkeypatch.setattr(module, symbol, stub, raising=False)

    spec = importlib.util.spec_from_file_location(
        "komari_bot.plugins.komari_management",
        _REAL_MANAGEMENT_INIT,
        submodule_search_locations=[str(_REAL_MANAGEMENT_INIT.parent)],
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    module.__package__ = "komari_bot.plugins.komari_management"
    spec.loader.exec_module(module)

    required_plugins: list[str] = []

    # ``nonebot.plugin.require`` is globally stubbed by the suite to a whitelist
    # that has no ``komari_help`` / ``komari_roulette`` entry.  The production
    # loader only uses ``require`` as a load declaration (its return value is
    # discarded), so record every name and stub the loading itself; the real
    # symbols the loader reads below still come from the real package tops.
    def _require(name: str) -> None:
        required_plugins.append(name)

    monkeypatch.setattr(module, "require", _require)
    return SimpleNamespace(
        load_components=module._load_management_components,
        required_plugins=required_plugins,
        registration=registration,
        driver=fake_driver,
        manager_requests=manager_requests,
    )


def test_production_loader_has_no_agent_run_logger_components(
    real_management_loader: SimpleNamespace,
) -> None:
    """TSK-200：真实 loader 不再装配 Agent Run 路由、读取器或配置资源。"""

    components = real_management_loader.load_components()

    assert "agent_run_logger" not in real_management_loader.required_plugins
    resource_ids = {resource.resource_id for resource in components.config_resources}
    assert "agent_run_logger" not in resource_ids
    fields = components.__dataclass_fields__
    assert "register_agent_run_log_api" not in fields
    assert "agent_run_log_reader_getter" not in fields


def test_production_loader_carries_real_roulette_symbols(
    real_management_loader: SimpleNamespace,
) -> None:
    """真实 loader 必须把真实 roulette 两符号传入管理组件（行为证据）。"""

    import komari_bot.plugins.komari_roulette as roulette_plugin

    components = real_management_loader.load_components()

    assert "komari_roulette" in real_management_loader.required_plugins
    assert "komari_agent" in real_management_loader.required_plugins
    assert "komari_memory" not in real_management_loader.required_plugins
    assert (
        components.register_roulette_management_api
        is roulette_plugin.register_roulette_management_api
    )
    assert (
        components.roulette_observation_getter
        is roulette_plugin.get_roulette_observation
    )


# ---------------------------------------------------------------------------
# TSK-186: the *production* loader must carry the real decision registrar.
#
# The suite shims ``komari_bot.plugins.komari_management`` (``__file__`` is
# ``None``), so its module body cannot be imported normally.  The assertions
# below therefore execute the real file through ``real_management_loader`` and
# check the actual ``_load_management_components`` wiring, not a hand-built
# component object.
# ---------------------------------------------------------------------------


def test_production_loader_carries_real_decision_registrar(
    real_management_loader: SimpleNamespace,
) -> None:
    """真实 loader 必须把真实顶层 decision registrar 传入管理组件（行为证据）。"""

    import komari_bot.plugins.komari_decision as decision_plugin

    components = real_management_loader.load_components()

    assert "komari_decision" in real_management_loader.required_plugins
    assert components.register_decision_api is decision_plugin.register_decision_api


def test_production_startup_uses_dotenv_without_management_storage(
    real_management_loader: SimpleNamespace,
) -> None:
    config = real_management_loader.registration["config"]
    assert isinstance(config, ManagementConfig)
    assert config.komari_management_api_enabled is True
    assert config.komari_management_api_credentials[0].credential_id == "startup-test"
    assert "api_token_getter" not in real_management_loader.registration
    components = real_management_loader.load_components()
    resource_ids = {resource.resource_id for resource in components.config_resources}
    assert "komari_management" not in resource_ids
    assert {
        "komari_chat",
        "komari_context",
        "komari_decision",
        "komari_knowledge",
    } <= resource_ids
    assert "komari_management" not in real_management_loader.manager_requests
    assert all(
        hook.__name__ != "_cleanup_management_v1_config"
        for hook in real_management_loader.driver.startup_hooks
    )


@pytest.mark.asyncio
async def test_production_config_routes_do_not_expose_management_credentials(
    real_management_loader: SimpleNamespace,
) -> None:
    from fastapi import FastAPI

    from komari_bot.plugins.komari_management.config_api import register_config_api

    components = real_management_loader.load_components()
    config = real_management_loader.registration["config"]
    api_app = FastAPI()
    register_config_api(
        api_app,
        api_token=config.komari_management_api_credentials,
        allowed_origins=[],
        resources=components.config_resources,
    )
    headers = {
        "Authorization": "Bearer test-startup-token-0123456789",
        "X-Komari-Change-Reason": "test-retired-resource",
    }
    base = "/api/v2/komari-management-config/resources/komari_management"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api_app),
        base_url="http://management.test",
    ) as client:
        detail = await client.get(base, headers=headers)
        write = await client.patch(
            f"{base}/fields/api_credentials",
            headers=headers,
            json={"value": []},
        )
        reload_response = await client.post(f"{base}/reload", headers=headers)
    assert detail.status_code == 404
    assert write.status_code == 404
    assert reload_response.status_code == 404
