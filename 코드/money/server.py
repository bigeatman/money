"""Small stateless Streamable HTTP MCP endpoint, JSON-RPC 2.0, no SSE.

The opaque URL capability is for private development connections using Auth=None.
Bearer authentication is also accepted. TLS must be provided by a trusted tunnel
or reverse proxy; the daemon binds only to loopback by default.
"""
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json

from .common import clean

INSTRUMENT = {"type": "object", "properties": {"symbol": {"type": "string"},
    "exchange": {"type": "string", "enum": ["KRX", "NY", "ND", "NA"]}},
    "required": ["symbol", "exchange"], "additionalProperties": False}
ACTION = {"type": "object", "properties": {
    "action": {"type": "string", "enum": ["BUY", "SELL", "REPRICE", "HOLD", "CANCEL"]},
    "symbol": {"type": "string"}, "exchange": {"type": "string", "enum": ["KRX", "NY", "ND", "NA"]},
    "quantity": {"type": "integer", "minimum": 0}, "buy_limit": {"type": "string"},
    "preopen": {"type": "boolean", "description": "BUY only: opt in to the KRX 30-minute opening auction; expires at regular open."},
    "order_type": {"type": "string", "enum": ["LIMIT", "MARKET"],
        "description": "Default LIMIT. MARKET requires preopen=true; buy_limit must cover today's exchange upper_limit for cash reservation, not a broker limit price."},
    "target": {"type": "string"}, "order_ref": {"type": "string"}, "reason": {"type": "string"}},
    "required": ["action", "symbol", "exchange", "reason"], "additionalProperties": False}
PLAN = {"type": "object", "properties": {
    "plan_id": {"type": "string"}, "market": {"type": "string", "enum": ["KR", "US"]},
    "generated_at": {"type": "string"}, "expires_at": {"type": "string"},
    "session": {"type": "object", "properties": {"opens_at": {"type": "string"},
        "closes_at": {"type": "string"}, "calendar_sources": {"type": "array", "items": {"type": "string"}, "minItems": 1}},
        "required": ["opens_at", "closes_at", "calendar_sources"], "additionalProperties": False},
    "actions": {"type": "array", "items": ACTION, "maxItems": 30}},
    "required": ["plan_id", "market", "generated_at", "expires_at", "session", "actions"], "additionalProperties": False}


