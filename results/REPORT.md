# Results: LiteLLM Proxy vs Bifrost vs MLflow AI Gateway

Generated 2026-10-16 11:36 by `scripts/run_eval.py`. Every row is an automated test in [`tests/`](../tests).

| | LiteLLM Proxy | Bifrost | MLflow AI Gateway | MLflow + login add-on¹ |
|---|---|---|---|---|
| **Checks passed** | **25 / 25** | **24 / 25** | **15 / 25** | **6 / 7** |
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
| Time added, 1 at once (p50 / p95 ms) | 9.6 / 11.8 | 1.3 / 0.9 | 13.2 / 15.9 |
| Time added, 100 at once (p50 / p95 ms) | 130.8 / 175.7 | 1.1 / <1 | 364.3 / 506.1 |
| Throughput at 100 at once (req/s) | 271.2 | 656.7 | 162.4 |
| Stores prompts with default settings | no | yes | yes |
| **All graded checks** | **25 / 25** | **24 / 25** | **15 / 25** |

## Basic features

| Check | LiteLLM Proxy | Bifrost | MLflow AI Gateway | MLflow + login add-on¹ |
|---|---|---|---|---|
| Chat completion works | ✅ tokens reported: 7 | ✅ tokens reported: 7 | ✅ tokens reported: 7 | – |
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
| Default settings: cost is tracked per call | ✅ cost of one call (USD): 3.15e-06 | ✅ cost of one call (USD): 3.15e-06 | ✅ cost of one call (USD): 3.15e-06 | – |
| Cost is still tracked while No-Retention settings are on | ✅ cost of one call (USD): 3.15e-06; usage row in database: {'model': 'openai/gpt-4o-mini', 'spend': 3.15e-06, 'total_tokens': 6}; prompt stored in that row: False | ✅ cost of one call (USD): 3.15e-06 | ❌ cost of one call (USD): None; AssertionError: no cost available: cost tracking depends on stored request traces | – |

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
| A key limited to 20 requests/minute lets at most 20 of a 60-request burst through | ✅ let through: 20 / 60 (limit 20); refused with 429: 40; other errors: 0 | ❌ let through: 26 / 60 (limit 20); refused with 429: 34; other errors: 0; assert (26 <= 20) | ❌ Failed: no per-key request rate limits in MLflow's gateway (only cost budgets, which need stored traces) | – |
| 100 private prompts, 50 at once (hardened settings): none stored anywhere | ✅ succeeded: 100 / 100; prompts found in: nowhere | ✅ succeeded: 100 / 100; prompts found in: nowhere | ✅ succeeded: 100 / 100; prompts found in: nowhere | – |
| 100 identical calls, 50 at once (No-Retention on): exactly 100 usage records with the right total cost | ✅ usage records: 100 / 100; total cost (USD): 0.00051000; same cost on every record: True | ✅ usage records: 100 / 100; total cost (USD): 0.00051000; same cost on every record: True | ❌ Failed: no usage or cost records while No-Retention (usage tracking off) is on | – |
| No failed requests at 50 and 100 requests at once | ✅ errors at 50 at once: 0 of 400; errors at 100 at once: 0 of 400 | ✅ errors at 50 at once: 0 of 400; errors at 100 at once: 0 of 400 | ✅ errors at 50 at once: 0 of 400; errors at 100 at once: 0 of 400 | – |

## Speed

| Check | LiteLLM Proxy | Bifrost | MLflow AI Gateway | MLflow + login add-on¹ |
|---|---|---|---|---|
| Time added per call at 1 / 10 / 50 / 100 requests at once (informational) | ℹ️ 1 at once: added p50 / p95 (ms): 9.6 / 11.8; 10 at once: added p50 / p95 (ms): 12.0 / 35.5; 50 at once: added p50 / p95 (ms): 81.1 / 123.2; 100 at once: added p50 / p95 (ms): 130.8 / 175.7 | ℹ️ 1 at once: added p50 / p95 (ms): 1.3 / 0.9; 10 at once: added p50 / p95 (ms): 2.8 / 2.3; 50 at once: added p50 / p95 (ms): 3.1 / 16.8; 100 at once: added p50 / p95 (ms): 1.1 / <1 | ℹ️ 1 at once: added p50 / p95 (ms): 13.2 / 15.9; 10 at once: added p50 / p95 (ms): 12.1 / 58.1; 50 at once: added p50 / p95 (ms): 92.5 / 361.7; 100 at once: added p50 / p95 (ms): 364.3 / 506.1 | – |
| Requests per second through the gateway at 100 at once (informational) | ℹ️ gateway req/s at 100 at once: 271.2; provider alone could do (req/s): 1000 | ℹ️ gateway req/s at 100 at once: 656.7; provider alone could do (req/s): 1000 | ℹ️ gateway req/s at 100 at once: 162.4; provider alone could do (req/s): 1000 | – |

## How to read this

- **Hardened** means each gateway's recommended production settings: LiteLLM with `turn_off_message_logging`, Bifrost with `disable_content_logging`, MLflow endpoints with usage tracking (request traces) off. **Default** means the out-of-the-box settings.
- The provider is a **fake model** (`mock_provider/`) that answers in ~100 ms, so tests are free and repeatable. Real Bedrock was not called in this run.
- Speed numbers come from one machine with everything in Docker. Compare the two gateways with each other, not with other benchmarks.
