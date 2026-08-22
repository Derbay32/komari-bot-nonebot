"""TSK-225 真实业务 seam 补齐红基线（第 2 部分）。

补齐 vision_completion / tool_fetch_page / image_download / reply_send
行为级红断（AC-2）+ AC3（阶段屏障）+ AC4（send 粒度）+ AC5（sibling 隔离）
+ AC6（恢复不复活）。驱动真实生产 seam；未接入准入故预期红。
"""

from __future__ import annotations

import asyncio
import importlib
from types import SimpleNamespace
from typing import Any, cast

import pytest

from tests.group_admission.chat_admission_support import (
    ScriptedAdjudicate,
    install_scripted_adjudicate,
)

pytestmark = pytest.mark.group_admission_acceptance
GROUP_ID = 100

mh = importlib.import_module("komari_bot.plugins.komari_chat.handlers.message_handler")
llm = importlib.import_module("komari_bot.plugins.komari_chat.services.llm_service")
vs = importlib.import_module("komari_bot.plugins.komari_chat.services.vision_service")
irs = importlib.import_module("komari_bot.plugins.komari_chat.services.image_reading_session")
idm = importlib.import_module("komari_bot.plugins.komari_chat.services.image_downloader")
init = importlib.import_module("komari_bot.plugins.komari_chat")


def _pol(**o: object) -> Any:
    v = {"max_images": 4, "max_image_bytes": 8 << 20, "max_total_bytes": 20 << 20,
         "max_pixels": 40_000_000, "concurrency": 2, "connect_timeout_seconds": 5.0,
         "read_timeout_seconds": 30.0, "total_timeout_seconds": 45.0}
    v.update(o)
    return idm.ImageDownloadPolicy(**v)  # type: ignore[arg-type]


class _DL:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def download(self, url: str) -> str | None:
        self.calls.append(url)
        return "data:image/png;base64,QQ=="

    async def close(self) -> None:
        return None


def _sess(monkeypatch: pytest.MonkeyPatch, dl: Any,
          quoted: list[str] | None = None, current: list[str] | None = None) -> Any:
    monkeypatch.setattr(irs, "ImageDownloadSession", lambda _p: dl)
    vis = SimpleNamespace(__call__=lambda _images, **_kw: ["d"])
    monkeypatch.setattr(irs, "read_images", vis)
    return irs.ImageReadingSession.build(
        quoted_sources=quoted or [], current_sources=current or [],
        policy=_pol(), vision_model="v", vision_temperature=0.3,
        vision_max_tokens=1024, request_trace_id="t", collector=None)


async def test_vision_completion_restricted_blocks_vision_llm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    s = ScriptedAdjudicate("restricted")
    install_scripted_adjudicate(monkeypatch, s)
    monkeypatch.setattr(vs, "llm_provider_config_manager",
                        SimpleNamespace(get=lambda: SimpleNamespace(api_token="x")))
    calls: list[dict[str, Any]] = []

    class _P:
        async def generate_messages_completion(self, **kw: Any) -> Any:
            calls.append(kw)
            return SimpleNamespace(content="视觉")

    monkeypatch.setattr(vs, "llm_provider", _P())
    await vs._read_single_image(
        image_data_uri="data:image/png;base64,QQ==", image_index=0,
        vision_model="v", vision_description_prompt="p",
        temperature=0.3, max_tokens=1024,
    )
    assert s.calls != [], "vision 前未裁决"
    assert calls == [], "受限下仍调用视觉 LLM"


async def test_fetch_page_restricted_blocks_fetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    s = ScriptedAdjudicate("restricted")
    install_scripted_adjudicate(monkeypatch, s)
    fc: list[dict[str, Any]] = []

    async def f(urls: list[str], **kw: object) -> str:
        fc.append({"urls": urls, **kw})
        return "[正文]"

    monkeypatch.setattr(llm, "komari_search",
                        SimpleNamespace(fetch_page=f))
    tc = SimpleNamespace(id="c", type="function",
                         function=SimpleNamespace(name="fetch_page",
                                                  arguments="{}"),
                         raw_arguments="{}",
                         parsed_arguments={"urls": ["https://x/a"]})
    await llm._execute_business_tool(
        tool_call=tc, image_session=None, phase_prefix="p", round_num=1,
        memory_service=None, group_id=str(GROUP_ID),
        allowed_profile_user_ids=frozenset(), caller_user_id=None,
        caller_group_id=None, caller_is_superuser=False, request_trace_id="t")
    assert s.calls != [], "fetch_page 前未裁决"
    assert fc == [], "受限下仍发起 fetch_page"


