"""komari_agent: lifecycle owner of the shared DSH process boundary."""

from __future__ import annotations

from nonebot import get_driver, logger
from nonebot.plugin import PluginMetadata

from komari_bot.agent import AgentHost, AgentHostError
from komari_bot.agent.management import DshManagementClient

__plugin_meta__ = PluginMetadata(
    name="komari_agent",
    description="单容器 DSH 任务运行时与进程生命周期",
    usage="自动运行，无需命令",
)


class PluginState:
    """Owned host reference for the single Bot worker."""

    def __init__(self) -> None:
        self.host: AgentHost | None = None
        self.management_client: DshManagementClient | None = None


state = PluginState()


def get_agent_host() -> AgentHost:
    """Return the process-wide host, creating it lazily."""
    if state.host is None:
        state.host = AgentHost()
    return state.host


def get_management_client() -> DshManagementClient:
    """Return the narrow management surface over the shared host."""
    if state.management_client is None:
        state.management_client = DshManagementClient(get_agent_host)
    return state.management_client


async def close_agent_host() -> None:
    """Close and forget the owned host, if one was created."""
    host = state.host
    state.host = None
    state.management_client = None
    if host is not None:
        await host.close()


@get_driver().on_startup
async def _startup_agent_host() -> None:
    """Start DSH without making a model call; an unavailable host stays bounded."""
    host = get_agent_host()
    try:
        await host.start()
    except AgentHostError as exc:
        logger.error("[komari_agent] DSH 启动失败: code={}", exc.code)
    except Exception as exc:
        logger.error("[komari_agent] DSH 启动异常: error_type={}", type(exc).__name__)


@get_driver().on_shutdown
async def _shutdown_agent_host() -> None:
    """Release the child process and any Python-side task state."""
    await close_agent_host()


__all__ = [
    "AgentHost",
    "DshManagementClient",
    "close_agent_host",
    "get_agent_host",
    "get_management_client",
]
