"""Read-only Kiwoom market inventory and batched observations.

Field names and request parameters: Kiwoom-Securities/Kiwoom-REST-API,
953e5dbff123f437ab4d11a78a95191a685eb51f. No third-party client is required.
Ranking responses are only an optional data transport AFTER the full inventory;
they never define or truncate the universe. Missing rows are queried individually.
"""
from datetime import datetime
import json
import re
import time
from zoneinfo import ZoneInfo

from .common import number, symbol

SPEC_COMMIT = "953e5dbff123f437ab4d11a78a95191a685eb51f"
SPEC_URL = "https://github.com/Kiwoom-Securities/Kiwoom-REST-API/tree/" + SPEC_COMMIT
KR_SEGMENTS = ("0", "10", "50", "8", "60", "70", "90")
KR_EXCLUDED = {"8": "ETF", "60": "ETN", "70": "ETN", "90": "ETN"}
US_EXCHANGES = {"NY", "ND", "NA"}


def code_kr(value):
    value = str(value).strip()
    if len(value) == 7 and value.startswith("A"):
        value = value[1:]
    return value


def us_exchange(value):
    value = str(value).strip().upper()
    return {"NYSE": "NY", "NASDAQ": "ND", "AMEX": "NA",
            "1": "NY", "2": "ND", "3": "NA"}.get(value, value)


def nonnegative(value):
    result = number(value, signed=True)
    if result < 0:
        raise ValueError("negative volume")
    return result


