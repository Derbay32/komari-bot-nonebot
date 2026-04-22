"""Komari Status API 客户端测试。"""

from __future__ import annotations

from datetime import datetime

import pytest

from komari_bot.plugins.komari_status.api_client import UptimeKumaClient
from komari_bot.plugins.komari_status.config_schema import StatusConfig
from komari_bot.plugins.komari_status.renderer import StatusData


def _build_config() -> StatusConfig:
    return StatusConfig(
        uptime_kuma_username="demo",
        uptime_kuma_password="secret",
    )


@pytest.mark.asyncio
async def test_fetch_status_uses_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    client = UptimeKumaClient(lambda: _build_config())
    call_count = 0

    def fake_fetch_status_sync(config: StatusConfig) -> StatusData:
        nonlocal call_count
        call_count += 1
        return StatusData(
            title=config.status_page_slug,
            monitors=[],
            maintenances=[],
            status_page_url=config.status_page_url,
            fetched_at=datetime.now().astimezone(),
        )

    monkeypatch.setattr(client, "_fetch_status_sync", fake_fetch_status_sync)

    first = await client.fetch_status()
    second = await client.fetch_status()

    assert first is second
    assert call_count == 1


def test_build_maintenance_infos_filters_ended_items() -> None:
    client = UptimeKumaClient(lambda: _build_config())

    maintenances = client._build_maintenance_infos(
        [
            {
                "id": 1,
                "title": "进行中的维护",
                "description": "升级中",
                "status": "active",
                "timeslotList": [
                    {
                        "startDate": "2099-01-01 00:00:00",
                        "endDate": "2099-01-01 02:00:00",
                    }
                ],
            },
            {
                "id": 2,
                "title": "已结束维护",
                "status": "ended",
                "timeslotList": [
                    {
                        "startDate": "2020-01-01 00:00:00",
                        "endDate": "2020-01-01 02:00:00",
                    }
                ],
            },
        ],
        set(),
    )

    assert [item.id for item in maintenances] == [1]
