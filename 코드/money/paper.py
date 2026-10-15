from decimal import Decimal
from .common import key, now


class PaperBroker:
    """Offline deterministic test broker; distinct from the actual Kiwoom demo service."""

    mode = "offline-paper"

    def __init__(self, clock=now):
        self.clock = clock
        self.orders = {}
        self.quotes = {}
        self.next_id = 1

    def set_quote(self, market, code, exchange, bid, ask, fx=1):
        self.quotes[key(market, code)] = {"market": market, "symbol": code, "exchange": exchange,
            "bid": str(bid), "ask": str(ask), "last": str(ask), "fx": str(fx),
            "fetched_at": self.clock().isoformat(), "source": "offline fixture", "raw": {}}

    def quote(self, market, code, exchange):
        q = dict(self.quotes[key(market, code)])
        q["fetched_at"] = self.clock().isoformat()
        return q

    def account(self, market):
        return {"available_cash": "1000000000", "fx": "1" if market == "KR" else "1350",
                "positions": {}, "raw": {}, "mode": self.mode}

    def history(self, market, date):
        return {oid: dict(o) for oid, o in self.orders.items() if o["market"] == market}

    def place(self, market, code, exchange, side, qty, price=None):
        oid = f"p{self.next_id}"
        self.next_id += 1
        self.orders[oid] = {"market": market, "symbol": code, "side": side, "qty": qty,
                            "filled": 0, "amount": "0", "remaining": qty, "status": "open"}
        return oid

    def fill(self, oid, qty, price):
        o = self.orders[oid]
        if qty > o["remaining"]:
            raise ValueError("fill exceeds remaining")
        o["filled"] += qty
        o["remaining"] -= qty
        o["amount"] = str(Decimal(o["amount"]) + Decimal(str(price)) * qty)
        if not o["remaining"]:
            o["status"] = "filled"

    def cancel(self, market, code, exchange, oid):
        self.orders[oid]["remaining"] = 0
        self.orders[oid]["status"] = "cancelled"

    def chart(self, market, code, exchange):
        return {"source": "offline fixture", "bars": []}