class MarketData:
    def __init__(self, broker, clock, check, progress, batch_size=50, max_pages=10000):
        self.broker, self.clock, self.check, self.progress = broker, clock, check, progress
        self.batch_size, self.max_pages = batch_size, max_pages

    def pages(self, market, api, path, body, table):
        cursor, seen = "", set()
        for page in range(self.max_pages):
            self.check()
            data, headers = self.broker.scan_page(market, api, path, body, cursor)
            observed = self.clock().isoformat()
            records = data.get(table)
            if not isinstance(records, list) or any(not isinstance(r, dict) for r in records):
                raise ValueError(api + ": missing or malformed " + table)
            json.dumps(records, allow_nan=False)
            h = {k.lower(): str(v) for k, v in headers.items()}
            continued = h.get("cont-yn", "N").upper()
            if continued not in {"Y", "N", ""}:
                raise ValueError(api + ": unknown continuation flag")
            self.progress(api=api, page=page + 1, received=len(records), observed_at=observed)
            yield records, observed
            if continued != "Y":
                return
            nxt = h.get("next-key", "")
            if not records or not nxt or nxt in seen:
                raise ValueError(api + ": empty page or repeated/missing continuation cursor")
            seen.add(nxt)
            cursor = nxt
            # Release the broker lock between pages so execution can reconcile.
            time.sleep(0)
        raise ValueError(api + ": page limit reached before end-of-data")

    def inventory(self, market):
        records, sources, errors = {}, [], []
        if market == "KR":
            queries = [(segment, "ka10099", "/api/dostk/stkinfo", {"mrkt_tp": segment})
                       for segment in KR_SEGMENTS]
        else:
            queries = [("%", "usa10099", "/api/us/stkinfo", {"stex_tp": "%"})]
        for segment, api, path, body in queries:
            count = 0
            try:
                for rows, observed in self.pages(market, api, path, body, "list"):
                    for raw in rows:
                        count += 1
                        if market == "KR":
                            code, exchange = code_kr(raw["code"]), "KRX"
                            category = str(raw.get("marketCode") or segment)
                            asset = KR_EXCLUDED.get(segment) or KR_EXCLUDED.get(category) or "cash_equity"
                            item = {"symbol": code, "exchange": exchange, "name": str(raw["name"]),
                                    "asset_type": asset, "segment": category, "state": raw.get("state"),
                                    "reference_close": raw.get("lastPrice"), "received_at": observed}
                        else:
                            code, exchange = str(raw["stk_cd"]).strip(), us_exchange(raw["stex_tp"])
                            flag = raw.get("isEtf")
                            if flag not in {"0", "1", 0, 1, "N", "Y", False, True}:
                                raise ValueError("US inventory has no recognized ETF classification")
                            item = {"symbol": code, "exchange": exchange, "name": str(raw["stk_nm"]),
                                    "asset_type": "ETF_ETN" if flag in {"1", 1, "Y", True} else "cash_equity",
                                    "segment": exchange, "state": None, "received_at": observed}
                        k = exchange + ":" + code
                        prior = records.get(k)
                        if prior and (prior["name"] != item["name"] or
                                      prior["asset_type"] != item["asset_type"] and
                                      "cash_equity" not in {prior["asset_type"], item["asset_type"]}):
                            raise ValueError("conflicting duplicate inventory: " + k)
                        if prior and prior["asset_type"] != "cash_equity":
                            item["asset_type"] = prior["asset_type"]
                        records[k] = item
                if not count and segment in {"0", "10", "50", "%"}:
                    raise ValueError("empty required inventory segment " + segment)
                sources.append({"api": api, "segment": segment, "rows": count, "complete": True})
            except Exception as exc:
                sources.append({"api": api, "segment": segment, "rows": count, "complete": False})
                errors.append({"stage": "inventory", "api": api, "segment": segment,
                               "error": type(exc).__name__, "message": str(exc)})
        if market == "US" and not US_EXCHANGES <= {i["exchange"] for i in records.values()}:
            errors.append({"stage": "inventory", "message": "NYSE/NASDAQ/AMEX inventory coverage is incomplete"})
        return records, sources, errors

    @staticmethod
    def policy(market, item):
        if item["asset_type"] != "cash_equity":
            return "asset type excluded: " + item["asset_type"]
        if market == "US" and item["exchange"] not in US_EXCHANGES:
            return "exchange is not supported by Kiwoom demo"
        try:
            symbol(market, item["symbol"], item["exchange"])
        except ValueError:
            return "symbol is not supported by the current execution adapter"
        return None

    def kr_observations(self, items):
        result, errors = {}, []
        keys = list(items)
        for start in range(0, len(keys), self.batch_size):
            self.check()
            batch = keys[start:start + self.batch_size]
            requested = {items[k]["symbol"]: k for k in batch}
            try:
                for rows, observed in self.pages("KR", "ka10095", "/api/dostk/stkinfo",
                        {"stk_cd": "|".join(requested)}, "atn_stk_infr"):
                    for raw in rows:
                        code = code_kr(raw["stk_cd"])
                        if code not in requested:
                            raise ValueError("unrequested stock in ka10095 response: " + code)
                        k = requested[code]
                        normalized = self.normalize_kr(raw, observed)
                        if k in result and result[k]["raw"] != raw:
                            raise ValueError("conflicting duplicate quote: " + code)
                        result[k] = normalized
            except Exception as exc:
                errors.append({"stage": "quotes", "api": "ka10095", "symbols": list(requested),
                               "error": type(exc).__name__, "message": str(exc)})
            self.progress(observations=result)
        return result, errors

    def us_observations(self, items):
        # The inventory above is authoritative. Even a ranking API returning only
        # 100 rows cannot silently shrink this set: every absent row is filled below.
        prices, volumes, errors = {}, {}, []
        bulk = [
            ("usa20570", "/api/us/stkinfo", {"stex_tp": "0", "stk_tp": "0", "stk_cnd": "0",
              "inds_cd": "000", "trde_qty_tp": "0", "pric_cnd1": "0", "pric_cnd2": "999999.99",
              "trde_prica_cnd": "0"}, prices),
            ("usa20530", "/api/us/rkinfo", {"stex_tp": "0", "inds_cd": "000", "stk_tp": "0",
              "trde_qty_tp": "0", "qry_tp": "0", "stk_cnd": "0", "pric_cnd": "0",
              "trde_prica_cnd": "0"}, volumes)]
        for api, path, body, target in bulk:
            try:
                for rows, observed in self.pages("US", api, path, body, "result_list"):
                    for raw in rows:
                        k = us_exchange(raw["stex_tp"]) + ":" + str(raw["stk_cd"]).strip()
                        if k in target and target[k][0] != raw:
                            raise ValueError("conflicting duplicate bulk quote: " + k)
                        target[k] = (raw, observed)
            except Exception as exc:
                # A failed/truncated bulk stream is never trusted as complete.
                target.clear()
                errors.append({"stage": "bulk_fallback", "api": api, "error": type(exc).__name__,
                               "message": str(exc)})
        # A provider can cap an all-market ranking. Partition the already-known
        # universe's data transport by EVERY official industry before resorting
        # to thousands of single-symbol reads. Coverage is still checked against
        # inventory; neither rankings nor industry order select candidates.
        missing = lambda: [k for k in items if k not in prices or k not in volumes]
        if len(missing()) > 100:
            try:
                sectors = []
                for rows, _ in self.pages("US", "usa10101", "/api/us/stkinfo", {"gubun": "1"}, "list"):
                    sectors.extend(str(r["inds_cd"]) for r in rows if str(r["inds_cd"]) not in {"", "000"})
                for sector in dict.fromkeys(sectors):
                    if not missing():
                        break
                    self.check()
                    for api, path, body, target in bulk:
                        if all(k in target for k in items):
                            continue
                        additions = {}
                        try:
                            for rows, observed in self.pages("US", api, path, dict(body, stk_tp="1", inds_cd=sector), "result_list"):
                                for raw in rows:
                                    k = us_exchange(raw["stex_tp"]) + ":" + str(raw["stk_cd"]).strip()
                                    if k in items and k not in target:
                                        if k in additions and additions[k][0] != raw:
                                            raise ValueError("conflicting duplicate sector quote: " + k)
                                        additions[k] = (raw, observed)
                            target.update(additions)
                        except Exception as exc:
                            errors.append({"stage": "bulk_fallback", "api": api, "sector": sector,
                                           "error": type(exc).__name__, "message": str(exc)})
                    self.progress(bulk_missing_rows=len(missing()))
            except Exception as exc:
                errors.append({"stage": "bulk_fallback", "api": "usa10101", "error": type(exc).__name__,
                               "message": str(exc)})
        result = {}
        for k, item in items.items():
            self.check()
            try:
                if k in prices and k in volumes:
                    p, at = prices[k]; v, vat = volumes[k]
                    result[k] = self.normalize_us(p, at, v["acc_trde_qty"], vat,
                                                  sources=["usa20570", "usa20530"])
                else:
                    raise ValueError("bulk row missing")
            except (ValueError, KeyError, TypeError):
                try:
                    raw, _ = self.broker.scan_page("US", "usa20100", "/api/us/mrkcond",
                        {"stex_tp": item["exchange"], "stk_cd": item["symbol"]}, "")
                    at = self.clock().isoformat()
                    if str(raw.get("stk_cd", "")).strip() != item["symbol"]:
                        raise ValueError("single quote symbol mismatch")
                    result[k] = self.normalize_us(raw, at, raw["acc_trde_qty"], at,
                                                  sources=["usa20100"])
                except Exception as exc:
                    errors.append({"stage": "quotes", "symbol": item["symbol"], "exchange": item["exchange"],
                                   "api": "usa20100", "error": type(exc).__name__, "message": str(exc)})
            self.progress(observations=result)
        return result, errors

    @staticmethod
    def normalized(raw, observed, volume, sources):
        json.dumps(raw, allow_nan=False)
        price = number(raw["cur_prc"])
        opened, high, low = (number(raw[k]) for k in ("open_pric", "high_pric", "low_pric"))
        change = number(str(raw["flu_rt"]).rstrip("%"), signed=True)
        vol = nonnegative(volume)
        untraded = vol == 0 and opened == high == low == 0
        if price < 0 or (not untraded and not 0 < low <= min(price, opened) <= max(price, opened) <= high):
            raise ValueError("missing or inconsistent price/OHLC")
        if change <= -100:
            raise ValueError("invalid percent change")
        previous = price / (1 + change / 100)
        return {"last": str(price), "open": str(opened), "high": str(high), "low": str(low),
                "previous_close": str(previous), "previous_close_basis": "derived from last and percent change",
                "change_percent": str(change), "volume": str(vol), "no_session_trades": untraded,
                "received_at": observed, "sources": sources, "raw": raw,
                "provider_timestamp": None, "timestamp_basis": "request reception; source timestamp absent"}

    def normalize_kr(self, raw, observed):
        out = self.normalized(raw, observed, raw["trde_qty"], ["ka10095"])
        date, clock = str(raw.get("dt", "")), str(raw.get("bid_tm") or raw.get("cntr_tm") or "")
        if re.fullmatch(r"\d{8}", date) and re.fullmatch(r"\d{6}", clock):
            out["provider_timestamp"] = datetime.strptime(date + clock, "%Y%m%d%H%M%S").replace(
                tzinfo=ZoneInfo("Asia/Seoul")).isoformat()
            out["timestamp_basis"] = "broker dt + cntr_tm/bid_tm, Asia/Seoul"
        return out

    def normalize_us(self, raw, observed, volume, volume_at, sources):
        out = self.normalized(raw, observed, volume, sources)
        out["volume_received_at"] = volume_at
        out["trading_suspended"] = (str(raw["trd_susp_tp"]) not in {"0", "", "N"}
                                    if "trd_susp_tp" in raw else None)
        return out
