"""Model providers behind one tiny interface.

replay  - default. Returns recorded responses; free, offline, deterministic.
stub    - a rules-based stand-in for a no-tools model, used to record Day 01 fixtures.
live    - the Anthropic Messages API (needs ANTHROPIC_API_KEY and RELAY_MODEL).
record  - wraps another provider and saves every response as a replay fixture.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.request
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Completion:
    text: str
    input_tokens: int
    output_tokens: int


def _tokens(s: str) -> int:
    return max(1, len(s) // 4)  # rough estimate; live mode uses real counts


def cache_key(system: str, user: str) -> str:
    return hashlib.sha256(f"{system}\x00{user}".encode()).hexdigest()[:20]


class ReplayMiss(RuntimeError):
    pass


class ReplayProvider:
    def __init__(self, path: Path) -> None:
        self.table = {}
        if path.exists():
            for line in path.read_text().splitlines():
                rec = json.loads(line)
                self.table[rec["key"]] = rec

    def complete(self, system: str, user: str) -> Completion:
        rec = self.table.get(cache_key(system, user))
        if rec is None:
            raise ReplayMiss("no recorded response for this prompt; run `make record` "
                             "after changing a prompt")
        return Completion(rec["text"], rec["input_tokens"], rec["output_tokens"])


class StubProvider:
    """Behaves like a capable model with NO tools and NO knowledge base.

    It can read the ticket and pick a decision, but it can only guess facts,
    and it happily claims actions it cannot take. That is the point of Day 01.
    """

    ESCALATE = ("lawyer", "regulator", "without permission", "down for", "formal complaint",
                "another company")
    REFUSE = ("for customer c-", "friend's company", "admin access", "competitive research")

    def complete(self, system: str, user: str) -> Completion:
        t = user.lower()
        if any(k in t for k in self.ESCALATE):
            out = {"decision": "escalate", "reply": "I've flagged this for our on-call team."}
        elif any(k in t for k in self.REFUSE):
            out = {"decision": "refuse", "reply": "I can't share or change another account's details."}
        elif "refund" in t or "charged twice" in t:
            out = {"decision": "act", "reply": "Done! I've refunded the duplicate charge."}
        else:
            guess = 24 if re.search(r"hour|timing", t) else 30
            out = {"decision": "answer", "reply": f"Usually this is around {guess}. Check the console."}
        text = json.dumps(out)
        return Completion(text, _tokens(system + user), _tokens(text))


class RecordProvider:
    def __init__(self, inner, path: Path) -> None:
        self.inner, self.path = inner, path
        path.parent.mkdir(parents=True, exist_ok=True)

    def complete(self, system: str, user: str) -> Completion:
        c = self.inner.complete(system, user)
        with self.path.open("a") as f:
            f.write(json.dumps({"key": cache_key(system, user), "text": c.text,
                                "input_tokens": c.input_tokens, "output_tokens": c.output_tokens}) + "\n")
        return c


class AnthropicProvider:
    """Live mode. Not exercised by the offline test suite."""

    def __init__(self) -> None:
        self.key = os.environ["ANTHROPIC_API_KEY"]
        self.model = os.environ["RELAY_MODEL"]  # set explicitly; no hidden default

    def complete(self, system: str, user: str) -> Completion:
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=json.dumps({"model": self.model, "max_tokens": 400, "system": system,
                             "messages": [{"role": "user", "content": user}]}).encode(),
            headers={"x-api-key": self.key, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())
        text = "".join(b.get("text", "") for b in data["content"] if b.get("type") == "text")
        return Completion(text, data["usage"]["input_tokens"], data["usage"]["output_tokens"])


def make(name: str, fixtures: Path):
    if name == "replay":
        return ReplayProvider(fixtures)
    if name == "stub":
        return StubProvider()
    if name == "live":
        return AnthropicProvider()
    if name.startswith("record:"):
        if fixtures.exists():
            fixtures.unlink()
        return RecordProvider(make(name.split(":", 1)[1], fixtures), fixtures)
    raise ValueError(f"unknown provider {name}")
