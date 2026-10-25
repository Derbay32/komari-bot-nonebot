"""TSK-197 real NoneBot + management assembly retirement acceptance.

The case runs in a fresh subprocess so root ``tests/conftest.py`` cannot install
its fake ``require`` registry or package shims.  It loads the real local
plugins, the real ORM plugin and the real FastAPI management assembly, drives
the real memory/context/user_data lifecycles, and replaces only the official
Hindsight SDK client boundary with a deterministic failure.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn
from urllib.parse import unquote, urlsplit
from uuid import uuid4

import asyncpg
import pytest
import redis.asyncio as aioredis

from komari_bot.context.redis_keys import RedisKeys
from tests.db.tsk197_gate_support import (
    POSTGRES_URL,
    SKIP_NO_POSTGRES,
    SQLALCHEMY_URL,
    drop_scratch_database,
    recreate_scratch_database,
    run_bootstrap,
    run_seed,
    same_database,
    scratch_url,
)

pytestmark = [SKIP_NO_POSTGRES, pytest.mark.asyncio]

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ASSEMBLY_TOKEN = "tsk197-local-admin-token-01"
ASSEMBLY_GROUP_PREFIX = "tsk197-assembly-canary"
REQUIRED_PLUGINS = frozenset(
    {
        "komari_memory",
        "komari_context",
        "komari_chat",
        "komari_management",
        "user_data",
        "group_admission",
        "komari_knowledge",
        "komari_help",
        "nonebot_plugin_orm",
    }
)

_ASSEMBLY_SCRIPT = r"""
import asyncio
import json
import os
import sys

from komari_bot.core.nonebot_compat import install_nonebot_forwardref_compatibility

install_nonebot_forwardref_compatibility()

import nonebot

nonebot.init(_env_file=None, driver="~fastapi", log_level="INFO")
nonebot.load_plugin("nonebot_plugin_orm")

