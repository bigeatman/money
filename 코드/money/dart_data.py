"""Deterministic data differences and conservative financial inputs, no LLM."""
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
import json
import re

from .dart_client import DartError, unpack


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


class Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden += 1
        if tag in {"p", "div", "tr", "br", "title", "section", "table"}:
            self.parts.append("\n")
        elif tag in {"td", "th"}:
            self.parts.append("\t")

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)
        if tag in {"p", "div", "tr", "title", "section", "table"}:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def document_text(raw):
    result = []
    for name, body in sorted(unpack(raw), key=lambda member: member[0]):
        if not name.lower().endswith((".xml", ".html", ".htm", ".xbrl", ".xsd")):
            continue
        decoded = None
        for encoding in ("utf-8-sig", "cp949"):
            try:
                decoded = body.decode(encoding)
                break
            except UnicodeError:
                pass
        if decoded is None:
            raise DartError("document_encoding")
        parser = Text()
        parser.feed(decoded)
        lines = [re.sub(r"[ \r\f\v]+", " ", x).strip() for x in "".join(parser.parts).splitlines()]
        result.append(name + "\n" + "\n".join(x for x in lines if x))
    if not result:
        raise DartError("document_text_missing")
    return "\n\n".join(result)


def row_key(row):
    return "|".join(str(row.get(k, "")) for k in
                    ("sj_div", "account_id", "account_nm", "account_detail", "currency"))


def changes(old, new):
    """Keep all differences in SQLite; the reader pages the JSON evidence."""
    if isinstance(new, dict) and isinstance(new.get("list"), list):
        def keyed(value):
            out = {}
            for row in (value or {}).get("list", []):
                key = row_key(row)
                # Preserve repeated custom account rows instead of overwriting.
                while key in out:
                    key += "|duplicate"
                out[key] = row
            return out
        left, right = keyed(old), keyed(new)
    elif isinstance(new, dict):
        left, right = old or {}, new
    else:
        return {"kind": "document", "new_text_chars": len(document_text(new))}
    result = []
    for key in sorted(left.keys() | right.keys()):
        a, b = left.get(key), right.get(key)
        if a == b:
            continue
        if isinstance(a, dict) and isinstance(b, dict):
            fields = {k: {"before": a.get(k), "after": b.get(k)}
                      for k in sorted(a.keys() | b.keys()) if a.get(k) != b.get(k)}
            result.append({"field": key, "changes": fields})
        else:
            result.append({"field": key, "before": a, "after": b})
    return {"kind": "fields", "count": len(result), "fields": result}


def amount(value):
    if value is None or not str(value).strip() or str(value).strip() == "-":
        return None
    text = str(value).strip().replace(",", "")
    if text.startswith("(") and text.endswith(")"):
        text = "-" + text[1:-1]
    try:
        number = Decimal(text)
        return number if number.is_finite() else None
    except InvalidOperation:
        return None


def metrics(payload):
    """Only unambiguous standard XBRL IDs; missing/ambiguous never becomes 0."""
    rows = payload.get("list", [])
    inputs = {}
    definitions = {
        "revenue": ({"ifrs-full_Revenue"}, {"IS", "CIS"}),
        "operating_profit": ({"dart_OperatingIncomeLoss"}, {"IS", "CIS"}),
        "liabilities": ({"ifrs-full_Liabilities"}, {"BS"}),
        "equity": ({"ifrs-full_Equity"}, {"BS"}),
        "current_assets": ({"ifrs-full_CurrentAssets"}, {"BS"}),
        "current_liabilities": ({"ifrs-full_CurrentLiabilities"}, {"BS"}),
    }
    for label, (ids, statements) in definitions.items():
        candidates = [r for r in rows if r.get("account_id") in ids and r.get("sj_div") in statements]
        if any(r.get("sj_div") == "IS" for r in candidates):
            candidates = [r for r in candidates if r.get("sj_div") == "IS"]
        if len(candidates) != 1:
            continue
        row = candidates[0]
        value = amount(row.get("thstrm_amount"))
        if value is not None and row.get("currency"):
            inputs[label] = {"value": str(value), "currency": row["currency"],
                "account_id": row["account_id"], "statement": row["sj_div"],
                "amount_field": "thstrm_amount", "period_label": row.get("thstrm_nm"),
                "receipt": row.get("rcept_no"), "report_code": row.get("reprt_code")}
    values = {}
    for name, numerator, denominator in [("operating_margin_pct", "operating_profit", "revenue"),
        ("liabilities_to_equity_pct", "liabilities", "equity"),
        ("current_ratio_pct", "current_assets", "current_liabilities")]:
        a, b = inputs.get(numerator), inputs.get(denominator)
        if a and b and a["currency"] == b["currency"] and Decimal(b["value"]) > 0:
            values[name] = str((Decimal(a["value"]) / Decimal(b["value"]) * 100).quantize(Decimal("0.0001")))
    return {"inputs": inputs, "ratios": values,
            "missing_inputs": sorted(definitions.keys() - inputs.keys()),
            "income_scope": "thstrm_amount: annual or 3-month IS/CIS; cumulative fields retained in source"}
