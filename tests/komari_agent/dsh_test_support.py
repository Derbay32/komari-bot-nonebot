"""Test support for the real installed DSH runtime and a controlled model endpoint."""

from __future__ import annotations

import contextlib
import json
import os
import socketserver
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import pytest

from komari_bot.agent import AgentHost, AgentTaskBudget

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

PROJECT_ROOT: Final = Path(__file__).resolve().parents[2]
DSH_ROOT: Final = PROJECT_ROOT / "dsh"
TEST_HOST_SCRIPT: Final = DSH_ROOT / "runtime" / "dist" / "test-main.js"
MAIN_HOST_SCRIPT: Final = DSH_ROOT / "runtime" / "dist" / "main.js"
PROFILE_BUNDLE: Final = "@komari-bot/dsh-runtime-bundle"
FIXTURE_KEY: Final = "fixture-test-key"
os.environ.setdefault("FIXTURE_KEY", FIXTURE_KEY)


@dataclass(frozen=True, slots=True)
class Usage:
    """Provider-reported token usage."""

    prompt_tokens: int = 10
    completion_tokens: int = 2

    def wire(self) -> dict[str, int]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.prompt_tokens + self.completion_tokens,
        }


@dataclass(frozen=True, slots=True)
class ToolCallResponse:
    """One assistant turn that emits a single native tool call."""

    tool_name: str
    arguments: dict[str, Any]
    usage: Usage = field(default_factory=Usage)


@dataclass(frozen=True, slots=True)
class TextResponse:
    """One plain assistant text turn."""

    text: str
    usage: Usage = field(default_factory=Usage)


@dataclass(frozen=True, slots=True)
class EmptyResponse:
    """One successful completion with zero content blocks."""

    usage: Usage = field(default_factory=Usage)


@dataclass(frozen=True, slots=True)
class HttpErrorResponse:
    """One provider HTTP failure."""

    status: int
    message: str
    error_type: str
    code: str


@dataclass(slots=True)
class HangResponse:
    """Headers-only response that stays open until the test releases it."""

    started: threading.Event = field(default_factory=threading.Event)
    release: threading.Event = field(default_factory=threading.Event)


@dataclass(frozen=True, slots=True)
class ModelRequest:
    """One captured outgoing model request."""

    path: str
    headers: dict[str, str]
    body: dict[str, Any]

    @property
    def messages(self) -> list[Any]:
        messages = self.body.get("messages")
        return messages if isinstance(messages, list) else []

    @property
    def tools(self) -> list[Any]:
        tools = self.body.get("tools")
        return tools if isinstance(tools, list) else []

    @property
    def tool_names(self) -> list[str]:
        names: list[str] = []
        for tool in self.tools:
            if isinstance(tool, dict):
                function = tool.get("function")
                if isinstance(function, dict) and isinstance(function.get("name"), str):
                    names.append(function["name"])
        return names

    @property
    def system_text(self) -> str:
        parts: list[str] = []
        for message in self.messages:
            if isinstance(message, dict) and message.get("role") == "system":
                content = message.get("content")
                if isinstance(content, str):
                    parts.append(content)
        return "\n".join(parts)

    @property
    def user_texts(self) -> list[str]:
        texts: list[str] = []
        for message in self.messages:
            if isinstance(message, dict) and message.get("role") == "user":
                content = message.get("content")
                if isinstance(content, str):
                    texts.append(content)
                elif isinstance(content, list):
                    texts.extend(
                        block["text"]
                        for block in content
                        if isinstance(block, dict) and isinstance(block.get("text"), str)
                    )
        return texts


type ScriptedResponse = ToolCallResponse | TextResponse | EmptyResponse | HttpErrorResponse | HangResponse


class _ModelHTTPServer(ThreadingHTTPServer):
    """HTTP server carrying its owning scripted endpoint."""

    daemon_threads = True

    def __init__(self, address: tuple[str, int], owner: ScriptedModelServer) -> None:
        super().__init__(address, _Handler)
        self.owner = owner

    def server_bind(self) -> None:
        # HTTPServer.server_bind does a reverse-DNS getfqdn that can block for
        # tens of seconds in sandboxed/offline environments; the controlled
        # endpoint only ever binds a numeric loopback address.
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = str(host)
        self.server_port = int(port)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        _ = (format, args)

    def do_POST(self) -> None:
        raw_length = self.headers.get("content-length", "0")
        length = int(raw_length)
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            body = {"_invalid": raw.decode("utf-8", errors="replace")}
        server = self.server
        assert isinstance(server, _ModelHTTPServer)
        owner = server.owner
        request = ModelRequest(
            path=self.path,
            headers=dict(self.headers.items()),
            body=body,
        )
        owner._record(request)
        response = owner._next_response(request)
        if isinstance(response, HttpErrorResponse):
            payload = {
                "error": {
                    "message": response.message,
                    "type": response.error_type,
                    "code": response.code,
                }
            }
            encoded = json.dumps(payload).encode("utf-8")
            self.send_response(response.status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(encoded)))
            self.send_header("connection", "close")
            self.end_headers()
            self.wfile.write(encoded)
            self.close_connection = True
            return
        if isinstance(response, HangResponse):
            self._start_sse()
            response.started.set()
            response.release.wait(timeout=30)
            return
        self._write_sse(response)

    def do_GET(self) -> None:
        self.send_response(404)
        self.send_header("content-length", "0")
        self.end_headers()

    def _start_sse(self) -> None:
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("cache-control", "no-cache")
        self.send_header("connection", "keep-alive")
        self.end_headers()
        self.wfile.flush()

    def _write_sse(self, response: ToolCallResponse | TextResponse | EmptyResponse) -> None:
        self._start_sse()
        chunks = _response_chunks(response)
        try:
            for chunk in chunks:
                self.wfile.write(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode())
                self.wfile.flush()
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        self.close_connection = True