async def test_image_download_restricted_blocks_download(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    s = ScriptedAdjudicate("restricted")
    install_scripted_adjudicate(monkeypatch, s)
    dl = _DL()
    session = _sess(monkeypatch, dl, quoted=["https://example.com/a.png"])
    await session.read(0)
    assert s.calls != [], "下载前未裁决"
    assert dl.calls == [], "受限下仍触发图片下载"


async def _perm_ok(_a: object, _b: object, _c: object) -> tuple[bool, str]:
    del _a, _b, _c
    return (True, "")


async def _not_banned(_a: object, _b: object, _c: object) -> bool:
    del _a, _b, _c
    return False


def _pending() -> SimpleNamespace:
    m = SimpleNamespace(group_id=str(GROUP_ID))
    return SimpleNamespace(message=m, reply="hi", reply_to_message_id="9",
                           decision_id="d9", decision_payload={}, reason="at")


async def test_reply_send_restricted_blocks_outbound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    s = ScriptedAdjudicate("admitted")
    s.set_sequence("admitted", "restricted")  # provider admitted, send restricted
    install_scripted_adjudicate(monkeypatch, s)
    mod = _fresh_chat_module(monkeypatch)
    monkeypatch.setattr(mod, "permission_manager_plugin",
                        SimpleNamespace(check_runtime_permission=_perm_ok),
                        raising=False)
    monkeypatch.setattr(mod, "user_ban_plugin",
                        SimpleNamespace(is_event_banned=_not_banned),
                        raising=False)
    monkeypatch.setattr(
        mod, "get_memory_config",
        lambda: SimpleNamespace(plugin_enable=True, face_reaction_enabled=False,
                                face_reaction_id=None, error_notify_enabled=False),
        raising=False)
    monkeypatch.setattr(mod, "get_config", lambda: SimpleNamespace(),
                        raising=False)
    sent: list[str] = []

    class _Sender:
        def __init__(self, bot: object) -> None:
            self.bot = bot

        async def __call__(self, _req: object) -> None:
            sent.append("sent")

    class _Handler:
        async def process_message(self, _bot: object, _event: object,
                                  **kw: object) -> Any:
            del _bot, _event, kw
            return _pending()
        async def report_reply_failure(self, **kw: object) -> None:
            del kw
        def _log_decision(self, _obj: object = None) -> None:
            return None

    class _Wf:
        async def fulfill(self, reply: object, **kw: object) -> bool:
            sr = cast("Any", kw.get("send_reply"))
            if sr is not None:
                await sr(reply)
            return True

    monkeypatch.setattr(mod, "_get_or_build_handler",
                        lambda: _Handler(), raising=False)
    monkeypatch.setattr(mod, "_get_or_build_reply_fulfillment",
                        lambda: _Wf(), raising=False)
    monkeypatch.setattr(mod, "OneBotReplySender", _Sender, raising=False)
    bot = SimpleNamespace(self_id="12345", type="OneBot V11")
    event = SimpleNamespace(group_id=GROUP_ID, message_id=9, user_id="7")
    await mod.handle_group_message(bot, event)
    assert s.calls != [], "reply_send 前未裁决"
    assert sent == [], "受限下仍执行群回复 send/write"


async def test_phase_barrier_admitted_then_restricted_blocks_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    s = ScriptedAdjudicate("admitted")
    s.set_sequence("admitted", "restricted")  # 入口获准，效果启动前翻转受限
    install_scripted_adjudicate(monkeypatch, s)
    monkeypatch.setattr(vs, "llm_provider_config_manager",
                        SimpleNamespace(get=lambda: SimpleNamespace(api_token="x")))
    started = asyncio.Event()

    async def gated(**kw: object) -> Any:
        del kw
        # 效果真正启动的屏障信号：受限（已翻转）时不得置位。
        started.set()
        return SimpleNamespace(content="d")

    monkeypatch.setattr(vs, "llm_provider",
                        SimpleNamespace(generate_messages_completion=gated))
    await vs._read_single_image(
        image_data_uri="data:image/png;base64,QQ==", image_index=0,
        vision_model="v", vision_description_prompt="p",
        temperature=0.3, max_tokens=1024)
    assert len(s.calls) >= 2, "入口与效果两个时点都应裁决"
    assert started.is_set() is False, "翻转受限后效果不应被启动"


async def test_reauth_does_not_resurrect_skipped_effect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    s = ScriptedAdjudicate("admitted")
    s.set_sequence("admitted", "restricted", "admitted")
    install_scripted_adjudicate(monkeypatch, s)
    dl = _DL()
    session = _sess(monkeypatch, dl, quoted=["https://x/0.png", "https://x/1.png"],
                    current=["https://x/2.png"])
    await session.read(0)
    await session.read(1)
    await session.read(2)
    assert dl.calls == ["https://x/0.png", "https://x/2.png"], (
        "受限时该 sibling 效果被跳过，恢复准入后不得补执行/复活")


async def test_concurrent_sibling_admitted_completes_restricted_not_started(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    s = ScriptedAdjudicate("admitted")
    s.set_sequence("admitted", "restricted")
    install_scripted_adjudicate(monkeypatch, s)
    dl = _DL()
    session = _sess(monkeypatch, dl, quoted=["https://x/0.png", "https://x/1.png"],
                    current=["https://x/2.png"])
    await asyncio.gather(session.read(0), session.read(1))
    assert len(s.calls) >= 2, "两个 sibling 各自在 dispatch 前裁决"
    assert len(dl.calls) == 1, ("并发 sibling：获准者完成下载，受限者不启动；"
                                "已 dispatch 结果不外溢")



def _fresh_chat_module(monkeypatch: pytest.MonkeyPatch) -> Any:
    """按 test_plugin_entry 模式独立加载 chat 插件入口，避开 NoneBot 插件装载。"""
    import importlib.util
    import pathlib
    import sys

    import nonebot.plugin as np

    original_require = np.require

    def _require(name: str) -> object:
        if name in {"komari_memory", "komari_decision", "permission_manager",
                    "user_ban", "user_data"}:
            return SimpleNamespace(get_plugin_manager=lambda: None)
        return original_require(name)

    monkeypatch.setattr(np, "require", _require)
    module_name = "komari_bot.plugins.komari_chat._entry_test"
    module_path = (pathlib.Path(__file__).resolve().parents[2]
                   / "komari_bot" / "plugins" / "komari_chat" / "__init__.py")
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        msg = "无法加载聊天插件入口"
        raise RuntimeError(msg)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module