stage = "load_plugins"
try:
    nonebot.load_plugins("komari_bot/plugins")

    from komari_bot.context.redis_store import MessageSchema
    from komari_bot.plugins import komari_context, komari_memory, user_data
    from komari_bot.plugins.komari_chat.prompt_schema import KomariChatPromptSchema
    from komari_bot.plugins.group_history_summary.prompt_schema import (
        GroupHistorySummaryPromptSchema,
    )
    from komari_bot.plugins.komari_decision.prompt_schema import (
        KomariDecisionPromptSchema,
    )
    from komari_bot.plugins.komari_memory.config_schema import (
        KomariMemoryConfigSchema,
    )
    from komari_bot.plugins.komari_memory.services import hindsight_service
    from tests.komari_memory.test_hindsight_service import _FakeBackend

    required_plugins = {
        "komari_memory",
        "komari_context",
        "komari_chat",
        "komari_management",
        "user_data",
        "group_admission",
        "komari_knowledge",
        "komari_help",
        "nonebot_plugin_orm",
    }
    loaded_names = {plugin.name for plugin in nonebot.get_loaded_plugins()}
    if not required_plugins <= loaded_names:
        raise AssertionError("required production plugins did not load")

    token = os.environ["TSK197_ASSEMBLY_TOKEN"]
    group_id = os.environ["TSK197_ASSEMBLY_GROUP"]
    headers = {"Authorization": f"Bearer {token}"}

    stage = "inspect_management"
    import httpx

    app = nonebot.get_driver().server_app
    transport = httpx.ASGITransport(app=app)

    async def inspect_management() -> dict[str, object]:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://assembly.test"
        ) as client:
            prompts = await client.get(
                "/api/v2/komari-management-prompt/resources", headers=headers
            )
            configs = await client.get(
                "/api/v2/komari-management-config/resources", headers=headers
            )
            memory = await client.get(
                "/api/v2/komari-management-config/resources/komari_memory",
                headers=headers,
            )
            old_route = await client.get(
                "/api/v2/komari-memory/conversations", headers=headers
            )
        if prompts.status_code != 200 or configs.status_code != 200:
            raise AssertionError("management resource APIs are not mounted")
        if memory.status_code != 200:
            raise AssertionError("Hindsight config resource is not mounted")
        prompt_ids = sorted(item["resource_id"] for item in prompts.json()["items"])
        expected_prompt_ids = sorted(
            {
                KomariChatPromptSchema.prompt_resource_id,
                GroupHistorySummaryPromptSchema.prompt_resource_id,
                KomariDecisionPromptSchema.prompt_resource_id,
            }
        )
        if prompt_ids != expected_prompt_ids:
            raise AssertionError("management Prompt resource set is not current")
        if "komari_memory_summary" in prompt_ids:
            raise AssertionError("retired memory-summary Prompt resource is mounted")
        memory_fields = sorted(memory.json()["fields"])
        expected_memory_fields = sorted(
            set(KomariMemoryConfigSchema.model_fields)
            - {"id", "revision", "updated_at"}
        )
        if memory_fields != expected_memory_fields:
            raise AssertionError("Hindsight config resource fields are not current")
        openapi_paths = set(app.openapi()["paths"])
        if any(path.startswith("/api/v2/komari-memory") for path in openapi_paths):
            raise AssertionError("retired memory management route is mounted")
        if old_route.status_code != 404:
            raise AssertionError("retired memory management route is reachable")
        return {
            "config_memory_present": "komari_memory"
            in {item["resource_id"] for item in configs.json()["items"]},
            "memory_config_fields": memory_fields,
            "old_memory_http_status": old_route.status_code,
            "old_memory_route_absent": True,
            "prompt_resource_ids": prompt_ids,
        }

    stage = "inspect_scheduler"
    from nonebot_plugin_apscheduler import scheduler

    job_ids = sorted(job.id for job in scheduler.get_jobs())
    if any(job_id.startswith("komari_memory_") for job_id in job_ids):
        raise AssertionError("retired memory worker is registered in scheduler")

    stage = "inspect_imports"
    retired_prefixes = (
        "komari_bot.plugins.komari_memory.agent",
        "komari_bot.plugins.komari_memory.api",
        "komari_bot.plugins.komari_memory.api_models",
        "komari_bot.plugins.komari_memory.core",
        "komari_bot.plugins.komari_memory.database",
        "komari_bot.plugins.komari_memory.handlers",
        "komari_bot.plugins.komari_memory.prompt_schema",
        "komari_bot.plugins.komari_memory.repositories",
        "komari_bot.plugins.komari_memory.services.admission",
        "komari_bot.plugins.komari_memory.services.conversation_processing",
        "komari_bot.plugins.komari_memory.services.conversation_processing_lifecycle",
        "komari_bot.plugins.komari_memory.services.forgetting_service",
        "komari_bot.plugins.komari_memory.services.interaction_event_summary_service",
        "komari_bot.plugins.komari_memory.services.llm_service",
        "komari_bot.plugins.komari_memory.services.memory_service",
        "komari_bot.plugins.komari_memory.services.message_chunking",
        "komari_bot.plugins.komari_memory.services.profile_compaction",
        "komari_bot.plugins.komari_memory.services.redis_keys",
        "komari_bot.plugins.komari_memory.services.redis_manager",
        "komari_bot.plugins.komari_memory.services.summary_prompt_template",
        "komari_bot.plugins.komari_memory.services.token_counter",
        "komari_bot.memory",
        "komari_bot.db.memory_agent_locks",
    )
    retired_imports = sorted(
        name
        for name in sys.modules
        if any(
            name == prefix or name.startswith(prefix + ".")
            for prefix in retired_prefixes
        )
    )
    if retired_imports:
        raise AssertionError("retired memory modules were imported")

    stage = "memory_startup"

    async def run_lifecycles() -> dict[str, object]:
        global stage
        stage = "inspect_management"
        management = await inspect_management()
        stage = "memory_startup"
        backend = _FakeBackend()

        async def _failing_bank(_client, _bank_id):
            raise ConnectionError("deterministic SDK boundary failure")

        backend.bank_handler = _failing_bank
        original_build_client = hindsight_service._build_client
        hindsight_service._build_client = lambda settings: backend.build_client()
        try:
            await komari_memory.startup()
        finally:
            hindsight_service._build_client = original_build_client

        sdk_bank_boundary_calls = len(backend.bank_calls)
        if sdk_bank_boundary_calls != 1:
            raise AssertionError(
                "fake SDK bank boundary was not called exactly once"
            )
        service = komari_memory.get_hindsight_memory()
        if service is None:
            raise AssertionError("memory startup did not retain its real caller")
        if service._ready:
            raise AssertionError("Hindsight failure unexpectedly marked the caller ready")

        stage = "context_startup"
        await komari_context._startup_context()
        stage = "user_data_startup"
        await user_data.on_startup()
        store = komari_context.get_context_store()
        if store is None:
            raise AssertionError("context lifecycle did not expose a store")
        message = MessageSchema(
            user_id="assembly-user",
            user_nickname="装配用户",
            group_id=group_id,
            content="assembly canary",
            timestamp=1.0,
            message_id="assembly-message",
        )
        await store.push_message(group_id, message)
        context_before = [item.message_id for item in await store.get_buffer(group_id)]
        user_count_before = await user_data.get_user_count()
        if context_before != ["assembly-message"]:
            raise AssertionError("context public append/read is not usable")
        if not isinstance(user_count_before, int):
            raise AssertionError("user_data public count is not usable")

        stage = "memory_shutdown"
        await komari_memory.shutdown()
        if komari_memory.get_hindsight_memory() is not None:
            raise AssertionError("memory shutdown did not close its caller")
        await store.push_message(group_id, message)
        context_after_memory = [
            item.message_id for item in await store.get_buffer(group_id)
        ]
        user_count_after_memory = await user_data.get_user_count()
        if context_after_memory != ["assembly-message", "assembly-message"]:
            raise AssertionError("memory shutdown destroyed the shared Redis resource")
        if not isinstance(user_count_after_memory, int):
            raise AssertionError(
                "memory shutdown destroyed the shared user_data resource"
            )

        stage = "context_shutdown"
        await komari_context._shutdown_context_hook()
        if komari_context.get_context_store() is not None:
            raise AssertionError("context shutdown did not close its store")
        user_count_after_context = await user_data.get_user_count()
        if not isinstance(user_count_after_context, int):
            raise AssertionError(
                "context shutdown destroyed the shared user_data resource"
            )
        stage = "user_data_shutdown"
        await user_data.on_shutdown()
        return {
            **management,
            "sdk_bank_boundary_calls": sdk_bank_boundary_calls,
            "context_usable": context_before == ["assembly-message"],
            "context_usable_after_memory_failure": context_after_memory
            == ["assembly-message", "assembly-message"],
            "memory_failure_isolated": service._ready is False,
            "memory_startup_did_not_escape": True,
            "shutdown_context_clean": True,
            "shutdown_memory_clean": True,
            "user_data_usable": isinstance(user_count_before, int),
            "user_data_usable_after_memory_failure": isinstance(
                user_count_after_memory, int
            ),
            "user_data_usable_after_context_shutdown": isinstance(
                user_count_after_context, int
            ),
        }

    lifecycle = asyncio.run(run_lifecycles())
    print(
        "RESULT_JSON:"
        + json.dumps(
            {
                **lifecycle,
                "retired_jobs_absent": True,
                "retired_modules_absent": True,
                "required_plugins_loaded": sorted(required_plugins),
            }
        )
    )
