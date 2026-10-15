"""Security: who can call the gateway, who can reconfigure it, and is the build pinned.

Runs against three targets:
  litellm        LiteLLM Proxy with a master key (its standard setup)
  mlflow         MLflow AI Gateway as installed (no login)
  mlflow_login   MLflow with its optional login add-on ("--app-name basic-auth", -full image),
                 called as a non-admin user who may only use the allowed endpoints
"""

import json
import subprocess

import httpx
import pytest

from conftest import TARGETS, mock_calls, reset_mock

MSG = [{"role": "user", "content": "hi"}]
OWN_GW = True
SEC_TARGETS = {**TARGETS["gateways"], **TARGETS.get("security_only", {})}


@pytest.fixture(params=list(SEC_TARGETS))
def gw(request):  # overrides conftest's fixture: this file also covers mlflow_login
    return {"name": request.param, **SEC_TARGETS[request.param]}


def _chat(gw, auth_header, model="fast-model"):
    headers = {"Authorization": auth_header} if auth_header else {}
    return httpx.post(gw["base_url"] + "/chat/completions", headers=headers, timeout=30,
                      json={"model": model, "messages": MSG})


def test_requires_key(gw, note):
    """Calls without a key are refused"""
    r = _chat(gw, None)
    note("status without key", r.status_code)
    assert r.status_code in (401, 403)


def test_wrong_key_refused(gw, note):
    """Calls with a wrong key are refused"""
    wrong = "Basic YXBwLXVzZXI6d3Jvbmc=" if gw["name"] == "mlflow_login" else "Bearer sk-wrong"
    r = _chat(gw, wrong)
    note("status with wrong key", r.status_code)
    assert r.status_code in (401, 403)


def test_per_key_model_restriction(gw, note):
    """A key can be limited to certain models (e.g. one key per compliance mode)"""
    if gw["name"] == "mlflow":
        pytest.fail("no keys or users without the login add-on, so no per-model limits")
    if gw["name"] == "litellm":
        r = httpx.post(gw["root_url"] + "/key/generate", timeout=30,
                       headers={"Authorization": gw["auth_header"]},
                       json={"models": ["fast-model"], "key_alias": "eu-mode-key"})
        r.raise_for_status()
        auth = f"Bearer {r.json()['key']}"
        note("how", "virtual key limited to fast-model")
    elif gw["name"] == "bifrost":
        auth = f"Bearer {gw['limited_key']}"
        note("how", "virtual key limited to fast-model (from config)")
    else:
        auth = gw["auth_header"]
        note("how", "user with a role that may USE every endpoint except outside-model")
    reset_mock()
    allowed, blocked = _chat(gw, auth, "fast-model"), _chat(gw, auth, "outside-model")
    reached_provider = "outside-model" in {c["model"] for c in mock_calls()}
    note("allowed model", allowed.status_code)
    note("other model", blocked.status_code)
    note("other model reached the provider", reached_provider)
    # Blocked = refused with a 4xx AND never forwarded (status codes differ per gateway)
    assert allowed.status_code == 200
    assert 400 <= blocked.status_code < 500 and not reached_provider


def test_management_api_locked(gw, note):
    """Gateway configuration can't be read or changed without logging in"""
    if gw["name"] == "litellm":
        r = httpx.get(gw["root_url"] + "/model/info", timeout=30)
        note("read model config without key", r.status_code)
        assert r.status_code in (401, 403)
        return
    if gw["name"] == "bifrost":
        listed = httpx.get(gw["root_url"] + "/api/providers", timeout=30)
        changed = httpx.post(gw["root_url"] + "/api/providers", timeout=30,
                             json={"provider": "anthropic", "keys": [{"value": "x"}]})
        note("list providers without login", listed.status_code)
        note("add a provider without login", changed.status_code)
        assert listed.status_code in (401, 403) and changed.status_code in (401, 403)
        return
    list_url = gw["root_url"] + "/ajax-api/3.0/mlflow/gateway/endpoints/list"
    create_url = gw["root_url"] + "/ajax-api/3.0/mlflow/gateway/secrets/create"
    secret = {"secret_name": "attacker-secret", "secret_value": {"api_key": "x"},
              "provider": "openai"}
    listed = httpx.get(list_url, timeout=30)
    created = httpx.post(create_url, json=secret, timeout=30)
    note("list endpoints without login", listed.status_code)
    note("create a provider secret without login", created.status_code)
    assert listed.status_code in (401, 403) and created.status_code in (401, 403)


