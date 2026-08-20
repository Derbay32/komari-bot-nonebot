"""Komari Chat 视觉读图服务测试。"""

from __future__ import annotations

import asyncio
import json
from importlib import import_module
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest

vision_service_module = import_module("komari_bot.plugins.komari_chat.services.vision_service")

#: TSK-195 第二轮安全验收：secret URL path/query/token 与 data URI（模拟
#: provider 异常正文可能内嵌的敏感内容）。
_SECRET_URL = "https://secret.example/private/photo.png?token=abc123"
_SECRET_DATA_URI = "data:image/png;base64,SUMSECRETUX=="


class _FakeConfigManager:
    @staticmethod
    def get() -> SimpleNamespace:
        return SimpleNamespace(
            api_token="token",
            api_base="https://example.test/v1",
            timeout_seconds=30,
        )


class _FakeLLMProvider:
    """伪造 llm_provider，模拟 generate_messages_completion 视觉调用。"""

    active: ClassVar[int] = 0
    max_active: ClassVar[int] = 0
    fail_next: ClassVar[bool] = False

    @classmethod
    def reset(cls) -> None:
        cls.active = 0
        cls.max_active = 0
        cls.fail_next = False

    async def generate_messages_completion(self, **kwargs: Any) -> SimpleNamespace:
        self.__class__.active += 1
        self.__class__.max_active = max(
            self.__class__.max_active,
            self.__class__.active,
        )
        try:
            await asyncio.sleep(0.01)
            if self.__class__.fail_next:
                self.__class__.fail_next = False
                msg = "视觉模型故障"
                raise RuntimeError(msg)
            image_url = kwargs["messages"][0]["content"][1]["image_url"]["url"]
            return SimpleNamespace(
                content=f"图片描述：{image_url}",
                finish_reason="stop",
                duration_ms=50.0,
                usage=None,
            )
        finally:
            self.__class__.active -= 1


def _vision_description_field() -> str:
    """从 Schema 按职责解析视觉描述字段名（当前为 vision_description_prompt）。"""
    from tests.config.chat_prompt_field_contract import (
        chat_prompt_field_names,
        resolve_behavior_field_names,
    )

    return resolve_behavior_field_names(chat_prompt_field_names())["视觉描述"]


async def _fake_vision_description_template() -> dict[str, str]:
    """经公开 loader seam 返回的非空测试视觉描述 Prompt（TSK-190 AC5，不触 DB）。"""
    return {_vision_description_field(): "TEST-VISION-DESCRIPTION-PROMPT"}


def _patch_vision_prompt_loader(
    monkeypatch: pytest.MonkeyPatch,
    loader: Any,
) -> None:
    """经 chat Prompt 公开 loader seam 注入替身 loader。

    vision_service 可能以 ``from ... import get_template`` 本地绑定使用
    loader，也可能经 ``prompt_template.get_template`` 属性访问；两种风格
    都覆盖，不锁死 import 风格。
    """
    from komari_bot.plugins.komari_chat.services import (
        prompt_template as chat_prompt_template,
    )

    monkeypatch.setattr(chat_prompt_template, "get_template", loader)
    if hasattr(vision_service_module, "get_template"):
        monkeypatch.setattr(vision_service_module, "get_template", loader)


