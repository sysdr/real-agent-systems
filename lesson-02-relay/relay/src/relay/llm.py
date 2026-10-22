"""One small model interface, with adapters behind it.

The agent loop only ever sees `Model.generate(system, messages, tools) -> Turn`.
Everything provider-specific lives in an adapter:

  AnthropicAdapter - live mode, speaks the Anthropic Messages API over HTTP
  ReplayModel      - default; returns recorded turns, no key and no network
  RecordModel      - wraps another model and saves each turn as a replay fixture

Neutral message format (what the loop builds and every adapter accepts):
  {"role": "user" | "assistant", "content": [block, ...]} where a block is
  {"type": "text", "text": str}
  {"type": "tool_call", "id": str, "name": str, "input": dict}
  {"type": "tool_result", "id": str, "content": str, "is_error": bool (optional)}
"""
from __future__ import annotations

import hashlib
import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

Message = dict
ToolSpec = dict  # {"name", "description", "input_schema"}


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    input: dict


@dataclass(frozen=True)
class Turn:
    """One model response in neutral form. stop is end | tool_use | max_tokens | other."""

    text: str
    tool_calls: tuple[ToolCall, ...] = ()
    stop: str = "end"
    input_tokens: int = 0
    output_tokens: int = 0

    def to_message(self) -> Message:
        blocks: list[dict] = []
        if self.text:
            blocks.append({"type": "text", "text": self.text})
        blocks += [{"type": "tool_call", "id": c.id, "name": c.name, "input": c.input}
                   for c in self.tool_calls]
        return {"role": "assistant", "content": blocks}

    def to_json(self) -> dict:
        return {"text": self.text, "stop": self.stop,
                "tool_calls": [{"id": c.id, "name": c.name, "input": c.input} for c in self.tool_calls],
                "input_tokens": self.input_tokens, "output_tokens": self.output_tokens}

    @classmethod
    def from_json(cls, d: dict) -> "Turn":
        return cls(d["text"], tuple(ToolCall(c["id"], c["name"], c["input"]) for c in d["tool_calls"]),
                   d["stop"], d["input_tokens"], d["output_tokens"])


class Model(Protocol):
    def generate(self, system: str, messages: list[Message], tools: list[ToolSpec]) -> Turn: ...


class ProviderError(RuntimeError):
    """The provider failed (network, rate limit, server error). Not an agent mistake."""

    def __init__(self, message: str, status: int | None = None, retryable: bool = False) -> None:
        super().__init__(message)
        self.status, self.retryable = status, retryable


class ReplayMiss(RuntimeError):
    pass


# ---------------------------------------------------------------- live adapter

_STOP = {"end_turn": "end", "stop_sequence": "end", "tool_use": "tool_use", "max_tokens": "max_tokens"}


