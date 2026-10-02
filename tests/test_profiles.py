"""Profile store: v2 file format, v1 translation, selection, model cache."""

from __future__ import annotations

import json

import pytest

from lithe_cli.profiles import (
    DEFAULT_PROFILE,
    ProfileStore,
    fetch_models,
    masked_key,
)


def test_v1_flat_file_translates_without_rewrite(tmp_path, monkeypatch):
    from lithe_cli.config import load_saved_endpoint

    monkeypatch.setenv("LITHE_HOME", str(tmp_path))
    raw = {"api_key": "sk-old", "base_url": "https://old.example/api",
           "model": "old-model"}
    (tmp_path / "config.json").write_text(
        json.dumps(raw), encoding="utf-8")

    store = ProfileStore()
    assert store.names() == [DEFAULT_PROFILE]
    assert store.active_name() == DEFAULT_PROFILE
    assert store.endpoint() == {**raw}
    assert load_saved_endpoint() == raw
    # in-memory translation only: the file keeps its flat v1 shape
    on_disk = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert on_disk == raw


def test_upsert_set_active_set_model_delete_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("LITHE_HOME", str(tmp_path))
    store = ProfileStore()

    store.upsert("zhipu", "https://z.example/api", "sk-z", "glm-4.6", 128000)
    store.upsert("openrouter", "https://or.example/api", "sk-or", "sonnet-4")
    assert store.names() == ["zhipu", "openrouter"]
    # first upsert set the only-then-empty active marker
    assert store.active_name() == "zhipu"

    assert store.set_active("openrouter")
    assert store.active_name() == "openrouter"
    assert store.endpoint()["model"] == "sonnet-4"

    assert store.set_model("zhipu", "glm-4.5-air")
    assert store.endpoint("zhipu")["model"] == "glm-4.5-air"

    # deleting the active profile falls back to a surviving one
    assert store.delete("openrouter")
    assert store.active_name() == "zhipu"
    assert store.names() == ["zhipu"]

    assert not store.set_active("ghost")
    assert not store.set_model("ghost", "m")
    assert not store.delete("ghost")

    # the file on disk is v2 with a masked-key-safe structure
    data = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert data["version"] == 2 and data["active"] == "zhipu"
    assert data["profiles"]["zhipu"]["context_window"] == 128000
    # masked listing never exposes the whole key
    assert masked_key("sk-1234567890") == "sk-12…7890"
    assert masked_key("") == ""
    assert store.masked("zhipu")["api_key"] == "sk-z"  # short keys stay


def test_unknown_selected_profile_exits_loudly(tmp_path, monkeypatch):
    monkeypatch.setenv("LITHE_HOME", str(tmp_path))
    store = ProfileStore()
    store.upsert("a", "https://a.example", "sk-a", "m-a")
    with pytest.raises(SystemExit) as e:
        store.endpoint("nope")
    assert "nope" in str(e.value.code) and "a" in str(e.value.code)


def test_invalid_profile_name_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("LITHE_HOME", str(tmp_path))
    with pytest.raises(SystemExit):
        ProfileStore().upsert("bad name!", "https://x", "k", "m")


def test_fetch_models_parses_openai_shape(monkeypatch):
    import lithe_cli.profiles as profiles

    class Resp:
        def __init__(self, payload):
            self.payload = payload

        def read(self):
            return json.dumps(self.payload).encode()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_urlopen(req, timeout=10.0):
        assert req.full_url.endswith("/models")
        return Resp({"data": [{"id": "glm-4.6"}, {"id": "glm-4.5-air"}]})

    monkeypatch.setattr(profiles, "urlopen", fake_urlopen)
    assert fetch_models("https://z.example/api", "sk") == [
        "glm-4.6", "glm-4.5-air",
    ]

    def broken(req, timeout=10.0):
        raise OSError("no route")

    monkeypatch.setattr(profiles, "urlopen", broken)
    assert fetch_models("https://z.example/api", "sk") is None
    # malformed 200 (no data list) is also None, not a crash
    monkeypatch.setattr(
        profiles, "urlopen", lambda req, timeout=10.0: Resp({"oops": 1})
    )
    assert fetch_models("https://z.example/api", "sk") is None