except BaseException as exc:
    print(f"RESULT_ERROR:{stage}:{type(exc).__name__}")
    raise
"""


def _invalid_redis_config(message: str) -> NoReturn:
    raise ValueError(message)


def _assembly_error(message: str) -> NoReturn:
    raise AssertionError(message)


@dataclass(frozen=True, slots=True)
class _RedisTestSettings:
    """Exact Redis endpoint selected for this assembly run."""

    host: str
    port: int
    db: int
    password: str


def _parse_redis_test_url(url: str) -> _RedisTestSettings:
    """Parse the supported redis:// transport without silently retargeting."""
    parsed = urlsplit(url)
    if parsed.scheme != "redis":
        _invalid_redis_config("KOMARI_TEST_REDIS_URL must use the redis:// transport")
    if parsed.username:
        _invalid_redis_config("Redis ACL usernames are not supported by this test")
    if parsed.hostname is None or not parsed.hostname.strip():
        _invalid_redis_config("KOMARI_TEST_REDIS_URL must include a host")
    if parsed.query or parsed.fragment:
        _invalid_redis_config(
            "KOMARI_TEST_REDIS_URL must not include query or fragment"
        )
    if not parsed.path or not parsed.path.startswith("/"):
        _invalid_redis_config("KOMARI_TEST_REDIS_URL must include an explicit database")
    path_db = parsed.path.removeprefix("/")
    if not path_db.isdigit():
        _invalid_redis_config("KOMARI_TEST_REDIS_URL database path must be an integer")
    db = int(path_db)
    if db < 0:
        _invalid_redis_config("KOMARI_TEST_REDIS_URL database must be non-negative")
    port = parsed.port if parsed.port is not None else 6379
    if not 0 < port < 65536:
        _invalid_redis_config("KOMARI_TEST_REDIS_URL port is out of range")
    password = unquote(parsed.password) if parsed.password is not None else ""
    return _RedisTestSettings(
        host=parsed.hostname,
        port=port,
        db=db,
        password=password,
    )


