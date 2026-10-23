"""The agent loop: call the model, run the tools it asks for, repeat, with hard caps.

Caps decide whether to CONTINUE. They never throw away an answer the model already finished.
Day 03: a RecoverableError from a tool goes back to the model as an error tool_result so it can
correct itself. A plain ToolError (the world is broken) still ends the run.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field

from relay import log
from relay.llm import Message, Model
from relay.tools import RecoverableError, ToolBox, ToolError

SYSTEM = """You are Relay, the support agent for Northwind Cloud.
You have one tool: docs_search. Use it before answering any product question, and quote the number the doc gives.
You cannot change accounts or issue refunds. If a request needs a change on the account, escalate it.
Escalate legal, security, outage, and formal-complaint messages. Refuse requests about another customer's data.
Finish with JSON only:
{"decision": "answer" | "act" | "escalate" | "refuse", "reply": "<message to the customer>"}"""

SYSTEM_TYPED = """You are Relay, the support agent for Northwind Cloud.
Tools: docs_search, ticket_get, invoice_get, refund_issue.
Use docs_search before answering any product question, and quote the number the doc gives.
To refund a duplicate charge: call invoice_get, check that status is duplicate_charge and the customer matches the ticket, then call refund_issue with that invoice's amount_cents, then finish with decision "act".
If a tool returns an error with "retryable": true, read the problems, fix the arguments, and call again. On any other error, do not guess: escalate.
Escalate legal, security, outage, and formal-complaint messages. Refuse requests about another customer's data.
Finish with JSON only:
{"decision": "answer" | "act" | "escalate" | "refuse", "reply": "<message to the customer>"}"""

DECISIONS = {"answer", "act", "escalate", "refuse"}
HANDOFF = "I couldn't finish this safely, so I've passed it to a teammate."


@dataclass(frozen=True)
class Caps:
    max_steps: int = 6        # model calls per task
    max_tokens: int = 8000    # input + output tokens across ALL calls: history is re-sent each step

    @classmethod
    def from_env(cls) -> "Caps":
        return cls(int(os.environ.get("RELAY_MAX_STEPS", cls.max_steps)),
                   int(os.environ.get("RELAY_MAX_TOKENS", cls.max_tokens)))


@dataclass
class AgentResult:
    decision: dict
    stop: str  # done | step_cap | budget_cap | tool_error | truncated | unexpected_stop
    steps: int
    input_tokens: int
    output_tokens: int
    messages: list[Message] = field(default_factory=list)
    detail: str = ""
    recovered: int = 0  # tool errors the model saw and kept going after


def parse_decision(text: str) -> dict:
    """Read the model's final JSON. Never raise: a bad format becomes a plain answer."""
    candidates = [text]
    if "{" in text and "}" in text:
        candidates.append(text[text.index("{"): text.rindex("}") + 1])
    for raw in candidates:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and data.get("decision") in DECISIONS:
            return data
    return {"decision": "answer", "reply": text, "parse_error": True}


def _ticket_text(ticket: dict) -> str:
    return f"Subject: {ticket['subject']}\nCustomer: {ticket['customer_id']}\n\n{ticket['body']}"


def run_agent(model: Model, toolbox: ToolBox, ticket: dict, caps: Caps | None = None,
              system: str = SYSTEM, recover: bool = True) -> AgentResult:
    caps = caps or Caps()
    messages: list[Message] = [{"role": "user", "content": [{"type": "text", "text": _ticket_text(ticket)}]}]
    specs = toolbox.specs()
    steps = tokens_in = tokens_out = recovered = 0

    def stop(reason: str, detail: str = "") -> AgentResult:
        level = logging.WARNING if reason.endswith("_cap") else logging.INFO
        log.event("agent_stop", level, reason=reason, steps=steps, tokens=tokens_in + tokens_out,
                  detail=detail, recovered=recovered, ticket=ticket.get("id"))
        return AgentResult({"decision": "escalate", "reply": HANDOFF, "stop": reason}, reason,
                           steps, tokens_in, tokens_out, messages, detail, recovered)

    while True:
        if steps >= caps.max_steps:
            return stop("step_cap", f"{caps.max_steps} model calls used")
        turn = model.generate(system, messages, specs)
        steps += 1
        tokens_in += turn.input_tokens
        tokens_out += turn.output_tokens
        messages.append(turn.to_message())
        log.event("model_call", step=steps, stop=turn.stop, tools=[c.name for c in turn.tool_calls],
                  input_tokens=turn.input_tokens, output_tokens=turn.output_tokens)

        if turn.stop == "end":
            return AgentResult(parse_decision(turn.text), "done", steps, tokens_in, tokens_out, messages,
                               recovered=recovered)
        if turn.stop == "tool_use" and turn.tool_calls:
            if tokens_in + tokens_out > caps.max_tokens:
                return stop("budget_cap", f"{tokens_in + tokens_out} tokens > {caps.max_tokens}")
            results = []
            for call in turn.tool_calls:
                try:
                    out = toolbox.run(call)
                except RecoverableError as exc:
                    if not recover:  # Day 02 behaviour: any tool failure ends the run
                        log.event("tool_error", tool=call.name, error=exc.code)
                        return stop("tool_error", f"{exc.code}: {exc.message}")
                    recovered += 1
                    log.event("tool_error_recoverable", tool=call.name, error=exc.code, step=steps)
                    results.append({"type": "tool_result", "id": call.id, "content": exc.to_result(),
                                    "is_error": True})
                    continue
                except ToolError as exc:
                    log.event("tool_error", tool=call.name, error=str(exc))
                    return stop("tool_error", str(exc))
                log.event("tool_result", tool=call.name, chars=len(out))
                results.append({"type": "tool_result", "id": call.id, "content": out})
            # Every tool_use gets its result in the very next user message, results only.
            messages.append({"role": "user", "content": results})
            continue
        # max_tokens may have cut a tool call in half: never run a partial call.
        return stop("truncated" if turn.stop == "max_tokens" else "unexpected_stop", f"stop={turn.stop}")
