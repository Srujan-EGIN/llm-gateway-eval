"""A fake model provider that speaks several provider APIs.

Lets the gateways be tested for free and deterministically:
- OpenAI-compatible (`/v1/chat/completions`): the default, and how on-prem servers such as
  vLLM or Ollama look.
- Azure OpenAI (`/openai/deployments/<deployment>/chat/completions` and `/openai/v1/...`).
- Azure AI Foundry model inference (`/models/chat/completions`).
- AWS Bedrock Converse (`/model/<model-id>/converse`); the AWS region the request was signed
  for is read from its SigV4 signature.

Every call is recorded (`GET /_calls`): which provider API, which model, which region, which
tools were offered. Tests use that to see what really happened behind the gateway.
Any model whose name contains "broken" always fails (503), to test fallback.
A fixed delay (MOCK_LATENCY_MS) makes latency comparisons fair.
"""

import asyncio
import json
import os
import re
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

LATENCY_S = int(os.environ.get("MOCK_LATENCY_MS", "100")) / 1000

# Models served on the OpenAI-compatible route. "eu" / "us" / "global" only label residency.
MODELS = {
    "fast-model": {"residency": "eu"},
    "smart-model": {"residency": "eu"},
    "backup-model": {"residency": "eu"},
    "broken-model": {"residency": "eu"},
    "outside-model": {"residency": "global"},
    "embed-model": {"residency": "eu"},
    # Real model names, so each gateway's built-in price list can be tested.
    "gpt-4o-mini": {"residency": "eu"},
    "gpt-4o-mini-2024-07-18": {"residency": "eu"},
    # Routing tests
    "split-a": {"residency": "eu"}, "split-b": {"residency": "eu"},
    "eu-model": {"residency": "eu"}, "us-model": {"residency": "us"},
    "eu-broken": {"residency": "eu"}, "eu-broken-2": {"residency": "eu"},
    "flaky-broken": {"residency": "eu"}, "flaky-ok": {"residency": "eu"},
    # On-prem (vLLM-style) model for the provider tests
    "llama-3.1-8b": {"residency": "onprem"},
}

app = FastAPI(title="mock-provider")
CALLS: list[dict] = []


def _record(provider: str, path: str, model: str, request: Request, body: dict, **extra) -> None:
    tools = [t.get("function", {}).get("name") or t.get("toolSpec", {}).get("name")
             for t in (body.get("tools") or body.get("toolConfig", {}).get("tools") or [])]
    CALLS.append({"provider": provider, "path": path, "model": model, "time": time.time(),
                  "tools": [t for t in tools if t],
                  # Tool definitions as the model received them (to check no secret leaks in)
                  "tools_json": json.dumps(body.get("tools") or body.get("toolConfig") or []),
                  **extra})


