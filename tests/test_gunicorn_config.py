"""Gunicorn 单 worker 启动契约测试。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

GUNICORN_CONFIG = Path(__file__).parents[1] / "docker" / "gunicorn_conf.py"


def _run_config(**overrides: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    for key in ("MAX_WORKERS", "WEB_CONCURRENCY", "WORKERS_PER_CORE"):
        env.pop(key, None)
    env.update(overrides)
    return subprocess.run(
        [sys.executable, str(GUNICORN_CONFIG)],
        check=False,
        capture_output=True,
        env=env,
        text=True,
    )


@pytest.mark.parametrize(
    "settings",
    [
        {"MAX_WORKERS": "1"},
        {"WEB_CONCURRENCY": "1"},
        {"MAX_WORKERS": "1", "WEB_CONCURRENCY": "1"},
    ],
)
def test_single_worker_configuration_is_accepted(settings: dict[str, str]) -> None:
    result = _run_config(**settings)

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["workers"] == 1


@pytest.mark.parametrize(
    "settings",
    [
        {},
        {"MAX_WORKERS": "2"},
        {"WEB_CONCURRENCY": "2"},
        {"MAX_WORKERS": "2", "WEB_CONCURRENCY": "1"},
        {"MAX_WORKERS": "1", "WEB_CONCURRENCY": "2"},
    ],
)
def test_multi_worker_configuration_is_rejected(settings: dict[str, str]) -> None:
    result = _run_config(**settings)

    assert result.returncode != 0
    assert "仅支持单 worker" in result.stderr


@pytest.mark.parametrize(
    "settings,error_message",
    [
        ({"MAX_WORKERS": "0"}, "MAX_WORKERS 必须是正整数"),
        ({"WEB_CONCURRENCY": "0"}, "WEB_CONCURRENCY 必须是正整数"),
        (
            {"WORKERS_PER_CORE": "0", "MAX_WORKERS": "1"},
            "WORKERS_PER_CORE 必须大于 0",
        ),
    ],
)
def test_non_positive_worker_configuration_is_rejected(
    settings: dict[str, str],
    error_message: str,
) -> None:
    result = _run_config(**settings)

    assert result.returncode != 0
    assert error_message in result.stderr