def tools():
    return [
        {"name": "get_trading_snapshot", "description": "Read current Kiwoom DEMO cash, bot-owned shares, pending orders, order books, daily reference/upper/lower limits and 5-minute chart bars. Always call before a trading decision. External holdings are not bot-owned. Missing/stale data means no new buy; zero top-of-book during the KRX opening auction is expected when fresh daily reference/limits are available.",
         "inputSchema": {"type": "object", "properties": {"market": {"type": "string", "enum": ["KR", "US"]},
             "instruments": {"type": "array", "items": INSTRUMENT, "maxItems": 10},
             "include_charts": {"type": "boolean"}}, "required": ["market"], "additionalProperties": False},
         "annotations": {"readOnlyHint": True, "openWorldHint": True}},
        {"name": "submit_trading_plan", "description": "Queue authorized DEMO-only trading decisions within shared KR+US KRW 1,000,000 including fees. BUY needs quantity,buy_limit,target. Default buys are regular-session LIMIT. For KRX opening-auction buys set preopen=true and order_type=LIMIT or MARKET. MARKET buy_limit must cover the daily upper_limit; reserve that worst-case cost, not last price. A market auction fill can differ from reference; the initial sell target is raised to cover actual fill costs if needed. Unsent preopen instructions expire at open. No end-of-session or next-session forced sale: confirmed holdings may carry overnight. A fresh verified calendar session is required to resume next-day order management; US prior-day unresolved orders require reconciliation. SELL quantity 0 sells ALL bot-owned shares. Check status; accepted is not filled.",
         "inputSchema": {"type": "object", "properties": {"plan": PLAN}, "required": ["plan"], "additionalProperties": False},
         "annotations": {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": True, "idempotentHint": True}},
        {"name": "get_execution_status", "description": "Read submitted/filled/cancelled/ambiguous order outcomes and strategy cash. Use after submitting a plan; never claim a fill before broker confirmation.",
         "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
         "annotations": {"readOnlyHint": True, "openWorldHint": False}},
    ]


def make_server(engine, token, host="127.0.0.1", port=8765, allowed_origins=()):
    if len(token) < 32:
        raise ValueError("bridge token must have at least 32 characters")

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            pass  # URL can contain the bridge capability; never log it.

        def reply(self, code, payload=None):
            data = b"" if payload is None else json.dumps(clean(payload), ensure_ascii=False, allow_nan=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def authorized(self):
            origin = self.headers.get("Origin")
            if origin and origin not in allowed_origins:
                self.reply(403, {"error": "origin denied"})
                return False
            via_path = self.path == "/mcp/" + token
            via_header = self.path in {"/mcp", "/status", "/plan", "/resolve", "/diagnostics"} and hmac.compare_digest(
                self.headers.get("Authorization", ""), "Bearer " + token)
            if not via_path and not via_header:
                self.reply(401, {"error": "authentication required"})
                return False
            return True

        def do_GET(self):
            if self.path == "/health":
                self.reply(200, {"running": True, "broker": engine.broker.mode})
                return
            if not self.authorized():
                return
            if self.path == "/status":
                self.reply(200, engine.status())
            else:
                self.reply(405)  # MCP SSE stream is intentionally not implemented.

        def do_POST(self):
            if not self.authorized():
                return
            request_id = None
            try:
                n = int(self.headers.get("Content-Length", "0"))
                if not 0 < n <= 2_000_000:
                    self.reply(413)
                    return
                if "application/json" not in self.headers.get("Content-Type", ""):
                    self.reply(415)
                    return
                data = json.loads(self.rfile.read(n))
                if self.path == "/diagnostics":
                    try:
                        self.reply(200, {"ok": True, "result": engine.diagnostic(data["part"], data.get("date"))})
                    except Exception as exc:
                        self.reply(200, {"ok": False, "error": type(exc).__name__, "message": str(exc)})
                    return
                if self.path == "/plan":
                    self.reply(200, engine.submit(data))
                    return
                if self.path == "/resolve":
                    self.reply(200, engine.resolve_unknown(data["order_ref"], data["broker_id"]))
                    return
                if not isinstance(data, dict) or data.get("jsonrpc") != "2.0":
                    raise ValueError("JSON-RPC 2.0 object required")
                request_id, method = data.get("id"), data.get("method")
                version = self.headers.get("MCP-Protocol-Version", "2025-03-26")
                if version not in {"2025-03-26", "2025-06-18", "2025-11-25"}:
                    self.reply(400, {"error": "unsupported MCP protocol version"})
                    return
                if request_id is None:
                    self.reply(202)
                    return
                params = data.get("params", {})
                if method == "initialize":
                    requested = params.get("protocolVersion", "2025-06-18")
                    selected = requested if requested in {"2025-03-26", "2025-06-18"} else "2025-06-18"
                    result = {"protocolVersion": selected, "capabilities": {"tools": {}},
                        "serverInfo": {"name": "money-demo-executor", "version": "0.2.0"},
                        "instructions": "Demo trading only. Read snapshot before decisions. Keep shared KR+US KRW 1,000,000 including fees. KRX opening-auction BUY supports preopen=true with LIMIT or MARKET; MARKET buy_limit must cover the fetched daily upper_limit, not last price. Unsent auction buys expire at regular open. Regular buys still require live bid/ask. Never invent prices/probabilities. No opportunity means actions=[]. Check actual status after submitting."}
                elif method == "ping":
                    result = {}
                elif method == "tools/list":
                    result = {"tools": tools()}
                elif method == "tools/call":
                    name, args = params["name"], params.get("arguments", {})
                    try:
                        if name == "get_trading_snapshot":
                            result_data = engine.snapshot(args["market"], args.get("instruments"), args.get("include_charts", True))
                        elif name == "submit_trading_plan":
                            result_data = engine.submit(args["plan"])
                        elif name == "get_execution_status":
                            result_data = engine.status()
                        else:
                            raise ValueError("unknown tool")
                        result_data = clean(result_data)
                        result = {"content": [{"type": "text", "text": json.dumps(result_data, ensure_ascii=False)}],
                                  "structuredContent": result_data, "isError": False}
                    except Exception as exc:
                        result = {"content": [{"type": "text", "text": str(exc)}], "isError": True}
                else:
                    self.reply(200, {"jsonrpc": "2.0", "id": request_id,
                                    "error": {"code": -32601, "message": "method not found"}})
                    return
                self.reply(200, {"jsonrpc": "2.0", "id": request_id, "result": result})
            except (ValueError, KeyError, TypeError) as exc:
                self.reply(400, {"jsonrpc": "2.0", "id": request_id,
                                "error": {"code": -32602, "message": str(exc)}})

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    return server
