"""Bad-argument fault injection: make a model emit the mistakes real models make.

The simulator's switchboard breaks the WORLD (timeouts, 500s). This one breaks the MODEL's output:
it rewrites the arguments of the first tool call in a fraction of runs. The corrupted call stays in
the history, so the error the tool returns refers to what the model "said", exactly as in production.

Selection is a hash of the ticket text, never a random stream. The same ticket always gets the same
fault, a higher rate only ever adds tickets, and replay fixtures recorded at rate 1.0 cover any rate.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json

from relay import log
from relay.llm import Message, Model, ToolCall, ToolSpec, Turn

MODES = ("drop", "type", "format", "rename")


def parse_spec(spec: str | None) -> tuple[str, float] | None:
    """'mixed:0.3' -> ('mixed', 0.3). 'off', '' and None -> None."""
    if not spec or spec == "off":
        return None
    mode, _, rate = spec.partition(":")
    if mode != "mixed" and mode not in MODES:
        raise ValueError(f"unknown argument fault {mode!r}; use off, mixed, or one of {MODES}")
    value = float(rate) if rate else 1.0
    if not 0.0 <= value <= 1.0:
        raise ValueError("fault rate must be between 0 and 1")
    return mode, value


def _draw(seed: int, ticket: str, salt: str) -> float:
    digest = hashlib.sha256(f"{seed}|{salt}|{ticket}".encode()).hexdigest()
    return int(digest[:8], 16) / 0xFFFFFFFF


def corrupt(args: dict, schema: dict, mode: str) -> dict:
    """Break the first required argument in one specific way."""
    field = schema["required"][0]
    kind = schema["properties"][field]
    out = dict(args)
    value = out.get(field)
    if mode == "drop":
        out.pop(field, None)
    elif mode == "type":
        out[field] = str(value) if kind["type"] == "integer" else [value]
    elif mode == "format":
        if kind["type"] == "integer":
            out[field] = -abs(int(value))
        elif "pattern" in kind:
            out[field] = str(value).lower().replace("-", " ")
        else:
            out[field] = ""
    elif mode == "rename":
        parts = field.split("_")  # invoice_id -> invoiceId, query -> Query: a near-miss name, never the same
        out.pop(field, None)
        out[parts[0] + "".join(p.title() for p in parts[1:]) if len(parts) > 1 else field.title()] = value
    return out


class ArgFaultModel:
    """Wraps a model. Corrupts the first tool call of a hash-selected fraction of tickets, once."""

    def __init__(self, inner: Model, mode: str = "mixed", rate: float = 0.3, seed: int = 7) -> None:
        self.inner, self.mode, self.rate, self.seed = inner, mode, rate, seed
        self.injected: list[tuple[str, str]] = []  # (tool, mode) for every fault injected

    def generate(self, system: str, messages: list[Message], tools: list[ToolSpec]) -> Turn:
        turn = self.inner.generate(system, messages, tools)
        first_call = not any(b["type"] == "tool_call" for m in messages for b in m["content"])
        if not turn.tool_calls or not first_call or self.rate <= 0:
            return turn
        ticket = messages[0]["content"][0]["text"]
        if _draw(self.seed, ticket, "pick") >= self.rate:
            return turn
        mode = self.mode if self.mode != "mixed" else MODES[int(_draw(self.seed, ticket, "mode") * len(MODES)) % len(MODES)]
        call = turn.tool_calls[0]
        schema = next(t["input_schema"] for t in tools if t["name"] == call.name)
        bad = ToolCall(call.id, call.name, corrupt(call.input, schema, mode))
        self.injected.append((call.name, mode))
        log.event("arg_fault_injected", tool=call.name, mode=mode, before=json.dumps(call.input),
                  after=json.dumps(bad.input))
        return dataclasses.replace(turn, tool_calls=(bad,) + turn.tool_calls[1:])
