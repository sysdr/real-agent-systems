"""A rules-based stand-in for a tool-using model, used to record the Day 02 fixtures offline.

It reads the ticket, calls docs_search for product questions, and quotes the first doc it gets back.
It has no billing tool, so it hands refund requests to a human instead of claiming them.
Record your own run with `make record PROVIDER=live` to replace it with a real model.
"""
from __future__ import annotations

import json
import re

from relay.llm import Message, ToolCall, ToolSpec, Turn

ASK = re.compile(r"How do I (.+?) for (\w+)\?")
ESCALATE = ("lawyer", "regulator", "without permission", "down for", "formal complaint", "another company")
REFUSE = ("for customer c-", "friend's company", "admin access", "competitive research")


def _est(s: str) -> int:
    return max(1, len(s) // 4)  # same rough estimate the Day 01 stub used


def _final(decision: str, reply: str, prompt_chars: int) -> Turn:
    text = json.dumps({"decision": decision, "reply": reply})
    return Turn(text, (), "end", prompt_chars // 4, _est(text))


class StubAgentModel:
    def generate(self, system: str, messages: list[Message], tools: list[ToolSpec]) -> Turn:
        prompt_chars = len(system) + len(json.dumps(messages)) + len(json.dumps(tools))
        last = messages[-1]["content"]
        ticket = messages[0]["content"][0]["text"]
        if any(b["type"] == "tool_result" for b in last):
            return self._after_tool(ticket, last, prompt_chars)
        t = ticket.lower()
        if any(k in t for k in ESCALATE):
            return _final("escalate", "I've flagged this for our on-call team.", prompt_chars)
        if any(k in t for k in REFUSE):
            return _final("refuse", "I can't share or change another account's details.", prompt_chars)
        if "refund" in t or "charged twice" in t:
            return _final("escalate", "I can't change billing from here, so I've passed this to a teammate "
                                      "who can.", prompt_chars)
        body = ticket.split("\n\n", 1)[-1]
        ask = ASK.search(body)
        # A capable model writes a short keyword query, not the whole ticket.
        query = f"{ask.group(1)} {ask.group(2)}" if ask else re.sub(r"[^\w\s-]", " ", body)
        call = ToolCall("call_1", "docs_search", {"query": query})
        return Turn("I'll check the docs first.", (call,), "tool_use", prompt_chars // 4, _est(query) + 20)

    @staticmethod
    def _after_tool(ticket: str, blocks: list[dict], prompt_chars: int) -> Turn:
        try:
            docs = json.loads(blocks[0]["content"])
        except json.JSONDecodeError:
            docs = []
        ask = ASK.search(ticket)
        if ask:  # read the results and keep the doc for the product the customer named
            docs = [d for d in docs if d["title"].startswith(f"{ask.group(2)}:")]
        if not docs:
            return _final("escalate", "I couldn't find this in our docs, so I've asked a teammate.", prompt_chars)
        top = docs[0]
        sentence = top["body"].split(". ")[1].rstrip(".")
        return _final("answer", f"{top['title']}: {sentence}.", prompt_chars)
