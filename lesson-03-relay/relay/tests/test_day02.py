"""Day 02 DONE WHEN: the same task passes live and in replay; step and budget caps are tested."""
import json
import os
import tempfile
import unittest
import urllib.request
from pathlib import Path

os.environ.setdefault("RELAY_LOG_LEVEL", "ERROR")  # keep cap warnings out of test output

from relay import agent, llm, stubmodel, tools
from relay.llm import AnthropicAdapter, ProviderError, ReplayMiss, ReplayModel, RecordModel, ToolCall, Turn
from relaybench import grader, run, tasks as bench
from tests.fake_messages_api import API_KEY, FakeMessagesAPI
from tests.helpers import World

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "replay" / "day02.jsonl"
TICKET = {"id": "T-1", "customer_id": "C-0001", "subject": "Question about Edge",
          "body": "How do I download invoices for Edge? What is the limit or timing?"}


class Scripted:
    """A model that plays back a fixed list of turns, then repeats the last one."""

    def __init__(self, *turns: Turn) -> None:
        self.turns, self.calls = list(turns), 0

    def generate(self, system, messages, tools_):
        turn = self.turns[min(self.calls, len(self.turns) - 1)]
        self.calls += 1
        return turn


def tool_turn(tokens: int = 10, call_id: str = "c1") -> Turn:
    return Turn("", (ToolCall(call_id, "ping", {}),), "tool_use", tokens, 0)


def box(counter: list[int]) -> tools.ToolBox:
    def ping(_args: dict) -> str:
        counter.append(1)
        return "pong"
    return tools.ToolBox([tools.Tool("ping", "ping", {"type": "object"}, ping)])


class TestCaps(unittest.TestCase):
    def test_step_cap_stops_a_model_that_never_finishes(self):
        ran: list[int] = []
        model = Scripted(tool_turn())
        res = agent.run_agent(model, box(ran), TICKET, agent.Caps(max_steps=3, max_tokens=10_000))
        self.assertEqual((res.stop, res.steps, model.calls, len(ran)), ("step_cap", 3, 3, 3))
        self.assertEqual(res.decision["decision"], "escalate")

    def test_budget_cap_stops_before_the_tool_runs(self):
        ran: list[int] = []
        res = agent.run_agent(Scripted(tool_turn(tokens=5000)), box(ran), TICKET,
                              agent.Caps(max_steps=6, max_tokens=1000))
        self.assertEqual((res.stop, res.steps, len(ran)), ("budget_cap", 1, 0))

    def test_budget_is_cumulative_across_steps(self):
        ran: list[int] = []
        res = agent.run_agent(Scripted(tool_turn(tokens=600)), box(ran), TICKET,
                              agent.Caps(max_steps=6, max_tokens=1000))
        self.assertEqual((res.stop, res.steps, len(ran)), ("budget_cap", 2, 1))

    def test_a_finished_answer_is_kept_even_over_budget(self):
        final = Turn(json.dumps({"decision": "answer", "reply": "42"}), (), "end", 9000, 10)
        res = agent.run_agent(Scripted(final), box([]), TICKET, agent.Caps(max_steps=6, max_tokens=1000))
        self.assertEqual((res.stop, res.decision["reply"]), ("done", "42"))

    def test_truncated_tool_call_is_never_run(self):
        ran: list[int] = []
        cut = Turn("", (ToolCall("c1", "ping", {}),), "max_tokens", 10, 1024)
        res = agent.run_agent(Scripted(cut), box(ran), TICKET)
        self.assertEqual((res.stop, len(ran)), ("truncated", 0))

    def test_unknown_tool_ends_the_run_with_tool_error(self):
        bad = Turn("", (ToolCall("c1", "delete_everything", {}),), "tool_use", 10, 1)
        res = agent.run_agent(Scripted(bad), box([]), TICKET)
        self.assertEqual(res.stop, "tool_error")
        self.assertIn("unknown tool", res.detail)

    def test_unparseable_final_answer_becomes_a_plain_answer(self):
        res = agent.run_agent(Scripted(Turn("just words", (), "end", 5, 5)), box([]), TICKET)
        self.assertEqual(res.stop, "done")
        self.assertTrue(res.decision["parse_error"])

    def test_tool_results_go_back_in_one_user_message_after_the_call(self):
        calls = (ToolCall("a", "ping", {}), ToolCall("b", "ping", {}))
        two = Turn("", calls, "tool_use", 10, 1)
        done = Turn(json.dumps({"decision": "answer", "reply": "ok"}), (), "end", 10, 1)
        res = agent.run_agent(Scripted(two, done), box([]), TICKET)
        user_msg = res.messages[2]
        self.assertEqual([b["id"] for b in user_msg["content"]], ["a", "b"])
        self.assertTrue(all(b["type"] == "tool_result" for b in user_msg["content"]))