@pytest.mark.parametrize(
    "url",
    (
        "rediss://localhost:6379/11",
        "redis://user:pass@localhost:6379/11",
        "redis://localhost:6379",
        "redis://localhost:6379/not-a-db",
        "redis://localhost:6379/11?retry=1",
    ),
)
async def test_unsupported_redis_test_url_forms_are_rejected(url: str) -> None:
    """Unsupported transports/ACL/ambiguous database forms fail explicitly."""
    with pytest.raises(ValueError):
        _parse_redis_test_url(url)


async def test_password_only_redis_test_url_is_accepted() -> None:
    """Password-only URLs remain supported; only ACL usernames are rejected."""
    settings = _parse_redis_test_url("redis://:local-test-pass@localhost:6380/9")
    assert settings.host == "localhost"
    assert settings.port == 6380
    assert settings.db == 9
    assert settings.password == "local-test-pass"


def _explicit_redis_test_settings() -> _RedisTestSettings | None:
    """Select URL first, otherwise explicit REDIS_*; never default a DB."""
    url = os.getenv("KOMARI_TEST_REDIS_URL", "").strip()
    if url:
        return _parse_redis_test_url(url)

    host = os.getenv("REDIS_HOST", "").strip()
    port_raw = os.getenv("REDIS_PORT", "").strip()
    db_raw = os.getenv("REDIS_DB", "").strip()
    if not (host and port_raw and db_raw):
        return None
    if not port_raw.isdigit() or not db_raw.isdigit():
        _invalid_redis_config("REDIS_PORT and REDIS_DB must be integers")
    port = int(port_raw)
    db = int(db_raw)
    if not 0 < port < 65536:
        _invalid_redis_config("REDIS_PORT is out of range")
    if db < 0:
        _invalid_redis_config("REDIS_DB must be non-negative")
    return _RedisTestSettings(
        host=host,
        port=port,
        db=db,
        password=os.getenv("REDIS_PASSWORD", ""),
    )


def _child_env(
    database_url: str,
    *,
    redis_settings: _RedisTestSettings,
    canary_group: str,
) -> dict[str, str]:
    env = os.environ.copy()
    env["SQLALCHEMY_DATABASE_URL"] = database_url
    env["PYTHONPATH"] = str(PROJECT_ROOT)
    env["LOG_LEVEL"] = "INFO"
    env["TSK197_ASSEMBLY_TOKEN"] = ASSEMBLY_TOKEN
    env["TSK197_ASSEMBLY_GROUP"] = canary_group
    # Use exactly the endpoint selected for the persisted context row and
    # parent cleanup; do not let inherited REDIS_* values retarget the child.
    env["REDIS_HOST"] = redis_settings.host
    env["REDIS_PORT"] = str(redis_settings.port)
    env["REDIS_DB"] = str(redis_settings.db)
    env["REDIS_PASSWORD"] = redis_settings.password
    env.pop("KOMARI_TEST_REDIS_URL", None)
    env.pop("KOMARI_TEST_REDIS_SOCKET", None)
    return env


def _run_assembly(
    database_url: str,
    *,
    redis_settings: _RedisTestSettings,
    canary_group: str,
) -> dict[str, Any]:
    result = subprocess.run(
        [sys.executable, "-c", _ASSEMBLY_SCRIPT],
        cwd=PROJECT_ROOT,
        env=_child_env(
            database_url,
            redis_settings=redis_settings,
            canary_group=canary_group,
        ),
        capture_output=True,
        text=True,
        check=False,
        timeout=240,
    )
    error_marker = "RESULT_ERROR:"
    error_line = next(
        (line for line in result.stdout.splitlines() if line.startswith(error_marker)),
        "RESULT_ERROR:unknown:Unknown",
    )
    stage_name = error_line[len(error_marker) :]
    if result.returncode != 0:
        _assembly_error(
            "real assembly subprocess failed "
            f"(returncode={result.returncode}, stage={stage_name})"
        )
    marker = "RESULT_JSON:"
    for line in result.stdout.splitlines():
        if line.startswith(marker):
            payload: dict[str, Any] = json.loads(line[len(marker) :])
            return payload
    _assembly_error(
        f"real assembly subprocess exited zero without a result (stage={stage_name})"
    )