def _patch_vision_dependencies(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeLLMProvider.reset()
    monkeypatch.setattr(vision_service_module, "llm_provider_config_manager", _FakeConfigManager())
    monkeypatch.setattr(vision_service_module, "llm_provider", _FakeLLMProvider())
    # 视觉描述 Prompt 经 chat Prompt 公开 loader seam 注入非空测试值，
    # 既有并发/错误/collector 用例不依赖真实数据库。
    _patch_vision_prompt_loader(monkeypatch, _fake_vision_description_template)


@pytest.mark.asyncio
async def test_read_images_limits_model_call_concurrency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_vision_dependencies(monkeypatch)
    monkeypatch.setattr(vision_service_module, "_VISION_READ_SEMAPHORE", asyncio.Semaphore(2))

    result = await vision_service_module.read_images(
        ["image-1", "image-2", "image-3", "image-4"],
        vision_model="vision-model",
    )

    assert result == [
        "图片描述：image-1",
        "图片描述：image-2",
        "图片描述：image-3",
        "图片描述：image-4",
    ]
    assert _FakeLLMProvider.max_active <= 2


@pytest.mark.asyncio
async def test_read_images_returns_empty_list_for_empty_input() -> None:
    assert await vision_service_module.read_images([], vision_model="vision-model") == []


@pytest.mark.asyncio
async def test_read_images_formats_single_image_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """视觉失败回主模型文本只含稳定异常类型（TSK-195 第二轮安全验收反馈）。"""
    _patch_vision_dependencies(monkeypatch)
    _FakeLLMProvider.fail_next = True

    result = await vision_service_module.read_images(
        ["image-1"],
        vision_model="vision-model",
    )

    assert result == ["[图片读取失败: RuntimeError]"]


@pytest.mark.asyncio
async def test_read_images_passes_trace_to_collector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """视觉调用在传入 collector 时记录 LLMCallTrace。"""
    _patch_vision_dependencies(monkeypatch)
    from komari_bot.plugins.agent_run_logger.diagnostic import LLMDiagnosticCollector

    collector = LLMDiagnosticCollector(request_id="test-vision-trace")
    result = await vision_service_module.read_images(
        ["image-trace"],
        vision_model="vision-model",
        request_trace_id="trace-1",
        parent_call_id="parent-1",
        collector=collector,
    )

    assert result == ["图片描述：image-trace"]
    assert len(collector.calls) == 1
    assert collector.calls[0].phase == "vision_read_image"
    assert collector.calls[0].parent_call_id == "parent-1"


@pytest.mark.asyncio
async def test_read_images_records_error_in_collector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """视觉调用失败时记录错误到 collector。"""
    _patch_vision_dependencies(monkeypatch)
    _FakeLLMProvider.fail_next = True
    from komari_bot.plugins.agent_run_logger.diagnostic import LLMDiagnosticCollector

    collector = LLMDiagnosticCollector(request_id="test-vision-error")
    result = await vision_service_module.read_images(
        ["image-fail"],
        vision_model="vision-model",
        collector=collector,
    )

    assert "图片读取失败" in result[0]
    assert len(collector.errors) >= 1
    assert collector.errors[0]["type"] == "RuntimeError"


@pytest.mark.asyncio
async def test_read_image_uses_db_snapshot_prompt_for_vision_description(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC5：视觉描述调用从 PostgreSQL 快照读取可调行为 Prompt。

    通过公开 loader seam（chat prompt 模板 ``get_template``）注入替身值，
    捕获发送给 LLM 提供者的真实请求载荷，断言描述文本来自快照而非
    Python 常量；不真正调用外部模型。当前实现经 loader 读取快照，
    本用例验证 DB 快照值逐字到达视觉子调用载荷。
    """
    from komari_bot.plugins.komari_chat.services import (
        prompt_template as chat_prompt_template,
    )
    from tests.config.chat_prompt_field_contract import (
        chat_prompt_field_names,
        resolve_behavior_field_names,
    )

    _patch_vision_dependencies(monkeypatch)

    captured: list[dict[str, Any]] = []

    # 视觉描述字段名不在 ticket 中唯一指定：按职责从 Schema 解析，字段
    # 缺失时在责任解析处清晰失败（当前 RED）；Schema 落地后再验证行为。
    vision_description_field = resolve_behavior_field_names(
        chat_prompt_field_names()
    )["视觉描述"]

    class _CapturingProvider:
        async def generate_messages_completion(self, **kwargs: Any) -> Any:
            captured.append(kwargs)
            return SimpleNamespace(
                content="图片描述：已按快照 Prompt 生成",
                finish_reason="stop",
                duration_ms=10.0,
                usage=None,
            )

    async def fake_get_template() -> dict[str, str]:
        return {vision_description_field: "DB-SNAPSHOT-VISION-PROMPT"}

    monkeypatch.setattr(vision_service_module, "llm_provider", _CapturingProvider())
    # monkeypatch 源模块属性无法截获已 ``from ... import get_template`` 绑定
    # 的本地符号；兼容两种正常实现：
    # - vision_service 模块有本地 ``get_template``（导入绑定）→ patch 该绑定；
    # - 否则走 ``chat_prompt_template.get_template`` 属性访问 → patch 源模块。
    # 无论哪种，视觉服务都必须经由 chat Prompt 的公开 loader 契约取数，
    # 不允许回退到 Python 常量。
    if hasattr(vision_service_module, "get_template"):
        monkeypatch.setattr(vision_service_module, "get_template", fake_get_template)
    else:
        monkeypatch.setattr(chat_prompt_template, "get_template", fake_get_template)

    result = await vision_service_module.read_images(
        ["data:image/png;base64,seed-fixture"],
        vision_model="vision-model",
    )

    assert result == ["图片描述：已按快照 Prompt 生成"]
    assert len(captured) == 1
    messages = captured[0]["messages"]
    assert messages[0]["role"] == "user"
    content = messages[0]["content"]
    assert content[0]["type"] == "text"
    assert content[0]["text"] == "DB-SNAPSHOT-VISION-PROMPT"
    assert "详细描述这张图片" not in content[0]["text"]


@pytest.mark.asyncio
async def test_read_images_propagates_loader_cold_start_failure_without_llm_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC5/AC7：公开 loader 冷启动失败必须向上传播，不得降级为空文本继续调用。

    非空图片输入时 ``read_images`` 必须把 ``RuntimeError("Prompt ...")``
    传播给调用方，且不调用 LLM provider；禁止降级为空字符串、Python
    常量或继续视觉调用。当前实现把 loader 异常吞掉并降级为空 Prompt
    继续生成，因此本用例是 TSK-190 的可解释 RED。
    """
    _FakeLLMProvider.reset()
    monkeypatch.setattr(
        vision_service_module,
        "llm_provider_config_manager",
        _FakeConfigManager(),
    )
    monkeypatch.setattr(vision_service_module, "llm_provider", _FakeLLMProvider())

    async def cold_start_failure() -> dict[str, str]:
        msg = "Prompt 的 PostgreSQL 初始数据不可用且无缓存，无法冷启动: komari_chat"
        raise RuntimeError(msg)

    _patch_vision_prompt_loader(monkeypatch, cold_start_failure)

    with pytest.raises(RuntimeError, match=r"^Prompt"):
        await vision_service_module.read_images(
            ["image-cold-start"],
            vision_model="vision-model",
        )
    assert _FakeLLMProvider.max_active == 0, (
        "loader 失败时不得调用 LLM provider"
    )


class _CapturingLogRecorder:
    """记录日志调用与 kwargs，用于断言没有 exc_info/敏感正文。"""

    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    def _record(
        self,
        level: str,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> None:
        self.records.append({"level": level, "args": args, "kwargs": dict(kwargs)})

    def info(self, *args: Any, **kwargs: Any) -> None:
        self._record("info", args, kwargs)

    def warning(self, *args: Any, **kwargs: Any) -> None:
        self._record("warning", args, kwargs)

    def error(self, *args: Any, **kwargs: Any) -> None:
        self._record("error", args, kwargs)

    def debug(self, *args: Any, **kwargs: Any) -> None:
        self._record("debug", args, kwargs)

    def exception(self, *args: Any, **kwargs: Any) -> None:
        self._record("exception", args, kwargs)


@pytest.mark.asyncio
async def test_read_images_failure_normalized_without_secret_leak(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TSK-195 第二轮：provider 异常正文内嵌 secret URL/data URI 时，视觉
    失败回主模型与收集器的文本只含稳定异常类型，绝不泄漏 URL/base64；
    日志不捕获 traceback（无 exc_info）且不含敏感正文。"""
    _patch_vision_dependencies(monkeypatch)
    recorder = _CapturingLogRecorder()
    monkeypatch.setattr(vision_service_module, "logger", recorder)

    class _SecretLeakingProvider:
        async def generate_messages_completion(self, **kwargs: Any) -> Any:
            del kwargs
            msg = f"provider failed {_SECRET_URL} {_SECRET_DATA_URI}"
            raise RuntimeError(msg)

    monkeypatch.setattr(
        vision_service_module,
        "llm_provider",
        _SecretLeakingProvider(),
    )
    from komari_bot.plugins.agent_run_logger.diagnostic import LLMDiagnosticCollector

    collector = LLMDiagnosticCollector(request_id="test-vision-secret")
    result = await vision_service_module.read_images(
        [_SECRET_DATA_URI],
        vision_model="vision-model",
        request_trace_id="trace-secret",
        parent_call_id="parent-secret",
        collector=collector,
    )

    # 回主模型的工具文本只含稳定异常类型，绝不携带 provider 原始正文
    assert result == ["[图片读取失败: RuntimeError]"]
    assert _SECRET_URL not in result[0]
    assert _SECRET_DATA_URI not in result[0]

    # collector 内存 errors / call error 只含安全归一化信息
    assert len(collector.errors) == 1
    assert collector.errors[0]["type"] == "RuntimeError"
    assert collector.errors[0]["message"] == "[图片读取失败: RuntimeError]"
    assert _SECRET_URL not in str(collector.errors)
    assert _SECRET_DATA_URI not in str(collector.errors)
    assert len(collector.calls) == 1
    call = collector.calls[0]
    assert call.status == "error"
    assert call.phase == "vision_read_image"
    assert call.parent_call_id == "parent-secret"
    assert call.error == {
        "type": "RuntimeError",
        "message": "[图片读取失败: RuntimeError]",
    }
    assert _SECRET_URL not in str(collector.calls)
    assert _SECRET_DATA_URI not in str(collector.calls)

    # 最终 projection（含 request 的既有结构化 trace）也不含敏感正文；
    # 请求对象走现有 sanitizer 保留二进制摘要。
    collector.mark_finished(status="error", error=RuntimeError("boom"))
    record = collector.build_record()
    record_json = json.dumps(record, ensure_ascii=False, default=str)
    assert _SECRET_URL not in record_json
    assert _SECRET_DATA_URI not in record_json
    assert "/private/" not in record_json and "photo.png" not in record_json
    assert "token=abc123" not in record_json
    image_url_summary = record["rounds"][0]["request"]["messages"][0]["content"][1][
        "image_url"
    ]["url"]
    assert image_url_summary["redacted_binary"] is True

    # 日志不捕获 traceback（无 exc_info/exception）且不含敏感正文
    assert recorder.records, "视觉失败应产生日志"
    for item in recorder.records:
        assert "exc_info" not in item["kwargs"], (
            f"[{item['level']}] 日志不得携带 traceback 捕获参数: {item['kwargs']}"
        )
        serialized = json.dumps(item, ensure_ascii=False, default=str)
        assert _SECRET_URL not in serialized
        assert _SECRET_DATA_URI not in serialized
        assert "/private/" not in serialized and "photo.png" not in serialized
        assert "token=abc123" not in serialized


async def test_read_images_empty_input_does_not_touch_prompt_loader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC5：空图片列表直接返回 []，无需访问 loader。"""

    async def fail_if_called() -> dict[str, str]:
        msg = "空图片列表不应访问 chat Prompt loader"
        raise AssertionError(msg)

    _patch_vision_prompt_loader(monkeypatch, fail_if_called)

    assert await vision_service_module.read_images([], vision_model="vision-model") == []
