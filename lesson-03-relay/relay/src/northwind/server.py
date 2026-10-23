"""Northwind Cloud simulator: a tiny JSON API over the seeded SQLite world.

It also hosts the fault switchboard. Flip a fault on and every business
endpoint starts failing the way real APIs fail: slow, rate-limited, broken,
or returning half-written JSON.
"""
from __future__ import annotations

import argparse
import json
import random
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# "lost" is a write fault: the refund is committed, then the response is replaced by a 500.
FAULT_MODES = {"off", "timeout", "429", "500", "malformed", "lost"}


def ensure_schema(db_path: Path) -> None:
    """Add the idempotency column to worlds seeded before Day 03. Safe to run twice."""
    con = sqlite3.connect(db_path)
    cols = {row[1] for row in con.execute("PRAGMA table_info(refunds)")}
    if "idem_key" not in cols:
        con.execute("ALTER TABLE refunds ADD COLUMN idem_key TEXT")
    con.execute("CREATE UNIQUE INDEX IF NOT EXISTS refunds_idem ON refunds(idem_key)")
    con.commit()
    con.close()


class Faults:
    """Shared fault state. A seeded RNG keeps fault patterns reproducible."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.mode = "off"
        self.rate = 0.0
        self.rng = random.Random(7)

    def set(self, mode: str, rate: float) -> None:
        if mode not in FAULT_MODES or not 0.0 <= rate <= 1.0:
            raise ValueError("bad fault config")
        with self.lock:
            self.mode, self.rate, self.rng = mode, rate, random.Random(7)

    def draw(self) -> str:
        with self.lock:
            if self.mode != "off" and self.rng.random() < self.rate:
                return self.mode
            return "off"


def make_handler(db_path: Path, faults: Faults, timeout_s: float):
    class Handler(BaseHTTPRequestHandler):
        _lost = False  # set per request by the "lost" fault

        def log_message(self, fmt, *args):  # keep test output quiet
            pass

        # ---------- helpers ----------
        def _db(self) -> sqlite3.Connection:
            con = sqlite3.connect(db_path)
            con.row_factory = sqlite3.Row
            return con

        def _send(self, code: int, payload: object, raw: bytes | None = None) -> None:
            body = raw if raw is not None else json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _fault_gate(self) -> bool:
            """Return True if a fault was injected and the response is already sent."""
            mode = faults.draw()
            if mode == "off":
                return False
            if mode == "timeout":
                time.sleep(timeout_s)
                self._send(504, {"error": "gateway timeout"})
            elif mode == "429":
                self.send_response(429)
                self.send_header("Retry-After", "1")
                self.send_header("Content-Length", "0")
                self.end_headers()
            elif mode == "500":
                self._send(500, {"error": "internal error"})
            elif mode == "malformed":
                self._send(200, None, raw=b'{"id": "T-0', )
            elif mode == "lost":
                self._lost = True  # let the request run; the refund handler drops the response
                return False
            return True

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length", 0))
            return json.loads(self.rfile.read(n) or b"{}")

        def _one(self, sql: str, arg: str):
            with self._db() as con:
                row = con.execute(sql, (arg,)).fetchone()
            if row is None:
                self._send(404, {"error": "not found"})
            else:
                self._send(200, dict(row))

        # ---------- routes ----------
        def do_GET(self) -> None:
            url = urlparse(self.path)
            parts = url.path.strip("/").split("/")
            if url.path == "/health":
                return self._send(200, {"ok": True})
            if url.path == "/_faults":
                return self._send(200, {"mode": faults.mode, "rate": faults.rate})
            if url.path == "/_refunds":  # harness-only view: never faulted
                with self._db() as con:
                    rows = [dict(r) for r in con.execute("SELECT * FROM refunds ORDER BY id")]
                return self._send(200, rows)
            if self._fault_gate():
                return
            if len(parts) == 2 and parts[0] == "tickets":
                return self._one("SELECT id, customer_id, subject, body FROM tickets WHERE id=?", parts[1])
            if len(parts) == 2 and parts[0] == "customers":
                return self._one("SELECT * FROM customers WHERE id=?", parts[1])
            if len(parts) == 2 and parts[0] == "invoices":
                return self._one("SELECT * FROM invoices WHERE id=?", parts[1])
            if parts == ["docs", "search"]:
                q = parse_qs(url.query).get("q", [""])[0].lower()
                words = [w for w in q.split() if len(w) > 3]
                with self._db() as con:
                    rows = con.execute("SELECT id, title, body FROM docs").fetchall()
                scored = sorted(((sum(w in (r["title"] + r["body"]).lower() for w in words), r["id"], dict(r))
                                 for r in rows), key=lambda t: (-t[0], t[1]))
                return self._send(200, [d for s, _, d in scored[:5] if s > 0])
            if len(parts) == 2 and parts[0] == "docs":
                return self._one("SELECT id, title, body FROM docs WHERE id=?", parts[1])
            if parts == ["refunds"]:
                with self._db() as con:
                    rows = [dict(r) for r in con.execute("SELECT * FROM refunds ORDER BY id")]
                return self._send(200, rows)
            self._send(404, {"error": "no route"})

        def do_POST(self) -> None:
            path = urlparse(self.path).path
            try:
                data = self._body()
            except json.JSONDecodeError:
                return self._send(400, {"error": "bad json"})
            if path == "/_faults":
                try:
                    faults.set(data.get("mode", "off"), float(data.get("rate", 0.0)))
                except (ValueError, TypeError) as exc:
                    return self._send(400, {"error": str(exc)})
                return self._send(200, {"mode": faults.mode, "rate": faults.rate})
            if path == "/_reset":
                with self._db() as con:
                    con.execute("DELETE FROM refunds")
                    con.execute("DELETE FROM sqlite_sequence WHERE name='refunds'")  # ids restart at 1
                return self._send(200, {"ok": True})
            if self._fault_gate():
                return
            if path == "/refunds":
                inv, amount = data.get("invoice_id"), data.get("amount_cents")
                if not isinstance(inv, str) or not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
                    return self._send(422, {"error": "invoice_id (str) and amount_cents (int > 0) required"})
                key = self.headers.get("Idempotency-Key")
                replayed = False
                with self._db() as con:
                    if con.execute("SELECT 1 FROM invoices WHERE id=?", (inv,)).fetchone() is None:
                        return self._send(404, {"error": "unknown invoice"})
                    prior = (con.execute("SELECT id, invoice_id, amount_cents FROM refunds WHERE idem_key=?",
                                         (key,)).fetchone() if key else None)
                    if prior is not None and (prior["invoice_id"], prior["amount_cents"]) != (inv, amount):
                        return self._send(409, {"error": "idempotency key reused with different arguments"})
                    if prior is not None:
                        refund_id, replayed = prior["id"], True
                    else:
                        refund_id = con.execute(
                            "INSERT INTO refunds(invoice_id, amount_cents, created_by, idem_key) VALUES (?,?,?,?)",
                            (inv, amount, data.get("actor", "unknown"), key)).lastrowid
                if self._lost:  # the write is committed above; the caller never hears about it
                    return self._send(500, {"error": "internal error"})
                body = {"refund_id": refund_id, **({"replayed": True} if replayed else {})}
                return self._send(200 if replayed else 201, body)
            self._send(404, {"error": "no route"})

    return Handler


def serve(db_path: Path, port: int, timeout_s: float = 2.0) -> ThreadingHTTPServer:
    ensure_schema(db_path)
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(db_path, Faults(), timeout_s))
    return server


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/northwind.db")
    ap.add_argument("--port", type=int, default=8701)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()
    ensure_schema(Path(args.db))
    srv = ThreadingHTTPServer((args.host, args.port), make_handler(Path(args.db), Faults(), 2.0))
    print(f"northwind simulator on http://{args.host}:{args.port}", flush=True)
    srv.serve_forever()
