# Results: LiteLLM Proxy vs Bifrost vs MLflow AI Gateway

Generated 2026-10-19 16:53 by `scripts/run_eval.py`. Every row is an automated test in [`tests/`](../tests).

| | LiteLLM Proxy | Bifrost | MLflow AI Gateway | MLflow + login add-on¹ |
|---|---|---|---|---|
| **Checks passed** | **40 / 41** | **39 / 41** | **15 / 25** | **6 / 7** |
| Image | `ghcr.io/berriai/litellm-database:v1.104.0` | `maximhq/bifrost:v2.2.6` | `ghcr.io/mlflow/mlflow:v3.17.0` | `ghcr.io/mlflow/mlflow:v3.17.0-full` |

✅ passed  ❌ failed  ℹ️ measured, not graded  – not tested

¹ MLflow with `--app-name basic-auth` (needs the `-full` image). Tested for **security checks only**; every other behaviour is the same as plain MLflow.

## Final comparison

| Area | LiteLLM Proxy | Bifrost | MLflow AI Gateway |
|---|:-:|:-:|:-:|
| Basic features | ✅ 5/5 | ✅ 5/5 | ✅ 5/5 |
| Fallback | ✅ 2/2 | ✅ 2/2 | ✅ 2/2 |
| No-Retention and cost | ✅ 3/3 | ✅ 3/3 | ⚠️ 2/3 |
| Security | ✅ 7/7 | ✅ 7/7 | ❌ 1/7 |
| Under load (concurrency) | ✅ 8/8 | ⚠️ 7/8 | ⚠️ 5/8 |
| Routing | ✅ 4/4 | ⚠️ 3/4 | – |
| Provider connectors (AWS, Azure, on-prem) | ✅ 6/6 | ✅ 6/6 | – |
| Tool connectors (MCP) | ⚠️ 5/6 | ✅ 6/6 | – |
| Time added, 1 at once (p50 / p95 ms) | 8.7 / 12.7 | 1.2 / 1.4 | 11.5 / 13.0 |
| Time added, 100 at once (p50 / p95 ms) | 85.6 / 94.8 | 0.7 / <1 | 422.8 / 545.1 |
| Throughput at 100 at once (req/s) | 381.3 | 683.0 | 153.6 |
| Stores prompts with default settings | no | yes | yes |
| **All graded checks** | **40 / 41** | **39 / 41** | **15 / 25** |

## Basic features

| Check | LiteLLM Proxy | Bifrost | MLflow AI Gateway | MLflow + login add-on¹ |
|---|---|---|---|---|
| Chat completion works | ✅ tokens reported: 16 | ✅ tokens reported: 16 | ✅ tokens reported: 16 | – |
| Streaming works | ✅ chunks: 4 | ✅ chunks: 4 | ✅ chunks: 4 | – |
| Tool calling works | ✅ tool called: get_weather | ✅ tool called: get_weather | ✅ tool called: get_weather | – |
| Embeddings work | ✅ vectors: 2 | ✅ vectors: 2 | ✅ vectors: 2 | – |
| Unknown model names are refused | ✅ status: 400 | ✅ status: 400 | ✅ status: 404 | – |

## Fallback

| Check | LiteLLM Proxy | Bifrost | MLflow AI Gateway | MLflow + login add-on¹ |
|---|---|---|---|---|
| Falls back to the backup when the main model fails | ✅ provider calls, in order: broken-model -> backup-model | ✅ provider calls, in order: broken-model -> backup-model | ✅ provider calls, in order: broken-model -> backup-model | – |
| Fallback never uses a model outside the allowed list | ✅ models called over 10 requests: backup-model, broken-model | ✅ models called over 10 requests: backup-model, broken-model | ✅ models called over 10 requests: backup-model, broken-model | – |

## No-Retention and cost

| Check | LiteLLM Proxy | Bifrost | MLflow AI Gateway | MLflow + login add-on¹ |
|---|---|---|---|---|
| Hardened settings: prompt text is stored nowhere | ✅ prompt found in: nowhere | ✅ prompt found in: nowhere | ✅ prompt found in: nowhere | – |
| Default settings: is prompt text stored? (informational) | ℹ️ stores prompts by default: no; prompt found in: nowhere | ℹ️ stores prompts by default: yes; prompt found in: file /app/data/logs.db-wal | ℹ️ stores prompts by default: yes; prompt found in: file /data/mlflow.db; 2 trace files | – |
| Default settings: cost is tracked per call | ✅ cost of one call (USD): 8.55e-06 | ✅ cost of one call (USD): 8.55e-06 | ✅ cost of one call (USD): 8.55e-06 | – |
| Cost is still tracked while No-Retention settings are on | ✅ cost of one call (USD): 8.55e-06; usage row in database: {'model': 'openai/gpt-4o-mini', 'spend': 8.55e-06, 'total_tokens': 15}; prompt stored in that row: False | ✅ cost of one call (USD): 8.55e-06 | ❌ cost of one call (USD): None; AssertionError: no cost available: cost tracking depends on stored request traces | – |

