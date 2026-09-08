"""TSK-274 dual-protocol startup and QQ adapter capability contracts."""

from __future__ import annotations

import runpy
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, TypeVar, cast

import nonebot
import pytest
from nonebot.adapters.onebot.v11 import Adapter as OneBotV11Adapter
from nonebot.adapters.qq import Bot as QQBot
from nonebot.adapters.qq import adapter as qq_adapter_module
from nonebot.adapters.qq.adapter import Adapter as QQAdapter
from nonebot.adapters.qq.config import BotInfo, Intents
from nonebot.adapters.qq.config import Config as QQConfig
from nonebot.config import Config as NoneBotConfig
from nonebot.drivers import ASGIMixin, Driver, HTTPClientMixin, WebSocketClientMixin
from pydantic import BaseModel

from tests.group_admission.qq_admission_support import APP_ID, OFFICIAL_BOT_QQ

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable, Mapping

pytestmark = pytest.mark.group_admission_acceptance

_ENTRYPOINT = Path(__file__).resolve().parents[2] / "docker" / "bot.py"
_ConfigT = TypeVar("_ConfigT", bound=BaseModel)


class _DriverBase:
    def __init__(self, name: str = "test-driver") -> None:
        self.config = NoneBotConfig.model_validate({"driver": name})
        self.shutdown_hooks: list[Callable[..., object]] = []
        self._lifespan = SimpleNamespace(on_ready=lambda callback: callback)

    def on_shutdown(self, callback: Callable[..., object]) -> None:
        self.shutdown_hooks.append(callback)

    @property
    def type(self) -> str:
        return str(self.config.driver)


