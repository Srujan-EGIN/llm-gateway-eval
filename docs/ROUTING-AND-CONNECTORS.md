# Model routing and connectors: LiteLLM vs Bifrost

How each gateway decides **which model serves a request**, how **providers** (AWS Bedrock, Azure AI Foundry, on-prem) are added, and how **tool connectors** (MCP servers such as Gmail or Drive) plug in.

Sources are the official docs (linked at the end) and Bifrost's v2.2.6 config schema. Items marked 🧪 were also confirmed in this repo's tests.

## TL;DR

| Question | LiteLLM | Bifrost | For Genie |
|---|---|---|---|
| **How is a model picked?** | Model groups + a strategy (random by weight, least busy, latency, cost); tags restrict it | Virtual key → CEL routing rules → weights | Genie's kernel decides; the gateway only balances and falls back **within** what the kernel allows |
| **Weighted split, EU routing, fallback chain** | ✅ all tested 🧪 | ✅ all tested 🧪 | Both fine |
| **Failing backend taken out of rotation?** | ✅ automatic cooldown 🧪 | ❌ not in the free version (circuit breaker is paid); about half the requests failed 🧪 | LiteLLM, or Bifrost Enterprise |
| **AWS Bedrock** | ✅ 🧪 | ✅ 🧪 (private endpoints need a TLS host name) | Both |
| **Azure OpenAI** | ✅ 🧪 | ✅ 🧪 | Both |
| **Azure AI Foundry** (Mistral, Llama…) | ✅ Foundry inference API 🧪 | ✅ via Azure's OpenAI v1 API 🧪 (real Foundry to confirm) | LiteLLM is the more direct fit |
| **On-prem** (vLLM, Ollama) | ✅ 🧪 | ✅ 🧪 | Both |
| **Cross-cloud fallback, EU Sovereign key** | ✅ 🧪 | ✅ 🧪 | Both |
| **Tool connectors (MCP)** | Per-key and per-tool access ✅ 🧪; **auto-run covers every tool** ❌ 🧪 | Per-key access ✅ 🧪; auto-run is a per-tool list ✅ 🧪 | **Don't use yet.** Gateways keep tokens per *user*; Genie needs per *user + context*, with tokens and approvals in the kernel |

**Bottom line:**
- **Routing:** both are capable, but only LiteLLM (free) stops sending traffic to a failing backend. Keep the routing *policy* in Genie.
- **Providers:** both reached AWS, Azure, Foundry and on-prem in the tests. LiteLLM uses Foundry's own API; Bifrost's route still needs checking against a real Foundry resource.
- **Connectors:** keep them in Genie's own connector service, and leave the gateways' MCP features off.
  - If they are ever used, Bifrost's per-tool auto-run is the safer design.
  - With LiteLLM, keep risky tools off the key.

**Test results:** groups 7–9 in this repo (`tests/test_7_routing.py`, `test_8_providers.py`, `test_9_mcp.py`). LiteLLM passed 15/16 and Bifrost 15/16. See the README for the full table.

---|---|---|---|
| **How is a model picked?** | Model groups + a strategy (random by weight, least busy, latency, cost); tags restrict it | Virtual key → CEL routing rules → weights | Genie's kernel decides; the gateway only balances and falls back **within** what the kernel allows |
| **Fallback?** | Yes, plus automatic cooldown of failing deployments | Yes; cooldown/circuit breaker is paid | Both fine; tested 🧪 |
| **EU-only routing?** | Tags (old region routing is deprecated) | Rules + allowed models per key | Use one key per compliance mode (tested 🧪) |
| **AWS Bedrock** | ✅ | ✅ | Both |
| **Azure AI Foundry** (Mistral, Llama…) | ✅ directly | ⚠️ not documented | LiteLLM is the safer fit |
| **On-prem** (vLLM, Ollama) | ✅ | ✅ | Both |
| **Tool connectors (MCP)** | Yes: OAuth, per-key access, can run tools | Yes: per-user OAuth, deny-by-default per key, agent mode | **Don't use yet.** Gateways keep tokens per *user*; Genie needs per *user + context*, with tokens and approvals in the kernel |

**Bottom line:**
- **Routing:** both are capable; keep the routing *policy* in Genie.
- **Providers:** LiteLLM covers AWS, Azure Foundry and on-prem most directly.
- **Connectors:** keep them in Genie's own connector service, and leave the gateways' MCP features off.

---

## 1. Model routing

### In one picture

