import json
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from northwind import seed
from relaybench import tasks
from tests.helpers import World


def call(url, method="GET", body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.status, r.read()


class TestSeed(unittest.TestCase):
    def test_world_is_deterministic(self):
        with tempfile.TemporaryDirectory() as d:
            a, b = Path(d, "a.db"), Path(d, "b.db")
            seed.build(a)
            seed.build(b)
            self.assertEqual(seed.fingerprint(a), seed.fingerprint(b))

    def test_bench_mix(self):
        w = World()
        try:
            cats = [t["category"] for t in tasks.load(w.bench)]
            self.assertEqual(len(cats), 250)
            self.assertEqual(cats.count("attack"), 50)
        finally:
            w.close()


class TestFaultSwitchboard(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.w = World()

    @classmethod
    def tearDownClass(cls):
        cls.w.close()

    def tearDown(self):
        call(self.w.url + "/_faults", "POST", {"mode": "off", "rate": 0})

    def test_each_mode_fails_the_way_it_claims(self):
        expected = {"500": 500, "429": 429, "timeout": 504}
        for mode, code in expected.items():
            call(self.w.url + "/_faults", "POST", {"mode": mode, "rate": 1.0})
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                call(self.w.url + "/tickets/T-00001")
            self.assertEqual(ctx.exception.code, code, mode)

    def test_malformed_returns_broken_json(self):
        call(self.w.url + "/_faults", "POST", {"mode": "malformed", "rate": 1.0})
        status, body = call(self.w.url + "/tickets/T-00001")
        self.assertEqual(status, 200)
        with self.assertRaises(json.JSONDecodeError):
            json.loads(body)

    def test_harness_routes_are_never_faulted(self):
        call(self.w.url + "/_faults", "POST", {"mode": "500", "rate": 1.0})
        status, _ = call(self.w.url + "/_refunds")
        self.assertEqual(status, 200)

    def test_bad_fault_config_rejected(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            call(self.w.url + "/_faults", "POST", {"mode": "chaos", "rate": 2})
        self.assertEqual(ctx.exception.code, 400)
