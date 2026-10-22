"""The tool seam: a name, a JSON schema for the model, and a function that runs it.

Day 02 ships ONE deliberately plain tool so the loop has something to call.
Day 03 replaces it with typed tools and errors the agent can recover from.
Until then, any tool failure ends the run (see agent.run_agent).
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable

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
