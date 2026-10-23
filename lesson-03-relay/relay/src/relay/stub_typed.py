"""A rules-based stand-in for a tool-using model that can read Day 03's typed errors.

It records the Day 03 fixtures offline. For a duplicate-charge ticket it looks up the invoice, checks it,
and refunds it. When a tool answers with `invalid_argument`, it re-issues the call it meant to make,
which is what a capable model does after reading the problems list. For any other error it escalates.
Record your own run with `make record AGENT=typed PROVIDER=live` to replace it with a real model.
"""
from __future__ import annotations

import json
import re

from relay.llm import Message, ToolCall, ToolSpec, Turn
from relay.stubmodel import ASK, ESCALATE, REFUSE, StubAgentModel, _est, _final

CHARGED = re.compile(r"I was charged twice\. (INV-D\d{4}) looks like a duplicate of (INV-\d{5})\.")
CUSTOMER = re.compile(r"^Customer: (C-\d{4})$", re.M)
RECOVERABLE = ("invalid_argument", "unknown_tool")


def _pairs(messages: list[Message]) -> list[tuple[dict, dict]]:
    """Every (tool_call, tool_result) pair so far, in order."""
    out: list[tuple[dict, dict]] = []
    for i, m in enumerate(messages[:-1]):
        calls = [b for b in m["content"] if b["type"] == "tool_call"] if m["role"] == "assistant" else []
        results = {b["id"]: b for b in messages[i + 1]["content"] if b["type"] == "tool_result"}
        out += [(c, results[c["id"]]) for c in calls if c["id"] in results]
    return out


class StubTypedModel:
    def generate(self, system: str, messages: list[Message], tools: list[ToolSpec]) -> Turn:
        chars = len(system) + len(json.dumps(messages)) + len(json.dumps(tools))
        ticket = messages[0]["content"][0]["text"]
        t = ticket.lower()
        if any(k in t for k in ESCALATE):
            return _final("escalate", "I've flagged this for our on-call team.", chars)
        if any(k in t for k in REFUSE):
            return _final("refuse", "I can't share or change another account's details.", chars)
        charged = CHARGED.search(ticket)
        if not charged and ("refund" in t or "charged twice" in t):
            return _final("escalate", "I can't change billing from here, so I've passed this to a teammate "
                                      "who can.", chars)
        pairs = _pairs(messages)
        last = messages[-1]["content"]
        for block in (b for b in last if b["type"] == "tool_result" and b.get("is_error")):
            if json.loads(block["content"]).get("error") not in RECOVERABLE:
                return _final("escalate", "I couldn't verify this in our systems, so I've asked a teammate.", chars)
        ok = [(c, json.loads(r["content"])) for c, r in pairs if not r.get("is_error")]
        call_id = f"call_{len(pairs) + 1}"
        if charged:
            return self._refund(ticket, charged, ok, call_id, chars)
        if not ok:
            body = ticket.split("\n\n", 1)[-1]
            ask = ASK.search(body)
            # A capable model writes a short keyword query, not the whole ticket.
            query = f"{ask.group(1)} {ask.group(2)}" if ask else re.sub(r"[^\w\s-]", " ", body)
            return Turn("I'll check the docs first.", (ToolCall(call_id, "docs_search", {"query": query}),),
                        "tool_use", chars // 4, _est(query) + 20)
        return StubAgentModel._after_tool(ticket, [{"type": "tool_result", "content": json.dumps(ok[0][1])}], chars)

    @staticmethod
    def _refund(ticket: str, charged: re.Match, ok: list, call_id: str, chars: int) -> Turn:
        dup, original = charged.groups()
        customer = CUSTOMER.search(ticket).group(1)
        if not ok:
            call = ToolCall(call_id, "invoice_get", {"invoice_id": dup})
            return Turn("I'll look up the invoice.", (call,), "tool_use", chars // 4, 40)
        invoice = ok[0][1]
        if (invoice.get("status"), invoice.get("duplicate_of"), invoice.get("customer_id")) != \
                ("duplicate_charge", original, customer):
            return _final("escalate", "The invoice doesn't match this request, so I've asked a teammate.", chars)
        if len(ok) == 1:
            call = ToolCall(call_id, "refund_issue", {"invoice_id": dup, "amount_cents": invoice["amount_cents"]})
            return Turn("It is a duplicate of the earlier invoice, so I'll refund it.", (call,),
                        "tool_use", chars // 4, 50)
        amount = f"${invoice['amount_cents'] / 100:,.2f}"
        return _final("act", f"I've refunded {amount} for the duplicate charge {dup}.", chars)