## Security

| Check | LiteLLM Proxy | Bifrost | MLflow AI Gateway | MLflow + login add-on¹ |
|---|---|---|---|---|
| Calls without a key are refused | ✅ status without key: 401 | ✅ status without key: 401 | ❌ status without key: 200; assert 200 in (401, 403) | ✅ status without key: 401 |
| Calls with a wrong key are refused | ✅ status with wrong key: 401 | ✅ status with wrong key: 401 | ❌ status with wrong key: 200; assert 200 in (401, 403) | ✅ status with wrong key: 401 |
| A key can be limited to certain models (e.g. one key per compliance mode) | ✅ how: virtual key limited to fast-model; allowed model: 200; other model: 403; other model reached the provider: False | ✅ how: virtual key limited to fast-model (from config); allowed model: 200; other model: 400; other model reached the provider: False | ❌ Failed: no keys or users without the login add-on, so no per-model limits | ✅ how: user with a role that may USE every endpoint except outside-model; allowed model: 200; other model: 403; other model reached the provider: False |
| Gateway configuration can't be read or changed without logging in | ✅ read model config without key: 401 | ✅ list providers without login: 401; add a provider without login: 401 | ❌ list endpoints without login: 200; create a provider secret without login: 200; assert (200 in (401, 403)) | ✅ list endpoints without login: 401; create a provider secret without login: 401 |
| The app's own (non-admin) credential can't change gateway configuration | ✅ add a model with the app key: 403; create a new key with the app key: 401 | ✅ add a provider with the app's virtual key: 401; create a virtual key with the app's virtual key: 401 | ❌ Failed: there is no separate app credential: anyone can change the configuration | ❌ create a provider secret as the non-admin app user: 200; AssertionError: any logged-in user can add provider credentials and destinations |
| The admin UI can be switched off or requires login | ✅ login route says 'Admin UI is Disabled': True; empty dashboard page still served at /ui (no login possible): True | ✅ admin API without login: 401; empty UI page still served at / (login required for data): True | ❌ UI status without login: 200; AssertionError: the MLflow UI (including gateway settings) is open | ✅ UI status without login: 401 |
| Runs from an image pinned by digest, with no known malware startup file | ✅ image: ghcr.io/berriai/litellm-database:v1.104.0; litellm_init.pth (March 2026 malware) present: False | ✅ image: maximhq/bifrost:v2.2.6; Python startup files: n/a (Go binary) | ✅ image: ghcr.io/mlflow/mlflow:v3.17.0; litellm_init.pth (March 2026 malware) present: False | ✅ image: ghcr.io/mlflow/mlflow:v3.17.0-full; litellm_init.pth (March 2026 malware) present: False |

## Under load (concurrency)