```
LiteLLM:  request "model: eu-chat"
          → model group "eu-chat" (all deployments with that name)
          → pre-call checks (context window, tags/region)
          → routing strategy picks one deployment (shuffle by weight, least busy, latency, cost …)
          → on error: retries → cooldown → fallbacks to other groups

Bifrost:  request "model: eu-chat"
          → virtual key (which providers/models it may use)
          → routing rules (CEL expressions, first match wins)
          → virtual-key weights pick a provider (others become fallbacks)
          → model catalog fills in the provider if still unknown
          → key selection → on error: retries → fallback chain
```

### Side by side

| | LiteLLM | Bifrost |
|---|---|---|
| **Basic unit** | *Model group*: every deployment with the same `model_name` | *Provider* + *model*, chosen per *virtual key* |
| **Load balancing** | Strategies: `simple-shuffle` (default, recommended), `least-busy`, `latency-based-routing`, `usage-based-routing-v2`, `cost-based-routing`, or custom; plus `weight`, `rpm`/`tpm`, `order` per deployment | Weighted random across a virtual key's `provider_configs` and across provider keys. Performance-based ("adaptive") balancing is **Enterprise** |
| **Rule-based routing** | Tag routing: deployments carry `tags`, requests send `x-litellm-tags`; `&tag` = must match, `!tag` = exclude. Team-pinned tags are **Enterprise** | **Routing rules** with CEL expressions (headers, params, virtual key, team, budget used …), scoped to key/team/customer/global, each with its own fallbacks 🧪 |
| **Fallbacks** | `fallbacks`, plus `context_window_fallbacks` and content-policy fallbacks; `num_retries`, `RetryPolicy` per error type 🧪 | Fallback chain per rule or from remaining weighted providers; retries per provider (`max_retries`) 🧪 |
| **Unhealthy providers** | Automatic **cooldown** (default: 3 fails → 5 s out; also on 429/401/404) | Circuit breaker is **Enterprise**; OSS relies on retries and fallbacks |
| **Residency (EU-only)** | `region_name` + `enable_pre_call_checks` (old "region-based routing", now **deprecated** in favour of tags) → use tags such as `region:eu` with `&region:eu`. Docs warn `allow_fail_open` can bypass an exclusion | Rules/keys restrict providers and models; a disallowed fallback is refused (`403 provider_blocked / model_blocked`) 🧪 |
| **Smart (cheap vs. strong model) routing** | `auto_router/complexity_router`: tiers SIMPLE/MEDIUM/COMPLEX/REASONING mapped to models; semantic router deprecated. LiteLLM's own benchmark found the rule-based score weakly predictive | `complexity_analyzer_config`: lexical tiers, keywords, optional embedding (semantic) or LLM classifier, session-aware |
| **Shared state across instances** | Redis required for exact limits/cooldowns across workers 🧪 | Built in to the process; multi-node clustering is **Enterprise** |

### What this means for Genie

Genie's architecture puts the **decision** in the kernel. The kernel filters models by compliance mode and picks fast or deliberate. The gateway should only **carry out** that decision safely:

| Genie need | LiteLLM | Bifrost |
|---|---|---|
| Kernel chooses the model; gateway only serves it | Send the exact model group name | Send the exact model name |
| Fallback **within the same residency** only | Fallbacks between groups of the same residency | Fallback chain on a routing rule per residency |
| Second lock: one key per compliance mode | Key with `models: [...]` 🧪 | Virtual key with `allowed_models` 🧪 |
| Fast vs. deliberate (MVP: rules) | Keep in the kernel (rules); optionally `complexity_router` later | Keep in the kernel; optionally `complexity_analyzer_config` later |

**Recommendation:** keep routing **policy** in Genie's kernel. Use the gateway for load balancing, retries and fallback **within** the candidates the kernel allows. Don't let the gateway's automatic routing pick models across residencies.

---

## 2. Provider connectors: AWS, Azure AI Foundry, on-prem

