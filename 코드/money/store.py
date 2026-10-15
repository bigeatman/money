import json
import os
from pathlib import Path
import sqlite3


class Store:
    """Single daemon, durable ledger. An order intent is persisted BEFORE transmission."""

    def __init__(self, path, capital):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        os.chmod(path, 0o600)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY, body TEXT NOT NULL)")
        row = self.db.execute("SELECT body FROM state WHERE id=1").fetchone()
        if row:
            self.state = json.loads(row[0])
            if self.state["capital"] != str(capital):
                raise ValueError("capital change requires an explicit ledger migration")
        else:
            self.state = {"capital": str(capital), "cash": str(capital), "sessions": {},
                          "plans": {}, "commands": {}, "orders": {}, "positions": {},
                          "watchlist": {}, "events": [], "paused": False}
            self.save()
        # A crash during an order request is ambiguous. Never resend it automatically.
        for order in self.state["orders"].values():
            if order["status"] == "sending":
                order["status"] = "unknown"
        self.save()

    def save(self):
        body = json.dumps(self.state, ensure_ascii=False, allow_nan=False)
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO state VALUES (1, ?)", (body,))

    def close(self):
        self.db.close()
