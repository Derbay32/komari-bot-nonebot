"""TSK-208: overlapping production presets, leases, death and revocation.

Only external business dependencies are controlled. All model requests cross the
installed MAIN_HOST_SCRIPT and configuration writes cross authenticated FastAPI.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import os
import signal
from collections import Counter
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest
from httpx import ASGITransport, AsyncClient
from PIL import Image

from komari_bot.agent import AgentHostUnavailableError, AgentTaskError, HostState
from komari_bot.context.redis_store import MessageSchema
from komari_bot.plugins.group_history_summary.dsh_summary_service import (
    DshSummaryService,
    SummaryGenerationError,
)
from tests.group_history_summary.test_dsh_summary_service import (
    _frozen,
    _selection,
)
from tests.group_history_summary.test_dsh_summary_service import (
    _message as history_message,
)
from tests.komari_agent.dsh_test_support import (
    MAIN_HOST_SCRIPT,
    ModelRequest,
    ScriptedModelServer,
    ToolCallResponse,
    allowed_profile_files,
    host_for,
)
from tests.komari_chat.hindsight_recall_support import (
    FakeHindsightMemory,
    fake_recall_session,
    recall_response,
)
from tests.komari_chat.test_dsh_chat_image_real_runtime import (
    _config,
    _data_uri,
    _empty_search,
    _empty_uid,
    _favorability,
    _group_bindings,
    _image_urls,
    _source_image,
    _StaticDownloader,
    native_module,
    service_module,
)
from tests.komari_management.test_dsh_management_real_runtime import (
    _AUTH,
    _SECRET,
    API_PREFIX,
    _app,
    _configure_model,
    _provider_body,
    _write_headers,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping

pytestmark = pytest.mark.asyncio

_SOURCE = "https://example.invalid/combined-image"
_CASES = ("text", "image", "summary", "fresh-chat", "fresh-summary")
_CONTEXT = {name: f"CONTEXT-{name}-208" for name in _CASES}
_QUERY = {name: f"BUSINESS-{name}-208" for name in _CASES}
_RESULTS = {
    "text": "Text query completed independently.",
    "image": "Image query completed independently.",
    "summary": "Summary history completed independently.",
    "fresh-chat": "Fresh chat completed independently.",
    "fresh-summary": "Fresh summary completed independently.",
}
_CHAT_TOOLS = {
    "final_response",
    "record_favorability_delta",
    "recall_memory",
    "search_web",
}
_SUMMARY_TOOLS = {
    "fetch_recent_group_messages",
    "fetch_messages_by_user",
    "fetch_messages_by_topic",
    "final_summary",
}


def _safe_check(condition: bool, message: str) -> None:  # noqa: FBT001 - assertion predicate, not a behavior switch
    """Do not let assertion rewriting disclose captured requests or rasters."""
    if not condition:
        pytest.fail(message, pytrace=False)


class _Barrier:
    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.calls = 0
        self.completed = 0

    async def wait(self) -> None:
        self.calls += 1
        self.entered.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        self.completed += 1


class _History:
    def __init__(self, barrier: _Barrier, name: str) -> None:
        self.barrier = barrier
        self.name = name

    async def fetch_recent(self, arguments: Mapping[str, object]) -> Any:
        assert arguments == {"count": 10}
        await self.barrier.wait()
        return _selection([history_message(1, content=_QUERY[self.name])])

    async def fetch_user(self, arguments: Mapping[str, object]) -> Any:
        del arguments
        pytest.fail("unexpected user-history effect")

    async def fetch_topic(self, arguments: Mapping[str, object]) -> Any:
        del arguments
        pytest.fail("unexpected topic-history effect")


def _owner(request: ModelRequest) -> str:
    owners = [
        name for name in _CASES if _CONTEXT[name] in "\n".join(request.user_texts)
    ]
    assert len(owners) == 1, "request must have exactly one task context"
    return owners[0]


class _Lane:
    def __init__(self, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.data = root / "data"
        self.host = host_for(self.data, entry_script=MAIN_HOST_SCRIPT)
        self.barriers = {name: _Barrier() for name in _CASES}
        self.turns: Counter[str] = Counter()
        self.revoked: set[str] = set()
        self.tasks: list[asyncio.Task[Any]] = []
        self.server = ScriptedModelServer(self.respond)
        self.sentinel = root / "logs" / "agent_runs" / "historical.jsonl"
        self.sentinel.parent.mkdir(parents=True)
        self.sentinel.write_bytes(b'{"historical":"leave unchanged"}\n')
        self.original = self.sentinel.read_bytes()
        self.digest = hashlib.sha256(self.original).digest()
        raster = _source_image("PNG", size=(24, 16), color=(20, 40, 60))
        self.downloader = _StaticDownloader({_SOURCE: _data_uri(raster, "PNG")})
        monkeypatch.setattr(
            native_module, "ImageDownloadSession", lambda _policy: self.downloader
        )
        monkeypatch.setattr(
            service_module,
            "_user_data_config",
            lambda: SimpleNamespace(max_favorability_delta_per_reply=3),
        )
        monkeypatch.setattr(
            service_module,
            "effect_business_admitted",
            lambda *, group_id: group_id not in self.revoked,
        )

    def respond(self, request: ModelRequest) -> ToolCallResponse:
        name = _owner(request)
        self.turns[name] += 1
        turn = self.turns[name]
        summary = name.endswith("summary")
        if turn == 1:
            if summary:
                return ToolCallResponse("fetch_recent_group_messages", {"count": 10})
            return ToolCallResponse("search_web", {"query": name})
        if summary:
            assert turn == 2, "unexpected summary replay"
            return ToolCallResponse("final_summary", {"content": _RESULTS[name]})
        if turn == 2:
            return ToolCallResponse("final_response", {"content": _RESULTS[name]})
        assert turn == 3, "unexpected chat replay"
        return ToolCallResponse(
            "record_favorability_delta", {"delta": 1, "reason": f"reason-{name}"}
        )

    async def generate(self, name: str) -> Any:
        if name.endswith("summary"):
            provider = _History(self.barriers[name], name)
            service = DshSummaryService(
                host_getter=lambda: self.host,
                history_provider_factory=lambda *_args: provider,
                admission_checker=lambda group_id: group_id not in self.revoked,
            )
            return await service.generate(
                bot=object(),
                group_id=f"group-{name}",
                bot_self_id="999",
                user_request=_CONTEXT[name],
                request_trace_id=name,
                config=_frozen(),
            )

        async def search(query: str, *, request_trace_id: str | None = None) -> str:
            assert query == name
            assert request_trace_id == name
            await self.barriers[name].wait()
            return _QUERY[name]

        service = service_module.DshChatService(
            host_getter=lambda: self.host,
            knowledge_search=_empty_search,
            uid_search=_empty_uid,
            favorability_getter=_favorability,
            group_bindings_getter=_group_bindings,
            search_web=search,
            search_available_getter=lambda: True,
            fetch_available_getter=lambda: False,
        )
        return await service.generate(
            message=MessageSchema(
                user_id="u-1",
                user_nickname="Tester",
                group_id=f"group-{name}",
                content=_CONTEXT[name],
                timestamp=1.0,
                message_id=name,
            ),
            recent_messages=[],
            reply_context=None,
            recall_session=fake_recall_session(
                FakeHindsightMemory(response=recall_response())
            ),
            request_trace_id=name,
            config=_config(),
            image_urls=[_SOURCE] if name == "image" else None,
        )

    async def start(self, names: tuple[str, ...]) -> dict[str, asyncio.Task[Any]]:
        tasks = {name: asyncio.create_task(self.generate(name)) for name in names}
        self.tasks.extend(tasks.values())
        await asyncio.wait_for(
            asyncio.gather(*(self.barriers[name].entered.wait() for name in names)), 20
        )
        assert not any(task.done() for task in tasks.values())
        assert Counter(_owner(request) for request in self.server.requests) == Counter(
            dict.fromkeys(names, 1)
        )
        return tasks

    def assert_result(self, name: str, result: Any) -> None:
        if name.endswith("summary"):
            assert result.summary_text == _RESULTS[name]
            assert result.selection is not None
        else:
            assert (
                result.content,
                result.favorability_delta,
                result.favorability_reason,
            ) == (
                _RESULTS[name],
                1,
                f"reason-{name}",
            )

    def assert_requests(self, *, fresh_changed: bool = False) -> None:
        for request in self.server.requests:
            name = _owner(request)
            rendered = json.dumps(request.body)
            summary = name.endswith("summary")
            assert set(request.tool_names) == (
                _SUMMARY_TOOLS if summary else _CHAT_TOOLS
            )
            assert "chat.read_image_chunk" not in request.tool_names
            _safe_check(_SOURCE not in rendered, "source URL reached model request")
            for other in _CASES:
                if other != name:
                    _safe_check(
                        _CONTEXT[other] not in rendered, "cross-task context leak"
                    )
                    _safe_check(
                        _QUERY[other] not in rendered, "cross-task business result leak"
                    )
                    _safe_check(
                        _RESULTS[other] not in rendered, "cross-task completion leak"
                    )
            changed = fresh_changed and name.startswith("fresh-")
            expected_key = "combined-new-fixture-key" if changed else _SECRET
            authorized = any(
                key.lower() == "authorization" and value == f"Bearer {expected_key}"
                for key, value in request.headers.items()
            )
            assert authorized, "task used an unexpected credential revision"
            kind = "summary" if summary else "chat"
            _safe_check(
                f"{kind}-prompt-{'new' if changed else 'old'}" in request.system_text,
                "expected prompt missing",
            )
            _safe_check(
                f"{kind}-prompt-{'old' if changed else 'new'}"
                not in request.system_text,
                "unexpected prompt revision",
            )
            assert request.body["model"] == (
                f"{kind}-new" if changed else "fixture-model"
            )
            if any(message.get("role") == "tool" for message in request.messages):
                _safe_check(_QUERY[name] in rendered, "missing own business result")
            images = _image_urls(request)
            assert bool(images) == (name == "image")
            for url in images:
                _safe_check(
                    url.startswith("data:image/"),
                    "image was not projected as a data URI",
                )
                with Image.open(
                    io.BytesIO(base64.b64decode(url.split(",", 1)[1], validate=True))
                ) as raster:
                    assert raster.size == (24, 16)
                    assert raster.convert("RGB").getpixel((0, 0)) == (20, 40, 60)

    def assert_persistence(self) -> None:
        allowed = {
            "dsh/package.json",
            "dsh/cordis.patch.yml",
            "dsh/.credentials.yaml",
            "dsh/pnpm-workspace.yaml",
        }
        assert allowed_profile_files(self.data) == allowed
        assert {
            str(path.relative_to(self.data))
            for path in self.data.rglob("*")
            if path.is_file()
        } == allowed
        for path in self.data.rglob("*"):
            if path.is_file():
                content = path.read_text()
                assert all(
                    canary not in content
                    for canary in (
                        *_CONTEXT.values(),
                        *_QUERY.values(),
                        *_RESULTS.values(),
                    )
                )
        assert self.sentinel.read_bytes() == self.original
        assert hashlib.sha256(self.sentinel.read_bytes()).digest() == self.digest
        assert list(self.sentinel.parent.rglob("*")) == [self.sentinel]


async def _put(http: Any, path: str, body: dict[str, Any], *, status: int = 200) -> Any:
    before = await http.get(f"{API_PREFIX}/{path}", headers=_AUTH)
    assert before.status_code == 200
    response = await http.put(
        f"{API_PREFIX}/{path}",
        headers=_write_headers(before.json()["etag"], "combined-write"),
        json=body,
    )
    assert response.status_code == status
    if status == 409:
        assert response.json()["detail"]["code"] == "active_tasks"
        after = await http.get(f"{API_PREFIX}/{path}", headers=_AUTH)
        assert after.json() == before.json()
        assert after.headers["etag"] == before.headers["etag"]
    return response


def _provider(url: str) -> dict[str, Any]:
    body = _provider_body(url, models=("fixture-model", "chat-new", "summary-new"))
    for model in body["models"]:
        model["input"] = ["text", "image"]
    return body


async def _connections(http: Any, lane: _Lane, *, blocked: bool) -> None:
    body = _provider(lane.server.base_url)
    body["displayName"] = "Rejected edit" if blocked else "Accepted edit"
    await _put(http, "providers/fixture", body, status=409 if blocked else 200)
    await _put(
        http,
        "providers/fixture/credential",
        {"value": "combined-new-fixture-key"},
        status=409 if blocked else 200,
    )


@asynccontextmanager
async def _running(
    root: Path, monkeypatch: pytest.MonkeyPatch, app: Any
) -> AsyncIterator[tuple[_Lane, Any]]:
    lane = _Lane(root, monkeypatch)
    lane.server.start()
    try:
        await lane.host.start()
        async with app.test_server(asgi=cast("Any", _app(lane.host))) as ctx:
            http = ctx.get_client()
            await _configure_model(
                http,
                lane.server.base_url,
                models=("fixture-model", "chat-new", "summary-new"),
                chat_prompt="chat-prompt-old",
                summary_prompt="summary-prompt-old",
            )
            await _put(http, "providers/fixture", _provider(lane.server.base_url))
            yield lane, http
    finally:
        for task in lane.tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*lane.tasks, return_exceptions=True)
        await lane.host.close()
        lane.server.close()


async def test_overlapping_production_tasks_freeze_configuration_until_last_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    app: Any,
) -> None:
    async with _running(tmp_path, monkeypatch, app) as (lane, http):
        pid = lane.host.process_id
        assert pid is not None
        tasks = await lane.start(("text", "image", "summary"))
        for kind in ("chat", "summary"):
            await _put(http, f"prompts/{kind}", {"value": f"{kind}-prompt-new"})
            await _put(
                http,
                f"bindings/{kind}",
                {"provider": "fixture", "model": f"{kind}-new"},
            )
        for name in ("text", "image", "summary"):
            await _connections(http, lane, blocked=True)
            lane.barriers[name].release.set()
            lane.assert_result(name, await asyncio.wait_for(tasks[name], 20))
            assert lane.host.process_id == pid
            assert all(
                not task.done()
                for key, task in tasks.items()
                if key in ("image", "summary")
                and key != name
                and not lane.barriers[key].release.is_set()
            )
        await _connections(http, lane, blocked=False)
        for name in ("fresh-chat", "fresh-summary"):
            lane.barriers[name].release.set()
            lane.assert_result(name, await lane.generate(name))
        assert lane.host.process_id == pid
        assert lane.turns == {
            "text": 3,
            "image": 3,
            "summary": 2,
            "fresh-chat": 3,
            "fresh-summary": 2,
        }
        assert lane.downloader.calls == [_SOURCE]
        lane.assert_requests(fresh_changed=True)
        lane.assert_persistence()
    lane.assert_persistence()


async def test_real_child_death_drains_all_presets_without_replay_and_allows_explicit_fresh_host(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    app: Any,
) -> None:
    from komari_bot.agent import host as host_module

    async with _running(tmp_path, monkeypatch, app) as (lane, http):
        paths = (
            "providers/fixture",
            "providers/fixture/credential",
            "bindings/chat",
            "bindings/summary",
            "prompts/chat",
            "prompts/summary",
        )
        saved = {}
        for path in paths:
            response = await http.get(f"{API_PREFIX}/{path}", headers=_AUTH)
            assert response.status_code == 200
            saved[path] = {
                key: value for key, value in response.json().items() if key != "etag"
            }
        tasks = await lane.start(("text", "image", "summary"))
        old_host = lane.host
        pid = old_host.process_id
        root = old_host.attachment_root
        assert pid is not None and root is not None and root.is_dir()
        removed = asyncio.Event()
        real_rmtree = host_module.shutil.rmtree

        def observed_rmtree(path: Any, *args: Any, **kwargs: Any) -> None:
            if Path(path) == root:
                # Observe the real cleanup boundary, without replacing host lifecycle.
                assert old_host.process_id is None, (
                    "attachment root removed before observed child exit"
                )
                with pytest.raises(ProcessLookupError):
                    os.kill(pid, 0)
                real_rmtree(path, *args, **kwargs)
                removed.set()
            else:
                real_rmtree(path, *args, **kwargs)

        monkeypatch.setattr(host_module.shutil, "rmtree", observed_rmtree)
        os.kill(pid, signal.SIGKILL)
        results = await asyncio.wait_for(
            asyncio.gather(*tasks.values(), return_exceptions=True), 20
        )
        assert isinstance(results[0], AgentHostUnavailableError)
        assert isinstance(results[1], AgentHostUnavailableError)
        assert isinstance(results[2], SummaryGenerationError)
        assert results[0].code == "process_exited"
        assert results[1].code == "process_exited"
        assert results[2].code == "process_exited"
        await asyncio.wait_for(
            asyncio.gather(*(lane.barriers[name].cancelled.wait() for name in tasks)), 5
        )
        await asyncio.wait_for(removed.wait(), 5)
        assert not root.exists()
        assert old_host.state is HostState.UNAVAILABLE
        assert all(lane.barriers[name].completed == 0 for name in tasks)
        assert lane.turns == {"text": 1, "image": 1, "summary": 1}
        with pytest.raises(AgentHostUnavailableError):
            await lane.generate("fresh-chat")
        assert len(lane.server.requests) == 3
        assert old_host.state is HostState.UNAVAILABLE
        lane.assert_requests()
        lane.assert_persistence()
        failed_read = await http.get(f"{API_PREFIX}/providers", headers=_AUTH)
        assert failed_read.status_code == 503
        assert failed_read.json()["detail"]["code"] == "unavailable"
        await old_host.close()

        # This is a new owner/lifecycle, never an automatic retry of the old tasks.
        lane.host = host_for(lane.data, entry_script=MAIN_HOST_SCRIPT)
        await lane.host.start()
        assert lane.host is not old_host
        async with AsyncClient(
            transport=ASGITransport(app=_app(lane.host)), base_url="http://test"
        ) as fresh_http:
            for path in paths:
                response = await fresh_http.get(f"{API_PREFIX}/{path}", headers=_AUTH)
                assert response.status_code == 200
                assert {
                    key: value
                    for key, value in response.json().items()
                    if key != "etag"
                } == saved[path]
            for name in ("fresh-chat", "fresh-summary"):
                lane.barriers[name].release.set()
                lane.assert_result(name, await lane.generate(name))
        assert lane.turns == {
            "text": 1,
            "image": 1,
            "summary": 1,
            "fresh-chat": 3,
            "fresh-summary": 2,
        }
        lane.assert_requests()
        lane.assert_persistence()
    lane.assert_persistence()


async def test_group_revocation_stops_only_image_chat_while_summary_completes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    app: Any,
) -> None:
    async with _running(tmp_path, monkeypatch, app) as (lane, _http):
        pid = lane.host.process_id
        submissions: list[str] = []
        real_execute = lane.host.execute

        async def observe_execute(request: Any, callbacks: Any) -> Any:
            observed = dict(callbacks)
            for callback_name in (
                "chat.submit_final_response",
                "chat.submit_favorability",
            ):
                if callback_name not in callbacks:
                    continue

                async def observe(
                    callback_request: Any, *, name: str = callback_name
                ) -> Any:
                    submissions.append(name)
                    return await callbacks[name](callback_request)

                observed[callback_name] = observe
            return await real_execute(request, observed)

        monkeypatch.setattr(lane.host, "execute", observe_execute)
        tasks = await lane.start(("image", "summary"))
        assert lane.downloader.calls == [_SOURCE]
        lane.revoked.add("group-image")
        for name in tasks:
            lane.barriers[name].release.set()
        with pytest.raises(AgentTaskError) as denied:
            await asyncio.wait_for(tasks["image"], 20)
        assert denied.value.code == "agent_error"
        summary = await asyncio.wait_for(tasks["summary"], 20)
        assert submissions == []
        lane.assert_result("summary", summary)
        assert lane.turns == {"image": 1, "summary": 2}
        assert lane.barriers["image"].calls == lane.barriers["image"].completed == 1
        assert lane.barriers["summary"].calls == lane.barriers["summary"].completed == 1
        assert lane.host.process_id == pid
        lane.assert_requests()
        lane.assert_persistence()
    lane.assert_persistence()
