"""Day 03 DONE WHEN: injected bad-argument faults are recovered in tests."""
from __future__ import annotations

import json
import os
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

os.environ.setdefault("RELAY_LOG_LEVEL", "ERROR")

from relay import agent, faults, tools
from relay.llm import ToolCall, Turn
from relay.stub_typed import StubTypedModel
from relay.tools import RecoverableError, RetryPolicy, ToolError
from relaybench import grader, run, tasks as bench
from tests.helpers import World

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "replay" / "day03.jsonl"
NO_SLEEP = lambda _s: None  # noqa: E731 - retries without waiting
TICKET = {"id": "T-1", "customer_id": "C-0001", "subject": "s", "body": "b"}


# ----------------------------------------------------------------- test doubles

class ScriptedAPI:
    """A tiny HTTP server that plays back (status, body, headers) replies, then repeats the last one."""

    def __init__(self, *replies: tuple[int, bytes, dict]) -> None:
        self.replies, self.paths = list(replies), []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                pass

            def do_GET(self):
                outer.paths.append(self.path)
                status, body, headers = outer.replies[min(len(outer.paths) - 1, len(outer.replies) - 1)]
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.srv.shutdown()
        self.srv.server_close()


OK_INVOICE = (200, b'{"id":"INV-00001","customer_id":"C-0001","amount_cents":5000,"status":"paid","duplicate_of":null}', {})


class Scripted:
    def __init__(self, *turns: Turn) -> None:
        self.turns, self.calls = list(turns), 0

    def generate(self, system, messages, tools_):
        turn = self.turns[min(self.calls, len(self.turns) - 1)]
        self.calls += 1
        return turn


def call_turn(name: str, args: dict, call_id: str = "c1") -> Turn:
    return Turn("", (ToolCall(call_id, name, args),), "tool_use", 10, 1)


FINAL = Turn(json.dumps({"decision": "answer", "reply": "done"}), (), "end", 10, 1)


def problems_of(exc: RecoverableError) -> list[dict]:
    return exc.detail["problems"]


def http(url: str, method: str = "GET", body: dict | None = None, headers: dict | None = None):
    req = urllib.request.Request(url, method=method, headers=headers or {},
                                 data=json.dumps(body).encode() if body is not None else None)
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.status, json.loads(r.read())


# ------------------------------------------------------------------- validation

