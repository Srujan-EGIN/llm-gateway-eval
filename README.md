# LLM Gateway Evaluation: LiteLLM vs MLflow AI Gateway

> **Work in progress.** The test suite and generated report are still being built.

A hands-on comparison of **LiteLLM Proxy** and **MLflow AI Gateway**. Both run side by side in Docker against a fake model provider, and one test suite checks each gateway against the same requirements:

- Chat, streaming, tool calling and embeddings work
- **No-Retention:** prompt text is never stored in logs, databases or traces
- **Fallback:** when a model fails, the backup is used, and models outside the allowed list never are
- **Cost tracking:** each call reports its cost
- **Security defaults:** authentication, admin UI, pinned images
- **Overhead:** latency the gateway adds per call

Pinned versions are in [`images.json`](images.json): LiteLLM `v1.104.0`, MLflow `v3.17.0`.

## Run

Requires Docker and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
uv run python scripts/run_eval.py
```
