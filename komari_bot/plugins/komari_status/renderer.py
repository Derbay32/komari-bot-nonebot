"""Komari Status 消息渲染。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import datetime


@dataclass(slots=True)
class MonitorInfo:
    """监控项展示信息。"""

    id: int
    name: str
    status_text: str
    status_icon: str
    response_time_ms: int | None
    uptime_24h: float | None
    uptime_30d: float | None


@dataclass(slots=True)
class MaintenanceInfo:
    """维护计划展示信息。"""

    id: int
    title: str
    description: str | None
    status_label: str
    start_at: datetime | None
    end_at: datetime | None


@dataclass(slots=True)
class StatusData:
    """状态查询结果。"""

    title: str
    monitors: list[MonitorInfo]
    maintenances: list[MaintenanceInfo]
    status_page_url: str
    fetched_at: datetime


def _format_percentage(value: float | None) -> str:
    if value is None:
        return "--"
    return f"{value * 100:.1f}%"


def _format_response_time(value: int | None) -> str:
    if value is None:
        return "无数据"
    return f"{value}ms"


def _format_time(value: datetime | None) -> str:
    if value is None:
        return "未知"
    return value.astimezone().strftime("%Y-%m-%d %H:%M")


def render_status(data: StatusData) -> str:
    """将状态数据渲染为消息文本。"""
    lines = [f"📊 {data.title} 状态", "", "🖥️ 监控概览"]

    if data.monitors:
        lines.extend(
            f"{monitor.status_icon} {monitor.name} — {monitor.status_text}"
            f" ({_format_response_time(monitor.response_time_ms)})"
            for monitor in data.monitors
        )
    else:
        lines.append("暂无可展示的监控项")

    lines.extend(["", "📈 Uptime 统计"])
    if data.monitors:
        lines.extend(
            f"{monitor.name}: 24h {_format_percentage(monitor.uptime_24h)}"
            f" | 30d {_format_percentage(monitor.uptime_30d)}"
            for monitor in data.monitors
        )
    else:
        lines.append("暂无 uptime 统计")

    lines.extend(["", "🔧 维护计划"])
    if data.maintenances:
        for maintenance in data.maintenances:
            lines.append(f"[{maintenance.status_label}] {maintenance.title}")
            lines.append(
                f"   时间: {_format_time(maintenance.start_at)} - {_format_time(maintenance.end_at)}"
            )
            if maintenance.description:
                lines.append(f"   描述: {maintenance.description}")
    else:
        lines.append("目前没有已计划的维护")

    lines.extend(["", f"🔗 {data.status_page_url}"])
    return "\n".join(lines)