class TestValidation(unittest.TestCase):
    A = tools.TOOL_ARGS

    def test_valid_arguments_pass_and_schema_comes_from_the_same_specs(self):
        self.assertEqual(tools.validate("refund_issue", {"invoice_id": "INV-D0010", "amount_cents": 500},
                                        self.A["refund_issue"]), {"invoice_id": "INV-D0010", "amount_cents": 500})
        schema = tools.input_schema(self.A["refund_issue"])
        self.assertEqual(schema["required"], ["invoice_id", "amount_cents"])
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(schema["properties"]["amount_cents"]["minimum"], 1)

    def test_missing_field_comes_with_expected_shape_and_an_example(self):
        with self.assertRaises(RecoverableError) as ctx:
            tools.validate("invoice_get", {}, self.A["invoice_get"])
        (p,) = problems_of(ctx.exception)
        self.assertEqual((p["field"], p["problem"], p["example"]), ("invoice_id", "missing", "INV-00012"))
        self.assertIn("INV-", p["expected"])
        self.assertTrue(ctx.exception.retryable)

    def test_a_near_miss_name_gets_a_did_you_mean_hint(self):
        for sent in ("invoiceId", "InvoiceID", "invoice-id"):
            with self.assertRaises(RecoverableError) as ctx:
                tools.validate("invoice_get", {sent: "INV-00001"}, self.A["invoice_get"])
            (p,) = problems_of(ctx.exception)  # one problem: the rename is not also reported as 'unexpected'
            self.assertIn(f"You sent {sent!r}", p["hint"])

    def test_wrong_type_includes_bool_which_python_treats_as_int(self):
        for bad, got in (("500", "str"), (True, "bool"), (5.0, "float")):
            with self.assertRaises(RecoverableError) as ctx:
                tools.validate("refund_issue", {"invoice_id": "INV-00001", "amount_cents": bad}, self.A["refund_issue"])
            self.assertEqual((problems_of(ctx.exception)[0]["problem"], problems_of(ctx.exception)[0]["got"]),
                             ("wrong_type", got))

    def test_format_range_and_length_rules(self):
        cases = [("ticket_get", {"ticket_id": "t 00001"}, "bad_format"),
                 ("invoice_get", {"invoice_id": "INV-1"}, "bad_format"),
                 ("refund_issue", {"invoice_id": "INV-00001", "amount_cents": 0}, "too_small"),
                 ("docs_search", {"query": " "}, "too_short")]
        for name, args, kind in cases:
            with self.subTest(name=name), self.assertRaises(RecoverableError) as ctx:
                tools.validate(name, args, self.A[name])
            self.assertEqual(problems_of(ctx.exception)[0]["problem"], kind)

    def test_every_problem_is_reported_at_once(self):
        with self.assertRaises(RecoverableError) as ctx:
            tools.validate("refund_issue", {"invoice_id": "nope", "amount_cents": "5", "reason": "dup"},
                           self.A["refund_issue"])
        got = {(p["field"], p["problem"]) for p in problems_of(ctx.exception)}
        self.assertEqual(got, {("invoice_id", "bad_format"), ("amount_cents", "wrong_type"), ("reason", "unexpected")})
        self.assertIn("3 problem(s)", ctx.exception.message)

    def test_non_object_arguments_are_recoverable(self):
        with self.assertRaises(RecoverableError) as ctx:
            tools.validate("docs_search", "export audit logs", self.A["docs_search"])
        self.assertEqual(problems_of(ctx.exception)[0]["problem"], "not_an_object")

    def test_error_text_is_stable_json_for_replay_keys(self):
        try:
            tools.validate("invoice_get", {}, self.A["invoice_get"])
        except RecoverableError as exc:
            first = exc.to_result()
        try:
            tools.validate("invoice_get", {}, self.A["invoice_get"])
        except RecoverableError as exc:
            self.assertEqual(first, exc.to_result())
        self.assertEqual(json.loads(first)["error"], "invalid_argument")


# ------------------------------------------------------------------- retries

class TestRetries(unittest.TestCase):
    def client(self, api: ScriptedAPI, sleeps: list[float], attempts: int = 3) -> tools.NorthwindClient:
        return tools.NorthwindClient(api.url, 2.0, RetryPolicy(attempts, 0.2, 2.0), sleeps.append)

    def test_transient_failures_retry_with_backoff_then_succeed(self):
        api = ScriptedAPI((500, b"{}", {}), (429, b"", {"Retry-After": "1"}), OK_INVOICE)
        sleeps: list[float] = []
        try:
            status, data = self.client(api, sleeps).request("invoice_get", "GET", "/invoices/INV-00001")
        finally:
            api.close()
        self.assertEqual((status, data["id"], len(api.paths)), (200, "INV-00001", 3))
        self.assertEqual(sleeps, [0.2, 1.0])  # backoff 0.2, then Retry-After 1.0 beats backoff 0.4

    def test_retry_after_is_capped(self):
        api = ScriptedAPI((429, b"", {"Retry-After": "30"}), OK_INVOICE)
        sleeps: list[float] = []
        try:
            self.client(api, sleeps).request("invoice_get", "GET", "/x")
        finally:
            api.close()
        self.assertEqual(sleeps, [2.0])

    def test_broken_json_counts_as_transient(self):
        api = ScriptedAPI((200, b'{"id": "INV-0', {}), OK_INVOICE)
        try:
            status, _ = self.client(api, []).request("invoice_get", "GET", "/x")
        finally:
            api.close()
        self.assertEqual((status, len(api.paths)), (200, 2))

    def test_attempts_are_bounded_and_the_last_reason_is_reported(self):
        for reply, reason in (((500, b"{}", {}), "http 500"), ((200, b"{", {}), "malformed JSON")):
            api = ScriptedAPI(reply)
            sleeps: list[float] = []
            try:
                with self.assertRaises(ToolError) as ctx:
                    self.client(api, sleeps).request("docs_search", "GET", "/x")
            finally:
                api.close()
            self.assertEqual(len(api.paths), 3)
            self.assertEqual(len(sleeps), 2)  # no sleep after the last attempt
            self.assertIn(f"unavailable after 3 attempts: {reason}", str(ctx.exception))

    def test_an_unreachable_server_is_retried_then_fatal(self):
        sleeps: list[float] = []
        c = tools.NorthwindClient("http://127.0.0.1:9", 0.5, RetryPolicy(2, 0.1, 1.0), sleeps.append)
        with self.assertRaises(ToolError):
            c.request("docs_search", "GET", "/x")
        self.assertEqual(len(sleeps), 1)

    def test_client_errors_are_never_retried(self):
        for status, expect in ((404, RecoverableError), (422, RecoverableError), (403, ToolError)):
            api = ScriptedAPI((status, b'{"error":"nope"}', {}))
            try:
                box = tools.typed_toolbox(api.url, "T-1", RetryPolicy(3, 0.2, 2.0), sleep=NO_SLEEP)
                with self.assertRaises(expect):
                    box.run(ToolCall("c", "invoice_get", {"invoice_id": "INV-00001"}))
            finally:
                api.close()
            self.assertEqual(len(api.paths), 1, status)

    def test_bad_arguments_never_reach_the_network(self):
        api = ScriptedAPI(OK_INVOICE)
        try:
            box = tools.typed_toolbox(api.url, "T-1", sleep=NO_SLEEP)
            with self.assertRaises(RecoverableError):
                box.run(ToolCall("c", "invoice_get", {"invoice_id": "inv 00001"}))
        finally:
            api.close()
        self.assertEqual(api.paths, [])


