from datetime import timedelta
from decimal import Decimal, ROUND_CEILING
import hashlib
import json
import threading
import uuid
from zoneinfo import ZoneInfo

from .common import key, now, number, quantity, symbol, timestamp
from .kiwoom import Rejected, order_key

ACTIVE = {"sending", "unknown", "open", "cancel_requested"}


class Engine:
    def __init__(self, store, broker, config, clock=now, scanner=None, fundamentals=None):
        self.store, self.broker, self.config, self.clock = store, broker, config, clock
        self.s = store.state
        self.lock = threading.RLock()
        self.accounts = {}
        self.scanner = scanner
        self.fundamentals = fundamentals
        self.s.setdefault("targets", {})
        binding = {"mode": broker.mode, "accounts": broker.binding() if hasattr(broker, "binding") else {}}
        old_binding = self.s.get("binding")
        if old_binding and old_binding != binding and (self.s["orders"] or self.s["commands"]):
            raise ValueError("broker mode/account changed: use a separate runtime ledger")
        self.s["binding"] = binding
        if self.s.get("holding_policy_version", 0) < 5:
            for p in self.s["positions"].values():
                if not p.get("liquidating") or p.get("liquidation_source"):
                    continue
                purchases = [o["submitted_at"] for o in self.s["orders"].values()
                    if o["market"] == p["market"] and o["symbol"] == p["symbol"]
                    and o["side"] == "BUY" and o["filled"]]
                last_purchase = max((timestamp(at) for at in purchases), default=None)
                explicit = False
                for c in self.s["commands"].values():
                    if (c["market"] == p["market"] and c["symbol"] == p["symbol"]
                            and c["action"] == "SELL" and c["state"] == "done"):
                        plan = self.s["plans"].get(c["plan_id"], {}).get("plan", {})
                        generated = plan.get("generated_at")
                        if generated and last_purchase and timestamp(generated) >= last_purchase:
                            explicit = True
                if explicit:
                    p["liquidation_source"] = "explicit_SELL"
                else:
                    # v4 also set this flag merely for the closing clock or
                    # rollover. Those legacy automatic exit intents are removed.
                    p["liquidating"] = False
                    self.event("legacy_time_exit_removed", market=p["market"], symbol=p["symbol"])
            self.s["holding_policy_version"] = 5
        self.store.save()

    def event(self, kind, **fields):
        self.s["events"].append({"at": self.clock().isoformat(), "type": kind, **fields})
        self.s["events"] = self.s["events"][-1000:]

    def fees(self, market):
        f = self.config["fees"][market]
        result = tuple(number(f[k], signed=True) for k in ("buy", "sell", "sell_tax"))
        if any(v < 0 or v >= 1 for v in result):
            raise ValueError("invalid fee rate")
        return result

    def submit(self, plan):
        with self.lock:
            digest = hashlib.sha256(json.dumps(plan, sort_keys=True, ensure_ascii=False,
                                    allow_nan=False).encode()).hexdigest()
            pid = plan["plan_id"]
            if not isinstance(pid, str) or not 1 <= len(pid) <= 100:
                raise ValueError("invalid plan_id")
            previous = self.s["plans"].get(pid)
            if previous:
                if previous["digest"] != digest:
                    raise ValueError("same plan_id with different contents")
                return {"plan_id": pid, "accepted": True, "duplicate": True}
            market = plan["market"]
            if market not in {"KR", "US"}:
                raise ValueError("invalid market")
            generated, expires = timestamp(plan["generated_at"]), timestamp(plan["expires_at"])
            t = self.clock()
            if not t - timedelta(minutes=60) <= generated <= t + timedelta(seconds=60):
                raise ValueError("plan generation time is stale or in the future")
            if not t < expires <= generated + timedelta(minutes=60):
                raise ValueError("plan must expire within 60 minutes of generation")
            session = plan["session"]
            opening, closing = timestamp(session["opens_at"]), timestamp(session["closes_at"])
            timezone = ZoneInfo("Asia/Seoul" if market == "KR" else "America/New_York")
            if opening.astimezone(timezone).date() != t.astimezone(timezone).date():
                raise ValueError("session must belong to the current local market date")
            if opening.astimezone(timezone).date() != closing.astimezone(timezone).date():
                raise ValueError("regular session crosses a local calendar date")
            if opening.weekday() >= 5 or not timedelta(hours=1) <= closing - opening <= timedelta(hours=10):
                raise ValueError("invalid regular-session window")
            if not session.get("calendar_sources") or not all(isinstance(u, str) and u.startswith("https://")
                                                    for u in session["calendar_sources"]):
                raise ValueError("verified exchange calendar sources are required")
            if expires > closing:
                raise ValueError("plan may not outlive the regular session")
            old = self.s["sessions"].get(market)
            new_session = bool(old and timestamp(old["opens_at"]) != opening)
            cutoff = closing - timedelta(minutes=self.config["close_buffer_minutes"][market])
            actions = plan["actions"]
            if not isinstance(actions, list) or len(actions) > 30:
                raise ValueError("actions must be an array of at most 30 items")
            buy_symbols, checked = set(), []
            for i, action in enumerate(actions):
                a = dict(action)
                symbol(market, a["symbol"], a["exchange"])
                if a["action"] not in {"BUY", "SELL", "REPRICE", "HOLD", "CANCEL"}:
                    raise ValueError("unsupported action")
                if not isinstance(a.get("reason"), str) or not a["reason"].strip():
                    raise ValueError("a decision reason is required")
                preopen = a.get("preopen", False)
                order_type = a.get("order_type", "LIMIT")
                if not isinstance(preopen, bool) or order_type not in {"LIMIT", "MARKET"}:
                    raise ValueError("invalid preopen/order_type")
                if (preopen or order_type != "LIMIT") and a["action"] != "BUY":
                    raise ValueError("preopen/order_type applies only to BUY")
                if preopen and (market != "KR" or a["exchange"] != "KRX"):
                    raise ValueError("preopen buys support KRX only")
                if order_type == "MARKET" and not preopen:
                    raise ValueError("market buys require explicit preopen=true")
                if preopen and not opening - timedelta(minutes=30) <= t < opening:
                    raise ValueError("preopen buys require the 30-minute opening auction")
                if a["action"] == "BUY":
                    if a["symbol"] in buy_symbols:
                        raise ValueError("duplicate BUY for a symbol in the same plan")
                    buy_symbols.add(a["symbol"])
                    a["quantity"] = quantity(a["quantity"])
                    if not a["quantity"]:
                        raise ValueError("BUY quantity must be positive")
                    limit, target = number(a["buy_limit"], signed=True), number(a["target"], signed=True)
                    fb, fs, tax = self.fees(market)
                    if limit <= 0 or target <= 0:
                        raise ValueError("buy_limit and target must be positive")
                    if order_type == "LIMIT" and target * (1 - fs - tax) <= limit * (1 + fb):
                        raise ValueError("target does not cover estimated round-trip costs")
                    self.check_tick(market, limit)
                    self.check_tick(market, target)
                    if t >= cutoff:
                        raise ValueError("new buys are forbidden after the entry cutoff")
                elif a["action"] == "REPRICE":
                    if number(a["target"], signed=True) <= 0:
                        raise ValueError("target must be positive")
                    self.check_tick(market, number(a["target"]))
                elif a["action"] == "SELL":
                    a["quantity"] = quantity(a.get("quantity", 0))  # 0 means ALL managed shares.
                elif a["action"] == "CANCEL":
                    if a.get("order_ref") not in self.s["orders"]:
                        raise ValueError("CANCEL may reference only a bot-owned order")
                    o = self.s["orders"][a["order_ref"]]
                    if o["market"] != market or o["symbol"] != a["symbol"]:
                        raise ValueError("CANCEL order belongs to another instrument")
                a.update(market=market, plan_id=pid, expires_at=expires.isoformat(),
                         state="pending", command_id=f"{pid}:{i}")
                if preopen:
                    # Never turn a missed opening-auction instruction into a late buy.
                    a["expires_at"] = min(expires, opening).isoformat()
                checked.append(a)
            if len(buy_symbols) > self.config["max_positions"]:
                raise ValueError("too many BUY instruments")
            if self.scanner is not None:
                for a in checked:
                    if a["action"] == "BUY":
                        a["market_scan_id"] = self.scanner.require_complete(market, a["symbol"], a["exchange"])
            self.s["sessions"][market] = dict(session, cutoff=cutoff.isoformat())
            if new_session:
                # Holdings and explicit exit intents survive a session change.
                # Daily price limits do not: the new session needs a fresh quote.
                for p in self.s["positions"].values():
                    if p["market"] == market:
                        p.pop("auction_upper_limit", None)
                        p.pop("upper_limit_session", None)
                        p.pop("profit_order_blocked", None)
                self.s["watchlist"] = {k: v for k, v in self.s["watchlist"].items() if v["market"] != market}
            self.s["plans"][pid] = {"digest": digest, "plan": plan}
            # Superseded pending buys must not run after a newer review changes its mind.
            for c in self.s["commands"].values():
                if c["market"] == market and c["action"] == "BUY" and c["state"] not in {"done", "expired", "rejected"}:
                    c["state"] = "superseded"
            for a in checked:
                self.s["commands"][a["command_id"]] = a
                self.s["watchlist"][key(market, a["symbol"])] = {"market": market, "symbol": a["symbol"], "exchange": a["exchange"]}
            self.event("plan_accepted", plan_id=pid, market=market, decisions=len(checked))
            self.store.save()
            return {"plan_id": pid, "accepted": True, "duplicate": False, "execution": "queued; inspect execution status"}

    @staticmethod
    def tick_size(market, price):
        if market == "US":
            return Decimal("0.01") if price >= 1 else Decimal("0.0001")
        # Common KRX equity tick table. ETFs/ETNs remain excluded.
        return next(Decimal(v) for ceiling, v in [(2000, 1), (5000, 5), (20000, 10),
            (50000, 50), (200000, 100), (500000, 500), (float("inf"), 1000)] if price < ceiling)

    @classmethod
    def check_tick(cls, market, price):
        if price % cls.tick_size(market, price):
            raise ValueError("price does not match the exchange price increment")

    def command_ready(self, c, opening, closing, t):
        if c.get("preopen", False):
            return c["action"] == "BUY" and c["market"] == "KR" and opening - timedelta(minutes=30) <= t < opening
        if c["action"] == "CANCEL" and c["market"] == "KR" and opening - timedelta(minutes=30) <= t < opening:
            return True
        return opening <= t < closing

    def active_orders(self, market=None, code=None, side=None):
        return [(r, o) for r, o in self.s["orders"].items() if o["status"] in ACTIVE
                and (market is None or o["market"] == market) and (code is None or o["symbol"] == code)
                and (side is None or o["side"] == side)]

    def unknown(self):
        return any(o["status"] == "unknown" for o in self.s["orders"].values())

    def _place(self, market, code, exchange, side, qty, price, fx, target=None,
               preopen=False, reservation_price=None):
        if self.unknown():
            return None
        session = self.s["sessions"][market]
        t = self.clock()
        opening, closing = timestamp(session["opens_at"]), timestamp(session["closes_at"])
        if preopen:
            if not (market == "KR" and exchange == "KRX" and side == "BUY"
                    and opening - timedelta(minutes=30) <= t < opening):
                return None
        elif not opening <= t < closing:
            return None
        if side == "BUY" and t >= timestamp(session["cutoff"]):
            return None
        if side == "BUY" and price is None and (reservation_price is None or reservation_price <= 0):
            raise ValueError("a market buy needs an exchange price ceiling reservation")
        ref = uuid.uuid4().hex
        dt = timestamp(session["opens_at"]).astimezone(ZoneInfo("Asia/Seoul" if market == "KR" else "America/New_York"))
        order = {"market": market, "symbol": code, "exchange": exchange, "side": side,
            "qty": qty, "price": None if price is None else str(price), "fx": str(fx),
            "target": target, "filled": 0, "amount": "0", "remaining": qty, "status": "sending",
            "broker_id": None, "date": dt.strftime("%Y%m%d"), "terminal_observations": 0,
            "submitted_at": self.clock().isoformat()}
        if preopen:
            order.update(preopen=True, order_type="MARKET" if price is None else "LIMIT")
        if reservation_price is not None:
            order["reservation_price"] = str(reservation_price)
        self.s["orders"][ref] = order
        self.store.save()
        try:
            order["broker_id"] = self.broker.place(market, code, exchange, side, qty, price)
            order["status"] = "open"
            self.event("order_accepted", order_ref=ref, side=side, market=market, symbol=code, quantity=qty)
        except Rejected as exc:
            order.update(status="rejected", remaining=0)
            self.event("order_rejected", order_ref=ref, error=str(exc))
        except Exception as exc:
            order["status"] = "unknown"
            self.event("order_outcome_unknown", order_ref=ref, error=type(exc).__name__)
        self.store.save()
        return ref

    def _cancel(self, ref, order):
        if order["status"] not in {"open"} or not order["remaining"]:
            return
        order["status"] = "cancel_requested"
        self.store.save()
        try:
            self.broker.cancel(order["market"], order["symbol"], order["exchange"], order["broker_id"])
            self.event("cancel_requested", order_ref=ref)
        except Rejected:
            # A fill can race a cancel. Read history before deciding what remains.
            order["status"] = "open"
        except Exception as exc:
            order["status"] = "unknown"
            self.event("cancel_outcome_unknown", order_ref=ref, error=type(exc).__name__)
        self.store.save()

    def _reconcile(self, market):
        current_date = self.clock().astimezone(ZoneInfo("Asia/Seoul" if market == "KR" else "America/New_York")).strftime("%Y%m%d")
        relevant = [o for o in self.s["orders"].values() if o["market"] == market and o["broker_id"]
                    and (o["status"] in ACTIVE or o["date"] == current_date)]
        for date in sorted({o["date"] for o in relevant}):
            history = self.broker.history(market, date)
            for order in relevant:
                oid = order_key(order["broker_id"])
                if order["date"] != date or oid not in history:
                    continue
                h = history[oid]
                if h.get("symbol", order["symbol"]) != order["symbol"] or h.get("side", order["side"]) != order["side"] or h.get("qty", order["qty"]) != order["qty"]:
                    raise ValueError("broker order fingerprint differs from saved intent")
                order["fill_source_api"] = h.get("source_api", "kt00009")
                order["fill_amount_basis"] = h.get("amount_basis", "broker execution price times execution quantity")
                if "reported_fill_price" in h:
                    order["reported_fill_price"] = h["reported_fill_price"]
                filled, remaining, amount = quantity(h["filled"]), quantity(h["remaining"]), number(h["amount"])
                if filled < order["filled"] or filled > order["qty"] or remaining + filled > order["qty"]:
                    raise ValueError("inconsistent broker fill quantities")
                delta, delta_amount = filled - order["filled"], amount - Decimal(order["amount"])
                if delta_amount < 0 or (delta and delta_amount <= 0) or (not delta and delta_amount != 0):
                    raise ValueError("inconsistent cumulative fill amount")
                k = key(market, order["symbol"])
                if delta:
                    p = self.s["positions"].setdefault(k, {"market": market, "symbol": order["symbol"],
                        "exchange": order["exchange"], "qty": 0, "basis_krw": "0",
                        "target": order["target"], "fx": order["fx"], "liquidating": False})
                    fb, fs, tax = self.fees(market)
                    fx = Decimal(order["fx"])
                    if order["side"] == "BUY":
                        cost = delta_amount * fx * (1 + fb)
                        p["qty"] += delta
                        p["basis_krw"] = str(Decimal(p["basis_krw"]) + cost)
                        self.s["cash"] = str(Decimal(self.s["cash"]) - cost)
                        if order.get("order_type") == "MARKET":
                            p["auction_upper_limit"] = order["reservation_price"]
                            # The auction price is unknown at submission. An opening gap
                            # must not turn the initial profit order into an immediate loss.
                            net_cost = Decimal(p["basis_krw"]) / p["qty"] / fx / (1 - fs - tax)
                            tick = self.tick_size(market, net_cost)
                            floor = (net_cost / tick).to_integral_value(rounding=ROUND_CEILING) * tick
                            if floor <= net_cost:
                                floor += self.tick_size(market, floor)
                            current = Decimal(self.s["targets"].get(k, p["target"]))
                            if current < floor:
                                self.s["targets"][k] = p["target"] = str(floor)
                                self.event("market_target_cost_adjustment", market=market,
                                           symbol=p["symbol"], previous=str(current), target=str(floor))
                    else:
                        if delta > p["qty"]:
                            raise ValueError("sell fill exceeds bot-owned quantity")
                        p["basis_krw"] = str(Decimal(p["basis_krw"]) * Decimal(p["qty"] - delta) / p["qty"])
                        p["qty"] -= delta
                        self.s["cash"] = str(Decimal(self.s["cash"]) + delta_amount * fx * (1 - fs - tax))
                    self.event("fill", side=order["side"], market=market, symbol=order["symbol"],
                               quantity=delta, amount=str(delta_amount))
                # Disappearing from a pending-order query alone is not proof of a fill/cancel.
                terminal_allowed = (filled == order["qty"] or order["status"] in {"cancel_requested", "closed"}
                                    or h.get("status") == "cancelled"
                                    or (date < current_date and market == "KR" and h.get("status") == "expired"))
                if not remaining and not terminal_allowed:
                    remaining = order["qty"] - filled
                order.update(filled=filled, amount=str(amount), remaining=remaining)
                if remaining:
                    order["terminal_observations"] = 0
                    if order["status"] not in {"cancel_requested", "unknown"}:
                        order["status"] = "open"
                else:
                    order["terminal_observations"] += 1
                    if filled == order["qty"] or order["terminal_observations"] >= 2:
                        order["status"] = "closed"
        self.store.save()

    def resolve_unknown(self, order_ref, broker_id):
        """Explicit operator recovery after checking the broker's accepted order ID."""
        with self.lock:
            order = self.s["orders"][order_ref]
            if order["status"] != "unknown":
                raise ValueError("order does not have an ambiguous outcome")
            h = self.broker.history(order["market"], order["date"]).get(order_key(broker_id))
            if not h or h.get("symbol") != order["symbol"] or h.get("side") != order["side"] or h.get("qty") != order["qty"]:
                raise ValueError("broker order fingerprint does not match the persisted intent")
            if any(o is not order and o["market"] == order["market"] and o["date"] == order["date"]
                   and o["broker_id"] and order_key(o["broker_id"]) == order_key(broker_id) for o in self.s["orders"].values()):
                raise ValueError("broker order already belongs to another intent")
            order.update(broker_id=str(broker_id), status="open")
            self._reconcile(order["market"])
            self.event("ambiguous_order_resolved", order_ref=order_ref)
            self.store.save()
            return {"resolved": True, "order_ref": order_ref}

    def _reserved(self):
        total = Decimal(0)
        for _, o in self.active_orders(side="BUY"):
            price = number(o.get("reservation_price") or o["price"], signed=True)
            if price <= 0:
                raise ValueError("active buy has no positive cash reservation price")
            # Keep reserving cash until the terminal observation is confirmed.
            remaining = o["remaining"] or (o["qty"] - o["filled"])
            total += price * remaining * Decimal(o["fx"]) * (1 + self.fees(o["market"])[0])
        return total

    def available(self):
        exposure = sum((Decimal(p["basis_krw"]) for p in self.s["positions"].values()), Decimal(0))
        return max(Decimal(0), min(Decimal(self.s["cash"]), Decimal(self.s["capital"]) - exposure) - self._reserved())

    def _buy(self, c):
        market, code, ex = c["market"], c["symbol"], c["exchange"]
        k = key(market, code)
        if self.s["paused"] or self.unknown():
            c["state"] = "blocked"
            return
        if self.scanner is not None:
            try:
                c["market_scan_id"] = self.scanner.require_complete(market, code, ex)
                c.pop("scan_block_reason", None)
            except ValueError as exc:
                c.update(state="waiting_market_scan", scan_block_reason=str(exc))
                return
        if market == "US" and hasattr(self.broker, "execution_capabilities"):
            current_date = self.clock().astimezone(ZoneInfo("America/New_York")).strftime("%Y%m%d")
            verified = self.broker.execution_capabilities().get("us_history_verified")
            if not verified or verified["trade_date"] != current_date:
                self.broker.history(market, current_date)
        p = self.s["positions"].get(k)
        if p and p["liquidating"] and p["qty"]:
            c["state"] = "waiting_liquidation"
            return
        occupied = {k for k, p in self.s["positions"].items() if p["qty"]}
        occupied.update(key(o["market"], o["symbol"]) for _, o in self.active_orders(side="BUY"))
        if k not in occupied and len(occupied) >= self.config["max_positions"]:
            c["state"] = "waiting_slot"
            return
        quote = self.broker.quote(market, code, ex)
        limit = number(c["buy_limit"])
        age = self.clock() - timestamp(quote["fetched_at"])
        if not timedelta(0) <= age <= timedelta(seconds=30):
            c["state"] = "waiting_quote"
            return
        preopen = c.get("preopen", False)
        market_order = c.get("order_type", "LIMIT") == "MARKET"
        reservation_price, order_price = limit, limit
        if preopen:
            # Zero top-of-book prices are normal during the opening auction.
            # Use freshly fetched daily price limits, never zero or old live quotes.
            reference = number(quote.get("reference_price", "0"))
            upper = number(quote.get("upper_limit", "0"))
            lower = number(quote.get("lower_limit", "0"))
            if not 0 < lower <= reference <= upper or upper == lower:
                c["state"] = "waiting_reference"
                return
            if market_order:
                if upper > limit:
                    c.update(state="rejected", rejection_reason="market price ceiling exceeds buy_limit")
                    self.event("decision_rejected", command_id=c["command_id"], reason=c["rejection_reason"])
                    return
                fb, fs, tax = self.fees(market)
                if number(c["target"]) * (1 - fs - tax) <= reference * (1 + fb):
                    c.update(state="rejected", rejection_reason="target does not cover costs at auction reference price")
                    self.event("decision_rejected", command_id=c["command_id"], reason=c["rejection_reason"])
                    return
                reservation_price, order_price = upper, None
            elif not lower <= limit <= upper:
                c.update(state="rejected", rejection_reason="limit price is outside today's exchange price limits")
                return
        else:
            ask, bid = number(quote["ask"]), number(quote["bid"])
            if not 0 < bid <= ask:
                c["state"] = "waiting_quote"
                return
            if ask > limit:
                c["state"] = "waiting_price"
                return
        account = self.accounts[market]
        fx = number(account["fx"], signed=True)
        if fx <= 0:
            raise ValueError("account FX rate must be positive")
        required = reservation_price * c["quantity"] * fx * (1 + self.fees(market)[0])
        if required > self.available() or required / fx > number(account["available_cash"], signed=True):
            c["state"] = "waiting_cash"
            return
        if hasattr(self.broker, "orderable"):
            q = self.broker.orderable(market, code, ex, reservation_price, buy_fee=self.fees(market)[0])
            if q is not None and c["quantity"] > q:
                c["state"] = "waiting_broker_cash"
                return
        # Network/data retrieval can cross expiry or the closing cutoff.
        session = self.s["sessions"][market]
        current = self.clock()
        if (current >= timestamp(c["expires_at"]) or current >= timestamp(session["cutoff"])
                or not self.command_ready(c, timestamp(session["opens_at"]), timestamp(session["closes_at"]), current)):
            c["state"] = "expired"
            return
        if not timedelta(0) <= current - timestamp(quote["fetched_at"]) <= timedelta(seconds=30):
            c["state"] = "waiting_quote"
            return
        if self.scanner is not None:
            try:
                c["market_scan_id"] = self.scanner.require_complete(market, code, ex)
            except ValueError as exc:
                c.update(state="waiting_market_scan", scan_block_reason=str(exc))
                return
        target = str(number(c["target"]))
        self.s["targets"][k] = target
        if p:
            p.update(target=target, liquidating=False)
            p.pop("liquidation_source", None)
        ref = self._place(market, code, ex, "BUY", c["quantity"], order_price, fx, target,
                          preopen=preopen, reservation_price=reservation_price if market_order else None)
        c.update(state="done" if ref else "blocked", order_ref=ref)
        # Do not count the broker's same cash twice in another purchase this tick.
        if ref and self.s["orders"][ref]["status"] != "rejected":
            account["available_cash"] = str(number(account["available_cash"], signed=True) - required / fx)

    def _protect(self, k, p):
        target = self.s["targets"].get(k, p["target"])
        p["target"] = target
        orders = self.active_orders(p["market"], p["symbol"], "SELL")
        if any(not o["remaining"] for _, o in orders):
            # A first terminal observation is not yet a confirmed release.
            return
        wrong = [(r, o) for r, o in orders if o["price"] != target]
        if wrong:
            for r, o in wrong:
                self._cancel(r, o)
            return
        session_id = self.s["sessions"][p["market"]]["opens_at"]
        if (p["market"] == "KR" and self.broker.mode == "kiwoom-demo"
                and p.get("upper_limit_session") != session_id):
            q = self.broker.quote("KR", p["symbol"], p["exchange"])
            age = self.clock() - timestamp(q["fetched_at"])
            upper = number(q.get("upper_limit", "0"))
            if not timedelta(0) <= age <= timedelta(seconds=30) or upper <= 0:
                raise ValueError("fresh daily upper limit required for a carried KR target order")
            p.update(auction_upper_limit=str(upper), upper_limit_session=session_id)
        if p.get("auction_upper_limit") and Decimal(target) > Decimal(p["auction_upper_limit"]):
            # A gap to the upper limit can make an above-cost exit impossible
            # today. Keep the shares until the target becomes valid or an
            # explicit exit is requested; the clock never forces a sale.
            if p.get("profit_order_blocked") != "target_above_daily_upper_limit":
                self.event("profit_order_blocked", market=p["market"], symbol=p["symbol"],
                           reason="target_above_daily_upper_limit", target=target)
            p["profit_order_blocked"] = "target_above_daily_upper_limit"
            return
        p.pop("profit_order_blocked", None)
        reserved = sum(o["remaining"] for _, o in orders)
        free = p["qty"] - reserved
        if free > 0:
            account_pos = self.accounts[p["market"]]["positions"].get(p["symbol"])
            if self.broker.mode == "kiwoom-demo":
                free = min(free, account_pos["available"] if account_pos else 0)
            if free > 0:
                self._place(p["market"], p["symbol"], p["exchange"], "SELL", free,
                            Decimal(target), Decimal(p["fx"]))

    def _liquidate(self, market, code):
        p = self.s["positions"].get(key(market, code))
        if p:
            p["liquidating"] = True
            p["liquidation_source"] = "explicit_SELL"
        # Cancel outstanding buys too: they must not refill a closing position.
        orders = self.active_orders(market, code)
        for ref, o in orders:
            if o["side"] == "BUY" or o["price"] is not None:
                self._cancel(ref, o)
        if self.active_orders(market, code) or not p or not p["qty"]:
            return
        free = p["qty"]
        if self.broker.mode == "kiwoom-demo":
            bp = self.accounts[market]["positions"].get(code)
            free = min(free, bp["available"] if bp else 0)
        if free:
            self._place(market, code, p["exchange"], "SELL", free, None, Decimal(p["fx"]))

    def tick(self):
        with self.lock:
            t = self.clock()
            for market, session in list(self.s["sessions"].items()):
                opening, closing = timestamp(session["opens_at"]), timestamp(session["closes_at"])
                # Reconcile beyond closing as well, so a closing fill is recorded.
                if t < opening - timedelta(minutes=30) or t > closing + timedelta(hours=12):
                    continue
                try:
                    self._reconcile(market)
                    pending = []
                    for c in self.s["commands"].values():
                        if c["market"] != market or c["state"] in {"done", "expired", "rejected", "superseded"}:
                            continue
                        if t >= timestamp(c["expires_at"]):
                            c["state"] = "expired"
                        elif self.command_ready(c, opening, closing, t):
                            pending.append(c)
                    owned = any(p["market"] == market and p["qty"] for p in self.s["positions"].values())
                    # Empty sessions need no repeated account queries. Snapshots
                    # still read the broker, and fills are reconciled above.
                    if not pending and not owned and not self.active_orders(market):
                        continue
                    if market == "US" and hasattr(self.broker, "account_report"):
                        self.accounts[market] = self.broker.account_report(market)
                        for detail in self.accounts[market]["errors"].values():
                            self.event("broker_sync_error", market=market, **detail)
                        if self.accounts[market]["positions"] is None:
                            raise ValueError("US holdings unavailable; cannot verify available shares")
                    else:
                        self.accounts[market] = self.broker.account(market)
                    if self.broker.mode == "kiwoom-demo":
                        for p in self.s["positions"].values():
                            if p["market"] == market and p["qty"]:
                                bp = self.accounts[market]["positions"].get(p["symbol"])
                                if not bp or bp["qty"] < p["qty"]:
                                    raise ValueError("managed shares exceed broker balance; reconcile external trades")
                    for c in self.s["commands"].values():
                        if c["market"] != market or c["state"] in {"done", "expired", "rejected", "superseded"}:
                            continue
                        if t >= timestamp(c["expires_at"]):
                            c["state"] = "expired"
                            continue
                        if not self.command_ready(c, opening, closing, t):
                            continue
                        a, k = c["action"], key(market, c["symbol"])
                        if a == "BUY":
                            if t >= timestamp(session["cutoff"]):
                                c["state"] = "expired"
                            elif self.accounts[market].get("complete") is False:
                                c["state"] = "waiting_account"
                            else:
                                self._buy(c)
                        elif a == "HOLD":
                            c["state"] = "done"
                        elif a == "REPRICE":
                            self.s["targets"][k] = str(number(c["target"]))
                            if k in self.s["positions"]:
                                self.s["positions"][k]["target"] = self.s["targets"][k]
                            c["state"] = "done"
                        elif a == "CANCEL":
                            self._cancel(c["order_ref"], self.s["orders"][c["order_ref"]])
                            c["state"] = "done"
                        elif a == "SELL":
                            # v0.1 intentionally liquidates ALL owned shares; never external account holdings.
                            p = self.s["positions"].get(k)
                            if c["quantity"] and p and c["quantity"] != p["qty"]:
                                c["state"] = "rejected"
                                self.event("decision_rejected", command_id=c["command_id"], reason="SELL supports ALL managed shares")
                            else:
                                self._liquidate(market, c["symbol"])
                                c["state"] = "done"
                    if opening <= t < closing:
                        cutoff = t >= timestamp(session["cutoff"])
                        codes = {p["symbol"] for p in self.s["positions"].values() if p["market"] == market and p["qty"]}
                        codes.update(o["symbol"] for _, o in self.active_orders(market))
                        for code in codes:
                            p = self.s["positions"].get(key(market, code))
                            if cutoff:
                                # Stop late entry fills without selling shares already held.
                                for ref, order in self.active_orders(market, code, "BUY"):
                                    self._cancel(ref, order)
                            if p and p["liquidating"]:
                                self._liquidate(market, code)
                            elif p and p["qty"]:
                                self._protect(key(market, code), p)
                except Exception as exc:
                    self.event("broker_sync_error", market=market, error=type(exc).__name__, message=str(exc))
            self.store.save()

    def snapshot(self, market, instruments=None, include_charts=True):
        if self.scanner is not None:
            try:
                self.scanner.start(market)  # Never waits for the census.
            except (OSError, ValueError):
                pass  # Scan failure cannot prevent account/holding reads.
        with self.lock:
            if market not in {"KR", "US"}:
                raise ValueError("invalid market")
            reconciliation_error = None
            try:
                if market == "US" and hasattr(self.broker, "execution_capabilities"):
                    date = self.clock().astimezone(ZoneInfo("America/New_York")).strftime("%Y%m%d")
                    verified = self.broker.execution_capabilities().get("us_history_verified")
                    if not verified or verified["trade_date"] != date:
                        self.broker.history(market, date)
                self._reconcile(market)
            except Exception as exc:
                reconciliation_error = {"error": type(exc).__name__, "message": str(exc)}
            if market == "US" and hasattr(self.broker, "account_report"):
                account = self.broker.account_report(market)
            else:
                account = self.broker.account(market)
            if account.get("complete") is not False:
                self.accounts[market] = account
            selected = {k: p for k, p in self.s["watchlist"].items() if p["market"] == market} if instruments is None else {}
            selected.update({k: p for k, p in self.s["positions"].items() if p["market"] == market and p["qty"]})
            for item in instruments or []:
                symbol(market, item["symbol"], item["exchange"])
                selected[key(market, item["symbol"])] = dict(item, market=market)
            if len(selected) > 30:
                raise ValueError("snapshot limited to 30 instruments; request candidate batches")
            quotes, errors = {}, {}
            for k, item in selected.items():
                try:
                    q = self.broker.quote(market, item["symbol"], item["exchange"])
                    if include_charts:
                        q["chart"] = self.broker.chart(market, item["symbol"], item["exchange"])
                    quotes[k] = q
                except Exception as exc:
                    errors[k] = {"error": type(exc).__name__, "message": str(exc)}
            return {"as_of": self.clock().isoformat(), "market": market, "mode": self.broker.mode,
                "capital_krw": self.s["capital"], "strategy_cash_krw": self.s["cash"],
                "available_strategy_krw": str(self.available()), "fees": self.config["fees"],
                "session": self.s["sessions"].get(market), "account": account,
                "managed_positions": self.s["positions"], "managed_orders": self.s["orders"],
                "quotes": quotes, "errors": errors,
                "account_errors": account.get("errors", {}), "reconciliation_error": reconciliation_error,
                "account_complete": account.get("complete", True),
                "execution_capabilities": self.execution_capabilities(),
                "paused_new_buys": self.s["paused"] or account.get("complete") is False or reconciliation_error is not None,
                "unknown_order_outcome": self.unknown(),
                "market_scan": self.scanner.report(market, limit=100) if self.scanner is not None else
                               {"market": market, "status": "not_configured", "data_complete": False}}

    def market_scan(self, market, force=False):
        if self.scanner is None:
            raise ValueError("market scanner is not configured")
        return self.scanner.start(market, force)

    def market_scan_report(self, market, scan_id=None, view="all", offset=0, limit=100):
        if self.scanner is None:
            raise ValueError("market scanner is not configured")
        return self.scanner.report(market, scan_id, view, offset, limit)

    def market_context(self, market, instruments, interval=60):
        if not hasattr(self.broker, "market_context"):
            raise ValueError("daily/hourly market context is unavailable for this broker")
        return self.broker.market_context(market, instruments, interval)

    def execution_capabilities(self):
        caps = dict(self.broker.execution_capabilities()) if hasattr(self.broker, "execution_capabilities") else {}
        caps["market_scanner"] = self.scanner.capabilities() if self.scanner is not None else {"supported": False}
        caps["fundamentals"] = (self.fundamentals.capabilities()
                                if self.fundamentals is not None else {"supported": False})
        caps["kr_preopen_buys"] = {"supported": True, "exchange": "KRX", "minutes_before_open": 30,
            "order_types": ["LIMIT", "MARKET"], "opt_in": "preopen=true",
            "market_reservation": "daily upper_limit times quantity including buy fees; buy_limit must cover upper_limit",
            "after_open": "unsent preopen commands expire; accepted orders reconcile normally"}
        caps["holding_policy"] = {"version": 5, "force_close_at_session_end": False,
            "force_close_on_session_rollover": False, "overnight_holdings": True,
            "sell_triggers": ["target_limit_fill", "explicit_SELL"],
            "close_buffer_minutes": "entry cutoff and unfilled BUY cancellation only",
            "next_session": "requires a fresh verified exchange-calendar session plan; HOLD is sufficient",
            "kr_day_orders": "reconcile dated broker history before replacing expired orders",
            "us_prior_unresolved_orders": "blocked until explicit broker reconciliation; today-only API"}
        return caps

    def call_dart_api(self, endpoint, params=None, refresh=False, include_data=False):
        if self.fundamentals is None:
            raise ValueError("OpenDART is not configured")
        return self.fundamentals.call_api(endpoint, params, refresh, include_data)

    def fundamental_report(self, symbol=None, after_seq=None, offset=0, limit=20, through_seq=None):
        if self.fundamentals is None:
            raise ValueError("OpenDART collector is not configured")
        return self.fundamentals.report(symbol, after_seq, offset, limit, through_seq)

    def fundamental_evidence(self, source_key, offset=0, limit=4000):
        if self.fundamentals is None:
            raise ValueError("OpenDART collector is not configured")
        return self.fundamentals.evidence(source_key, offset, limit)

    def diagnostic(self, part, date=None):
        """Independent read-only US queries for an operator; no plans or orders."""
        with self.lock:
            if self.broker.mode != "kiwoom-demo":
                raise ValueError("diagnostics requires kiwoom-demo")
            if part == "kr_preopen":
                return {"as_of": self.clock().isoformat(), "mode": self.broker.mode, "market": "KR",
                    "part": part, "api": "kt00018/kt00001/ka10001/ka10004",
                    "data": self.snapshot("KR", [{"symbol": "010140", "exchange": "KRX"}], False)}
            if part == "buying_power":
                data = self.broker.us_cash_buying_power(self.fees("US")[0])
                api = "ust21050/ust21110"
            elif part == "quote":
                data = self.broker.quote("US", "MSFT", "ND")
                api = "usa20100/usa20101"
            elif part == "orders":
                date = date or self.clock().astimezone(ZoneInfo("America/New_York")).strftime("%Y%m%d")
                data = self.broker.history("US", date)
                api = "ust21510/ust21050"
            else:
                queries = {
                    "balance": ("ust21070", {}, True),
                    "cash": ("ust21110", {}, True),
                    "rates": ("ust21120", {"cmsn_incl_tp": "1", "exrt_tp": "1"}, False),
                    "realized": ("ust21630", {"stex_tp": "", "stk_cd": "", "fc_krw_tp": "0"}, True)}
                if part not in queries:
                    raise ValueError("unsupported diagnostic query")
                api, body, pages = queries[part]
                data = self.broker.call("US", api, "/api/us/acnt", body, pages=pages)
            return {"as_of": self.clock().isoformat(), "mode": self.broker.mode,
                    "market": "US", "part": part, "api": api, "data": data}

    def status(self):
        with self.lock:
            return {"as_of": self.clock().isoformat(), "mode": self.broker.mode,
                "capital_krw": self.s["capital"], "cash_krw": self.s["cash"],
                "available_krw": str(self.available()), "positions": self.s["positions"],
                "orders": self.s["orders"], "commands": self.s["commands"],
                "events": self.s["events"][-50:], "paused": self.s["paused"], "unknown": self.unknown(),
                "execution_capabilities": self.execution_capabilities(),
                "market_scans": {market: self.scanner.report(market, limit=1) for market in ("KR", "US")}
                                if self.scanner is not None else {}}