class AnthropicAdapter:
    """Live mode. Translates neutral messages to the Messages API and back."""

    def __init__(self, api_key: str, model: str, *, max_tokens: int = 1024,
                 base_url: str = "https://api.anthropic.com", timeout_s: float = 60.0) -> None:
        self.api_key, self.model, self.max_tokens = api_key, model, max_tokens
        self.base_url, self.timeout_s = base_url.rstrip("/"), timeout_s

    @classmethod
    def from_env(cls) -> "AnthropicAdapter":
        if not os.environ.get("RELAY_MODEL"):
            raise RuntimeError("set RELAY_MODEL to a model id from your provider console; no hidden default")
        return cls(os.environ["ANTHROPIC_API_KEY"], os.environ["RELAY_MODEL"],
                   max_tokens=int(os.environ.get("RELAY_MAX_OUTPUT_TOKENS", "1024")))

    def build_request(self, system: str, messages: list[Message], tools: list[ToolSpec]) -> dict:
        body: dict = {"model": self.model, "max_tokens": self.max_tokens, "system": system,
                      "messages": [self._wire_message(m) for m in messages]}
        if tools:
            body["tools"] = tools
        return body

    @staticmethod
    def _wire_message(message: Message) -> dict:
        blocks: list[dict] = []
        for b in message["content"]:
            if b["type"] == "tool_call":
                blocks.append({"type": "tool_use", "id": b["id"], "name": b["name"], "input": b["input"]})
            elif b["type"] == "tool_result":
                wire = {"type": "tool_result", "tool_use_id": b["id"], "content": b["content"]}
                if b.get("is_error"):
                    wire["is_error"] = True
                blocks.append(wire)
            else:
                blocks.append(b)
        return {"role": message["role"], "content": blocks}

    def parse_response(self, data: dict) -> Turn:
        content = data.get("content")
        if not isinstance(content, list) or "usage" not in data:
            raise ProviderError("unexpected response shape from provider")
        text = "".join(b.get("text", "") for b in content if b.get("type") == "text")
        calls = tuple(ToolCall(b["id"], b["name"], b.get("input", {}))
                      for b in content if b.get("type") == "tool_use")
        stop = _STOP.get(data.get("stop_reason", ""), "other")
        if stop == "tool_use" and not calls:
            stop = "other"
        u = data["usage"]
        # Cached input tokens are still input tokens the model processed; count them all.
        tokens_in = (u.get("input_tokens", 0) + u.get("cache_creation_input_tokens", 0)
                     + u.get("cache_read_input_tokens", 0))
        return Turn(text, calls, stop, tokens_in, u.get("output_tokens", 0))

    def generate(self, system: str, messages: list[Message], tools: list[ToolSpec]) -> Turn:
        return self.parse_response(self._post(self.build_request(system, messages, tools)))

    def _post(self, body: dict) -> dict:
        req = urllib.request.Request(
            f"{self.base_url}/v1/messages", data=json.dumps(body).encode(), method="POST",
            headers={"x-api-key": self.api_key, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as exc:  # before URLError: HTTPError is a subclass
            detail = exc.read()[:200].decode("utf-8", "replace")
            retryable = exc.code in (408, 409, 429) or exc.code >= 500
            raise ProviderError(f"http {exc.code}: {detail}", exc.code, retryable) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ProviderError(type(exc).__name__, None, True) from exc
        except json.JSONDecodeError as exc:
            raise ProviderError("provider returned invalid JSON", None, False) from exc


# ------------------------------------------------------------ replay and record

def request_key(system: str, messages: list[Message], tools: list[ToolSpec]) -> str:
    """Hash of the WHOLE request. Two calls match only if every byte the model sees matches."""
    canon = json.dumps({"system": system, "messages": messages, "tools": tools},
                       sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canon.encode()).hexdigest()[:20]


class ReplayModel:
    def __init__(self, path: Path) -> None:
        self.table: dict[str, dict] = {}
        self.calls = 0
        if path.exists():
            for line in path.read_text().splitlines():
                if line.strip():
                    rec = json.loads(line)
                    self.table[rec["key"]] = rec["turn"]

    def generate(self, system: str, messages: list[Message], tools: list[ToolSpec]) -> Turn:
        self.calls += 1
        rec = self.table.get(request_key(system, messages, tools))
        if rec is None:
            raise ReplayMiss("no recorded response for this request; run `make record` "
                             "after changing a prompt, a tool, or a tool result")
        return Turn.from_json(rec)


class RecordModel:
    def __init__(self, inner: Model, path: Path) -> None:
        self.inner, self.path = inner, path
        self.seen: set[str] = set()
        path.parent.mkdir(parents=True, exist_ok=True)

    def generate(self, system: str, messages: list[Message], tools: list[ToolSpec]) -> Turn:
        turn = self.inner.generate(system, messages, tools)
        key = request_key(system, messages, tools)
        if key not in self.seen:  # identical requests share one fixture line
            self.seen.add(key)
            with self.path.open("a") as f:
                f.write(json.dumps({"key": key, "turn": turn.to_json()}) + "\n")
        return turn


def make_model(name: str, fixtures: Path) -> Model:
    if name == "replay":
        return ReplayModel(fixtures)
    if name == "stub":
        from relay.stubmodel import StubAgentModel
        return StubAgentModel()
    if name == "live":
        return AnthropicAdapter.from_env()
    if name.startswith("record:"):
        if fixtures.exists():
            fixtures.unlink()
        return RecordModel(make_model(name.split(":", 1)[1], fixtures), fixtures)
    raise ValueError(f"unknown provider {name}")