def _usage(prompt_text: str, completion_text: str) -> dict:
    p, c = max(1, len(prompt_text) // 4), max(1, len(completion_text) // 4)
    return {"prompt_tokens": p, "completion_tokens": c, "total_tokens": p + c}


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(p.get("text", "") for p in content if isinstance(p, dict))
    return ""


def _prompt_text(messages: list) -> str:
    return " ".join(_text_of(m.get("content")) for m in messages)


def _tool_args(tool: dict) -> dict:
    params = tool.get("function", {}).get("parameters") or {}
    return {p: ("Copenhagen" if p == "city" else "test") for p in params.get("required", [])}


def _openai_answer(model: str, body: dict):
    """The OpenAI-format answer: text, a tool call, or a reply to a tool result."""
    messages = body.get("messages", [])
    prompt = _prompt_text(messages)
    cid, created = f"chatcmpl-{uuid.uuid4().hex[:12]}", int(time.time())

    def done(message, finish):
        return {"id": cid, "object": "chat.completion", "created": created, "model": model,
                "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                "usage": _usage(prompt, json.dumps(message))}

    # Second turn after a tool ran: answer with the tool's result.
    if messages and messages[-1].get("role") == "tool":
        return done({"role": "assistant",
                     "content": f"Tool said: {_text_of(messages[-1].get('content'))}"}, "stop")

    # Tool calling: call the tool named in "CALL:<part of name>", else the first one.
    if body.get("tools"):
        tools = body["tools"]
        want = re.search(r"CALL:([A-Za-z0-9_-]+)", prompt)
        tool = next((t for t in tools if want and want.group(1) in t["function"]["name"]), tools[0])
        return done({"role": "assistant", "content": None, "tool_calls": [{
            "id": f"call_{uuid.uuid4().hex[:8]}", "type": "function",
            "function": {"name": tool["function"]["name"],
                         "arguments": json.dumps(_tool_args(tool))}}]}, "tool_calls")

    answer = f"Reply from {model}."
    # Echo a request tag ("TAG:abc") so load tests can spot answers delivered to the wrong caller.
    if tag := re.search(r"TAG:[A-Za-z0-9-]+", prompt):
        answer += f" {tag.group(0)}"
    if body.get("stream"):
        async def gen():
            words = answer.split(" ")
            for i, w in enumerate(words):
                chunk = {"id": cid, "object": "chat.completion.chunk", "created": created,
                         "model": model,
                         "choices": [{"index": 0, "finish_reason": None,
                                      "delta": {"content": w + (" " if i < len(words) - 1 else "")}}]}
                yield f"data: {json.dumps(chunk)}\n\n"
                await asyncio.sleep(0.01)
            final = {"id": cid, "object": "chat.completion.chunk", "created": created,
                     "model": model, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                     "usage": _usage(prompt, answer)}
            yield f"data: {json.dumps(final)}\n\n"
            yield "data: [DONE]\n\n"
        return StreamingResponse(gen(), media_type="text/event-stream")
    return done({"role": "assistant", "content": answer}, "stop")


def _failure(model: str):
    return JSONResponse({"error": {"message": f"{model} is unavailable", "type": "server_error"}},
                        status_code=503)


@app.get("/health")
async def health():
    return {"ok": True}


@app.get("/_calls")
async def calls():
    return CALLS


@app.delete("/_calls")
async def reset_calls():
    CALLS.clear()
    return {"cleared": True}


@app.get("/v1/models")
async def list_models():
    return {"object": "list", "data": [{"id": m, "object": "model"} for m in MODELS]}


# --------------------------------------------------------------- OpenAI-compatible / on-prem
@app.post("/v1/chat/completions")
async def chat(request: Request):
    body = await request.json()
    model = body.get("model", "")
    _record("openai-compatible", "/v1/chat/completions", model, request, body)
    if model not in MODELS:
        return JSONResponse({"error": {"message": f"unknown model {model}"}}, status_code=404)
    await asyncio.sleep(LATENCY_S)
    return _failure(model) if "broken" in model else _openai_answer(model, body)


# --------------------------------------------------------------- Azure OpenAI
@app.post("/openai/deployments/{deployment}/chat/completions")
async def azure_classic(deployment: str, request: Request):
    body = await request.json()
    _record("azure-openai", request.url.path, deployment, request, body,
            api_version=request.query_params.get("api-version"),
            auth="api-key" if request.headers.get("api-key") else request.headers.get("authorization", "")[:6])
    await asyncio.sleep(LATENCY_S)
    return _failure(deployment) if "broken" in deployment else _openai_answer(deployment, body)


@app.post("/openai/v1/chat/completions")
async def azure_v1(request: Request):
    body = await request.json()
    model = body.get("model", "")
    _record("azure-openai", request.url.path, model, request, body,
            auth="api-key" if request.headers.get("api-key") else request.headers.get("authorization", "")[:6])
    await asyncio.sleep(LATENCY_S)
    return _failure(model) if "broken" in model else _openai_answer(model, body)


# --------------------------------------------------------------- Azure AI Foundry
@app.post("/models/chat/completions")
async def foundry(request: Request):
    body = await request.json()
    model = body.get("model", "")
    _record("azure-foundry", request.url.path, model, request, body,
            api_version=request.query_params.get("api-version"))
    await asyncio.sleep(LATENCY_S)
    return _failure(model) if "broken" in model else _openai_answer(model, body)


# --------------------------------------------------------------- AWS Bedrock (Converse)
_SIGV4_REGION = re.compile(r"Credential=[^/]+/\d{8}/([a-z0-9-]+)/bedrock/aws4_request")


@app.post("/model/{model_id:path}/converse")
async def bedrock_converse(model_id: str, request: Request):
    body = await request.json()
    auth = request.headers.get("authorization", "")
    region = (m.group(1) if (m := _SIGV4_REGION.search(auth)) else None)
    _record("aws-bedrock", "/model/{id}/converse", model_id, request, body,
            region=region, auth="sigv4" if auth.startswith("AWS4-HMAC-SHA256") else auth[:6])
    await asyncio.sleep(LATENCY_S)
    if "broken" in model_id:
        return JSONResponse({"message": f"{model_id} is unavailable"}, status_code=503,
                            headers={"x-amzn-ErrorType": "ServiceUnavailableException"})
    prompt = " ".join(_text_of(m.get("content")) for m in body.get("messages", []))
    answer = f"Reply from {model_id}."
    if tag := re.search(r"TAG:[A-Za-z0-9-]+", prompt):
        answer += f" {tag.group(0)}"
    u = _usage(prompt, answer)
    return {"output": {"message": {"role": "assistant", "content": [{"text": answer}]}},
            "stopReason": "end_turn",
            "usage": {"inputTokens": u["prompt_tokens"], "outputTokens": u["completion_tokens"],
                      "totalTokens": u["total_tokens"]},
            "metrics": {"latencyMs": int(LATENCY_S * 1000)}}


# --------------------------------------------------------------- embeddings
@app.post("/v1/embeddings")
async def embeddings(request: Request):
    body = await request.json()
    _record("openai-compatible", "/v1/embeddings", body.get("model"), request, body)
    await asyncio.sleep(LATENCY_S)
    inputs = body.get("input")
    inputs = inputs if isinstance(inputs, list) else [inputs]
    return {
        "object": "list", "model": body.get("model"),
        "data": [{"object": "embedding", "index": i, "embedding": [0.1, 0.2, 0.3]}
                 for i, _ in enumerate(inputs)],
        "usage": {"prompt_tokens": 3 * len(inputs), "total_tokens": 3 * len(inputs)},
    }
