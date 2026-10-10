"""Provider-first setup wizard + the protocol-aware connectivity probe."""

from __future__ import annotations

import lithe_cli.setup as setup
from lithe_cli.profiles import ProfileStore


class _Script:
    """Feed the wizard canned answers, mimicking prompts.ask/choose."""

    def __init__(self, asks, chooses):
        self._asks = iter(asks)
        self._chooses = iter(chooses)

    def ask(self, label, default="", password=False, validate=None):
        answer = next(self._asks)
        if answer is None:
            return None  # scripted cancel (Ctrl+C / bail-out)
        if not answer and default:
            return default  # what a bare Enter does in the real prompt
        return answer

    def choose(self, label, choices, default=""):
        answer = next(self._chooses)
        return answer or default


def _run_wizard(tmp_path, monkeypatch, *, asks, chooses, probe=False):
    monkeypatch.setenv("LITHE_HOME", str(tmp_path))
    for var in ("LITHE_API_KEY", "LITHE_BASE_URL", "LITHE_MODEL",
                "LITHE_PROVIDER"):
        monkeypatch.delenv(var, raising=False)
    script = _Script(asks, chooses)
    monkeypatch.setattr(setup.prompts, "ask", script.ask)
    monkeypatch.setattr(setup.prompts, "choose", script.choose)
    monkeypatch.setattr(setup.prompts, "yes_no", lambda *a, **k: probe)
    monkeypatch.setattr(setup, "stdin_is_interactive", lambda: True)
    return setup.run_setup_wizard()


def test_wizard_custom_router_keeps_own_base_url(tmp_path, monkeypatch):
    """自建 openrouter 格式路由：provider 定方言，URL/key/model 自己的。"""
    result = _run_wizard(
        tmp_path, monkeypatch,
        asks=["myrouter",                 # 档案名
              "https://my-router/api/v1",  # base_url（不用官方默认）
              "sk-or",                     # api_key
              "claude-sonnet-4"],          # model
        chooses=["openrouter"])
    assert result["provider"] == "openrouter"
    ep = ProfileStore().endpoint("myrouter")
    assert ep["provider"] == "openrouter"
    assert ep["base_url"] == "https://my-router/api/v1"
    assert ep["api_key"] == "sk-or"
    assert ep["model"] == "claude-sonnet-4"


def test_wizard_preset_fills_official_base_url(tmp_path, monkeypatch):
    """anthropic 档案：base_url 留空回车 → preset 官方 URL 落盘。"""
    result = _run_wizard(
        tmp_path, monkeypatch,
        asks=["claude",            # 档案名
              "",                  # base_url：回车沿用 preset 默认
              "sk-ant",            # api_key
              "claude-sonnet-4"],  # model
        chooses=["anthropic"])
    assert result["base_url"] == "https://api.anthropic.com/v1"
    assert result["provider"] == "anthropic"
    ep = ProfileStore().endpoint("claude")
    assert ep["provider"] == "anthropic"
    assert ep["base_url"] == "https://api.anthropic.com/v1"


def test_wizard_none_clears_existing_provider(tmp_path, monkeypatch):
    monkeypatch.setenv("LITHE_HOME", str(tmp_path))
    store = ProfileStore()
    store.upsert("old", "https://z.example/api", "sk", "glm-4.6",
                 provider="zai")

    result = _run_wizard(
        tmp_path, monkeypatch,
        asks=["old",        # 编辑既有档案
              "",           # base_url：回车沿用已保存值
              "sk",         # api_key
              "glm-4.7"],   # model
        chooses=["none"])
    assert "provider" not in result
    ep = ProfileStore().endpoint("old")
    assert "provider" not in ep, "显式选 none 必须降级为无 preset 档案"
    assert ep["base_url"] == "https://z.example/api"
    assert ep["model"] == "glm-4.7"


def test_wizard_edit_defaults_to_active_profile_and_keeps_provider(
        tmp_path, monkeypatch):
    """回车穿越全部问题 = 原样重存 active 档案，provider 等字段保留。"""
    monkeypatch.setenv("LITHE_HOME", str(tmp_path))
    store = ProfileStore()
    store.upsert("zhipu", "https://z.example/api", "sk-z", "glm-4.6",
                 provider="zai")

    result = _run_wizard(
        tmp_path, monkeypatch,
        asks=["", "", "", ""],  # 档案名/base_url/key/model 全部回车
        chooses=[""])
    assert result["provider"] == "zai"
    ep = ProfileStore().endpoint("zhipu")
    assert ep == {"api_key": "sk-z", "base_url": "https://z.example/api",
                  "model": "glm-4.6", "provider": "zai"}


def test_wizard_first_run_env_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("LITHE_HOME", str(tmp_path))
    monkeypatch.setenv("LITHE_BASE_URL", "https://env.example/api")
    monkeypatch.setenv("LITHE_API_KEY", "sk-env")
    monkeypatch.setenv("LITHE_MODEL", "env-model")
    monkeypatch.setenv("LITHE_PROVIDER", "zai")

    script = _Script(["", "", "", ""], [""])
    monkeypatch.setattr(setup.prompts, "ask", script.ask)
    monkeypatch.setattr(setup.prompts, "choose", script.choose)
    monkeypatch.setattr(setup.prompts, "yes_no", lambda *a, **k: False)
    monkeypatch.setattr(setup, "stdin_is_interactive", lambda: True)
    result = setup.run_setup_wizard()

    assert result["provider"] == "zai"
    assert result["base_url"] == "https://env.example/api"
    ep = ProfileStore().endpoint("default")
    assert ep["api_key"] == "sk-env" and ep["model"] == "env-model"


def test_wizard_cancel_saves_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("LITHE_HOME", str(tmp_path))
    script = _Script([None], [])  # 档案名即取消
    monkeypatch.setattr(setup.prompts, "ask", script.ask)
    monkeypatch.setattr(setup.prompts, "choose", script.choose)
    monkeypatch.setattr(setup, "stdin_is_interactive", lambda: True)
    assert setup.run_setup_wizard() is None
    assert ProfileStore().names() == []


class _ProbeResp:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_probe_is_protocol_aware(monkeypatch):
    seen = {}

    def fake_urlopen(req, timeout=10.0):
        seen["url"] = req.full_url
        seen["headers"] = dict(req.headers)
        return _ProbeResp()

    monkeypatch.setattr(setup, "urlopen", fake_urlopen)
    assert setup.probe_endpoint("https://api.anthropic.com/v1", "sk-a",
                                provider="anthropic")
    assert seen["url"].endswith("/models?limit=1000")
    headers = {k.lower(): v for k, v in seen["headers"].items()}
    assert headers.get("x-api-key") == "sk-a"
    assert headers.get("anthropic-version") == "2023-06-01"
    assert "authorization" not in headers

    # chat 家族（或无 provider）保持 Bearer 探测
    assert setup.probe_endpoint("https://z.example/api", "sk-z")
    assert seen["url"] == "https://z.example/api/models"
    assert any(k.lower() == "authorization" for k in seen["headers"])