class TestAdapterAndReplay(unittest.TestCase):
    def test_request_translation_matches_the_messages_api(self):
        a = AnthropicAdapter(API_KEY, "some-model", max_tokens=300)
        msgs = [{"role": "user", "content": [{"type": "text", "text": "hi"}]},
                {"role": "assistant", "content": [{"type": "tool_call", "id": "t1", "name": "ping", "input": {}}]},
                {"role": "user", "content": [{"type": "tool_result", "id": "t1", "content": "boom", "is_error": True}]}]
        body = a.build_request("sys", msgs, [{"name": "ping", "description": "d", "input_schema": {"type": "object"}}])
        self.assertEqual(body["model"], "some-model")
        self.assertEqual(body["messages"][1]["content"][0],
                         {"type": "tool_use", "id": "t1", "name": "ping", "input": {}})
        self.assertEqual(body["messages"][2]["content"][0],
                         {"type": "tool_result", "tool_use_id": "t1", "content": "boom", "is_error": True})
        self.assertNotIn(API_KEY, json.dumps(body))

    def test_response_parsing_counts_cached_tokens_and_maps_stops(self):
        a = AnthropicAdapter(API_KEY, "m")
        turn = a.parse_response({"content": [{"type": "text", "text": "x"},
                                             {"type": "tool_use", "id": "t9", "name": "ping", "input": {"q": 1}}],
                                 "stop_reason": "tool_use",
                                 "usage": {"input_tokens": 10, "cache_read_input_tokens": 90, "output_tokens": 5}})
        self.assertEqual((turn.stop, turn.input_tokens, turn.output_tokens), ("tool_use", 100, 5))
        self.assertEqual(turn.tool_calls[0], ToolCall("t9", "ping", {"q": 1}))
        cut = a.parse_response({"content": [], "stop_reason": "max_tokens", "usage": {}})
        self.assertEqual(cut.stop, "max_tokens")
        odd = a.parse_response({"content": [], "stop_reason": "refusal", "usage": {}})
        self.assertEqual(odd.stop, "other")

    def test_http_path_sends_headers_and_classifies_errors(self):
        api = FakeMessagesAPI()
        try:
            a = AnthropicAdapter(API_KEY, "m", base_url=api.url, timeout_s=5)
            msgs = [{"role": "user", "content": [{"type": "text", "text": TICKET["body"]}]}]
            turn = a.generate("sys", msgs, [])
            self.assertEqual(turn.stop, "tool_use")
            self.assertEqual(api.requests[0]["headers"]["x-api-key"], API_KEY)
            for code, retryable in ((429, True), (500, True), (400, False)):
                api.fail_with = code
                with self.assertRaises(ProviderError) as ctx:
                    a.generate("sys", msgs, [])
                self.assertEqual((ctx.exception.status, ctx.exception.retryable), (code, retryable))
        finally:
            api.close()

    def test_replay_key_changes_when_anything_the_model_sees_changes(self):
        m = [{"role": "user", "content": [{"type": "text", "text": "a"}]}]
        base = llm.request_key("s", m, [])
        self.assertEqual(base, llm.request_key("s", json.loads(json.dumps(m)), []))
        self.assertNotEqual(base, llm.request_key("s2", m, []))
        self.assertNotEqual(base, llm.request_key("s", m + m, []))
        self.assertNotEqual(base, llm.request_key("s", m, [{"name": "t"}]))

    def test_replay_miss_is_loud_and_record_dedupes(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "c.jsonl"
            m = [{"role": "user", "content": [{"type": "text", "text": "a"}]}]
            with self.assertRaises(ReplayMiss):
                ReplayModel(path).generate("s", m, [])
            rec = RecordModel(Scripted(Turn("hi", (), "end", 3, 2)), path)
            rec.generate("s", m, [])
            rec.generate("s", m, [])
            self.assertEqual(len(path.read_text().splitlines()), 1)
            self.assertEqual(ReplayModel(path).generate("s", m, []).text, "hi")


class TestDay02(unittest.TestCase):
    """DONE WHEN."""

    def test_same_task_passes_live_and_in_replay(self):
        w = World()
        api = FakeMessagesAPI()
        try:
            task = next(t for t in bench.load(w.bench)
                        if t["category"] == "answer" and "for CDN?" not in _ticket(w, t)["body"])
            ticket = _ticket(w, task)
            toolbox = tools.docs_toolbox(w.url)
            with tempfile.TemporaryDirectory() as d:
                cassette = Path(d) / "task.jsonl"
                live = RecordModel(AnthropicAdapter(API_KEY, "m", base_url=api.url, timeout_s=5), cassette)
                live_res = agent.run_agent(live, toolbox, ticket)
                live_grade = grader.grade(task, live_res.decision, _refunds(w))
                requests_after_live = len(api.requests)
                api.close()  # from here on there is no network path to a model

                replay = ReplayModel(cassette)
                replay_res = agent.run_agent(replay, toolbox, ticket)
                replay_grade = grader.grade(task, replay_res.decision, _refunds(w))

            self.assertTrue(live_grade["passed"], live_grade)
            self.assertTrue(replay_grade["passed"], replay_grade)
            self.assertEqual(live_res.decision, replay_res.decision)
            self.assertEqual(live_res.messages, replay_res.messages)
            self.assertEqual((live_res.steps, live_res.input_tokens, live_res.output_tokens),
                             (replay_res.steps, replay_res.input_tokens, replay_res.output_tokens))
            self.assertEqual(requests_after_live, 2)  # tool_use turn, then the final answer
            self.assertEqual(replay.calls, 2)
        finally:
            w.close()

    def test_bench_in_replay_beats_the_day01_baseline(self):
        w = World()
        try:
            s = run.run(w.url, "replay", "day02-loop", w.bench, FIXTURES, w.scoreboard, agent="loop")
            self.assertEqual(s["tasks"], 250)
            self.assertEqual(s["infra_errors"], 0)
            self.assertEqual(s["capped"], 0)
            self.assertEqual(s["task_success"], 0.665)  # Day 01 baseline: 0.36
            self.assertEqual(s["by_category"]["answer"], "63/70")  # the 7 misses are CDN tickets
            self.assertEqual(s["claimed_but_not_done"], 0)  # Day 01 baseline: 60
        finally:
            w.close()

    def test_tool_fault_ends_the_run_cleanly(self):
        w = World()
        try:
            _set_fault(w, "malformed", 1.0)
            res = agent.run_agent(stubmodel.StubAgentModel(), tools.docs_toolbox(w.url), TICKET)
            self.assertEqual((res.stop, res.steps), ("tool_error", 1))
            self.assertIn("malformed", res.detail)
            for mode in ("500", "429", "timeout"):
                _set_fault(w, mode, 1.0)
                self.assertEqual(agent.run_agent(stubmodel.StubAgentModel(), tools.docs_toolbox(w.url),
                                                 TICKET).stop, "tool_error", mode)
        finally:
            w.close()

    def test_faulted_bench_run_reports_infra_errors_not_replay_misses(self):
        w = World()
        try:
            _set_fault(w, "500", 0.3)
            s = run.run(w.url, "replay", "day02-fault", w.bench, FIXTURES, w.scoreboard, agent="loop")
            self.assertGreater(s["infra_errors"], 0)
            self.assertLess(s["task_success"], 0.665)
        finally:
            w.close()


def _ticket(w: World, task: dict) -> dict:
    with urllib.request.urlopen(f"{w.url}/tickets/{task['ticket_id']}") as r:
        return json.loads(r.read())


def _refunds(w: World) -> list[dict]:
    with urllib.request.urlopen(f"{w.url}/_refunds") as r:
        return json.loads(r.read())


def _set_fault(w: World, mode: str, rate: float) -> None:
    req = urllib.request.Request(w.url + "/_faults", method="POST",
                                 data=json.dumps({"mode": mode, "rate": rate}).encode())
    urllib.request.urlopen(req).read()


if __name__ == "__main__":
    unittest.main()
