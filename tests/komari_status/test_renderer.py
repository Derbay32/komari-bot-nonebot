"""Komari Status 渲染测试。"""

from __future__ import annotations

from datetime import datetime

from komari_bot.plugins.komari_status.renderer import (
    MaintenanceInfo,
    MonitorInfo,
    StatusData,
    render_status,
)


def test_render_status_contains_all_sections() -> None:
    data = StatusData(
        title="Komari Bot",
        monitors=[
            MonitorInfo(
                id=1,
                name="Bot 核心服务",
                status_text="正常",
                status_icon="🟢",
                response_time_ms=156,
                uptime_24h=0.998,
                uptime_30d=0.995,
            )
        ],
        maintenances=[
            MaintenanceInfo(
                id=1,
                title="数据库升级维护",
                description="升级 PostgreSQL 至 16.2 版本",
                status_label="进行中",
                start_at=datetime(2026, 4, 22, 22, 0).astimezone(),
                end_at=datetime(2026, 4, 22, 23, 0).astimezone(),
            )
        ],
        status_page_url="https://status.example.com/status/komari-bot/",
        fetched_at=datetime.now().astimezone(),
    )

    rendered = render_status(data)

    assert "📊 Komari Bot 状态" in rendered
    assert "🖥️ 监控概览" in rendered
    assert "🟢 Bot 核心服务 — 正常 (156ms)" in rendered
    assert "📈 Uptime 统计" in rendered
    assert "24h 99.8% | 30d 99.5%" in rendered
    assert "[进行中] 数据库升级维护" in rendered
    assert "描述: 升级 PostgreSQL 至 16.2 版本" in rendered


def test_render_status_without_maintenance_uses_fallback_text() -> None:
    data = StatusData(
        title="Komari Bot",
        monitors=[],
        maintenances=[],
        status_page_url="https://status.example.com/status/komari-bot/",
        fetched_at=datetime.now().astimezone(),
    )

    rendered = render_status(data)

    assert "目前没有已计划的维护" in rendered
