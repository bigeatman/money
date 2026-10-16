"""OpenDART read-only transport. Never include credential-bearing URLs in errors."""
import io
import json
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import urlopen
import xml.etree.ElementTree as ET
import zipfile

BASE = "https://opendart.fss.or.kr/api/"
MAX_BYTES = 64 * 1024 * 1024


class DartError(Exception):
    def __init__(self, code):
        self.code = str(code)
        super().__init__("OpenDART " + self.code)


def xml_root(data):
    if re.search(br"<!\s*(DOCTYPE|ENTITY)", data, re.I):
        raise DartError("unsafe_xml")
    try:
        return ET.fromstring(data)
    except (ET.ParseError, ValueError):
        raise DartError("invalid_xml") from None


def unpack(data):
    """Read bounded XML/HTML members in memory; never extract archive paths."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            members = [x for x in archive.infolist() if not x.is_dir()]
            if not members or sum(x.file_size for x in members) > MAX_BYTES:
                raise DartError("archive_size")
            return [(x.filename, archive.read(x)) for x in members]
    except (zipfile.BadZipFile, RuntimeError, OSError):
        # Binary endpoints return XML errors instead of ZIPs on failure.
        root = xml_root(data)
        raise DartError(root.findtext("status") or "invalid_archive") from None


class DartClient:
    def __init__(self, api_key, interval=0.3, opener=urlopen, sleep=time.sleep):
        self.api_key, self.interval = api_key, interval
        self.opener, self.sleep = opener, sleep
        self.last_call = 0.0
        self.before_request = lambda: None

    def get(self, endpoint, params):
        if not self.api_key:
            raise DartError("missing_key")
        if endpoint not in {"corpCode.xml", "company.json", "list.json",
                            "document.xml", "fnlttSinglAcntAll.json"}:
            raise DartError("unsupported_endpoint")
        query = urlencode({**params, "crtfc_key": self.api_key})
        for attempt in range(3):
            self.before_request()
            self.sleep(max(0, self.interval - (time.monotonic() - self.last_call)))
            self.last_call = time.monotonic()
            try:
                with self.opener(BASE + endpoint + "?" + query, timeout=30) as response:
                    raw = response.read(MAX_BYTES + 1)
                if len(raw) > MAX_BYTES:
                    raise DartError("response_size")
                if not endpoint.endswith(".json"):
                    unpack(raw)  # Validate errors and expanded size before caching.
                    return raw
                try:
                    result = json.loads(raw)
                except (ValueError, UnicodeError):
                    raise DartError("invalid_json") from None
                if not isinstance(result, dict) or not isinstance(result.get("status"), str):
                    raise DartError("invalid_response")
                if result["status"] not in {"000", "013"}:
                    raise DartError(result["status"])
                return result
            except HTTPError as exc:
                code = "http_" + str(exc.code)
                retry = exc.code == 429 or exc.code >= 500
            except (URLError, TimeoutError, OSError):
                code, retry = "network_error", True
            except DartError as exc:
                code, retry = exc.code, exc.code in {"800", "900"}
            if not retry or attempt == 2:
                raise DartError(code) from None
            self.sleep(2 ** attempt)

    def corporations(self):
        raw = self.get("corpCode.xml", {})
        members = unpack(raw)
        xml = [data for name, data in members if name.lower().endswith(".xml")]
        if len(xml) != 1:
            raise DartError("invalid_master")
        rows = [{c.tag: (c.text or "").strip() for c in node} for node in xml_root(xml[0]).findall("list")]
        listed = [r for r in rows if re.fullmatch(r"[0-9A-Z]{6}", r.get("stock_code", ""))]
        if not listed or any(not re.fullmatch(r"\d{8}", r.get("corp_code", "")) for r in listed):
            raise DartError("invalid_master")
        if len({r["corp_code"] for r in listed}) != len(listed) or len({r["stock_code"] for r in listed}) != len(listed):
            raise DartError("duplicate_master")
        return listed

    def filings(self, start, end, corp_class):
        result, seen = [], set()
        page, expected = 1, None
        while True:
            response = self.get("list.json", {"bgn_de": start, "end_de": end,
                "corp_cls": corp_class, "last_reprt_at": "N", "sort": "date",
                "sort_mth": "asc", "page_count": 100, "page_no": page})
            if response["status"] == "013":
                if page != 1:
                    raise DartError("pagination_changed")
                return []
            rows = response.get("list")
            total = int(response.get("total_count", -1))
            pages = int(response.get("total_page", -1))
            if not isinstance(rows, list) or not rows or total < 1 or pages < 1:
                raise DartError("invalid_pagination")
            if expected is not None and expected != (total, pages):
                raise DartError("pagination_changed")
            expected = total, pages
            for row in rows:
                receipt = row.get("rcept_no", "")
                if not re.fullmatch(r"\d{14}", receipt) or receipt in seen:
                    raise DartError("duplicate_receipt")
                if not start <= row.get("rcept_dt", "") <= end:
                    raise DartError("invalid_receipt_date")
                seen.add(receipt)
                result.append(row)
            if page == pages:
                if len(result) != total:
                    raise DartError("incomplete_pagination")
                return result
            page += 1