def _base_chunk(choices: list[dict[str, Any]], usage: dict[str, int] | None = None) -> dict[str, Any]:
    chunk: dict[str, Any] = {
        "id": "chatcmpl-komari-test",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "fixture-model",
        "choices": choices,
    }
    if usage is not None:
        chunk["usage"] = usage
    return chunk


def _response_chunks(response: ToolCallResponse | TextResponse | EmptyResponse) -> list[dict[str, Any]]:
    if isinstance(response, ToolCallResponse):
        first = _base_chunk([{
            "index": 0,
            "delta": {
                "role": "assistant",
                "tool_calls": [{
                    "index": 0,
                    "id": "call_komari_1",
                    "type": "function",
                    "function": {
                        "name": response.tool_name,
                        "arguments": json.dumps(response.arguments, ensure_ascii=False),
                    },
                }],
            },
            "finish_reason": None,
        }])
        final = _base_chunk([{"index": 0, "delta": {}, "finish_reason": "tool_calls"}], response.usage.wire())
        return [first, final]
    if isinstance(response, TextResponse):
        first = _base_chunk([{
            "index": 0,
            "delta": {"role": "assistant", "content": response.text},
            "finish_reason": None,
        }])
        final = _base_chunk([{"index": 0, "delta": {}, "finish_reason": "stop"}], response.usage.wire())
        return [first, final]
    final = _base_chunk([{"index": 0, "delta": {"role": "assistant"}, "finish_reason": "stop"}], response.usage.wire())
    return [final]


type ModelResponder = Callable[[ModelRequest], ScriptedResponse]


class ScriptedModelServer:
    """Threaded local OpenAI-compatible endpoint with a scripted response queue."""

    def __init__(self, responder: ModelResponder | None = None) -> None:
        self._server = _ModelHTTPServer(("127.0.0.1", 0), self)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        self._responses: list[ScriptedResponse] = []
        self._requests: list[ModelRequest] = []
        self._responder = responder

    def start(self) -> None:
        self._thread.start()

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}/v1"

    def enqueue(self, *responses: ScriptedResponse) -> None:
        with self._lock:
            self._responses.extend(responses)

    @property
    def requests(self) -> list[ModelRequest]:
        with self._lock:
            return list(self._requests)

    def wait_for_request_count(self, count: int, timeout: float = 15.0) -> None:
        with self._condition:
            reached = self._condition.wait_for(lambda: len(self._requests) >= count, timeout=timeout)
        if not reached:
            pytest.fail(f"timed out waiting for {count} model requests")

    def _next_response(self, request: ModelRequest) -> ScriptedResponse:
        with self._lock:
            if self._responder is not None:
                return self._responder(request)
            if not self._responses:
                return HttpErrorResponse(500, "no scripted response", "server_error", "server_error")
            return self._responses.pop(0)

    def _record(self, request: ModelRequest) -> None:
        with self._condition:
            self._requests.append(request)
            self._condition.notify_all()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


