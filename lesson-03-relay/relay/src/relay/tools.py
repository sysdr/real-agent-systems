"""The tool seam: a name, a JSON schema for the model, and a function that runs it.

Day 02 shipped ONE deliberately plain tool (`docs_toolbox`); any failure ended the run.
Day 03 adds `typed_toolbox`: four tools whose arguments are checked before anything runs,
and two kinds of failure:

  RecoverableError  the model can fix it (bad arguments, unknown id, unknown tool).
                    The loop sends it back as an error tool_result and the model tries again.
  ToolError         the world is broken (timeouts, 5xx, 429, broken JSON) and bounded retries
                    did not help. The run ends, as on Day 02, and counts as an infra error.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable

from relay import log
from relay.llm import ToolCall, ToolSpec

MAX_TOOL_CHARS = 4000


class ToolError(RuntimeError):
    """A tool could not produce a trustworthy result."""


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_schema: dict
    fn: Callable[[dict], str]


class ToolBox:
    def __init__(self, tools: list[Tool]) -> None:
        self._tools = {t.name: t for t in tools}

    def specs(self) -> list[ToolSpec]:
        return [{"name": t.name, "description": t.description, "input_schema": t.input_schema}
                for t in self._tools.values()]

    def run(self, call: ToolCall) -> str:
        tool = self._tools.get(call.name)
        if tool is None:
            raise ToolError(f"unknown tool {call.name!r}")
        out = tool.fn(call.input)
        return out if len(out) <= MAX_TOOL_CHARS else out[:MAX_TOOL_CHARS] + "...[truncated]"


def docs_toolbox(sim_url: str, timeout_s: float = 5.0) -> ToolBox:
    def docs_search(args: dict) -> str:
        query = args.get("query")
        if not isinstance(query, str) or not query.strip():
            raise ToolError("docs_search needs a non-empty string 'query'")
        url = f"{sim_url}/docs/search?{urllib.parse.urlencode({'q': query})}"
        try:
            with urllib.request.urlopen(url, timeout=timeout_s) as resp:
                payload = json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            raise ToolError(f"docs_search failed: http {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ToolError(f"docs_search failed: {type(exc).__name__}") from exc
        except json.JSONDecodeError as exc:
            raise ToolError("docs_search returned malformed JSON") from exc
        if not isinstance(payload, list):
            raise ToolError("docs_search returned an unexpected shape")
        return json.dumps(payload, separators=(",", ":"))

    return ToolBox([Tool(
        "docs_search",
        "Search the Northwind help docs. Returns up to 5 docs as JSON with id, title, and body.",
        {"type": "object",
         "properties": {"query": {"type": "string", "description": "Keywords from the customer's question"}},
         "required": ["query"]},
        docs_search)])


# ------------------------------------------------------------------ Day 03: typed tools

class RecoverableError(Exception):
    """A failure the model can act on. It becomes an error tool_result, not the end of the run."""

    def __init__(self, code: str, tool: str, message: str, retryable: bool, **detail: object) -> None:
        super().__init__(message)
        self.code, self.tool, self.message, self.retryable, self.detail = code, tool, message, retryable, detail

    def to_result(self) -> str:
        """The text the model reads. Stable (no ids, no times) so replay keys stay stable."""
        return json.dumps({"error": self.code, "tool": self.tool, "retryable": self.retryable,
                           "message": self.message, **self.detail}, separators=(",", ":"))


@dataclass(frozen=True)
class Arg:
    """One argument. The JSON schema the model sees and the check we run both come from this."""

    name: str
    type: str  # "string" | "integer"
    description: str
    example: str | int
    pattern: str | None = None
    minimum: int | None = None
    min_length: int | None = None

    def schema(self) -> dict:
        out: dict = {"type": self.type, "description": self.description}
        if self.pattern:
            out["pattern"] = self.pattern
        if self.minimum is not None:
            out["minimum"] = self.minimum
        if self.min_length is not None:
            out["minLength"] = self.min_length
        return out

    def expected(self) -> str:
        parts = [self.type]
        if self.pattern:
            parts.append(f"matching {self.pattern}")
        if self.minimum is not None:
            parts.append(f">= {self.minimum}")
        if self.min_length is not None:
            parts.append(f"at least {self.min_length} characters")
        return " ".join(parts)


def input_schema(args: tuple[Arg, ...]) -> dict:
    return {"type": "object", "properties": {a.name: a.schema() for a in args},
            "required": [a.name for a in args], "additionalProperties": False}


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _problem(arg: Arg, kind: str, **extra: object) -> dict:
    return {"field": arg.name, "problem": kind, "expected": arg.expected(), "example": arg.example, **extra}


def validate(tool: str, raw: object, args: tuple[Arg, ...]) -> dict:
    """Check arguments against the specs. Report EVERY problem at once so one retry can fix them all."""
    if not isinstance(raw, dict):
        raise RecoverableError("invalid_argument", tool, f"{tool} was not run: arguments must be a JSON object.",
                               True, problems=[{"field": "$", "problem": "not_an_object"}])
    problems: list[dict] = []
    names = {a.name for a in args}
    stray = [k for k in raw if k not in names]
    for a in args:
        if a.name not in raw:
            hint = {}
            near = next((k for k in stray if _norm(k) == _norm(a.name)), None) \
                or next(iter(difflib.get_close_matches(a.name, stray, n=1, cutoff=0.7)), None)
            if near:
                hint = {"hint": f"You sent {near!r}; the field is named {a.name!r}."}
            problems.append(_problem(a, "missing", **hint))
            continue
        v = raw[a.name]
        if a.type == "integer":
            if isinstance(v, bool) or not isinstance(v, int):
                problems.append(_problem(a, "wrong_type", got=type(v).__name__))
            elif a.minimum is not None and v < a.minimum:
                problems.append(_problem(a, "too_small", got=v))
        else:
            if not isinstance(v, str):
                problems.append(_problem(a, "wrong_type", got=type(v).__name__))
            elif a.min_length is not None and len(v.strip()) < a.min_length:
                problems.append(_problem(a, "too_short", got=v[:40]))
            elif a.pattern and not re.fullmatch(a.pattern, v):
                problems.append(_problem(a, "bad_format", got=v[:40]))
    renamed = {_norm(k) for k in stray} & {_norm(a.name) for a in args if a.name not in raw}
    for k in stray:
        if _norm(k) not in renamed and not difflib.get_close_matches(k, [a.name for a in args], n=1, cutoff=0.7):
            problems.append({"field": k, "problem": "unexpected", "hint": f"Remove {k!r}. Allowed: {sorted(names)}."})
    if problems:
        raise RecoverableError("invalid_argument", tool,
                               f"{tool} was not run: {len(problems)} problem(s) with the arguments. "
                               "Fix them and call again.", True, problems=problems)
    return raw


class TypedToolBox(ToolBox):
    """Like ToolBox, but an unknown tool name is something the model can fix."""

    def run(self, call: ToolCall) -> str:
        if call.name not in self._tools:
            raise RecoverableError("unknown_tool", call.name, f"No tool named {call.name!r}.", True,
                                   available=sorted(self._tools))
        return super().run(call)


@dataclass(frozen=True)
class RetryPolicy:
    """Bounded retries for failures the world causes. Never applies to bad arguments."""

    max_attempts: int = 3
    base_delay_s: float = 0.2
    max_delay_s: float = 2.0

    @classmethod
    def from_env(cls) -> "RetryPolicy":
        import os
        return cls(int(os.environ.get("RELAY_RETRY_ATTEMPTS", cls.max_attempts)),
                   float(os.environ.get("RELAY_RETRY_BASE_S", cls.base_delay_s)))


class NorthwindClient:
    """HTTP to the simulator. Transient failures retry with backoff; everything else returns as-is."""

    def __init__(self, base_url: str, timeout_s: float = 5.0, retry: RetryPolicy | None = None,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.base_url, self.timeout_s = base_url.rstrip("/"), timeout_s
        self.retry, self.sleep = retry or RetryPolicy(), sleep

    def request(self, tool: str, method: str, path: str, body: dict | None = None,
                headers: dict[str, str] | None = None) -> tuple[int, object]:
        reason = "no attempt"
        for attempt in range(1, self.retry.max_attempts + 1):
            status, data, retry_after, reason = self._once(method, path, body, headers or {})
            if not reason:
                return status, data
            if attempt < self.retry.max_attempts:
                backoff = self.retry.base_delay_s * 2 ** (attempt - 1)
                delay = min(self.retry.max_delay_s, max(backoff, retry_after or 0.0))
                log.event("tool_retry", logging.INFO, tool=tool, attempt=attempt, reason=reason, delay_s=delay)
                self.sleep(delay)
        raise ToolError(f"{tool} unavailable after {self.retry.max_attempts} attempts: {reason}")

    def _once(self, method: str, path: str, body: dict | None,
              headers: dict[str, str]) -> tuple[int, object, float | None, str]:
        """One try. Returns (status, data, retry_after_s, transient_reason); reason is '' on success."""
        req = urllib.request.Request(
            self.base_url + path, method=method, data=json.dumps(body).encode() if body is not None else None,
            headers={"Content-Type": "application/json", **headers})
        retry_after: float | None = None
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                status, raw = resp.status, resp.read()
        except urllib.error.HTTPError as exc:  # before URLError: HTTPError is a subclass
            status, raw = exc.code, exc.read()
            value = exc.headers.get("Retry-After", "")
            retry_after = float(value) if value.replace(".", "", 1).isdigit() else None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            return 0, None, None, type(exc).__name__
        if status in (408, 429) or status >= 500:
            return status, None, retry_after, f"http {status}"
        try:
            data = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            return status, None, None, "malformed JSON"
        return status, data, None, ""


def _unwrap(tool: str, status: int, data: object, noun: str, id_example: str) -> object:
    """Turn an HTTP outcome into data, a RecoverableError, or a fatal ToolError."""
    if 200 <= status < 300:
        return data
    msg = data.get("error") if isinstance(data, dict) else None
    if status == 404:
        raise RecoverableError("not_found", tool, f"No {noun} matches that id ({msg}).", False,
                               hint=f"Ids look like {id_example}. Do not guess another id; escalate if the ticket gives none.")
    if status in (400, 422):
        raise RecoverableError("invalid_argument", tool, f"The server rejected the arguments: {msg}", True)
    raise ToolError(f"{tool} failed: http {status}")


TICKET_ID = Arg("ticket_id", "string", "Support ticket id, five digits", "T-00123", pattern=r"T-\d{5}")
INVOICE_ID = Arg("invoice_id", "string", "Invoice id: INV- plus five digits, or INV-D plus four for a duplicate charge",
                 "INV-00012", pattern=r"INV-(?:D\d{4}|\d{5})")
AMOUNT = Arg("amount_cents", "integer", "Amount in cents, copied from the invoice's amount_cents", 12900, minimum=1)
QUERY = Arg("query", "string", "One to six keywords of four or more letters, from the customer's question",
            "export audit logs", min_length=4)


TOOL_ARGS: dict[str, tuple[Arg, ...]] = {
    "ticket_get": (TICKET_ID,), "invoice_get": (INVOICE_ID,),
    "refund_issue": (INVOICE_ID, AMOUNT), "docs_search": (QUERY,),
}


def idempotency_key(ticket_id: str, invoice_id: str, amount_cents: int) -> str:
    """Same ticket, invoice, and amount always give the same key, so a repeated refund is recognised."""
    return hashlib.sha256(f"{ticket_id}|{invoice_id}|{amount_cents}".encode()).hexdigest()[:24]


def typed_toolbox(sim_url: str, ticket_id: str, retry: RetryPolicy | None = None, timeout_s: float = 5.0,
                  sleep: Callable[[float], None] = time.sleep) -> TypedToolBox:
    """Tickets, billing, and docs. Built per ticket so the refund key is tied to that ticket."""
    client = NorthwindClient(sim_url, timeout_s, retry or RetryPolicy.from_env(), sleep)

    def compact(obj: object) -> str:
        return json.dumps(obj, separators=(",", ":"))

    def make(name: str, description: str, fn: Callable[[dict], str]) -> Tool:
        args = TOOL_ARGS[name]
        return Tool(name, description, input_schema(args), lambda raw: fn(validate(name, raw, args)))

    def ticket_get(a: dict) -> str:
        path = f"/tickets/{urllib.parse.quote(a['ticket_id'], safe='')}"
        return compact(_unwrap("ticket_get", *client.request("ticket_get", "GET", path), "ticket", "T-00123"))

    def invoice_get(a: dict) -> str:
        path = f"/invoices/{urllib.parse.quote(a['invoice_id'], safe='')}"
        return compact(_unwrap("invoice_get", *client.request("invoice_get", "GET", path), "invoice", "INV-00012"))

    def refund_issue(a: dict) -> str:
        key = idempotency_key(ticket_id, a["invoice_id"], a["amount_cents"])
        body = {"invoice_id": a["invoice_id"], "amount_cents": a["amount_cents"], "actor": "relay"}
        status, data = client.request("refund_issue", "POST", "/refunds", body, {"Idempotency-Key": key})
        data = _unwrap("refund_issue", status, data, "invoice", "INV-00012")
        if not isinstance(data, dict) or not isinstance(data.get("refund_id"), int):
            raise ToolError("refund_issue returned an unexpected shape")
        return compact({"refund_id": data["refund_id"], "invoice_id": a["invoice_id"],
                        "amount_cents": a["amount_cents"], "replayed": bool(data.get("replayed"))})

    def docs_search(a: dict) -> str:
        path = f"/docs/search?{urllib.parse.urlencode({'q': a['query']})}"
        data = _unwrap("docs_search", *client.request("docs_search", "GET", path), "doc", "KB-0001")
        if not isinstance(data, list):
            raise ToolError("docs_search returned an unexpected shape")
        return compact(data)

    return TypedToolBox([
        make("ticket_get", "Fetch one support ticket by id. Returns id, customer_id, subject, body.",
             ticket_get),
        make("invoice_get", "Fetch one invoice by id. Returns id, customer_id, amount_cents, status, duplicate_of. "
             "Status duplicate_charge means it repeats the invoice named in duplicate_of.", invoice_get),
        make("refund_issue", "Refund one invoice in full. This moves money: call invoice_get first and pass that "
             "invoice's amount_cents. Repeating the same call is safe; it returns the first refund with replayed true.",
             refund_issue),
        make("docs_search", "Search the Northwind help docs. Returns up to 5 docs as JSON with id, title, and body.",
             docs_search),
    ])
