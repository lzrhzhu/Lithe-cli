"""Profile store: v2 file format, v1 translation, selection, model cache."""

from __future__ import annotations

import json

import pytest

from lithe_cli.profiles import (
    DEFAULT_PROFILE,
    ProfileStore,
    fetch_models,
    fetch_models_for,
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


def test_upsert_preserves_fields_the_caller_does_not_know(tmp_path, monkeypatch):
    """A wizard-era save must not erase provider/reasoning/context fields."""
    monkeypatch.setenv("LITHE_HOME", str(tmp_path))
    store = ProfileStore()
    # hand-tuned profile with every preserved-field flavor
    (tmp_path / "config.json").write_text(
        '{"version": 2, "active": "zai", "profiles": {"zai": {'
        '"api_key": "sk-old", "base_url": "https://z.example/api", '
        '"model": "glm-4.6", "provider": "zai", '
        '"reasoning_effort": "high", "document_format": "none", '
        '"context_window": 128000, "cached_models": ["glm-4.6"]}}}',
        encoding="utf-8")

    # the wizard triple re-config: same base_url, new key/model
    store.upsert("zai", "https://z.example/api", "sk-new", "glm-4.7")
    ep = store.endpoint("zai")
    assert ep["api_key"] == "sk-new" and ep["model"] == "glm-4.7"
    assert ep["provider"] == "zai"
    assert ep["reasoning_effort"] == "high"
    assert ep["document_format"] == "none"
    assert ep["context_window"] == 128000
    assert ep["cached_models"] == ["glm-4.6"]  # same endpoint → list kept


def test_save_is_atomic_and_leaves_recoverable_bak(tmp_path, monkeypatch):
    """Crash-safety: the previous file survives as .bak and serves as the
    load-time fallback when the main file is torn/corrupt."""
    monkeypatch.setenv("LITHE_HOME", str(tmp_path))
    store = ProfileStore()
    store.upsert("a", "https://a.example/api", "sk-a", "m-a")
    store.upsert("b", "https://b.example/api", "sk-b", "m-b")
    store.set_active("a")  # third save: .bak now holds both profiles

    bak = tmp_path / "config.json.bak"
    assert bak.exists(), "previous version must survive as .bak"
    import json as _json
    bak_profiles = _json.loads(bak.read_text(encoding="utf-8"))["profiles"]
    assert "sk-a" in bak_profiles["a"]["api_key"]
    assert "sk-b" in bak_profiles["b"]["api_key"]

    # a torn main write (crash mid-save) falls back to the .bak content
    (tmp_path / "config.json").write_text('{"version": 2, "act', encoding="utf-8")
    recovered = ProfileStore()
    assert sorted(recovered.names()) == ["a", "b"]
    assert recovered.endpoint("b")["api_key"] == "sk-b"

    # a corrupt main file with no usable .bak still reads empty, not raises
    bak.unlink()
    assert ProfileStore().names() == []
    # no temp litter left behind
    assert not (tmp_path / "config.json.tmp").exists()


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


def test_set_provider_roundtrip_case_and_clear(tmp_path, monkeypatch):
    from lithe.bundles.providers import known_providers

    monkeypatch.setenv("LITHE_HOME", str(tmp_path))
    store = ProfileStore()
    store.upsert("a", "https://a.example/api", "sk", "m")

    assert store.set_provider("a", "Anthropic")  # 大小写无关
    assert ProfileStore().endpoint("a")["provider"] == "anthropic"
    assert store.set_provider("a", "ZAI")
    assert ProfileStore().endpoint("a")["provider"] == "zai"

    # none/off/空 清除字段
    assert store.set_provider("a", "off")
    assert "provider" not in ProfileStore().endpoint("a")
    assert store.set_provider("a", "zai")
    assert store.set_provider("a", None)
    assert "provider" not in ProfileStore().endpoint("a")

    # 未知名 loudly（报错信息列出可用 preset）
    with pytest.raises(ValueError) as e:
        store.set_provider("a", "nope")
    assert all(name in str(e.value) for name in known_providers())
    # 未知档案名 → False 而非报错
    assert not store.set_provider("ghost", "zai")


def test_models_request_protocol_aware():
    from lithe_cli.profiles import models_request

    url, headers = models_request({
        "base_url": "https://api.anthropic.com/v1", "api_key": "sk-a",
        "provider": "anthropic"})
    assert url.endswith("/models?limit=1000")
    assert headers["x-api-key"] == "sk-a"
    assert headers["anthropic-version"] == "2023-06-01"

    # chat 家族 / 无 provider → Bearer；未知 provider 降级 Bearer
    url, headers = models_request({"base_url": "https://z.example/api",
                                   "api_key": "sk", "provider": "zai"})
    assert url.endswith("/models") and "?" not in url
    assert headers["Authorization"] == "Bearer sk"
    url, headers = models_request({"base_url": "https://x/api",
                                   "api_key": "k", "provider": "nope"})
    assert "Authorization" in headers

    # 缺凭据 → None（不抛）
    assert models_request({"base_url": "", "api_key": ""}) is None


def test_provider_flag_beats_env_and_profile(tmp_path, monkeypatch):
    from lithe_cli.config import ENV_PROVIDER, load_config

    monkeypatch.setenv("LITHE_HOME", str(tmp_path))
    for var in ("LITHE_API_KEY", "LITHE_BASE_URL", "LITHE_MODEL",
                "LITHE_PROFILE"):
        monkeypatch.delenv(var, raising=False)
    ProfileStore().upsert("a", "", "sk", "m", provider="zai")
    monkeypatch.setenv(ENV_PROVIDER, "deepseek")
    _PresetArgs.provider = "anthropic"
    try:
        cfg = load_config(_PresetArgs())
        assert cfg.provider == "anthropic", "flag > env > 档案字段"
        assert cfg.base_url == "https://api.anthropic.com/v1"
        assert cfg.pinned_keys >= {"provider"}
    finally:
        _PresetArgs.provider = None


def test_reasoning_effort_profile_roundtrip(tmp_path, monkeypatch):
    from lithe_cli.config import load_config

    monkeypatch.setenv("LITHE_HOME", str(tmp_path / "home"))
    for var in ("LITHE_API_KEY", "LITHE_BASE_URL", "LITHE_MODEL",
                "LITHE_PROFILE", "LITHE_PROVIDER"):
        monkeypatch.delenv(var, raising=False)
    store = ProfileStore()
    store.upsert("zai", "", "sk", "glm-4.6", provider="zai")
    store.set_reasoning_effort("zai", "high")

    cfg = load_config(_PresetArgs())
    assert cfg.reasoning_effort == "high", "档案字段经 load_config 进入 cfg"

    # "off" 归一化为不发（None），并存回时清除字段
    store.set_reasoning_effort("zai", "off")
    cfg = load_config(_PresetArgs())
    assert cfg.reasoning_effort is None
    assert "reasoning_effort" not in store.endpoint("zai")


# --- document_format 分层（provider 束缚格式，base_url 自填） ------------------

class _DocumentArgs(_PresetArgs):
    document_format = None


def test_document_format_from_provider_preset(tmp_path, monkeypatch):
    """自建 openrouter 格式路由：provider 定方言，base_url 用自己的。"""
    from lithe_cli.agent import build_llm
    from lithe_cli.config import load_config

    monkeypatch.setenv("LITHE_HOME", str(tmp_path))
    for var in ("LITHE_API_KEY", "LITHE_BASE_URL", "LITHE_MODEL",
                "LITHE_PROFILE", "LITHE_PROVIDER"):
        monkeypatch.delenv(var, raising=False)
    ProfileStore().upsert("selfhost", "https://my-router.example/api/v1",
                          "sk-r", "claude-sonnet", provider="openrouter")

    cfg = load_config(_DocumentArgs())
    assert cfg.base_url == "https://my-router.example/api/v1"
    assert cfg.document_format is None, "未显式设置时 cfg 不携带，preset 在 build_llm 补"
    llm = build_llm(cfg)
    assert llm.base_url == "https://my-router.example/api/v1"
    assert llm.document_format == "inline-file", "preset 家族默认方言"


def test_document_format_explicit_beats_preset(tmp_path, monkeypatch):
    from lithe_cli.agent import build_llm
    from lithe_cli.config import load_config

    monkeypatch.setenv("LITHE_HOME", str(tmp_path))
    for var in ("LITHE_API_KEY", "LITHE_BASE_URL", "LITHE_MODEL",
                "LITHE_PROFILE", "LITHE_PROVIDER"):
        monkeypatch.delenv(var, raising=False)
    # 中转站说 openrouter 家族但只认 strict-OpenAI 两步上传：档案显式改写方言
    ProfileStore().upsert("relay", "https://relay.example/api/v1", "sk-r",
                          "gpt-x", provider="openrouter",
                          document_format="files-api")

    cfg = load_config(_DocumentArgs())
    assert cfg.document_format == "files-api"
    llm = build_llm(cfg)
    assert llm.document_format == "files-api"
    assert llm.base_url == "https://relay.example/api/v1"


def test_document_format_none_provider_registers_probe_only(tmp_path,
                                                             monkeypatch):
    from lithe_cli.agent import build_llm
    from lithe_cli.config import load_config
    from lithe.bundles.documents import register_document_tools
    from lithe import ToolRegistry
    from lithe.bundles.workspace import Workspace

    monkeypatch.setenv("LITHE_HOME", str(tmp_path))
    for var in ("LITHE_API_KEY", "LITHE_BASE_URL", "LITHE_MODEL",
                "LITHE_PROFILE", "LITHE_PROVIDER"):
        monkeypatch.delenv(var, raising=False)
    ProfileStore().upsert("ds", "", "sk", "deepseek-chat", provider="deepseek")

    cfg = load_config(_DocumentArgs())
    llm = build_llm(cfg)
    assert llm.document_format == "none", "deepseek 家族不吃文档块"
    reg = ToolRegistry()
    ws = Workspace(tmp_path)
    register_document_tools(reg, lambda ctx: ws, llm_config=llm)
    assert "document_info" in reg.names()
    assert "analyze_document" not in reg.names()


def test_document_format_field_roundtrips(tmp_path, monkeypatch):
    monkeypatch.setenv("LITHE_HOME", str(tmp_path))
    store = ProfileStore()
    store.upsert("a", "https://a.example/v1", "sk", "m",
                 document_format="inline-file")
    fresh = ProfileStore()
    assert fresh.endpoint("a")["document_format"] == "inline-file"
    store.upsert("plain", "https://p.example", "sk", "m")
    assert "document_format" not in fresh.endpoint("plain")


# --- favorites（跨档案常用模型） -----------------------------------------------

def test_favorites_roundtrip_toggle_and_prune(tmp_path, monkeypatch):
    monkeypatch.setenv("LITHE_HOME", str(tmp_path))
    store = ProfileStore()
    store.upsert("zhipu", "https://z.example/api", "sk-z", "glm-4.6")
    store.upsert("anthropic", "https://api.anthropic.com/v1", "sk-a",
                 "claude-sonnet-4")

    # toggle on → persisted in order; a fresh store reads the same list
    assert store.toggle_favorite("zhipu:glm-4.6") is True
    assert store.toggle_favorite("anthropic:claude-sonnet-4") is True
    assert ProfileStore().favorites() == [
        "zhipu:glm-4.6", "anthropic:claude-sonnet-4"]

    # toggle again removes just that ref
    assert store.toggle_favorite("zhipu:glm-4.6") is False
    assert ProfileStore().favorites() == ["anthropic:claude-sonnet-4"]

    # malformed refs / unknown profiles are rejected, not saved
    assert store.toggle_favorite("ghost:model") is None
    assert store.toggle_favorite("no-colon") is None
    assert store.toggle_favorite("zhipu:") is None
    assert ProfileStore().favorites() == ["anthropic:claude-sonnet-4"]

    # deleting the profile prunes its favorites (load-time, ghost-free)
    store.delete("anthropic")
    assert ProfileStore().favorites() == []

    # a favorites key pointing at dead profiles on disk is cleaned on read
    (tmp_path / "config.json").write_text(json.dumps({
        "version": 2, "active": "zhipu",
        "favorites": ["zhipu:glm-4.6", "gone:m"],
        "profiles": {"zhipu": {"api_key": "sk", "base_url": "https://z",
                                "model": "glm-4.6"}},
    }), encoding="utf-8")
    assert ProfileStore().favorites() == ["zhipu:glm-4.6"]

    # unrelated mutations round-trip the favorites untouched
    store2 = ProfileStore()
    store2.upsert("b", "https://b.example", "sk", "m")
    data = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert data["favorites"] == ["zhipu:glm-4.6"]


# --- fetch_models_for（协议感知的 /models） ------------------------------------

class _Resp:
    def __init__(self, payload):
        self.payload = payload

    def read(self):
        return json.dumps(self.payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_fetch_models_for_messages_protocol_auth(tmp_path, monkeypatch):
    """anthropic 档案（messages transport）用 x-api-key + version 头拉取，
    并带 limit 避免 20 条默认分页截断。"""
    import lithe_cli.profiles as profiles

    seen = {}

    def fake_urlopen(req, timeout=10.0):
        seen["url"] = req.full_url
        seen["headers"] = dict(req.headers)
        return _Resp({"data": [{"id": "claude-sonnet-4"},
                               {"id": "claude-opus-4"}]})

    monkeypatch.setattr(profiles, "urlopen", fake_urlopen)
    models = fetch_models_for({
        "base_url": "https://api.anthropic.com/v1", "api_key": "sk-a",
        "provider": "anthropic"})
    assert models == ["claude-sonnet-4", "claude-opus-4"]
    assert seen["url"].endswith("/models?limit=1000")
    headers = {k.lower(): v for k, v in seen["headers"].items()}
    assert headers.get("x-api-key") == "sk-a"
    assert headers.get("anthropic-version") == "2023-06-01"
    assert "authorization" not in headers


def test_fetch_models_for_bearer_default_and_guards(monkeypatch):
    import lithe_cli.profiles as profiles

    seen = {}

    def fake_urlopen(req, timeout=10.0):
        seen["url"] = req.full_url
        seen["headers"] = dict(req.headers)
        return _Resp({"data": [{"id": "glm-4.6"}]})

    monkeypatch.setattr(profiles, "urlopen", fake_urlopen)
    # chat 档案（或没有 provider 字段）：OpenAI 式 Bearer
    models = fetch_models_for({"base_url": "https://z.example/api",
                               "api_key": "sk-z", "provider": "zai"})
    assert models == ["glm-4.6"]
    assert seen["url"].endswith("/models")
    assert not seen["url"].endswith("limit=1000")
    assert any(k.lower() == "authorization" for k in seen["headers"])
    # 未知 provider 退回 Bearer；缺凭据 → None（不抛）
    assert fetch_models_for({"base_url": "https://x", "api_key": "k",
                             "provider": "nope"}) is not None
    assert fetch_models_for({"base_url": "", "api_key": ""}) is None