def test_cache_models_roundtrip_and_upsert_keeps_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("LITHE_HOME", str(tmp_path))
    store = ProfileStore()
    store.upsert("zhipu", "https://z.example/api", "sk", "glm-4.6")
    store.cache_models("zhipu", ["glm-4.6", "glm-4.5-air"])

    fresh = ProfileStore()  # re-read from disk
    assert fresh.endpoint("zhipu")["cached_models"] == ["glm-4.6", "glm-4.5-air"]
    # re-upserting with the same base_url keeps the cached list
    fresh.upsert("zhipu", "https://z.example/api", "sk2", "glm-4.5-air")
    assert fresh.endpoint("zhipu")["cached_models"] == ["glm-4.6", "glm-4.5-air"]
    # a different base_url invalidates it
    fresh.upsert("zhipu", "https://other.example/api", "sk2", "m")
    assert "cached_models" not in fresh.endpoint("zhipu")


def test_load_config_resolves_profile_layers(tmp_path, monkeypatch):
    from lithe_cli.config import ENV_MODEL, ENV_PROFILE, load_config

    monkeypatch.setenv("LITHE_HOME", str(tmp_path))
    for var in ("LITHE_API_KEY", "LITHE_BASE_URL", "LITHE_MODEL",
                "LITHE_PROFILE"):
        monkeypatch.delenv(var, raising=False)
    ProfileStore().upsert("zhipu", "https://z.example/api", "sk-z",
                          "glm-4.6", 200000)
    ProfileStore().upsert("or", "https://or.example/api", "sk-or", "sonnet")

    class Args:
        api_key = base_url = model = profile = None
        store = workspace = user = None
        skills = mcp = None
        context_window = None
        download = code = shell = vision = color = no_color = verbose = False

    # active profile fills everything, including context_window
    cfg = load_config(Args())
    assert cfg.profile == "zhipu" and cfg.model == "glm-4.6"
    assert cfg.has_endpoint and cfg.context_window == 200000

    # LITHE_PROFILE selects another profile; per-key env still beats it
    monkeypatch.setenv(ENV_PROFILE, "or")
    monkeypatch.setenv(ENV_MODEL, "env-model")
    cfg = load_config(Args())
    assert cfg.profile == "or" and cfg.base_url == "https://or.example/api"
    assert cfg.model == "env-model" and cfg.api_key == "sk-or"

    # --profile flag beats the env var; unknown names die loudly
    # (per-key env still pins single fields, so clear the model pin first)
    monkeypatch.delenv(ENV_MODEL, raising=False)
    monkeypatch.delenv(ENV_PROFILE, raising=False)
    Args.profile = "zhipu"
    cfg = load_config(Args())
    assert cfg.profile == "zhipu" and cfg.model == "glm-4.6"
    Args.profile = "ghost"
    with pytest.raises(SystemExit):
        load_config(Args())


def test_save_endpoint_preserves_other_profiles(tmp_path, monkeypatch):
    from lithe_cli.config import load_saved_endpoint, save_endpoint

    monkeypatch.setenv("LITHE_HOME", str(tmp_path))
    store = ProfileStore()
    store.upsert("zhipu", "https://z.example/api", "sk-z", "glm-4.6")
    store.upsert("or", "https://or.example/api", "sk-or", "sonnet")
    store.set_active("zhipu")

    # the (wizard-era) flat save edits the ACTIVE profile only
    save_endpoint("sk-new", "https://z2.example/api", "glm-4.7")
    assert load_saved_endpoint() == {
        "api_key": "sk-new",
        "base_url": "https://z2.example/api",
        "model": "glm-4.7",
    }
    # the other profile survived untouched
    fresh = ProfileStore()
    assert fresh.endpoint("or")["api_key"] == "sk-or"
    assert fresh.names() == ["zhipu", "or"]


def test_corrupt_profile_file_reads_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("LITHE_HOME", str(tmp_path))
    (tmp_path / "config.json").write_text("{oops", encoding="utf-8")
    assert ProfileStore().names() == []
    assert ProfileStore().endpoint() == {}


# --- provider presets -------------------------------------------------------