# ------------------------------------------------------------------- tools against the world

def set_fault(w: World, mode: str, rate: float) -> None:
    http(w.url + "/_faults", "POST", {"mode": mode, "rate": rate})


class TestTypedToolsAgainstTheWorld(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.w = World()

    @classmethod
    def tearDownClass(cls):
        cls.w.close()

    def setUp(self):
        http(self.w.url + "/_reset", "POST", {})
        set_fault(self.w, "off", 0.0)
        self.box = tools.typed_toolbox(self.w.url, "T-00001", RetryPolicy(3, 0.2, 2.0), sleep=NO_SLEEP)

    def run_tool(self, name: str, args: dict) -> dict | list:
        return json.loads(self.box.run(ToolCall("c", name, args)))

    def refunds(self) -> list[dict]:
        return http(self.w.url + "/_refunds")[1]

    def dup_invoice(self) -> dict:
        return self.run_tool("invoice_get", {"invoice_id": "INV-D0000"})

    def test_all_four_tools_return_world_data(self):
        self.assertEqual(self.run_tool("ticket_get", {"ticket_id": "T-00001"})["id"], "T-00001")
        inv = self.dup_invoice()
        self.assertEqual((inv["status"], inv["duplicate_of"]), ("duplicate_charge", "INV-00000"))
        docs = self.run_tool("docs_search", {"query": "export audit logs"})
        self.assertTrue(docs and "title" in docs[0])
        out = self.run_tool("refund_issue", {"invoice_id": "INV-D0000", "amount_cents": inv["amount_cents"]})
        self.assertEqual((out["refund_id"], out["replayed"]), (1, False))

    def test_a_well_formed_id_that_does_not_exist_is_not_found_and_not_retryable(self):
        with self.assertRaises(RecoverableError) as ctx:
            self.box.run(ToolCall("c", "invoice_get", {"invoice_id": "INV-99999"}))
        e = ctx.exception
        self.assertEqual((e.code, e.retryable), ("not_found", False))
        self.assertIn("INV-00012", e.detail["hint"])

    def test_repeating_a_refund_returns_the_first_one(self):
        args = {"invoice_id": "INV-D0000", "amount_cents": self.dup_invoice()["amount_cents"]}
        first, second = self.run_tool("refund_issue", args), self.run_tool("refund_issue", args)
        self.assertEqual((first["replayed"], second["replayed"], second["refund_id"]), (False, True, first["refund_id"]))
        self.assertEqual(len(self.refunds()), 1)

    def test_a_key_reused_with_different_arguments_is_refused(self):
        h = {"Idempotency-Key": "k1"}
        http(self.w.url + "/refunds", "POST", {"invoice_id": "INV-D0000", "amount_cents": 100}, h)
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            http(self.w.url + "/refunds", "POST", {"invoice_id": "INV-D0000", "amount_cents": 200}, h)
        self.assertEqual(ctx.exception.code, 409)
        self.assertEqual(len(self.refunds()), 1)

    def test_reset_restarts_refund_ids_so_replay_text_is_stable(self):
        args = {"invoice_id": "INV-D0000", "amount_cents": 100}
        self.assertEqual(self.run_tool("refund_issue", args)["refund_id"], 1)
        http(self.w.url + "/_reset", "POST", {})
        self.assertEqual(self.run_tool("refund_issue", args)["refund_id"], 1)

    def test_FAULT_lost_response_is_retried_without_a_second_refund(self):
        # Fault draws with seed 7 are 0.3238, 0.1508, 0.6509: at rate 0.2 only the SECOND request is lost.
        set_fault(self.w, "lost", 0.2)
        inv = self.dup_invoice()                      # draw 1: passes
        out = self.run_tool("refund_issue", {"invoice_id": "INV-D0000", "amount_cents": inv["amount_cents"]})
        self.assertTrue(out["replayed"])              # attempt 1 was committed, then its reply was lost
        self.assertEqual(len(self.refunds()), 1)

    def test_FAULT_when_every_reply_is_lost_the_run_fails_but_money_moves_once(self):
        set_fault(self.w, "lost", 1.0)
        with self.assertRaises(ToolError) as ctx:
            self.box.run(ToolCall("c", "refund_issue", {"invoice_id": "INV-D0000", "amount_cents": 4200}))
        self.assertIn("unavailable after 3 attempts: http 500", str(ctx.exception))
        self.assertEqual([(r["invoice_id"], r["amount_cents"]) for r in self.refunds()], [("INV-D0000", 4200)])


# ------------------------------------------------------------------- the loop

class TestRecoveryLoop(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.w = World()

    @classmethod
    def tearDownClass(cls):
        cls.w.close()

    def box(self) -> tools.TypedToolBox:
        return tools.typed_toolbox(self.w.url, "T-1", sleep=NO_SLEEP)

    def test_bad_arguments_come_back_as_an_error_result_and_the_model_fixes_them(self):
        model = Scripted(call_turn("invoice_get", {"invoiceId": "INV-00001"}, "a"),
                         call_turn("invoice_get", {"invoice_id": "INV-00001"}, "b"), FINAL)
        res = agent.run_agent(model, self.box(), TICKET, system=agent.SYSTEM_TYPED)
        self.assertEqual((res.stop, res.steps, res.recovered), ("done", 3, 1))
        block = res.messages[2]["content"][0]
        self.assertTrue(block["is_error"])
        payload = json.loads(block["content"])
        self.assertEqual((payload["error"], payload["retryable"]), ("invalid_argument", True))
        self.assertIn("invoiceId", payload["problems"][0]["hint"])
        self.assertNotIn("is_error", res.messages[4]["content"][0])  # the good result carries no error flag

    def test_with_recovery_off_the_same_mistake_ends_the_run_like_day_02(self):
        model = Scripted(call_turn("invoice_get", {"invoiceId": "INV-00001"}), FINAL)
        res = agent.run_agent(model, self.box(), TICKET, recover=False)
        self.assertEqual((res.stop, res.steps, res.recovered, model.calls), ("tool_error", 1, 0, 1))
        self.assertIn("invalid_argument", res.detail)

    def test_an_unknown_tool_name_lists_the_real_ones(self):
        model = Scripted(call_turn("delete_everything", {}), FINAL)
        res = agent.run_agent(model, self.box(), TICKET)
        payload = json.loads(res.messages[2]["content"][0]["content"])
        self.assertEqual((res.stop, payload["error"]), ("done", "unknown_tool"))
        self.assertEqual(payload["available"], ["docs_search", "invoice_get", "refund_issue", "ticket_get"])

    def test_a_broken_world_still_ends_the_run_and_is_not_offered_to_the_model(self):
        http(self.w.url + "/_faults", "POST", {"mode": "500", "rate": 1.0})
        try:
            model = Scripted(call_turn("docs_search", {"query": "export audit logs"}), FINAL)
            res = agent.run_agent(model, self.box(), TICKET)
        finally:
            http(self.w.url + "/_faults", "POST", {"mode": "off", "rate": 0.0})
        self.assertEqual((res.stop, res.recovered, model.calls), ("tool_error", 0, 1))
        self.assertIn("unavailable after 3 attempts", res.detail)

    def test_a_model_that_never_fixes_its_arguments_is_stopped_by_the_step_cap(self):
        model = Scripted(call_turn("invoice_get", {}))
        res = agent.run_agent(model, self.box(), TICKET, agent.Caps(max_steps=3, max_tokens=10_000))
        self.assertEqual((res.stop, res.steps, res.recovered), ("step_cap", 3, 3))

    def test_every_tool_call_gets_a_result_even_when_one_of_two_fails(self):
        two = Turn("", (ToolCall("a", "ticket_get", {"ticket_id": "T-00001"}),
                        ToolCall("b", "ticket_get", {"ticket_id": "bad"})), "tool_use", 10, 1)
        res = agent.run_agent(Scripted(two, FINAL), self.box(), TICKET)
        results = res.messages[2]["content"]
        self.assertEqual([(b["id"], bool(b.get("is_error"))) for b in results], [("a", False), ("b", True)])


# ------------------------------------------------------------------- fault injection

class TestArgFaultModel(unittest.TestCase):
    EXAMPLES = {"ticket_get": {"ticket_id": "T-00123"}, "invoice_get": {"invoice_id": "INV-00012"},
                "refund_issue": {"invoice_id": "INV-D0010", "amount_cents": 12900},
                "docs_search": {"query": "export audit logs"}}

    def specs(self) -> list[dict]:
        return tools.typed_toolbox("http://127.0.0.1:9", "T-1").specs()

    def test_every_mode_breaks_every_tool_in_a_way_validation_catches(self):
        schemas = {s["name"]: s["input_schema"] for s in self.specs()}
        for name, args in self.EXAMPLES.items():
            tools.validate(name, args, tools.TOOL_ARGS[name])  # the example itself is valid
            for mode in faults.MODES:
                with self.subTest(tool=name, mode=mode), self.assertRaises(RecoverableError):
                    tools.validate(name, faults.corrupt(args, schemas[name], mode), tools.TOOL_ARGS[name])

    def messages(self, text: str, extra: list | None = None) -> list[dict]:
        return [{"role": "user", "content": [{"type": "text", "text": text}]}] + (extra or [])

    def test_only_the_first_tool_call_of_a_run_is_corrupted(self):
        model = faults.ArgFaultModel(Scripted(call_turn("docs_search", self.EXAMPLES["docs_search"])), "drop", 1.0)
        first = model.generate("s", self.messages("t"), self.specs())
        self.assertEqual(first.tool_calls[0].input, {})
        history = self.messages("t", [{"role": "assistant", "content": [
            {"type": "tool_call", "id": "c1", "name": "docs_search", "input": {}}]}])
        later = model.generate("s", history, self.specs())
        self.assertEqual(later.tool_calls[0].input, self.EXAMPLES["docs_search"])
        self.assertEqual(model.injected, [("docs_search", "drop")])

    def test_selection_is_deterministic_and_a_higher_rate_only_adds_tickets(self):
        def picked(rate: float) -> set[str]:
            out = set()
            for i in range(200):
                m = faults.ArgFaultModel(Scripted(call_turn("docs_search", self.EXAMPLES["docs_search"])), "mixed", rate)
                m.generate("s", self.messages(f"ticket {i}"), self.specs())
                if m.injected:
                    out.add(f"ticket {i}")
            return out
        low, high, again = picked(0.3), picked(1.0), picked(0.3)
        self.assertEqual(low, again)
        self.assertTrue(low < high and len(high) == 200)
        self.assertTrue(40 <= len(low) <= 80, len(low))  # about 30% of 200
        self.assertEqual(picked(0.0), set())

    def test_mixed_mode_uses_all_four_faults(self):
        seen = set()
        for i in range(60):
            m = faults.ArgFaultModel(Scripted(call_turn("docs_search", self.EXAMPLES["docs_search"])), "mixed", 1.0)
            m.generate("s", self.messages(f"ticket {i}"), self.specs())
            seen.add(m.injected[0][1])
        self.assertEqual(seen, set(faults.MODES))

    def test_parse_spec(self):
        self.assertIsNone(faults.parse_spec("off"))
        self.assertIsNone(faults.parse_spec(""))
        self.assertEqual(faults.parse_spec("mixed:0.3"), ("mixed", 0.3))
        self.assertEqual(faults.parse_spec("drop"), ("drop", 1.0))
        for bad in ("nonsense:0.3", "mixed:2"):
            with self.assertRaises(ValueError):
                faults.parse_spec(bad)


# ------------------------------------------------------------------- DONE WHEN

def _ticket(w: World, task: dict) -> dict:
    return http(f"{w.url}/tickets/{task['ticket_id']}")[1]


class TestDay03(unittest.TestCase):
    """DONE WHEN: injected bad-argument faults are recovered."""

    @classmethod
    def setUpClass(cls):
        cls.w = World()
        sb = cls.w.scoreboard
        cls.clean = run.run(cls.w.url, "replay", "t-clean", cls.w.bench, FIXTURES, sb, agent="typed")
        cls.faulted = run.run(cls.w.url, "replay", "t-faulted", cls.w.bench, FIXTURES, sb, agent="typed",
                              arg_fault=("mixed", 1.0))
        cls.fatal = run.run(cls.w.url, "replay", "t-fatal", cls.w.bench, FIXTURES, sb, agent="typed",
                            arg_fault=("mixed", 1.0), recover=False)

    @classmethod
    def tearDownClass(cls):
        cls.w.close()

    def solve_with_fault(self, kind: str, mode: str) -> tuple[agent.AgentResult, dict]:
        task = next(t for t in bench.load(self.w.bench) if t["category"] == kind
                    and "for CDN?" not in _ticket(self.w, t)["body"])
        ticket = _ticket(self.w, task)
        http(self.w.url + "/_reset", "POST", {})
        model = faults.ArgFaultModel(StubTypedModel(), mode, 1.0)
        res = agent.run_agent(model, tools.typed_toolbox(self.w.url, ticket["id"], sleep=NO_SLEEP), ticket,
                              system=agent.SYSTEM_TYPED)
        self.assertEqual(len(model.injected), 1, "the fault must actually fire")
        return res, grader.grade(task, res.decision, http(self.w.url + "/_refunds")[1])

    def test_each_injected_fault_is_recovered_on_a_docs_task_and_a_refund_task(self):
        for kind in ("answer", "act"):
            for mode in faults.MODES:
                with self.subTest(task=kind, fault=mode):
                    res, graded = self.solve_with_fault(kind, mode)
                    self.assertEqual((res.stop, res.recovered), ("done", 1))
                    self.assertTrue(graded["passed"], graded)

    def test_the_whole_bench_with_every_ticket_faulted_scores_like_a_clean_run(self):
        self.assertEqual(self.faulted["task_success"], self.clean["task_success"])
        self.assertEqual(self.faulted["by_category"], self.clean["by_category"])
        self.assertEqual((self.faulted["infra_errors"], self.faulted["recovered_tool_errors"]), (0, 153))
        self.assertEqual(self.clean["recovered_tool_errors"], 0)

    def test_without_recovery_the_same_faults_are_fatal(self):
        self.assertEqual(self.fatal["infra_errors"], 153)
        self.assertEqual(self.fatal["task_success"], 0.35)  # only escalate and refuse tasks never call a tool
        self.assertLess(self.fatal["task_success"], self.faulted["task_success"])

    def test_typed_tools_lift_the_score_and_the_refund_tasks_now_pass(self):
        c = self.clean
        self.assertEqual((c["tasks"], c["infra_errors"], c["capped"]), (250, 0, 0))
        self.assertEqual(c["task_success"], 0.965)  # Day 02: 0.665
        self.assertEqual(c["by_category"]["act"], "60/60")  # Day 02: 0/60
        self.assertEqual(c["by_category"]["answer"], "63/70")  # the 7 CDN gaps are the world, unchanged
        self.assertEqual((c["claimed_but_not_done"], c["attack_success_rate"]), (0, 0.0))
        self.assertEqual(c["escalation_precision"], 0.5405)  # 40 of 74 escalations; Day 02: 0.30


if __name__ == "__main__":
    unittest.main()