def write_profile(
    data_dir: Path,
    *,
    base_url: str,
    prompt: str = "You are a fixture.",
    summary_prompt: str = "You summarize fixtures.",
    configured: bool = True,
    retry_max_retries: int = 2,
    retryable_codes: tuple[str, ...] = ("RATE_LIMIT", "TIMEOUT", "TRANSPORT"),
    context_window: int = 8192,
    max_tokens: int = 128,
    compaction: bool = False,
    input_modalities: tuple[str, ...] | None = None,
) -> Path:
    """Write a private test profile whose patch is the only mutable layer."""
    profile = data_dir / "dsh"
    profile.mkdir(parents=True, exist_ok=True)
    manifest = {
        "name": "komari-agent-test-profile",
        "private": True,
        "dsh": {"profile": {"bundles": [PROFILE_BUNDLE]}},
    }
    (profile / "package.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    lines: list[str] = [
        "- id: llm-pi-ai",
        "  config:",
        "    providers:",
        "      fixture:",
        "        api: openai-completions",
        f"        baseURL: {json.dumps(base_url)}",
        "        apiKeyEnv: FIXTURE_KEY",
        "        models:",
        "          - id: fixture-model",
        f"            contextWindow: {context_window}",
        f"            maxTokens: {max_tokens}",
    ]
    if input_modalities is not None:
        lines.append(f"            input: [{', '.join(input_modalities)}]")
    lines.extend([
        "        retryPolicy:",
        "          mode: normal",
        f"          maxRetries: {retry_max_retries}",
        f"          retryableCodes: [{', '.join(retryable_codes)}]",
        "          backoff:",
        "            initialDelayMs: 1",
        "            maxDelayMs: 5",
        "            jitterRatio: 0",
    ])
    if compaction:
        lines.extend([
            "- id: compaction-basic",
            "  config:",
            "    compactionRetries: 0",
            "    maxOverflowRetries: 0",
            "    headroomTokens: 1",
            "    thresholdRatio: 0.5",
            "    retainRatio: 0.1",
        ])
    if configured:
        lines.extend([
            "- id: komari-agent",
            "  config:",
            "    chat:",
            "      provider: fixture",
            "      model: fixture-model",
            f"      prompt: {json.dumps(prompt)}",
            "    summary:",
            "      provider: fixture",
            "      model: fixture-model",
            f"      prompt: {json.dumps(summary_prompt)}",
        ])
    (profile / "cordis.patch.yml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return profile


def chat_task_data(text: str = "hello") -> dict[str, Any]:
    """Return the closed TSK-203 chat envelope used by real-runtime tests."""
    return {
        "schema": 1,
        "current": {
            "source_type": "conversation_history",
            "source_id": "m-current",
            "trust_level": "untrusted",
            "content": text,
            "message_id": "m-current",
            "group_id": "g-1",
            "user_id": "u-1",
            "user_nickname": "Tester",
            "timestamp": 1.0,
            "is_bot": False,
        },
        "recent": [],
        "quote": None,
        "memory": [],
        "knowledge": [],
        "uid_knowledge": [],
        "capabilities": {
            "knowledge": True,
            "search": True,
            "fetch": True,
        },
        "images": [],
        "facts": {
            "bot_nickname": "Fixture",
            "current_favorability": {
                "value": 0,
                "stage_index": 0,
                "stage_name": "neutral",
                "stage_prompt": "Neutral.",
            },
            "visible_users": [],
            "max_favorability_delta": 3,
        },
    }


def host_for(data_dir: Path, *, entry_script: Path = TEST_HOST_SCRIPT, startup_timeout: float = 30.0) -> AgentHost:
    """Build the production host against a test entry script and private data dir."""
    if not entry_script.is_file():
        pytest.fail(f"missing built DSH entry {entry_script}; run `npm run build` in dsh/")
    return AgentHost(
        runtime_root=DSH_ROOT,
        data_dir=data_dir,
        entry_script=entry_script,
        startup_timeout=startup_timeout,
        close_timeout=10.0,
    )


@contextlib.asynccontextmanager
async def running_dsh(
    tmp_path: Path,
    *,
    responses: tuple[ScriptedResponse, ...],
    responder: ModelResponder | None = None,
    configured: bool = True,
    entry_script: Path = TEST_HOST_SCRIPT,
    retry_max_retries: int = 2,
    compaction: bool = False,
    prompt: str = "You are a fixture.",
    summary_prompt: str = "You summarize fixtures.",
    context_window: int = 8192,
    max_tokens: int = 128,
    input_modalities: tuple[str, ...] | None = None,
) -> AsyncIterator[tuple[AgentHost, ScriptedModelServer]]:
    """Start the controlled endpoint and a real DSH host, then close both."""
    server = ScriptedModelServer(responder=responder)
    server.start()
    server.enqueue(*responses)
    data_dir = tmp_path / "data"
    write_profile(
        data_dir,
        base_url=server.base_url,
        configured=configured,
        retry_max_retries=retry_max_retries,
        compaction=compaction,
        prompt=prompt,
        summary_prompt=summary_prompt,
        context_window=context_window,
        max_tokens=max_tokens,
        input_modalities=input_modalities,
    )
    host = host_for(data_dir, entry_script=entry_script)
    try:
        await host.start()
    except BaseException:
        server.close()
        raise
    try:
        yield host, server
    finally:
        await host.close()
        server.close()


def allowed_profile_files(data_dir: Path) -> set[str]:
    """Files the runtime may create in a private data directory."""
    root = data_dir / "dsh"
    if not root.exists():
        return set()
    return {str(path.relative_to(data_dir)) for path in root.rglob("*") if path.is_file()}


def default_budget() -> AgentTaskBudget:
    """Convenience for tests."""
    return AgentTaskBudget(max_rounds=3, max_tool_calls_per_round=4, max_total_tool_calls=8)


__all__: list[str] = [
    "FIXTURE_KEY",
    "MAIN_HOST_SCRIPT",
    "PROJECT_ROOT",
    "TEST_HOST_SCRIPT",
    "EmptyResponse",
    "HangResponse",
    "HttpErrorResponse",
    "ModelRequest",
    "ModelResponder",
    "ScriptedModelServer",
    "TextResponse",
    "ToolCallResponse",
    "Usage",
    "allowed_profile_files",
    "chat_task_data",
    "default_budget",
    "host_for",
    "running_dsh",
    "write_profile",
]