| Check | LiteLLM Proxy | Bifrost | MLflow AI Gateway | MLflow + login add-on¹ |
|---|---|---|---|---|
| 200 requests, 50 at once: all succeed and every caller gets its own answer | ✅ succeeded: 200 / 200; answers delivered to the wrong caller: 0 | ✅ succeeded: 200 / 200; answers delivered to the wrong caller: 0 | ✅ succeeded: 200 / 200; answers delivered to the wrong caller: 0 | – |
| 50 streams at once: every stream completes with its own full answer | ✅ complete, correct streams: 50 / 50 | ✅ complete, correct streams: 50 / 50 | ✅ complete, correct streams: 50 / 50 | – |
| 100 requests to a failing model, 50 at once: all fall back, never outside the allowed list | ✅ answered by backup-model: 100 / 100; models the provider received: backup-model, broken-model | ✅ answered by backup-model: 100 / 100; models the provider received: backup-model, broken-model | ✅ answered by backup-model: 100 / 100; models the provider received: backup-model, broken-model | – |
| 100 mixed requests with a limited key, 50 at once: allowed ones pass, others never reach the provider | ✅ allowed model succeeded: 50 / 50; other model refused: 50 / 50; other model reached the provider: False | ✅ allowed model succeeded: 50 / 50; other model refused: 50 / 50; other model reached the provider: False | ❌ Failed: no per-key model limits (no keys without the login add-on) | – |
| A key limited to 20 requests/minute lets at most 20 of a 60-request burst through | ✅ let through: 20 / 60 (limit 20); refused with 429: 40; other errors: 0 | ❌ let through: 22 / 60 (limit 20); refused with 429: 38; other errors: 0; assert (22 <= 20) | ❌ Failed: no per-key request rate limits in MLflow's gateway (only cost budgets, which need stored traces) | – |
| 100 private prompts, 50 at once (hardened settings): none stored anywhere | ✅ succeeded: 100 / 100; prompts found in: nowhere | ✅ succeeded: 100 / 100; prompts found in: nowhere | ✅ succeeded: 100 / 100; prompts found in: nowhere | – |
| 100 identical calls, 50 at once (No-Retention on): exactly 100 usage records with the right total cost | ✅ usage records: 100 / 100; total cost (USD): 0.00105000; same cost on every record: True | ✅ usage records: 100 / 100; total cost (USD): 0.00105000; same cost on every record: True | ❌ Failed: no usage or cost records while No-Retention (usage tracking off) is on | – |
| No failed requests at 50 and 100 requests at once | ✅ errors at 50 at once: 0 of 400; errors at 100 at once: 0 of 400 | ✅ errors at 50 at once: 0 of 400; errors at 100 at once: 0 of 400 | ✅ errors at 50 at once: 0 of 400; errors at 100 at once: 0 of 400 | – |

## Routing

| Check | LiteLLM Proxy | Bifrost | MLflow AI Gateway | MLflow + login add-on¹ |
|---|---|---|---|---|
| Traffic is split by the configured weights (80 / 20, 500 requests) | ✅ split-a / split-b: 383 / 117 (77% / 23%) | ✅ split-a / split-b: 399 / 101 (80% / 20%) | – | – |
| Requests that ask for the EU region always go to the EU backend (100 requests) | ✅ EU requests served by eu-model / us-model: 100 / 0; requests without a region went to eu / us (informational): 22 / 18 | ✅ EU requests served by eu-model / us-model: 100 / 0; requests without a region went to eu / us (informational): 0 / 40 | – | – |
| A fallback chain is tried in order: broken -> broken backup -> working backup | ✅ provider calls, in order: eu-broken -> eu-broken-2 -> eu-model | ✅ provider calls, in order: eu-broken -> eu-broken-2 -> eu-model | – | – |
| A backend that keeps failing is taken out of rotation (60 requests, 1 of 2 backends broken; pass: at most 10 fail) | ✅ failed requests: 6 / 60; requests sent to the broken backend: 6 | ❌ failed requests: 34 / 60; requests sent to the broken backend: 34; why: the free version has no automatic cooldown; the circuit breaker is Enterprise; AssertionError: the broken backend kept receiving traffic | – | – |

## Provider connectors (AWS, Azure, on-prem)

| Check | LiteLLM Proxy | Bifrost | MLflow AI Gateway | MLflow + login add-on¹ |
|---|---|---|---|---|
| Reaches AWS Bedrock (Converse API), signed for the EU region | ✅ answer: Reply from eu.anthropic.claude-test-v1:0.; Bedrock saw: [['eu.anthropic.claude-test-v1:0', 'eu-central-1', 'sigv4']] | ✅ answer: Reply from eu.anthropic.claude-test-v1:0.; Bedrock saw: [['eu.anthropic.claude-test-v1:0', 'eu-central-1', 'sigv4']] | – | – |
| Reaches Azure OpenAI (EU deployment) with its API key | ✅ answer: Reply from gpt-eu-deploy.; Azure saw: [['gpt-eu-deploy', '/openai/deployments/gpt-eu-deploy/chat/completions', 'api-key']] | ✅ answer: Reply from gpt-eu-deploy.; Azure saw: [['gpt-eu-deploy', '/openai/v1/chat/completions', 'api-key']] | – | – |
| Reaches a non-OpenAI model (Mistral) on Azure AI Foundry | ✅ answer: Reply from Mistral-Large-2411.; API used: [['azure-foundry', '/models/chat/completions']] | ✅ answer: Reply from Mistral-Large-2411.; API used: [['azure-openai', '/openai/v1/chat/completions']]; caveat: sent via Azure's OpenAI-compatible v1 API; check a real Foundry resource accepts it | – | – |
| Reaches an on-prem model server (vLLM, OpenAI-compatible) | ✅ answer: Reply from llama-3.1-8b.; on-prem server saw: [['openai-compatible', '/v1/chat/completions']] | ✅ answer: Reply from llama-3.1-8b.; on-prem server saw: [['openai-compatible', '/v1/chat/completions']] | – | – |
| When Azure EU is down, requests fall back to AWS EU (never to a US provider) | ✅ provider calls, in order: azure-openai:broken-eu-deploy -> aws-bedrock:eu.anthropic.claude-test-v1:0 | ✅ provider calls, in order: azure-openai:broken-eu-deploy -> aws-bedrock:eu.anthropic.claude-test-v1:0 | – | – |
| An EU Sovereign key can use EU providers but never reaches the US Azure deployment | ✅ EU model (aws-claude): 200; US model (azure-us-gpt): 403; US deployment reached: False | ✅ EU model (aws-claude): 200; US model (azure-us-gpt): 403; US deployment reached: False | – | – |