class _HTTPDriver(_DriverBase, HTTPClientMixin):
    async def request(self, setup: Any) -> Any:
        del setup
        return None

    async def stream_request(
        self,
        setup: Any,
        *,
        chunk_size: int = 1024,
    ) -> AsyncGenerator[Any]:
        del setup, chunk_size
        if False:
            yield None

    def get_session(self, *args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        return None


class _ASGIDriver(_DriverBase, ASGIMixin):
    @property
    def server_app(self) -> Any:
        return None

    @property
    def asgi(self) -> Any:
        return None

    def setup_http_server(self, setup: Any) -> None:
        del setup

    def setup_websocket_server(self, setup: Any) -> None:
        del setup


class _WebSocketDriver(_DriverBase, WebSocketClientMixin):
    @asynccontextmanager
    async def websocket(self, setup: Any) -> AsyncGenerator[Any]:
        del setup
        yield None


class _HTTPASGIDriver(_HTTPDriver, ASGIMixin):
    @property
    def server_app(self) -> Any:
        return None

    @property
    def asgi(self) -> Any:
        return None

    def setup_http_server(self, setup: Any) -> None:
        del setup

    def setup_websocket_server(self, setup: Any) -> None:
        del setup


class _HTTPWebSocketDriver(_HTTPDriver, WebSocketClientMixin):
    @asynccontextmanager
    async def websocket(self, setup: Any) -> AsyncGenerator[Any]:
        del setup
        yield None


class _AllCapabilitiesDriver(_HTTPASGIDriver, WebSocketClientMixin):
    @asynccontextmanager
    async def websocket(self, setup: Any) -> AsyncGenerator[Any]:
        del setup
        yield None


class _StartupDriver(_AllCapabilitiesDriver):
    """Driver whose registration path instantiates a real QQ adapter."""

    def __init__(self) -> None:
        super().__init__(name="~all-capabilities")
        self.registered: list[type[object]] = []
        self.qq_adapters: list[QQAdapter] = []

    def register_adapter(self, adapter: type[object]) -> None:
        self.registered.append(adapter)
        if adapter is QQAdapter:
            self.qq_adapters.append(QQAdapter(cast("Driver", self)))


def _minimal_intents() -> Intents:
    """Only QQ group-at delivery is enabled for the 274 entry point."""
    return Intents(
        guilds=False,
        guild_members=False,
        guild_messages=False,
        guild_message_reactions=False,
        direct_message=False,
        open_forum_event=False,
        audio_live_member=False,
        group_members=False,
        c2c_group_at_messages=True,
        interaction=False,
        message_audit=False,
        forum_event=False,
        audio_action=False,
        at_messages=False,
    )


def _bot_info(*, use_websocket: bool) -> BotInfo:
    return BotInfo(
        id=APP_ID,
        token="test-token",
        secret="test-secret",
        intent=_minimal_intents(),
        use_websocket=use_websocket,
    )


def _raw_config_with_default_intents() -> QQConfig:
    """Represent a user-supplied qq_bots entry before 274 normalization."""
    return QQConfig.model_validate(
        {
            "qq_is_sandbox": True,
            "qq_bots": [
                {
                    "id": APP_ID,
                    "token": "test-token",
                    "secret": "test-secret",
                    "intent": {},
                    "use_websocket": False,
                }
            ],
        }
    )


def _run_entrypoint(
    monkeypatch: pytest.MonkeyPatch,
    driver: _StartupDriver,
    qq_config: QQConfig,
    *,
    observed_configs: list[object] | None = None,
    official_map: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Run only startup registration with NoneBot lifecycle/network replaced."""
    resolved_official_map = (
        {APP_ID: str(OFFICIAL_BOT_QQ)}
        if official_map is None
        else dict(official_map)
    )
    driver.config = NoneBotConfig.model_validate(
        {
            "driver": driver.config.driver,
            "qq_is_sandbox": qq_config.qq_is_sandbox,
            "qq_bots": [bot.model_dump() for bot in qq_config.qq_bots],
            "qq_official_bot_qq_by_app": resolved_official_map,
        }
    )
    monkeypatch.setattr(nonebot, "init", lambda: None)
    monkeypatch.setattr(nonebot, "get_driver", lambda: driver)
    monkeypatch.setattr(nonebot, "load_builtin_plugins", lambda *_args: None)
    monkeypatch.setattr(nonebot, "load_from_toml", lambda *_args: None)
    monkeypatch.setattr(nonebot, "run", lambda: None)

    real_get_plugin_config = nonebot.get_plugin_config

    def observe_config(config_type: type[_ConfigT]) -> _ConfigT:
        config = real_get_plugin_config(config_type)
        if observed_configs is not None:
            observed_configs.append(config)
        return config

    monkeypatch.setattr(nonebot, "get_plugin_config", observe_config)
    monkeypatch.setattr(qq_adapter_module, "get_plugin_config", observe_config)
    return cast(
        "dict[str, object]",
        runpy.run_path(str(_ENTRYPOINT), run_name="tsk274_startup"),
    )


def _setup_adapter(driver: object, *, bots: list[BotInfo]) -> QQAdapter:
    adapter = QQAdapter.__new__(QQAdapter)
    adapter.driver = cast("Any", driver)
    adapter.qq_config = QQConfig.model_validate(
        {
            "qq_api_base": "https://api.sgroup.qq.com/",
            "qq_sandbox_api_base": "https://sandbox.api.sgroup.qq.com",
            "qq_auth_base": "https://bots.qq.com/app/getAppAccessToken",
            "qq_bots": [bot.model_dump() for bot in bots],
        }
    )
    adapter.on_ready = lambda func: func
    return adapter


def test_entrypoint_runtime_skips_qq_adapter_when_user_has_no_qq_bots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver = _StartupDriver()
    _run_entrypoint(monkeypatch, driver, QQConfig.model_validate({"qq_bots": []}))

    assert driver.registered == [OneBotV11Adapter]
    assert driver.qq_adapters == []


def test_entrypoint_runtime_registers_qq_and_normalizes_user_intents(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Registration must exercise the real adapter with the final BotInfo."""
    driver = _StartupDriver()
    raw_config = _raw_config_with_default_intents()
    observed_configs: list[object] = []
    _run_entrypoint(
        monkeypatch,
        driver,
        raw_config,
        observed_configs=observed_configs,
    )

    assert driver.registered == [OneBotV11Adapter, QQAdapter]
    assert len(driver.qq_adapters) == 1
    adapter_config = driver.qq_adapters[0].qq_config
    assert adapter_config.qq_is_sandbox is True
    assert len(adapter_config.qq_bots) == 1
    info = adapter_config.qq_bots[0]
    assert info.id == APP_ID
    assert info.token == "test-token"
    assert info.secret == "test-secret"
    assert info.use_websocket is False
    assert info.intent.model_dump() == _minimal_intents().model_dump()
    assert any(
        getattr(config, "qq_official_bot_qq_by_app", None)
        == {APP_ID: str(OFFICIAL_BOT_QQ)}
        for config in observed_configs
    )


@pytest.mark.parametrize(
    ("driver_factory", "use_websocket"),
    [
        (_HTTPASGIDriver, False),
        (_HTTPWebSocketDriver, True),
        (_AllCapabilitiesDriver, False),
        (_AllCapabilitiesDriver, True),
    ],
    ids=["webhook", "websocket", "all-webhook", "all-websocket"],
)
def test_real_qq_adapter_setup_accepts_selected_transport_capabilities(
    driver_factory: type[_DriverBase],
    use_websocket: bool,  # noqa: FBT001
) -> None:
    """The installed adapter's own setup enforces the chosen transport."""
    driver = driver_factory()
    adapter = _setup_adapter(driver, bots=[_bot_info(use_websocket=use_websocket)])

    adapter.setup()

    assert len(driver.shutdown_hooks) == 1


@pytest.mark.parametrize(
    ("driver_factory", "use_websocket", "expected"),
    [
        (_ASGIDriver, True, "http client"),
        (_HTTPDriver, True, "websocket client"),
        (_HTTPDriver, False, "ASGI server"),
    ],
    ids=["missing-http", "missing-websocket", "missing-asgi"],
)
def test_real_qq_adapter_setup_fails_explicitly_when_capability_is_missing(
    driver_factory: type[_DriverBase],
    use_websocket: bool,  # noqa: FBT001
    expected: str,
) -> None:
    driver = driver_factory()
    adapter = _setup_adapter(driver, bots=[_bot_info(use_websocket=use_websocket)])

    with pytest.raises(RuntimeError, match=expected):
        adapter.setup()


def test_zero_qq_bots_require_only_http_for_the_adapter_setup_contract() -> None:
    """The production entry point must skip QQ registration for this case."""
    driver = _HTTPDriver()
    adapter = _setup_adapter(driver, bots=[])

    adapter.setup()

    assert len(driver.shutdown_hooks) == 1


def test_real_qq_bot_keeps_bot_info_app_id_separate_from_official_member_qq() -> None:
    info = _bot_info(use_websocket=True)
    bot = QQBot(cast("Any", object()), info.id, info)

    assert bot.self_id == APP_ID
    assert bot.bot_info.id == APP_ID
    assert bot.bot_info.id != OFFICIAL_BOT_QQ
