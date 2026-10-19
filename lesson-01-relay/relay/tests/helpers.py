"""Spin up a fresh seeded world on a free port for each test class."""
from __future__ import annotations

import tempfile
import threading
from pathlib import Path

from northwind import seed, server
from relaybench import tasks


class World:
    def __init__(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.db = root / "northwind.db"
        self.bench = root / "bench.jsonl"
        self.scoreboard = root / ".scoreboard"
        seed.build(self.db)
        tasks.generate(self.db, self.bench)
        self.srv = server.serve(self.db, port=0, timeout_s=0.05)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.srv.shutdown()
        self.srv.server_close()
        self.tmp.cleanup()
