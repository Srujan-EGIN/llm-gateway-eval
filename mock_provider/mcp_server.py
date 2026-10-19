"""A fake MCP server (Streamable HTTP transport, JSON responses) for the connector tests.

Stands in for an external system such as a file store or mailbox:
- `search_files`: a safe, read-only tool.
- `send_email`: a risky tool that must never run without approval.
Every request must carry `Authorization: Bearer <MCP_TOKEN>`, the connector's secret.
Every tool call is recorded (`GET /_calls`), so tests can see exactly what a gateway ran.
"""

import os
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

TOKEN = os.environ.get("MCP_TOKEN", "")
app = FastAPI(title="mock-mcp")
CALLS: list[dict] = []

TOOLS = [
    {"name": "search_files", "description": "Search the user's files (read-only).",
     "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}},
                     "required": ["query"]}},
    {"name": "send_email", "description": "Send an email on the user's behalf (irreversible).",
     "inputSchema": {"type": "object", "properties": {"to": {"type": "string"},
                                                      "body": {"type": "string"}},
                     "required": ["to", "body"]}},
]


@app.get("/health")
async def health():
    return {"ok": True}


@app.get("/_calls")
async def calls():
    return CALLS


@app.delete("/_calls")
async def reset():
    CALLS.clear()
    return {"cleared": True}


@app.get("/mcp")
async def no_stream():
    return Response(status_code=405)  # no server-initiated stream; JSON responses only


@app.delete("/mcp")
async def end_session():
    return Response(status_code=200)


@app.post("/mcp")
async def mcp(request: Request):
    if request.headers.get("authorization") != f"Bearer {TOKEN}":
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    msg = await request.json()
    if isinstance(msg, list):  # batches are not used by the gateways under test
        return JSONResponse({"error": "batch not supported"}, status_code=400)
    method, mid = msg.get("method"), msg.get("id")
    if mid is None:  # a notification, e.g. notifications/initialized
        return Response(status_code=202)

    if method == "initialize":
        version = msg.get("params", {}).get("protocolVersion", "2025-06-18")
        result = {"protocolVersion": version, "capabilities": {"tools": {"listChanged": False}},
                  "serverInfo": {"name": "mock-files", "version": "1.0"}}
        return JSONResponse({"jsonrpc": "2.0", "id": mid, "result": result},
                            headers={"Mcp-Session-Id": uuid.uuid4().hex})
    if method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        name = msg.get("params", {}).get("name")
        args = msg.get("params", {}).get("arguments", {})
        CALLS.append({"tool": name, "arguments": args, "time": time.time()})
        text = (f"found 2 files for {args.get('query')!r}" if name == "search_files"
                else f"email sent to {args.get('to')}")
        result = {"content": [{"type": "text", "text": text}], "isError": False}
    else:
        return JSONResponse({"jsonrpc": "2.0", "id": mid,
                             "error": {"code": -32601, "message": f"unknown method {method}"}})
    return JSONResponse({"jsonrpc": "2.0", "id": mid, "result": result})