async def _insert_assembly_configs(
    connection: asyncpg.Connection,
    *,
    redis_db: int,
) -> None:
    await connection.execute(
        "INSERT INTO komari_management_config "
        "(id, revision, updated_at, plugin_enable, api_credentials, "
        " api_allowed_origins, announce_status_page_url, announce_max_group_count, "
        " announce_send_interval_seconds, announce_request_cooldown_seconds) "
        "VALUES (1, 1, NOW(), TRUE, $1::jsonb, '[]'::jsonb, "
        "        'https://status.invalid', 20, 1.0, 30.0) "
        "ON CONFLICT (id) DO UPDATE SET "
        "plugin_enable = TRUE, api_credentials = EXCLUDED.api_credentials",
        json.dumps(
            [
                {
                    "credential_id": "tsk197-assembly",
                    "token": ASSEMBLY_TOKEN,
                    "permissions": ["*"],
                }
            ]
        ),
    )
    await connection.execute(
        "INSERT INTO komari_context_config "
        "(id, revision, updated_at, redis_db, bot_nickname, error_notify_enabled) "
        "VALUES (1, 1, NOW(), $1, '小鞠', TRUE) "
        "ON CONFLICT (id) DO UPDATE SET redis_db = EXCLUDED.redis_db",
        redis_db,
    )
    await connection.execute(
        "INSERT INTO komari_memory_config "
        "(id, revision, updated_at, plugin_enable, hindsight_base_url, "
        " hindsight_api_key, hindsight_bank_id, "
        " hindsight_request_timeout_seconds, hindsight_recovery_interval_seconds) "
        "VALUES (1, 1, NOW(), TRUE, 'https://hindsight.invalid', "
        "        'local-test-key-not-real', 'assembly-bank', 1.0, 0.01) "
        "ON CONFLICT (id) DO UPDATE SET "
        "plugin_enable = TRUE, hindsight_base_url = EXCLUDED.hindsight_base_url",
    )


async def _delete_assembly_redis_key(
    settings: _RedisTestSettings,
    canary_group: str,
) -> None:
    client = aioredis.Redis(
        host=settings.host,
        port=settings.port,
        db=settings.db,
        password=settings.password or None,
        decode_responses=True,
    )
    try:
        await client.delete(RedisKeys.buffer(canary_group))
    finally:
        await client.aclose()


async def test_real_assembly_retires_old_memory_surfaces_and_keeps_independent_resources() -> (
    None
):
    if not same_database(POSTGRES_URL, SQLALCHEMY_URL):
        pytest.skip("KOMARI_TEST_POSTGRES_URL 与 SQLALCHEMY_DATABASE_URL 不一致")

    try:
        redis_settings = _explicit_redis_test_settings()
    except ValueError as error:
        pytest.fail(f"invalid Redis test configuration: {error}")
    if redis_settings is None:
        pytest.skip(
            "未配置显式 Redis 测试连接（KOMARI_TEST_REDIS_URL 或 REDIS_HOST/PORT/DB）"
        )

    canary_group = f"{ASSEMBLY_GROUP_PREFIX}-{uuid4().hex}"
    suffix = f"_tsk197assembly_{uuid4().hex[:8]}"
    scratch = await recreate_scratch_database(suffix)
    database_url = scratch_url(str(scratch["database"]))
    try:
        result = run_bootstrap(database_url, "upgrade", "head")
        assert result.returncode == 0, result.stderr
        seed = run_seed(database_url)
        assert seed.returncode == 0, f"{seed.stdout}\n{seed.stderr}"

        connection = await asyncpg.connect(**scratch)
        try:
            await _insert_assembly_configs(
                connection,
                redis_db=redis_settings.db,
            )
        finally:
            await connection.close()

        payload = _run_assembly(
            database_url,
            redis_settings=redis_settings,
            canary_group=canary_group,
        )
        assert payload["required_plugins_loaded"] == sorted(REQUIRED_PLUGINS)
        assert payload["prompt_resource_ids"] == [
            "group_history_summary",
            "komari_chat",
            "komari_decision",
        ]
        assert payload["memory_config_fields"] == [
            "hindsight_api_key",
            "hindsight_bank_id",
            "hindsight_base_url",
            "hindsight_recovery_interval_seconds",
            "hindsight_request_timeout_seconds",
            "plugin_enable",
        ]
        assert payload["config_memory_present"] is True
        assert payload["old_memory_route_absent"] is True
        assert payload["old_memory_http_status"] == 404
        assert payload["retired_jobs_absent"] is True
        assert payload["retired_modules_absent"] is True
        assert payload["memory_startup_did_not_escape"] is True
        assert payload["memory_failure_isolated"] is True
        assert payload["sdk_bank_boundary_calls"] == 1
        assert payload["context_usable"] is True
        assert payload["context_usable_after_memory_failure"] is True
        assert payload["user_data_usable"] is True
        assert payload["user_data_usable_after_memory_failure"] is True
        assert payload["user_data_usable_after_context_shutdown"] is True
        assert payload["shutdown_memory_clean"] is True
        assert payload["shutdown_context_clean"] is True
    finally:
        await _delete_assembly_redis_key(redis_settings, canary_group)
        await drop_scratch_database(str(scratch["database"]))
