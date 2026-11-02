"""An Anthropic Messages API on 127.0.0.1, for the ``anthropic`` provider's tests.

The counterpart of ``StubModel`` in
``test_igni_reaches_nothing_outside_the_network.py``, which speaks
OpenAI's chat completions. A real server on a real socket, so the
``anthropic`` SDK's own HTTP stack makes each request and the egress
guard sees it as the internal call it is.

It answers ``POST /v1/messages`` from a script — one :class:`Reply` per
request, the last repeated — in JSON, or as server-sent events when the
request asks to stream, the way Anthropic's API does. Every request is
kept, headers and body, for the tests to read back.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from pydantic import BaseModel, Field


class ToolUse(BaseModel):
    """The model asks for a tool."""

    id: str
    name: str
    input: dict[str, Any] = Field(default_factory=dict)


class Reply(BaseModel):
    """One assistant turn: text, and optionally one tool call after it."""

    text: str = ""
    tool_use: ToolUse | None = None

    @property
    def stop_reason(self) -> str:
        return "tool_use" if self.tool_use else "end_turn"

    def blocks(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        if self.text:
            out.append({"type": "text", "text": self.text})
        if self.tool_use:
            out.append(
                {
                    "type": "tool_use",
                    "id": self.tool_use.id,
                    "name": self.tool_use.name,
                    "input": self.tool_use.input,
                }
            )
        return out


class Recorded(BaseModel):
    """One request the stub received."""

    path: str
    headers: dict[str, str]
    body: dict[str, Any]


USAGE = {
    "input_tokens": 12,
    "output_tokens": 5,
    "cache_creation_input_tokens": 0,
    "cache_read_input_tokens": 0,
}


class StubMessagesApi:
    """``/v1/messages`` on 127.0.0.1, answering from a script."""

    def __init__(self, *replies: Reply) -> None:
        self._script = list(replies) or [Reply(text="ok")]
        self.requests: list[Recorded] = []
        self._lock = threading.Lock()
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._server.stub = self  # type: ignore[attr-defined]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        """The base URL, as a model entry's ``url`` names it."""
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    @property
    def messages(self) -> list[Recorded]:
        return [r for r in self.requests if r.path.split("?")[0] == "/v1/messages"]

    def __enter__(self) -> StubMessagesApi:
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    def next_reply(self) -> Reply:
        with self._lock:
            return self._script.pop(0) if len(self._script) > 1 else self._script[0]

    def message(self, reply: Reply, model: str, n: int) -> dict[str, Any]:
        return {
            "id": f"msg_stub_{n}",
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": reply.blocks(),
            "stop_reason": reply.stop_reason,
            "stop_sequence": None,
            "usage": USAGE,
        }

    def events(self, reply: Reply, model: str, n: int) -> list[tuple[str, dict[str, Any]]]:
        start = self.message(reply, model, n) | {"content": [], "stop_reason": None}
        events: list[tuple[str, dict[str, Any]]] = [
            ("message_start", {"type": "message_start", "message": start})
        ]
        index = 0
        if reply.text:
            half = len(reply.text) // 2
            events.append(_block_start(index, {"type": "text", "text": ""}))
            for piece in (reply.text[:half], reply.text[half:]):
                events.append(_block_delta(index, {"type": "text_delta", "text": piece}))
            events.append(_block_stop(index))
            index += 1
        if reply.tool_use:
            tool = reply.tool_use
            events.append(
                _block_start(
                    index, {"type": "tool_use", "id": tool.id, "name": tool.name, "input": {}}
                )
            )
            raw = json.dumps(tool.input)
            half = len(raw) // 2
            for piece in (raw[:half], raw[half:]):
                events.append(
                    _block_delta(index, {"type": "input_json_delta", "partial_json": piece})
                )
            events.append(_block_stop(index))
        events.append(
            (
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": reply.stop_reason, "stop_sequence": None},
                    "usage": {"output_tokens": 5},
                },
            )
        )
        events.append(("message_stop", {"type": "message_stop"}))
        return events


def _block_start(index: int, block: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    return (
        "content_block_start",
        {"type": "content_block_start", "index": index, "content_block": block},
    )


def _block_delta(index: int, delta: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    return ("content_block_delta", {"type": "content_block_delta", "index": index, "delta": delta})


def _block_stop(index: int) -> tuple[str, dict[str, Any]]:
    return ("content_block_stop", {"type": "content_block_stop", "index": index})


class _Handler(BaseHTTPRequestHandler):
    server: ThreadingHTTPServer

    def do_POST(self) -> None:  # noqa: N802 — the stdlib's name
        stub: StubMessagesApi = self.server.stub  # type: ignore[attr-defined]
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        with stub._lock:
            stub.requests.append(
                Recorded(
                    path=self.path,
                    headers={k.lower(): v for k, v in self.headers.items()},
                    body=body,
                )
            )
            n = len(stub.requests)
        path = self.path.split("?")[0]
        if path == "/v1/messages/count_tokens":
            self._send_json({"input_tokens": 12})
            return
        if path != "/v1/messages":
            self.send_error(404)
            return
        reply = stub.next_reply()
        model = str(body.get("model", ""))
        if body.get("stream"):
            self._send_stream(stub.events(reply, model, n))
        else:
            self._send_json(stub.message(reply, model, n))

    def _send_json(self, payload: dict[str, Any]) -> None:
        data = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_stream(self, events: list[tuple[str, dict[str, Any]]]) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        for name, data in events:
            self.wfile.write(f"event: {name}\ndata: {json.dumps(data)}\n\n".encode())
        self.close_connection = True

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        return
