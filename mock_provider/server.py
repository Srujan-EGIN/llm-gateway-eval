"""A fake OpenAI-compatible model provider.

Lets both gateways be tested for free and deterministically:
- Models are chosen by name (see MODELS). "broken-model" always fails, to test fallback.
- Every call is recorded, so tests can see which model really served a request,
  and whether a model outside the allowed list was ever called.
- A fixed delay (MOCK_LATENCY_MS) makes latency comparisons fair.
"""

import asyncio
import json
import os
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

LATENCY_S = int(os.environ.get("MOCK_LATENCY_MS", "100")) / 1000

# name -> behaviour. "eu" / "global" only label residency for the fallback test.
MODELS = {
    "fast-model": {"residency": "eu"},
    "smart-model": {"residency": "eu"},
    "backup-model": {"residency": "eu"},
    "broken-model": {"residency": "eu", "fail": 503},
    "outside-model": {"residency": "global"},
    "embed-model": {"residency": "eu"},
    # A real model name, so each gateway's built-in price list can be tested.
    "gpt-4o-mini": {"residency": "eu"},
}

app = FastAPI(title="mock-provider")
CALLS: list[dict] = []


def _record(path: str, body: dict) -> None:
    CALLS.append({"path": path, "model": body.get("model"), "time": time.time()})


def _usage(prompt_text: str, completion_text: str) -> dict:
    p, c = max(1, len(prompt_text) // 4), max(1, len(completion_text) // 4)
    return {"prompt_tokens": p, "completion_tokens": c, "total_tokens": p + c}


def _prompt_text(body: dict) -> str:
    parts = []
    for m in body.get("messages", []):
        content = m.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            parts.extend(p.get("text", "") for p in content if isinstance(p, dict))
    return " ".join(parts)


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


@app.post("/v1/chat/completions")
async def chat(request: Request):
    body = await request.json()
    model = body.get("model", "")
    _record("/v1/chat/completions", body)
    spec = MODELS.get(model)
    if spec is None:
        return JSONResponse({"error": {"message": f"unknown model {model}"}}, status_code=404)
    await asyncio.sleep(LATENCY_S)
    if "fail" in spec:
        return JSONResponse(
            {"error": {"message": f"{model} is unavailable", "type": "server_error"}},
            status_code=spec["fail"],
        )

    prompt = _prompt_text(body)
    cid = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    created = int(time.time())

    # Tool calling: if tools are offered, call the first one.
    if body.get("tools"):
        fn = body["tools"][0]["function"]["name"]
        message = {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": f"call_{uuid.uuid4().hex[:8]}",
                "type": "function",
                "function": {"name": fn, "arguments": json.dumps({"city": "Copenhagen"})},
            }],
        }
        return {
            "id": cid, "object": "chat.completion", "created": created, "model": model,
            "choices": [{"index": 0, "message": message, "finish_reason": "tool_calls"}],
            "usage": _usage(prompt, fn),
        }

    answer = f"Reply from {model}."
    if body.get("stream"):
        async def gen():
            words = answer.split(" ")
            for i, w in enumerate(words):
                chunk = {
                    "id": cid, "object": "chat.completion.chunk", "created": created,
                    "model": model,
                    "choices": [{"index": 0, "delta": {"content": w + (" " if i < len(words) - 1 else "")},
                                 "finish_reason": None}],
                }
                yield f"data: {json.dumps(chunk)}\n\n"
                await asyncio.sleep(0.01)
            final = {
                "id": cid, "object": "chat.completion.chunk", "created": created, "model": model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": _usage(prompt, answer),
            }
            yield f"data: {json.dumps(final)}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream")

    return {
        "id": cid, "object": "chat.completion", "created": created, "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": answer},
                     "finish_reason": "stop"}],
        "usage": _usage(prompt, answer),
    }


@app.post("/v1/embeddings")
async def embeddings(request: Request):
    body = await request.json()
    _record("/v1/embeddings", body)
    await asyncio.sleep(LATENCY_S)
    inputs = body.get("input")
    inputs = inputs if isinstance(inputs, list) else [inputs]
    return {
        "object": "list", "model": body.get("model"),
        "data": [{"object": "embedding", "index": i, "embedding": [0.1, 0.2, 0.3]}
                 for i, _ in enumerate(inputs)],
        "usage": {"prompt_tokens": 3 * len(inputs), "total_tokens": 3 * len(inputs)},
    }