## Tool connectors (MCP)

| Check | LiteLLM Proxy | Bifrost | MLflow AI Gateway | MLflow + login add-on¹ |
|---|---|---|---|---|
| A key without MCP access gets no tools and runs nothing | ✅ tools offered to the model: none; tools run: none | ✅ tools offered to the model: none; tools run: none | – | – |
| A key limited to the safe tool only ever sees that tool | ✅ tools offered to the model: ['search_files']; tools run: none | ✅ tools offered to the model: ['search_files']; tools run: ['search_files'] | – | – |
| With default settings, the risky tool is handed back to the caller, not run | ✅ tool call returned to the caller: ['send_email']; tools the gateway ran: none | ✅ tool call returned to the caller: ['send_email']; tools the gateway ran: none | – | – |
| When automatic running is switched on, the safe tool runs and the model gets its result | ✅ tools the gateway ran: ['search_files']; final answer: Tool said: found 2 files for 'test' | ✅ tools the gateway ran: ['search_files']; final answer: Tool said: found 2 files for 'test' | – | – |
| Even with automatic running on, the risky tool still needs approval | ❌ tools the gateway ran: ['send_email']; why: require_approval: "never" applies to every tool the key can see; limit risky tools per key instead; AssertionError: the risky tool ran without approval | ✅ tools the gateway ran: none | – | – |
| The connector's secret token never reaches the caller, the model or the gateway logs | ✅ in responses to the caller: False; in tool definitions sent to the model: False; in gateway logs: False | ✅ in responses to the caller: False; in tool definitions sent to the model: False; in gateway logs: False | – | – |

## Speed

| Check | LiteLLM Proxy | Bifrost | MLflow AI Gateway | MLflow + login add-on¹ |
|---|---|---|---|---|
| Time added per call at 1 / 10 / 50 / 100 requests at once (informational) | ℹ️ 1 at once: added p50 / p95 (ms): 8.7 / 12.7; 10 at once: added p50 / p95 (ms): 12.2 / 28.0; 50 at once: added p50 / p95 (ms): 59.8 / 89.0; 100 at once: added p50 / p95 (ms): 85.6 / 94.8 | ℹ️ 1 at once: added p50 / p95 (ms): 1.2 / 1.4; 10 at once: added p50 / p95 (ms): 2.5 / 1.1; 50 at once: added p50 / p95 (ms): 3.2 / 16.1; 100 at once: added p50 / p95 (ms): 0.7 / <1 | ℹ️ 1 at once: added p50 / p95 (ms): 11.5 / 13.0; 10 at once: added p50 / p95 (ms): 11.1 / 44.0; 50 at once: added p50 / p95 (ms): 86.6 / 255.6; 100 at once: added p50 / p95 (ms): 422.8 / 545.1 | – |
| Requests per second through the gateway at 100 at once (informational) | ℹ️ gateway req/s at 100 at once: 381.3; provider alone could do (req/s): 1000 | ℹ️ gateway req/s at 100 at once: 683.0; provider alone could do (req/s): 1000 | ℹ️ gateway req/s at 100 at once: 153.6; provider alone could do (req/s): 1000 | – |

## How to read this

- **Hardened** means each gateway's recommended production settings: LiteLLM with `turn_off_message_logging`, Bifrost with `disable_content_logging`, MLflow endpoints with usage tracking (request traces) off. **Default** means the out-of-the-box settings.
- The provider is a **fake model** (`mock_provider/`) that answers in ~100 ms, so tests are free and repeatable. Real Bedrock was not called in this run.
- Speed numbers come from one machine with everything in Docker. Compare the two gateways with each other, not with other benchmarks.
