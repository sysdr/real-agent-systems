"""The Day 01 baseline: one prompt, no tools, no knowledge base."""
from __future__ import annotations

import json

SYSTEM = """You are Relay, the support agent for Northwind Cloud.
Read the ticket and respond with JSON only:
{"decision": "answer" | "act" | "escalate" | "refuse", "reply": "<message to the customer>"}
- answer: questions you can answer
- act: requests that need a change on the account
- escalate: legal, security, outages, or formal complaints
- refuse: requests about another customer's data or account"""


def handle(provider, ticket: dict) -> tuple[dict, object]:
    user = f"Subject: {ticket['subject']}\nCustomer: {ticket['customer_id']}\n\n{ticket['body']}"
    completion = provider.complete(SYSTEM, user)
    try:
        decision = json.loads(completion.text)
        if decision.get("decision") not in {"answer", "act", "escalate", "refuse"}:
            raise ValueError
    except (ValueError, json.JSONDecodeError, AttributeError):
        decision = {"decision": "answer", "reply": completion.text, "parse_error": True}
    return decision, completion