def test_app_credential_cannot_reconfigure(gw, note):
    """The app's own (non-admin) credential can't change gateway configuration"""
    if gw["name"] == "mlflow":
        pytest.fail("there is no separate app credential: anyone can change the configuration")
    if gw["name"] == "litellm":
        r = httpx.post(gw["root_url"] + "/key/generate", timeout=30,
                       headers={"Authorization": gw["auth_header"]},
                       json={"models": ["fast-model"], "key_alias": "app-key"})
        r.raise_for_status()
        app = {"Authorization": f"Bearer {r.json()['key']}"}
        add_model = httpx.post(gw["root_url"] + "/model/new", headers=app, timeout=30, json={
            "model_name": "rogue", "litellm_params": {"model": "openai/x",
                                                      "api_base": "https://attacker.example"}})
        new_key = httpx.post(gw["root_url"] + "/key/generate", headers=app, timeout=30, json={})
        note("add a model with the app key", add_model.status_code)
        note("create a new key with the app key", new_key.status_code)
        assert add_model.status_code in (401, 403) and new_key.status_code in (401, 403)
    elif gw["name"] == "bifrost":
        app = {"Authorization": gw["auth_header"]}
        add = httpx.post(gw["root_url"] + "/api/providers", headers=app, timeout=30,
                         json={"provider": "anthropic", "keys": [{"value": "x"}],
                               "network_config": {"base_url": "https://attacker.example"}})
        vk = httpx.post(gw["root_url"] + "/api/governance/virtual-keys", headers=app,
                        timeout=30, json={"name": "rogue"})
        note("add a provider with the app's virtual key", add.status_code)
        note("create a virtual key with the app's virtual key", vk.status_code)
        assert add.status_code in (401, 403) and vk.status_code in (401, 403)
    else:
        app = {"Authorization": gw["auth_header"]}
        create = httpx.post(gw["root_url"] + "/ajax-api/3.0/mlflow/gateway/secrets/create",
                            headers=app, timeout=30,
                            json={"secret_name": "app-made-secret",
                                  "secret_value": {"api_key": "x"}, "provider": "openai",
                                  "auth_config": {"api_base": "https://attacker.example/v1"}})
        note("create a provider secret as the non-admin app user", create.status_code)
        assert create.status_code in (401, 403), \
            "any logged-in user can add provider credentials and destinations"


def test_admin_ui_locked(gw, note):
    """The admin UI can be switched off or requires login"""
    if gw["name"] == "litellm":
        r = httpx.get(gw["root_url"] + "/sso/key/generate", timeout=30)
        disabled = "Admin UI is Disabled" in r.text
        note("login route says 'Admin UI is Disabled'", disabled)
        ui = httpx.get(gw["root_url"] + "/ui/", timeout=30, follow_redirects=True)
        note("empty dashboard page still served at /ui (no login possible)", ui.status_code == 200)
        assert disabled
    elif gw["name"] == "bifrost":
        api = httpx.get(gw["root_url"] + "/api/config", timeout=30)
        ui = httpx.get(gw["root_url"] + "/", timeout=30)
        note("admin API without login", api.status_code)
        note("empty UI page still served at / (login required for data)", ui.status_code == 200)
        assert api.status_code in (401, 403)
    else:
        r = httpx.get(gw["root_url"] + "/", timeout=30)
        note("UI status without login", r.status_code)
        assert r.status_code in (401, 403), "the MLflow UI (including gateway settings) is open"


def _container(gw):
    return next(s["container"] for s in gw["sinks"] if s["type"] == "logs")


def test_image_pinned(gw, note):
    """Runs from an image pinned by digest, with no known malware startup file"""
    expected = TARGETS["images"][gw["name"]]
    running = subprocess.run(["docker", "inspect", "--format", "{{.Config.Image}}", _container(gw)],
                             capture_output=True, text=True).stdout.strip()
    note("image", running.split("@")[0])
    scan = ("import site,glob,os,json;"
            "print(json.dumps(sorted({os.path.basename(p) for d in site.getsitepackages() "
            "for p in glob.glob(os.path.join(d,'*.pth'))})))")
    if gw["name"] == "bifrost":
        pth = []  # a single Go binary: no Python packages that could carry .pth files
        note("Python startup files", "n/a (Go binary)")
    else:
        out = subprocess.run(["docker", "exec", _container(gw), "python", "-c", scan],
                             capture_output=True, text=True).stdout.strip()
        pth = json.loads(out or "[]")
        note("litellm_init.pth (March 2026 malware) present", "litellm_init.pth" in pth)
    assert running == expected and "@sha256:" in running
    assert "litellm_init.pth" not in pth