| Provider | LiteLLM | Bifrost |
|---|---|---|
| **AWS Bedrock** | `model: bedrock/<model-id>` (Converse); IAM role or keys; cross-region and application inference profile ARNs; guardrails passthrough | `bedrock` provider: `region`; IAM default chain, static keys, STS AssumeRole or API key; inference profiles; prompt caching. The docs don't mention `eu.` profile IDs; test before relying on them |
| **Azure OpenAI** | `model: azure/<deployment>` | `azure` provider: `endpoint`; Entra ID, API key, or managed identity (DefaultAzureCredential); deployments via `aliases` |
| **Azure AI Foundry** (non-OpenAI models) | `model: azure_ai/<model>` with `api_base`; examples include Mistral, Cohere, Claude on Foundry | Docs cover OpenAI and Claude via Azure. In the tests a Mistral model was reached through the `azure` provider (Azure's OpenAI v1 API) 🧪; whether a real Foundry resource accepts that still needs checking |
| **On-prem** (vLLM, Ollama, TGI) | `hosted_vllm/…`, `ollama/…`, or any OpenAI-compatible `api_base` 🧪 (the fake provider is one) | `vllm`, `ollama` providers, or a **custom provider** with `base_provider_type: openai` and your `base_url` 🧪 |
| **Same provider twice** (e.g. Azure EU + Azure US) | Two deployments with different `api_base`, same or different `model_name` | Custom providers: several named instances of one base type (`openai-west`, `openai-east`) |
| **Coverage** | 100+ providers | ~30 built-in, plus custom OpenAI-compatible |

**For a three-provider Genie (AWS + Azure Foundry + on-prem):** both routed to all three in the tests 🧪, including an Azure EU → AWS EU fallback.
- **LiteLLM** supports Azure AI Foundry's non-OpenAI models directly.
- With **Bifrost**, Foundry's Mistral or Llama models would go through the custom-provider route, which still has to be tested.

---

## 3. Tool connectors (MCP): Gmail, Drive and other external systems

Both gateways can act as an **MCP gateway**. They connect to MCP servers and expose those tools to the model.

| | LiteLLM MCP Gateway | Bifrost MCP Gateway |
|---|---|---|
| **Add a server** | `mcp_servers:` in config (or UI/API with DB storage): `url`, `transport` (`http`, `sse`, `stdio`*) | `mcp.client_configs`: `connection_type` (`http`, `sse`, `stdio`, `openapi` beta), `connection_string` |
| **Auth to the external system** | `auth_type`: api key, bearer, basic, AWS SigV4, **OAuth 2.0** (`client_credentials` or per-user `authorization_code`), token passthrough, token exchange | `auth_type`: headers, **OAuth**, **per-user OAuth**, per-user headers; admin must verify config-declared servers |
| **Who may use which tools** | Permissions per key / team / org; `allow_all_keys`; clients pick servers with `x-mcp-servers`; tools prefixed by server name | Per virtual key `mcp_configs` with `tools_to_execute` (**deny by default**); filtering at client, request and key level |
| **Tool execution** | Proxy runs tools itself if `require_approval: "never"`, otherwise returns tool calls to the client | Tools auto-injected into requests by default (can disable); **Agent Mode** auto-runs tools listed in `tools_to_auto_execute`; **Code Mode** lets the model script several tools |
| **Paid tier** | No MCP features labelled Enterprise | "Virtual MCPs" (curated tool bundles) are **Enterprise** |
| **Security notes** | CVE-2026-59822 (MCP auth bypass, fixed in 1.84.0); `stdio` only with an env flag | Not tested here |

\* `stdio` is off unless the process sets `LITELLM_ENABLE_MCP_STDIO=true`.

### What this means for Genie

Genie's architecture has its own **connector service**. It works per **account + context**, keeps credentials in a vault the kernel controls, gives plugins only a credential handle, and enforces "a connection never crosses contexts".

- The gateways' per-user OAuth stores tokens **per user**, not per user **and context**. The same person's personal and professional Gmail connections would need two separate identities.
- With the gateway executing tools, the tokens and the policy step (approval for write or irreversible actions) would sit **inside the gateway** instead of the kernel.

**Recommendation:**
- **MVP and Phase 2:** keep connectors in Genie's **connector service and plugin host**, as the architecture says. Leave the gateway's MCP features **switched off**, and use the gateway for model traffic only.
- **Later, if wanted:** use the gateway's MCP support only as a **transport** (tool discovery and calls), with Genie still owning tokens, per-context separation and approvals. Re-check both gateways' MCP security before doing so.

---

## Sources
- LiteLLM: [Routing](https://docs.litellm.ai/docs/routing) · [Tag routing](https://docs.litellm.ai/docs/proxy/tag_routing) · [Region-based routing (deprecated)](https://docs.litellm.ai/docs/proxy/customer_routing) · [Auto routing](https://docs.litellm.ai/docs/proxy/auto_routing_semantic) · [Azure AI Foundry](https://docs.litellm.ai/docs/providers/azure_ai) · [Bedrock](https://docs.litellm.ai/docs/providers/bedrock) · [MCP Gateway](https://docs.litellm.ai/docs/mcp)
- Bifrost: [Provider routing order](https://docs.getbifrost.ai/providers/provider-routing.md) · [Custom providers](https://docs.getbifrost.ai/providers/custom-providers.md) · [Bedrock](https://docs.getbifrost.ai/providers/supported-providers/bedrock.md) · [Azure](https://docs.getbifrost.ai/providers/supported-providers/azure.md) · [Connecting MCP servers](https://docs.getbifrost.ai/mcp/connecting-to-servers.md) · [Enterprise features](https://docs.getbifrost.ai/enterprise/overview.md) · config schema `transports/config.schema.json` (v2.2.6)
