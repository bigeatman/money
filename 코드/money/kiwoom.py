"""Independent stdlib client for the published Kiwoom REST interface.

Only the official MOCK endpoint is reachable. No real-trading switch exists.
API field mappings were checked against the official API repository at
953e5dbff123f437ab4d11a78a95191a685eb51f (2026-10-14).
"""
from datetime import datetime, timedelta
from decimal import Decimal
import hashlib
import json
import os
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from .common import clean, number, quantity, now

MOCK_URL = "https://mockapi.kiwoom.com"


def order_key(value):
    text = str(value)
    return text.lstrip("0") or "0" if text.isdigit() else text


def rows(data, name="result_list", aliases=()):
    for k in (name, *aliases):
        if k in data and isinstance(data[k], list):
            return data[k]
    raise ValueError(f"broker response is missing the {name} array")


class Rejected(Exception):
    """A definitive broker rejection, distinct from an ambiguous transport failure."""


class KiwoomBroker:
    mode = "kiwoom-demo"

    def __init__(self):
        self.tokens = {}
        self.last_request = 0.0
        self.lock = threading.RLock()
        self.us_history_verified = None

    def binding(self):
        return {m: hashlib.sha256(os.environ[f"KIWOOM_{m}_APPKEY"].encode()).hexdigest()
                for m in ("KR", "US") if os.environ.get(f"KIWOOM_{m}_APPKEY")}

    def _failure(self, api, data):
        """Preserve the broker diagnostic message without exposing credentials."""
        message = str(data.get("return_msg") or "broker supplied no return_msg")
        sensitive = [v for k, v in os.environ.items()
                     if k.startswith("KIWOOM_") and ("APPKEY" in k or "SECRET" in k)]
        sensitive.extend(record[0] for record in self.tokens.values())
        for value in sorted(set(sensitive), key=len, reverse=True):
            if value:
                message = message.replace(value, "[redacted]")
        return f"broker rejected {api}: code {data.get('return_code', 'missing')}; {message[:1000]}"

    def _http(self, path, payload, headers):
        # A single serialized stream also respects demo per-TR 1 request/sec limits.
        wait = 1.05 - (time.monotonic() - self.last_request)
        if wait > 0:
            time.sleep(wait)
        self.last_request = time.monotonic()
        req = Request(MOCK_URL + path, json.dumps(payload).encode(), headers,
                      method="POST")
        timeout = 20 if headers.get("api-id") in {"kt10000", "kt10001", "kt10002", "kt10003", "ust20000", "ust20001", "ust20002", "ust20003"} else 12
        try:
            with urlopen(req, timeout=timeout) as res:
                data = json.load(res)
                return data, dict(res.headers)
        except TimeoutError as exc:
            api = headers.get("api-id", path)
            raise TimeoutError(f"broker response timed out ({timeout}s socket timeout): {api}") from exc

    def _request(self, path, body, headers, *, mutation=False):
        """Retry reads once after a transient transport failure, never orders."""
        for attempt in range(1 if mutation else 2):
            try:
                return self._http(path, body, headers)
            except (TimeoutError, URLError, ConnectionError) as exc:
                transient = not isinstance(exc, HTTPError) or exc.code in {429, 500, 502, 503, 504}
                if mutation or not transient or attempt:
                    raise
                time.sleep(2)

    def _token(self, market):
        record = self.tokens.get(market)
        if record and record[1] > now() + timedelta(minutes=10):
            return record[0]
        app = os.environ.get(f"KIWOOM_{market}_APPKEY", "")
        secret = os.environ.get(f"KIWOOM_{market}_SECRET", "")
        if not app or not secret:
            raise ValueError(f"{market} demo credentials are not configured")
        data, _ = self._request("/oauth2/token", {"grant_type": "client_credentials",
            "appkey": app, "secretkey": secret}, {"Content-Type": "application/json;charset=UTF-8"})
        if str(data.get("return_code", "0")) != "0" or not data.get("token"):
            raise Rejected(self._failure("oauth2/token", data))
        expiry = datetime.strptime(data["expires_dt"], "%Y%m%d%H%M%S").replace(tzinfo=ZoneInfo("Asia/Seoul"))
        self.tokens[market] = (data["token"], expiry)
        return data["token"]

    def call(self, market, api, path, body, *, mutation=False, pages=False):
        with self.lock:
            result = None
            cursor = ""
            for page in range(100 if pages else 1):
                headers = {"Content-Type": "application/json;charset=UTF-8", "api-id": api,
                           "authorization": "Bearer " + self._token(market)}
                if cursor:
                    headers.update({"cont-yn": "Y", "next-key": cursor})
                try:
                    data, response_headers = self._request(path, body, headers, mutation=mutation)
                except HTTPError as exc:
                    # For a mutating request even an HTTP failure may be ambiguous.
                    if mutation:
                        raise URLError("order outcome is unknown") from exc
                    raise ValueError(f"broker HTTP {exc.code}") from exc
                code = str(data.get("return_code", "missing"))
                invalid_token = code == "8005" or (code == "3" and "8005" in str(data.get("return_msg", "")))
                if invalid_token:
                    self.tokens.pop(market, None)
                    if not mutation:
                        headers["authorization"] = "Bearer " + self._token(market)
                        data, response_headers = self._request(path, body, headers)
                        code = str(data.get("return_code", "missing"))
                if code != "0":
                    if code == "missing":
                        raise ValueError("broker response has no return_code")
                    raise Rejected(self._failure(api, data))
                if result is None:
                    result = data
                else:
                    for k, v in data.items():
                        if isinstance(v, list):
                            result.setdefault(k, []).extend(v)
                h = {k.lower(): v for k, v in response_headers.items()}
                if h.get("cont-yn") != "Y" or not pages:
                    return result
                nxt = h.get("next-key", "")
                if not nxt or nxt == cursor:
                    raise ValueError("invalid continuation cursor")
                cursor = nxt
            raise ValueError("too many broker pages; refusing an incomplete snapshot")

    def place(self, market, code, exchange, side, qty, price=None):
        body = {"stk_cd": code, "ord_qty": str(qty), "ord_uv": "" if price is None else str(price)}
        if market == "KR":
            body.update(dmst_stex_tp="KRX", trde_tp="3" if price is None else "0")
            api, path = ("kt10000" if side == "BUY" else "kt10001"), "/api/dostk/ordr"
        else:
            body.update(stex_tp=exchange, trde_tp="03" if price is None else "00")
            api, path = ("ust20000" if side == "BUY" else "ust20001"), "/api/us/ordr"
        data = self.call(market, api, path, body, mutation=True)
        if not data.get("ord_no"):
            raise ValueError("order response has no order number; outcome unknown")
        return str(data["ord_no"])

    def cancel(self, market, code, exchange, oid):
        body = {"stk_cd": code, "orig_ord_no": oid}
        if market == "KR":
            body.update(dmst_stex_tp="KRX", cncl_qty="0")
            api, path = "kt10003", "/api/dostk/ordr"
        else:
            body["stex_tp"] = exchange
            api, path = "ust20003", "/api/us/ordr"
        return self.call(market, api, path, body, mutation=True)

    def account(self, market):
        if market == "KR":
            balance = self.call(market, "kt00018", "/api/dostk/acnt",
                {"qry_tp": "2", "dmst_stex_tp": "KRX"}, pages=True)
            cash = self.call(market, "kt00001", "/api/dostk/acnt", {"qry_tp": "2"}, pages=True)
            positions = {r["stk_cd"].removeprefix("A"): {"qty": quantity(r["rmnd_qty"]),
                "available": quantity(r["trde_able_qty"])} for r in balance.get("acnt_evlt_remn_indv_tot", [])}
            return {"available_cash": str(max(Decimal(0), number(cash["100stk_ord_alow_amt"], signed=True))), "fx": "1",
                "positions": positions, "raw": clean({"balance": balance, "cash": cash}), "mode": self.mode}
        report = self.account_report(market)
        if not report["complete"]:
            detail = "; ".join(e["message"] for e in report["errors"].values())
            raise ValueError("US account incomplete; no new buy: " + detail)
        return report

    def account_report(self, market):
        if market != "US":
            return self.account(market)
        raw, errors, observed = {}, {}, {}
        positions, available_cash, fx = None, None, None
        for name, api, body, pages in (
            ("balance", "ust21070", {}, True),
            ("cash", "ust21110", {}, True),
            ("rates", "ust21120", {"cmsn_incl_tp": "1", "exrt_tp": "1"}, False)):
            try:
                data = self.call(market, api, "/api/us/acnt", body, pages=pages)
                observed[name] = now().isoformat()
                if name == "balance":
                    parsed = {r["stk_cd"]: {"qty": quantity(r["poss_qty"]),
                              "available": quantity(r["sell_alowq"])} for r in rows(data)}
                    positions = parsed
                else:
                    usd = next((r for r in rows(data) if r.get("crnc_code") == "USD"), None)
                    if not usd:
                        raise ValueError("USD row missing: " + api)
                    if name == "cash":
                        available_cash = str(max(Decimal(0), number(usd["fc_ord_alowa"], signed=True)))
                    else:
                        rate = number(usd["crnc_rt"], signed=True)
                        if rate <= 0:
                            raise ValueError("USD account FX rate is not positive")
                        fx = str(rate)
                raw[name] = data
            except Exception as exc:
                errors[name] = {"api": api, "error": type(exc).__name__, "message": str(exc)}
        pnl = {}
        balance = raw.get("balance", {})
        for field in ("tot_pl_amt", "tot_pl_rt", "tdy_book_amt", "tdy_pl_amt", "tdy_pl_rt",
                      "tot_pl_amt_krw", "tdy_book_amt_krw", "tdy_pl_amt_krw"):
            if balance.get(field) not in (None, ""):
                try:
                    pnl[field] = str(number(balance[field], signed=True))
                except ValueError:
                    errors["pnl:" + field] = {"api": "ust21070", "error": "ValueError", "message": "invalid signed P&L field: " + field}
        return {"available_cash": available_cash, "fx": fx, "positions": positions,
                "pnl": {"source_api": "ust21070", "observed_at": observed.get("balance"),
                        "currency": balance.get("crnc_code"), "values": pnl,
                        "scope": "broker account, including pre-existing manual trades; separate from bot strategy",
                        "basis": "reported by broker; fee inclusion not independently verified"},
                "complete": not errors and positions is not None and available_cash is not None and fx is not None,
                "errors": errors, "observed_at": observed, "raw": clean(raw), "mode": self.mode}

    def orderable(self, market, code, exchange, price):
        if market == "KR":
            return None  # Account cash was queried at 100% cash margin above.
        data = self.call(market, "ust31490", "/api/us/ordr",
                         {"stex_tp": exchange, "stk_cd": code, "uv": str(price)})
        return quantity(data["min_ord_alowq"])  # Explicit no-credit buying quantity.

    def quote(self, market, code, exchange):
        if market == "KR":
            info = self.call(market, "ka10001", "/api/dostk/stkinfo", {"stk_cd": code})
            book = self.call(market, "ka10004", "/api/dostk/mrkcond", {"stk_cd": code})
            bid, ask, fx = book["buy_fpr_bid"], book["sel_fpr_bid"], "1"
        else:
            info = self.call(market, "usa20100", "/api/us/mrkcond", {"stex_tp": exchange, "stk_cd": code})
            book = self.call(market, "usa20101", "/api/us/mrkcond", {"stex_tp": exchange, "stk_cd": code})
            bid, ask, fx = book["fpr_buy_bid"], book["fpr_sel_bid"], info["base_exrt"]
            if str(info.get("trd_susp_tp", "0")) not in {"0", "", "N"}:
                raise ValueError("stock reports a trading suspension")
        result = {"market": market, "symbol": code, "exchange": exchange,
            "last": str(number(info["cur_prc"])), "bid": str(number(bid)), "ask": str(number(ask)),
            "fx": str(number(fx)), "fetched_at": now().isoformat(), "source": self.mode,
            "raw": clean({"quote": info, "order_book": book})}
        if market == "KR":
            result.update(reference_price=str(number(info.get("base_pric", "0") or "0")),
                          upper_limit=str(number(info.get("upl_pric", "0") or "0")),
                          lower_limit=str(number(info.get("lst_pric", "0") or "0")))
        return result

    def chart(self, market, code, exchange):
        if market == "KR":
            data = self.call(market, "ka10080", "/api/dostk/chart",
                {"stk_cd": code, "tic_scope": "5", "upd_stkpc_tp": "1"})
            bars = data.get("stk_min_pole_chart_qry", [])
        else:
            data = self.call(market, "usa06011", "/api/us/chart",
                {"stex_tp": exchange, "stk_cd": code, "strt_dt": now().astimezone(ZoneInfo("America/New_York")).strftime("%Y%m%d"),
                 "tic_scope": "5", "upd_stkpc_tp": "1", "exrt_appl_tp": "0"})
            bars = data.get("result_list", [])
        return {"source": self.mode, "interval_minutes": 5, "fetched_at": now().isoformat(),
                "bars": clean(bars), "complete_history": False,
                "note": "API first page; raw broker timestamp/timezone semantics retained"}

    def history(self, market, date):
        if market == "US":
            today = now().astimezone(ZoneInfo("America/New_York")).strftime("%Y%m%d")
            if date != today:
                raise ValueError("US mock fill query is today-only; previous session needs explicit reconciliation")
            self.us_history_verified = None
            data = self.call(market, "ust21510", "/api/us/acnt", {"slby_tp": "0"}, pages=True)
            result = {}
            for r in rows(data, aliases=("result_lsit",)):
                oid = order_key(r["ord_no"])
                out = self._us_order_row(r, "ust21510")
                if oid in result:
                    previous = result[oid]
                    if any(previous[k] != out[k] for k in ("symbol", "qty", "side", "filled", "remaining", "status")) or Decimal(previous["amount"]) != Decimal(out["amount"]):
                        raise ValueError("conflicting duplicate US order rows")
                result[oid] = out
            pending = self.call(market, "ust21050", "/api/us/acnt", {"ord_dt": "", "slby_tp": "0"}, pages=True)
            for r in rows(pending, aliases=("result_lsit",)):
                oid = order_key(r["ord_no"])
                out = self._us_order_row(r, "ust21050")
                previous = result.get(oid)
                if previous:
                    if any(previous[k] != out[k] for k in ("symbol", "qty", "side")):
                        raise ValueError("US pending order fingerprint differs from confirmed order")
                    if out["filled"] < previous["filled"]:
                        continue  # A lagging pending response cannot erase a confirmed fill.
                    if out["filled"] == previous["filled"] and Decimal(out["amount"]) != Decimal(previous["amount"]):
                        raise ValueError("US fill amount changed without a new fill")
                result[oid] = out
            self.us_history_verified = {"trade_date": date, "as_of": now().isoformat(), "observed_orders": len(result)}
            return result
        data = self.call(market, "kt00009", "/api/dostk/acnt",
            {"ord_dt": date, "stk_bond_tp": "1", "mrkt_tp": "0", "sell_tp": "0",
             "qry_tp": "0", "dmst_stex_tp": "KRX"}, pages=True)
        today = now().astimezone(ZoneInfo("Asia/Seoul")).strftime("%Y%m%d")
        if date > today:
            raise ValueError("cannot reconcile a future KR trade date")
        # Pending orders belong to today. Never merge a reused order number
        # from today's book into another trading day's execution history.
        pending = self.call(market, "ka10075", "/api/dostk/acnt",
            {"all_stk_tp": "0", "trde_tp": "0", "stex_tp": "1"}, pages=True) if date == today else {"oso": []}
        result, seen = {}, set()
        for r in rows(data, "acnt_ord_cntr_prst_array"):
            oid, fid = order_key(r["ord_no"]), str(r.get("cntr_no", ""))
            out = result.setdefault(oid, {"filled": 0, "amount": "0", "remaining": 0, "status": "closed"})
            out.update(symbol=r["stk_cd"].removeprefix("A"), qty=quantity(r["ord_qty"]),
                side="BUY" if "매수" in r["io_tp_nm"] else "SELL" if "매도" in r["io_tp_nm"] else "UNKNOWN")
            q = quantity(r.get("cntr_qty", "0") or "0")
            if q:
                if not fid or not fid.strip("0"):
                    raise ValueError("fill has no stable execution ID")
                if (oid, fid) not in seen:
                    seen.add((oid, fid))
                    out["filled"] += q
                    out["amount"] = str(Decimal(out["amount"]) + number(r["cntr_uv"]) * q)
        for r in rows(pending, "oso"):
            oid = order_key(r["ord_no"])
            out = result.setdefault(oid, {"filled": 0, "amount": "0"})
            out.update(remaining=quantity(r["oso_qty"]), status="open")
            out.update(symbol=r["stk_cd"].removeprefix("A"), qty=quantity(r["ord_qty"]),
                side="BUY" if "매수" in r["io_tp_nm"] else "SELL" if "매도" in r["io_tp_nm"] else "UNKNOWN")
        if date < today:
            for out in result.values():
                # Only an order actually present in dated broker history can
                # expire. A missing order is never assumed filled or cancelled.
                if out["filled"] < out["qty"]:
                    out["status"] = "expired"
        return result

    @staticmethod
    def _us_order_row(r, source_api):
        qty, filled, remaining = quantity(r["ord_qty"]), quantity(r["cntr_qty"]), quantity(r["ord_remnq"])
        if filled + remaining > qty:
            raise ValueError("US broker quantities exceed the submitted order")
        code, name = str(r.get("slby_tp", "")), str(r.get("slby_tp_nm", ""))
        side = {"1": "SELL", "2": "BUY"}.get(code)
        named = "BUY" if "매수" in name else "SELL" if "매도" in name else None
        if side and named and side != named:
            raise ValueError("US broker side code and name disagree")
        side = side or named
        if not side:
            raise ValueError("US broker order side is missing")
        price = number(r["cntr_uv"], signed=True) if filled else Decimal(0)
        if filled and price <= 0:
            raise ValueError("US confirmed fill has no positive reported price")
        amount = number(r["cntr_amt"], signed=True) if filled and r.get("cntr_amt") not in (None, "") else price * filled
        if amount < 0:
            raise ValueError("negative US fill amount")
        state = str(r.get("ord_stat", r.get("ord_stat_nm", "")))
        status = "open" if remaining else "closed" if qty and filled == qty else "cancelled" if "취소" in state else "unconfirmed"
        return {"filled": filled, "remaining": remaining, "amount": str(amount),
                "symbol": r["stk_cd"], "qty": qty, "side": side, "status": status,
                "reported_fill_price": str(price), "source_api": source_api,
                "amount_basis": "broker cumulative amount" if r.get("cntr_amt") not in (None, "") else "reported fill price times filled quantity; rounding possible"}

    def execution_capabilities(self):
        return {"adapter_version": "us-demo-v3", "us_history_api": "ust21510", "us_pending_api": "ust21050",
                "us_history_verified": self.us_history_verified, "us_history_today_only": True,
                "notional_basis": "reported fill price times quantity unless cumulative amount is supplied; fee estimates separate"}