class _PresetArgs:
    api_key = base_url = model = profile = provider = None
    store = workspace = user = None
    skills = mcp = None
    context_window = None
    download = code = shell = vision = color = no_color = verbose = False


def test_provider_preset_fills_base_url_and_build_llm(tmp_path, monkeypatch):
    from lithe_cli.agent import build_llm
    from lithe_cli.config import ENV_PROVIDER, load_config

    monkeypatch.setenv("LITHE_HOME", str(tmp_path))
    for var in ("LITHE_API_KEY", "LITHE_BASE_URL", "LITHE_MODEL",
                "LITHE_PROFILE", ENV_PROVIDER):
        monkeypatch.delenv(var, raising=False)
    # 档案只写 provider + 凭据 + 模型：base_url 由 preset 补齐
    ProfileStore().upsert("myzai", "", "sk-z", "glm-4.6", provider="zai")

    cfg = load_config(_PresetArgs())
    assert cfg.provider == "zai"
    assert cfg.base_url == "https://open.bigmodel.cn/api/paas/v4"
    assert cfg.has_endpoint, "preset 补齐 base_url 后即视为完整端点"

    llm = build_llm(cfg)
    assert llm.base_url == "https://open.bigmodel.cn/api/paas/v4"
    assert llm.model == "glm-4.6" and llm.transport == "chat"


def test_provider_preset_explicit_values_win(tmp_path, monkeypatch):
    from lithe_cli.agent import build_llm
    from lithe_cli.config import ENV_PROVIDER, load_config

    monkeypatch.setenv("LITHE_HOME", str(tmp_path))
    for var in ("LITHE_API_KEY", "LITHE_BASE_URL", "LITHE_MODEL",
                "LITHE_PROFILE", ENV_PROVIDER):
        monkeypatch.delenv(var, raising=False)
    # 档案手写了 base_url：显式值覆盖 preset
    ProfileStore().upsert("proxy", "https://my-proxy.example/v1", "sk-z",
                          "glm-4.6", provider="zai", context_window=128000)

    cfg = load_config(_PresetArgs())
    assert cfg.base_url == "https://my-proxy.example/v1"
    llm = build_llm(cfg)
    assert llm.base_url == "https://my-proxy.example/v1"
    assert llm.context_window == 128000


def test_provider_via_env_beats_profile_field(tmp_path, monkeypatch):
    from lithe_cli.config import ENV_PROVIDER, load_config

    monkeypatch.setenv("LITHE_HOME", str(tmp_path))
    for var in ("LITHE_API_KEY", "LITHE_BASE_URL", "LITHE_MODEL",
                "LITHE_PROFILE"):
        monkeypatch.delenv(var, raising=False)
    ProfileStore().upsert("a", "", "sk", "m", provider="zai")
    monkeypatch.setenv(ENV_PROVIDER, "deepseek")
    cfg = load_config(_PresetArgs())
    assert cfg.provider == "deepseek"
    assert cfg.base_url == "https://api.deepseek.com"


def test_unknown_provider_dies_loudly(tmp_path, monkeypatch):
    from lithe_cli.config import ENV_PROVIDER, load_config

    monkeypatch.setenv("LITHE_HOME", str(tmp_path))
    for var in ("LITHE_API_KEY", "LITHE_BASE_URL", "LITHE_MODEL",
                "LITHE_PROFILE", ENV_PROVIDER):
        monkeypatch.delenv(var, raising=False)
    ProfileStore().upsert("bad", "", "sk", "m", provider="nonexistent")
    with pytest.raises(SystemExit, match="未知 provider"):
        load_config(_PresetArgs())


def test_endpoint_roundtrips_provider_field(tmp_path, monkeypatch):
    monkeypatch.setenv("LITHE_HOME", str(tmp_path))
    store = ProfileStore()
    store.upsert("zai", "", "sk", "glm-4.6", provider="zai")
    fresh = ProfileStore()  # re-read from disk
    assert fresh.endpoint("zai")["provider"] == "zai"
    # 无 provider 的档案不受影响
    store.upsert("plain", "https://p.example", "sk", "m")
    assert "provider" not in fresh.endpoint("plain")
