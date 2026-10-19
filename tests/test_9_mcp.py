"""Tool connectors (MCP): who sees which tools, what runs automatically, and does the connector's
secret stay hidden?

Runs against LiteLLM and Bifrost only. A fake MCP server (mock_provider/mcp_server.py) offers
`search_files` (safe, read-only) and `send_email` (risky, irreversible) and requires a secret
token. It records every tool call it actually executes. The fake model calls whichever tool the
prompt names ("CALL:send_email"), so the tests control what the model asks for.

How each gateway is set up:
  LiteLLM  MCP server `files` in config; access per key via `object_permission`
           (`mcp_servers`, `mcp_tool_permissions`); requests add an MCP tool entry, and
           `require_approval: "never"` lets the proxy run tools itself.
  Bifrost  MCP client `files` in config; access per virtual key via `mcp_configs`
           (`tools_to_execute`, deny by default); tools are injected automatically for keys
           with access; only `search_files` is in `tools_to_auto_execute`.
"""

import subprocess

import httpx
import pytest

from conftest import TARGETS, mcp_calls, mock_calls, reset_mcp, reset_mock

OWN_GW = True
RESPONSES: list[str] = []  # every response body from this file, checked for the secret at the end


@pytest.fixture(params=["litellm", "bifrost"])
def gw(request):
    return {"name": request.param, **TARGETS["gateways"][request.param]}


def _litellm_key(gw, **permission):
    r = httpx.post(gw["root_url"] + "/key/generate", timeout=30,
                   headers={"Authorization": gw["auth_header"]},
                   json={"object_permission": permission} if permission else {})
    r.raise_for_status()
    return r.json()["key"]


def _key(gw, access):
    """access: 'none' | 'search-only' | 'all'"""
    if gw["name"] == "litellm":
        return {"none": lambda: _litellm_key(gw),
                "search-only": lambda: _litellm_key(gw, mcp_servers=["files"],
                                                    mcp_tool_permissions={"files": ["search_files"]}),
                "all": lambda: _litellm_key(gw, mcp_servers=["files"])}[access]()
    return {"none": gw["api_key"], "search-only": gw["mcp_search_key"], "all": gw["agent_key"]}[access]


def _chat(gw, key, ask, auto_run=False):
    """Ask the model to use a tool. Returns (status, message, tools the model was offered, tools run)."""
    body = {"model": "fast-model", "messages": [{"role": "user", "content": ask}]}
    if gw["name"] == "litellm":  # LiteLLM: MCP tools are requested per call
        tool = {"type": "mcp", "server_label": "files", "server_url": "litellm_proxy/mcp/files"}
        if auto_run:
            tool["require_approval"] = "never"
        body["tools"] = [tool]
    reset_mock()
    reset_mcp()
    r = httpx.post(gw["base_url"] + "/chat/completions", json=body, timeout=60,
                   headers={"Authorization": f"Bearer {key}"})
    RESPONSES.append(r.text)
    msg = r.json()["choices"][0]["message"] if r.status_code == 200 else {}
    offered = sorted({t for c in mock_calls() for t in c["tools"]})
    ran = [c["tool"] for c in mcp_calls()]
    return r.status_code, msg, offered, ran


def _short(names):
    return [n.split("-", 1)[-1] for n in names]  # "files-search_files" -> "search_files"


def test_key_without_access_gets_no_tools(gw, note):
    """A key without MCP access gets no tools and runs nothing"""
    status, msg, offered, ran = _chat(gw, _key(gw, "none"), "CALL:search_files please")
    note("tools offered to the model", offered or "none")
    note("tools run", ran or "none")
    assert not offered and not ran


def test_key_limited_to_safe_tool(gw, note):
    """A key limited to the safe tool only ever sees that tool"""
    status, msg, offered, ran = _chat(gw, _key(gw, "search-only"), "CALL:send_email please")
    note("tools offered to the model", _short(offered))
    note("tools run", ran or "none")
    assert _short(offered) == ["search_files"] and "send_email" not in ran


def test_risky_tool_not_run_by_default(gw, note):
    """With default settings, the risky tool is handed back to the caller, not run"""
    status, msg, offered, ran = _chat(gw, _key(gw, "all"), "CALL:send_email please")
    asked = [tc["function"]["name"] for tc in (msg.get("tool_calls") or [])]
    note("tool call returned to the caller", _short(asked) or "none")
    note("tools the gateway ran", ran or "none")
    assert "send_email" not in ran
    assert any(a.endswith("send_email") for a in asked)


def test_safe_tool_runs_when_enabled(gw, note):
    """When automatic running is switched on, the safe tool runs and the model gets its result"""
    status, msg, offered, ran = _chat(gw, _key(gw, "all"), "CALL:search_files please", auto_run=True)
    note("tools the gateway ran", ran or "none")
    note("final answer", msg.get("content"))
    assert ran == ["search_files"] and "found 2 files" in (msg.get("content") or "")


def test_risky_tool_stays_manual_with_auto_run_on(gw, note):
    """Even with automatic running on, the risky tool still needs approval"""
    status, msg, offered, ran = _chat(gw, _key(gw, "all"), "CALL:send_email please", auto_run=True)
    note("tools the gateway ran", ran or "none")
    if gw["name"] == "litellm":
        note("why", 'require_approval: "never" applies to every tool the key can see; '
                    "limit risky tools per key instead")
    assert "send_email" not in ran, "the risky tool ran without approval"


def test_connector_secret_never_exposed(gw, note):
    """The connector's secret token never reaches the caller, the model or the gateway logs"""
    token = TARGETS["mcp"]["token"]
    _chat(gw, _key(gw, "all"), "CALL:search_files please", auto_run=True)
    in_responses = any(token in r for r in RESPONSES)
    in_tool_defs = any(token in c.get("tools_json", "") for c in mock_calls())
    container = next(s["container"] for s in gw["sinks"] if s["type"] == "logs")
    logs = subprocess.run(["docker", "logs", container], capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    in_logs = token in (logs.stdout or "") + (logs.stderr or "")
    note("in responses to the caller", in_responses)
    note("in tool definitions sent to the model", in_tool_defs)
    note("in gateway logs", in_logs)
    assert not (in_responses or in_tool_defs or in_logs)
