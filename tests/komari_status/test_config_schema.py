"""Komari Status 配置测试。"""

from __future__ import annotations

import pytest

from komari_bot.plugins.komari_status.config_schema import StatusConfig


def test_status_config_normalizes_urls_and_slug() -> None:
    config = StatusConfig(
        uptime_kuma_url=" https://status.example.com/ ",
        status_page_url=" https://status.example.com/status/demo ",
        status_page_slug=" /demo/ ",
    )

    assert config.uptime_kuma_url == "https://status.example.com"
    assert config.status_page_url == "https://status.example.com/status/demo/"
    assert config.status_page_slug == "demo"


@pytest.mark.parametrize("field_name", ["request_timeout", "cache_ttl"])
def test_status_config_rejects_invalid_numeric_values(field_name: str) -> None:
    kwargs = {field_name: -1}
    with pytest.raises(ValueError):
        StatusConfig(**kwargs)  # type: ignore[arg-type]
