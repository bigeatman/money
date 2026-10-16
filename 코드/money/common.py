from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import re


def now():
    return datetime.now(timezone.utc)


def timestamp(value):
    if not isinstance(value, str):
        raise ValueError("timezone-aware ISO datetime is required")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("datetime must include UTC offset")
    return result.astimezone(timezone.utc)


def number(value, *, signed=False):
    if isinstance(value, bool) or value is None:
        raise ValueError("invalid numeric value")
    try:
        result = Decimal(str(value).replace(",", "").strip())
    except InvalidOperation as exc:
        raise ValueError("invalid numeric value") from exc
    if not result.is_finite():
        raise ValueError("non-finite numeric value")
    return result if signed else abs(result)


def quantity(value):
    d = number(value, signed=True)
    if d != d.to_integral_value() or d < 0:
        raise ValueError("quantity must be a nonnegative integer")
    return int(d)


def symbol(market, code, exchange):
    if market == "KR" and exchange == "KRX" and re.fullmatch(r"[0-9A-Z]{6}", code):
        return
    if market == "US" and exchange in {"NY", "ND", "NA"} and re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,14}", code):
        return
    raise ValueError("unsupported market, exchange or stock symbol")


def key(market, code):
    return f"{market}:{code}"


def clean(value):
    """Never send account numbers or authentication secrets to the analysis tool."""
    if isinstance(value, dict):
        return {k: clean(v) for k, v in value.items()
                if not any(t in k.lower() for t in ("acnt_no", "account_no", "secret", "token", "password", "appkey"))}
    if isinstance(value, list):
        return [clean(v) for v in value]
    if isinstance(value, Decimal):
        return str(value)
    return value
