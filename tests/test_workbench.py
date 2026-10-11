"""Workbench: sessions, model switching, dispatch — offline end-to-end."""

from __future__ import annotations

import asyncio


from lithe_cli.config import Config
from lithe_cli.workbench import Workbench

from conftest import _tc, scripted_transport


def _wb(tmp_path, responses, **kw) -> Workbench:
    cfg = Config(
        store_dir=tmp_path / "store",
        workspace_dir=tmp_path / "ws",
        transport=scripted_transport(responses),
        **kw,
    )
    return Workbench(cfg)


WRITE_THEN_ANSWER = [
    {"tool_calls": [_tc("write_file", {"path": "a.txt", "content": "hi"}, "c1")]},
    {"content": "已写入 a.txt。"},
]


def test_turns_attach_to_session_and_auto_title(tmp_path):
    wb = _wb(tmp_path, WRITE_THEN_ANSWER)
    wb.open()
    cid = wb.current["id"]
    asyncio.run(wb.run_turn("把 a.txt 写为 hi"))
    assert wb.current["title"] == "把 a.txt 写为 hi"

    runs = wb.sessions.runs(cid)
    assert len(runs) == 1 and runs[0].conversation_id == cid
    summary = wb.sessions.summary(cid)
    assert summary["n_runs"] == 1 and summary["last_status"] == "done"
    # two model calls × the scripted transport's 120-token usage
    assert summary["total_tokens"] == 240  # kernel v2 token persistence
    assert (tmp_path / "ws" / "a.txt").exists()


def test_cross_process_resume_replays_history(tmp_path):
    wb1 = _wb(tmp_path, WRITE_THEN_ANSWER)
    wb1.open()
    cid = wb1.current["id"]
    asyncio.run(wb1.run_turn("把 a.txt 写为 hi"))

    # a fresh process/store resolves the same session and its history
    seen = []

    class Recorder:
        async def complete(self, client, **kw):
            seen.append([dict(m) for m in (kw.get("messages") or [])])
            return {"content": "第二轮回答", "tool_calls": [], "usage": {}}

    cfg = Config(store_dir=tmp_path / "store", workspace_dir=tmp_path / "ws",
                 transport=Recorder())
    wb2 = Workbench(cfg)
    wb2.open(resume=str(cid))
    assert wb2.current["id"] == cid
    asyncio.run(wb2.run_turn("a.txt 里写了什么"))
    msgs = seen[0]
    assert msgs[-1]["role"] == "user" and "a.txt" in msgs[-1]["content"]
    assert any(m.get("role") == "assistant" and "已写入" in (m.get("content") or "")
               for m in msgs)
    assert wb2.sessions.summary(cid)["n_runs"] == 2


def test_new_resume_rename_delete_dispatch(tmp_path):
    wb = _wb(tmp_path, [])
    wb.open()
    first = wb.current["id"]

    r = wb.dispatch("/new 第二个会话")
    assert r.action == "none" and wb.current["id"] != first
    assert wb.current["title"] == "第二个会话"

    r = wb.dispatch("/resume " + str(first))
    assert wb.current["id"] == first

    r = wb.dispatch("/rename 新名字")
    assert wb.sessions.get(first)["title"] == "新名字"

    rows = wb.session_list()
    assert rows[0]["id"] == first  # most recent activity first

    r = wb.dispatch("/sessions")
    assert any("新名字" in text for _, text in r.messages)
    assert r.overlay == "sessions"

    assert wb.dispatch("/resume 不存在的").messages[0][0] == "err"
    assert wb.sessions.delete(first)
    assert wb.sessions.get(first) is None


def test_model_switch_dispatch_and_pinning(tmp_path, monkeypatch):
    monkeypatch.setenv("LITHE_HOME", str(tmp_path / "home"))
    wb = _wb(tmp_path, [])
    wb.profiles.upsert("zhipu", "https://z.example/api", "sk-z", "glm-4.6")
    wb.profiles.cache_models("zhipu", ["glm-4.6", "glm-4.5-air", "glm-4.5"])
    wb.cfg.profile = "zhipu"
    wb.cfg.base_url = "https://z.example/api"
    wb.cfg.api_key = "sk-z"
    wb.cfg.model = "glm-4.6"
    wb.open()

    r = wb.dispatch("/model")
    assert "glm-4.6" in "".join(t for _, t in r.messages)
    assert r.overlay == "model"

    # numeric pick from cached list (2 → glm-4.5-air), saved to profile
    r = wb.dispatch("/model 2 --save")
    assert wb.cfg.model == "glm-4.5-air"
    assert wb.profiles.endpoint("zhipu")["model"] == "glm-4.5-air"
    # the session meta remembers the pick
    assert wb.sessions.get(wb.current["id"])["meta"]["model"] == "glm-4.5-air"

    # unknown name is allowed (custom models), flagged nothing special
    r = wb.dispatch("/model my-fine-tune")
    assert wb.cfg.model == "my-fine-tune"

    # a pinned model (flag/env) refuses in-session switching
    wb.cfg.pinned_keys = frozenset({"model"})
    r = wb.dispatch("/model glm-4.6")
    assert r.messages[0][0] == "warn"
    assert wb.cfg.model == "my-fine-tune"


def test_profile_switch_updates_endpoint(tmp_path, monkeypatch):
    monkeypatch.setenv("LITHE_HOME", str(tmp_path / "home"))
    wb = _wb(tmp_path, [])
    wb.profiles.upsert("zhipu", "https://z.example/api", "sk-z", "glm-4.6", 128000)
    wb.profiles.upsert("or", "https://or.example/api", "sk-or", "sonnet-4")
    wb.cfg.profile = "zhipu"
    wb.cfg.base_url = "https://z.example/api"
    wb.cfg.api_key = "sk-z"
    wb.cfg.model = "glm-4.6"
    wb.open()

    r = wb.dispatch("/profile or")
    assert wb.cfg.profile == "or" and wb.cfg.model == "sonnet-4"
    assert wb.cfg.api_key == "sk-or"
    assert wb.cfg.base_url == "https://or.example/api"

    # pinned keys survive the switch
    wb.cfg.pinned_keys = frozenset({"api_key"})
    wb.cfg.api_key = "sk-flag"
    wb.dispatch("/profile zhipu")
    assert wb.cfg.profile == "zhipu" and wb.cfg.model == "glm-4.6"
    assert wb.cfg.api_key == "sk-flag"

    r = wb.dispatch("/profile ghost")
    assert r.messages[0][0] == "err"


def test_resume_restores_pinned_model_from_meta(tmp_path, monkeypatch):
    monkeypatch.setenv("LITHE_HOME", str(tmp_path / "home"))
    wb = _wb(tmp_path, [])
    wb.profiles.upsert("zhipu", "https://z.example/api", "sk-z", "glm-4.6")
    wb.cfg.profile = "zhipu"
    wb.cfg.base_url = "https://z.example/api"
    wb.cfg.api_key = "sk-z"
    wb.cfg.model = "glm-4.6"
    wb.open()
    cid = wb.current["id"]
    wb.dispatch("/model glm-4.5-air")

    wb2 = _wb(tmp_path, [])  # same store/workspace paths as wb1
    wb2.cfg.model = "glm-4.6"
    wb2.open(resume=str(cid))
    assert wb2.cfg.model == "glm-4.5-air"  # session pin restored


# --- 多 provider：限定名 / 常用区 / 协议感知 ------------------------------------

def _multi_provider_wb(tmp_path, monkeypatch, responses=None):
    monkeypatch.setenv("LITHE_HOME", str(tmp_path / "home"))
    wb = _wb(tmp_path, responses or [])
    wb.profiles.upsert("zhipu", "https://z.example/api", "sk-z", "glm-4.6",
                       provider="zai")
    wb.profiles.upsert("anthropic", "https://api.anthropic.com/v1", "sk-a",
                       "claude-sonnet-4", provider="anthropic")
    wb.profiles.cache_models("anthropic", ["claude-sonnet-4", "claude-opus-4"])
    wb.cfg.profile = "zhipu"
    wb.cfg.base_url = "https://z.example/api"
    wb.cfg.api_key = "sk-z"
    wb.cfg.model = "glm-4.6"
    wb.cfg.provider = "zai"
    wb.open()
    return wb


def test_profile_switch_carries_provider_and_dialect(tmp_path, monkeypatch):
    """切档案 = 整套端点真相：provider/transport 相关字段一起换，
    不得把上一个厂商的 preset 叠在新端点上。"""
    wb = _multi_provider_wb(tmp_path, monkeypatch)
    r = wb.dispatch("/profile anthropic")
    assert wb.cfg.provider == "anthropic"
    assert wb.cfg.base_url == "https://api.anthropic.com/v1"
    assert wb.cfg.model == "claude-sonnet-4"
    assert any("anthropic" in t and "messages" in t for _, t in r.messages)

    # 切回 zai：anthropic 的字段不残留
    wb.dispatch("/profile zhipu")
    assert wb.cfg.provider == "zai"
    assert wb.cfg.base_url == "https://z.example/api"

    # env/flag 钉住的 provider 不被会话内切换覆盖
    wb.cfg.pinned_keys = frozenset({"provider"})
    wb.cfg.provider = "deepseek"
    wb.dispatch("/profile anthropic")
    assert wb.cfg.provider == "deepseek", "钉住的 provider 不得被档案切换改写"


def test_model_listing_favorites_first_and_qualified_pick(tmp_path, monkeypatch):
    wb = _multi_provider_wb(tmp_path, monkeypatch)
    wb.profiles.toggle_favorite("anthropic:claude-sonnet-4")

    r = wb.dispatch("/model")
    lines = [t for _, t in r.messages]
    # 常用区在最前，条目带限定名；随后是档案分组（当前档案置顶）
    fav_idx = next(i for i, ln in enumerate(lines) if "★ 常用" in ln)
    assert "anthropic:claude-sonnet-4" in lines[fav_idx + 1]
    profile_idx = next(i for i, ln in enumerate(lines)
                       if ln.startswith(" ") and "glm-4.6" in ln)
    assert fav_idx < profile_idx
    assert any("zai · chat" in ln for ln in lines)
    assert any("anthropic · messages" in ln for ln in lines)

    # 序号选择跨档案：选常用区第 1 条 = 切档案 + 切模型
    r = wb.dispatch("/model 1")
    assert wb.cfg.profile == "anthropic"
    assert wb.cfg.model == "claude-sonnet-4"
    assert wb.cfg.provider == "anthropic"
    assert wb.cfg.base_url == "https://api.anthropic.com/v1"


def test_model_group_keeps_full_catalog_despite_quick_lanes(
        tmp_path, monkeypatch):
    """回归：收藏与会话最近（★常用/最近 快速通道）曾把分组里的模型
    全局去重掉，provider 分组只剩当前模型一条（甚至整组消失）。分组
    是档案的目录，必须完整——快速通道与分组是互补视图，不是分区。"""
    wb = _multi_provider_wb(tmp_path, monkeypatch)
    # anthropic 缓存 [claude-sonnet-4, claude-opus-4]，全部进快速通道：
    wb.profiles.toggle_favorite("anthropic:claude-sonnet-4")   # → ★常用
    wb.dispatch("/model anthropic:claude-opus-4")              # 会话 A meta → 最近
    wb.new_session()                                           # 会话 B（更新）
    wb.dispatch("/model zhipu:glm-4.6")                        # 切回 zhipu（当前）
    entries = wb.model_entries()
    group = {e["model"] for e in entries
             if e["section"] == "profile" and e["group"] == "anthropic"}
    assert group == {"claude-sonnet-4", "claude-opus-4"}, \
        "分组不得被收藏/最近去重掏空"
    # 快速通道照常工作：收藏在 ★，另一个在 最近
    assert any(e["section"] == "fav" and e["model"] == "claude-sonnet-4"
               for e in entries)
    assert any(e["section"] == "recent" and e["model"] == "claude-opus-4"
               for e in entries)
    # 分组内部仍去重：档案默认模型与缓存重叠时只列一次
    zhipu_group = [e["model"] for e in entries
                   if e["section"] == "profile" and e["group"] == "zhipu"]
    assert zhipu_group == ["glm-4.6"]


def test_model_qualified_ref_switches_profile(tmp_path, monkeypatch):
    wb = _multi_provider_wb(tmp_path, monkeypatch)
    wb.dispatch("/model anthropic:claude-opus-4")
    assert wb.cfg.profile == "anthropic"
    assert wb.cfg.model == "claude-opus-4"
    assert wb.cfg.provider == "anthropic"
    # 未知前缀不拆分：整串当模型名（自定义端点可用含冒号 id）
    wb.dispatch("/model weird:name")
    assert wb.cfg.model == "weird:name"
    # --save 落在新档案上
    wb.dispatch("/model anthropic:claude-sonnet-4 --save")
    assert wb.profiles.endpoint("anthropic")["model"] == "claude-sonnet-4"


def test_fav_command_toggle_and_listing(tmp_path, monkeypatch):
    wb = _multi_provider_wb(tmp_path, monkeypatch)
    r = wb.dispatch("/fav")
    assert any("还没有收藏" in t for _, t in r.messages)

    r = wb.dispatch("/fav anthropic:claude-opus-4")
    assert any("已收藏" in t for _, t in r.messages)
    r = wb.dispatch("/fav")
    assert any("anthropic:claude-opus-4" in t for _, t in r.messages)

    # 再执行一次即取消
    r = wb.dispatch("/fav anthropic:claude-opus-4")
    assert any("已取消收藏" in t for _, t in r.messages)
    assert wb.profiles.favorites() == []

    # 未知档案报错并列出可选项
    r = wb.dispatch("/fav ghost:m")
    assert r.messages[0][0] == "err"
    assert any("zhipu" in t for _, t in r.messages)


def test_models_all_fans_out_and_reports_qualified(tmp_path, monkeypatch):
    wb = _multi_provider_wb(tmp_path, monkeypatch)

    import lithe_cli.workbench as workbench_mod

    def fake_fetch(endpoint, timeout=10.0):
        if endpoint.get("provider") == "anthropic":
            return ["claude-sonnet-4", "claude-haiku-4"]
        return ["glm-4.6"]

    monkeypatch.setattr(workbench_mod, "fetch_models_for", fake_fetch)
    r = asyncio.run(wb.fetch_model_list("all"))
    lines = [t for _, t in r.messages]
    # 当前档案（zhipu）先报，全部限定名显示
    assert lines[0].startswith("zhipu：")
    assert any("anthropic:claude-haiku-4" in t for t in lines)
    # 各档案缓存写入
    assert "claude-haiku-4" in wb.profiles.endpoint("anthropic")["cached_models"]

    # 单档案失败不拖垮整体
    def flaky(endpoint, timeout=10.0):
        return None if endpoint.get("provider") == "anthropic" else ["glm-4.6"]

    monkeypatch.setattr(workbench_mod, "fetch_models_for", flaky)
    r = asyncio.run(wb.fetch_model_list("all"))
    assert any(t.startswith("✗") for _, t in r.messages)
    assert any("zhipu：" in t for _, t in r.messages)


def test_recent_models_derived_from_sessions(tmp_path, monkeypatch):
    wb = _multi_provider_wb(tmp_path, monkeypatch)
    # open() 种子 meta 携带当前档案/模型 —— 会话将用什么，recents 就有什么
    assert wb.recent_models() == [("zhipu", "glm-4.6")]
    wb.dispatch("/model anthropic:claude-opus-4")  # 写入会话 meta
    recents = wb.recent_models()
    assert recents[0] == ("anthropic", "claude-opus-4")
    # 收藏后从最近区移除（不重复出现）
    wb.profiles.toggle_favorite("anthropic:claude-opus-4")
    entries = wb.model_entries()
    assert [e for e in entries if e["model"] == "claude-opus-4"][0][
        "section"] == "fav"


def test_double_submit_rejected_and_events_fan_out(tmp_path):
    wb = _wb(tmp_path, WRITE_THEN_ANSWER)
    wb.open()
    events: list[tuple[int, dict]] = []
    wb.subscribe(lambda cid, ev: events.append((cid, ev)))

    async def scenario():
        t = await wb.submit("把 a.txt 写为 hi")
        # a second submit for the same session while the first is reserved
        # (it hasn't even started yet) is refused, not queued
        t2 = await wb.submit("再来一次")
        assert t2 is None
        await t
        # busy released afterwards
        assert not wb.busy()

    asyncio.run(scenario())
    kinds = [ev["type"] for _, ev in events]
    # the double-submit error lands first (the reserved turn hadn't started)
    assert kinds[0] == "error" and "进行中" in str(events[0][1].get("message"))
    assert "session_busy" in kinds and kinds[-1] == "session_idle"
    assert "tool_call" in kinds and "done" in kinds
    idle = events[-1][1]
    assert idle["status"] == "done" and idle["run_id"]


def test_cancel_sets_stop_handle_and_records_run(tmp_path):
    """Stop is cooperative (checked between model calls): the hanging 2nd
    call is released by the test, and cancellation bites before call 3."""
    import json as _json

    started = asyncio.Event()
    release = asyncio.Event()
    calls = {"n": 0}

    def tc(cid):
        return {"id": cid, "type": "function",
                "function": {"name": "write_file",
                             "arguments": _json.dumps(
                                 {"path": f"f{calls['n']}.txt", "content": "x"})}}

    class Cooperative:
        async def complete(self, client, **kw):
            calls["n"] += 1
            if calls["n"] == 2:
                started.set()
                await release.wait()  # hang until the test releases
            return {"content": "", "tool_calls": [tc(f"c{calls['n']}")],
                    "usage": {}}

    cfg = Config(store_dir=tmp_path / "store", workspace_dir=tmp_path / "ws",
                 transport=Cooperative())
    wb = Workbench(cfg)
    wb.open()
    cid = wb.current["id"]

    async def scenario():
        task = await wb.submit("慢任务")
        await started.wait()
        assert wb.busy(cid)
        assert wb.cancel(cid)  # plumbing: the turn's stop handle flips
        assert wb.turns[cid]["stop"].is_set()
        release.set()  # let the in-flight call return; stop bites next step
        await asyncio.wait_for(task, timeout=5)

    asyncio.run(scenario())
    assert not wb.busy(cid)
    runs = wb.sessions.runs(cid)
    assert runs and runs[0].status == "cancelled"


def test_plain_dispatch_submits_and_exit(tmp_path):
    wb = _wb(tmp_path, [])
    wb.open()
    r = wb.dispatch("你好")
    assert r.action == "submit" and r.text == "你好"
    assert wb.dispatch("/exit").action == "exit"
    assert wb.dispatch("exit").action == "exit"
    assert wb.dispatch("/nope").messages[0][0] == "err"
    assert wb.dispatch("/help").messages
    r = wb.dispatch("/tools")
    assert "write_file" in r.messages[0][1]


# --- /set: session-adjustable settings ------------------------------------------

def test_set_bare_lists_current_values_and_opens_picker(tmp_path):
    wb = _wb(tmp_path, [])
    r = wb.dispatch("/set")
    text = "".join(t for _, t in r.messages)
    assert "shell" in text and "max-steps" in text and "35" in text
    assert r.overlay == "set"


def test_set_bool_toggle_and_explicit_values(tmp_path):
    wb = _wb(tmp_path, [])
    wb._tool_names = ["stale"]  # 预填缓存，翻转后必须失效

    r = wb.dispatch("/set shell on")
    assert wb.cfg.shell is True
    assert r.messages[0][0] == "ok" and "下一轮生效" in r.messages[0][1]
    assert any("不可撤销" in t for c, t in r.messages), "shell 开启要重述信任警示"
    assert wb._tool_names is None, "影响工具集的设置要清空 /tools 缓存"

    r = wb.dispatch("/set shell")  # 不带值 = 切换
    assert wb.cfg.shell is False

    # vision 等分析能力现在默认开启：先关再开，覆盖双向翻转
    r = wb.dispatch("/set vision 0")
    assert wb.cfg.vision is False and r.messages[0][0] == "ok"
    r = wb.dispatch("/set vision true")
    assert wb.cfg.vision is True and r.messages[0][0] == "ok"
    r = wb.dispatch("/set vision 0")
    assert wb.cfg.vision is False

    # 关到已是 off 的值：友好提示，不算错误
    r = wb.dispatch("/set vision off")
    assert wb.cfg.vision is False and r.messages[0][0] == "dim"

    r = wb.dispatch("/set shell maybe")
    assert r.messages[0][0] == "err" and wb.cfg.vision is False


def test_set_by_index_and_underscore_alias(tmp_path):
    wb = _wb(tmp_path, [])
    r = wb.dispatch("/set 1 on")  # 序号 1 = shell
    assert wb.cfg.shell is True
    r = wb.dispatch("/set max_steps 50")  # 下划线别名
    assert wb.cfg.max_steps == 50
    r = wb.dispatch("/set 99 on")
    assert r.messages[0][0] == "err" and "可用" in r.messages[0][1]


def test_set_numeric_validation(tmp_path):
    wb = _wb(tmp_path, [])
    wb.dispatch("/set max-steps 50")
    assert wb.cfg.max_steps == 50
    wb.dispatch("/set timeout 30.5")
    assert wb.cfg.timeout == 30.5
    wb.dispatch("/set attempts 3")
    assert wb.cfg.attempts == 3
    # 缺值 / 非数值 / 非法数值都有可行动的报错
    assert wb.dispatch("/set max-steps").messages[0][0] == "err"
    assert wb.dispatch("/set max-steps abc").messages[0][0] == "err"
    assert wb.dispatch("/set timeout -1").messages[0][0] == "err"
    assert wb.cfg.max_steps == 50 and wb.cfg.timeout == 30.5


def test_set_unknown_key_lists_available(tmp_path):
    wb = _wb(tmp_path, [])
    r = wb.dispatch("/set turbo on")
    assert r.messages[0][0] == "err" and "shell" in r.messages[0][1]


def test_set_tools_cache_reflected_in_next_tools_listing(tmp_path):
    wb = _wb(tmp_path, [])
    wb.dispatch("/tools")                 # 填充缓存
    assert wb._tool_names is not None and "run_command" not in wb._tool_names
    wb.dispatch("/set shell on")          # 缓存失效
    assert wb._tool_names is None
    r = wb.dispatch("/tools")             # 重新派生，包含 shell 工具
    assert "run_command" in r.messages[0][1]


# --- /reasoning: in-session reasoning effort -------------------------------------

def test_reasoning_bare_lists_levels_and_opens_picker(tmp_path):
    wb = _wb(tmp_path, [])
    r = wb.dispatch("/reasoning")
    text = "".join(t for _, t in r.messages)
    assert "当前推理强度：off" in text
    for level in ("off", "minimal", "low", "medium", "high"):
        assert level in text
    assert r.overlay == "reasoning"


def test_reasoning_set_off_and_verbatim(tmp_path):
    wb = _wb(tmp_path, [])
    r = wb.dispatch("/reasoning high")
    assert wb.cfg.reasoning_effort == "high"
    assert r.messages[0][0] == "ok" and "下一轮生效" in r.messages[0][1]

    r = wb.dispatch("/reasoning off")
    assert wb.cfg.reasoning_effort is None
    assert "off" in r.messages[0][1]

    # 不在档位里的值原样透传（个别模型的 none 等）
    wb.dispatch("/reasoning none")
    assert wb.cfg.reasoning_effort == "none"

    # 序号选择：2 → minimal
    wb.dispatch("/reasoning off")
    wb.dispatch("/reasoning 2")
    assert wb.cfg.reasoning_effort == "minimal"
    assert wb.dispatch("/reasoning 99").messages[0][0] == "err"


def test_reasoning_save_persists_to_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("LITHE_HOME", str(tmp_path / "home"))
    wb = _wb(tmp_path, [])
    wb.profiles.upsert("zai", "", "sk", "glm-4.6", provider="zai")
    wb.cfg.profile = "zai"
    wb.dispatch("/reasoning high --save")
    assert wb.profiles.endpoint("zai")["reasoning_effort"] == "high"

    # off + --save 清除档案字段
    wb.dispatch("/reasoning off --save")
    assert "reasoning_effort" not in wb.profiles.endpoint("zai")


# --- F7 端点表单：save_profile / change_profile_provider --------------------------

def _wb_home(tmp_path, monkeypatch, responses=None) -> Workbench:
    monkeypatch.setenv("LITHE_HOME", str(tmp_path / "home"))
    return _wb(tmp_path, responses or [{"content": "ok"}])


def test_save_profile_new_switches_immediately(tmp_path, monkeypatch):
    wb = _wb_home(tmp_path, monkeypatch)
    r = wb.save_profile("myrouter", "openrouter",
                        "https://my-router/api/v1", "sk-r",
                        "vendor/claude-sonnet-4")
    assert r.messages[0][0] == "ok"
    assert wb.cfg.profile == "myrouter"
    assert wb.cfg.provider == "openrouter"
    assert wb.cfg.base_url == "https://my-router/api/v1"
    ep = wb.profiles.endpoint("myrouter")
    assert ep["provider"] == "openrouter"
    assert ep["model"] == "vendor/claude-sonnet-4"


def test_save_profile_edit_current_reapplies(tmp_path, monkeypatch):
    wb = _wb_home(tmp_path, monkeypatch)
    wb.profiles.upsert("cur", "https://old.example/api", "sk", "m")
    wb.set_profile("cur")
    r = wb.save_profile("cur", "anthropic",
                        "https://api.anthropic.com/v1", "sk-ant",
                        "claude-sonnet-4")
    assert wb.cfg.provider == "anthropic"
    assert wb.cfg.base_url == "https://api.anthropic.com/v1"
    assert wb.cfg.model == "claude-sonnet-4"
    assert any("下一轮生效" in t for _, t in r.messages)


def test_save_profile_none_clears_existing_provider(tmp_path, monkeypatch):
    wb = _wb_home(tmp_path, monkeypatch)
    wb.profiles.upsert("a", "https://a.example/api", "sk", "m", provider="zai")
    wb.save_profile("a", "none", "https://a.example/api", "sk", "m2")
    assert "provider" not in wb.profiles.endpoint("a")
    assert wb.cfg.provider is None


def test_save_profile_validation_and_bad_name(tmp_path, monkeypatch):
    wb = _wb_home(tmp_path, monkeypatch)
    assert wb.save_profile("", "none", "https://x", "k",
                           "m").messages[0][0] == "err"
    assert wb.save_profile("b", "none", "", "k", "m").messages[0][0] == "err"
    assert wb.save_profile("bad name!", "none", "https://x", "k",
                           "m").messages[0][0] == "err"


def test_save_profile_blank_model_optional(tmp_path, monkeypatch):
    """模型非必填：/models 拉取后再选，新建/编辑都能留空。"""
    wb = _wb_home(tmp_path, monkeypatch)
    r = wb.save_profile("bare", "none", "https://bare.example/api", "sk", "")
    assert r.messages[0][0] == "ok"
    assert wb.cfg.profile == "bare"
    assert "model" not in wb.profiles.endpoint("bare")
    assert any("/models" in t for _, t in r.messages)
    # 编辑清空模型 → 存储的默认模型一并清除
    wb.save_profile("bare", "none", "https://bare.example/api", "sk", "m1")
    assert wb.profiles.endpoint("bare")["model"] == "m1"
    wb.save_profile("bare", "none", "https://bare.example/api", "sk", "")
    assert "model" not in wb.profiles.endpoint("bare")


def test_delete_profile_current_falls_back_to_next_active(tmp_path, monkeypatch):
    wb = _wb_home(tmp_path, monkeypatch)
    wb.profiles.upsert("a", "https://a.example/api", "sk-a", "m-a")
    wb.profiles.upsert("b", "https://b.example/api", "sk-b", "m-b")
    wb.set_profile("b")
    r = wb.delete_profile("b")
    assert "b" not in wb.profiles.names()
    assert wb.cfg.profile == "a"
    assert wb.cfg.base_url == "https://a.example/api"
    assert wb.cfg.model == "m-a"
    assert any("已删除" in t for _, t in r.messages)


def test_delete_profile_last_one_keeps_endpoint_fields(tmp_path, monkeypatch):
    """删掉最后一个档案：会话沿用端点字段（env 模式），不中断任何东西。"""
    wb = _wb_home(tmp_path, monkeypatch)
    wb.profiles.upsert("solo", "https://s.example/api", "sk", "m")
    wb.set_profile("solo")
    r = wb.delete_profile("solo")
    assert wb.profiles.names() == []
    assert wb.cfg.profile is None
    assert wb.cfg.base_url == "https://s.example/api"
    assert any("env" in t for _, t in r.messages)
    assert wb.delete_profile("ghost").messages[0][0] == "err"


def test_change_profile_provider_current_and_clear(tmp_path, monkeypatch):
    wb = _wb_home(tmp_path, monkeypatch)
    wb.profiles.upsert("a", "", "sk", "glm-4.6", provider="zai")
    wb.set_profile("a")
    assert wb.cfg.provider == "zai"
    r = wb.change_profile_provider("a", "Anthropic")
    assert wb.profiles.endpoint("a")["provider"] == "anthropic"
    assert wb.cfg.provider == "anthropic"
    # preset-only 档案切换 provider 后，官方 base_url 跟着换
    assert wb.cfg.base_url == "https://api.anthropic.com/v1"
    assert any("下一轮生效" in t for _, t in r.messages)
    wb.change_profile_provider("a", "off")
    assert "provider" not in wb.profiles.endpoint("a")
    assert wb.cfg.provider is None
    assert wb.change_profile_provider("ghost", "zai").messages[0][0] == "err"
    assert wb.change_profile_provider("a", "nope").messages[0][0] == "err"


def test_switch_to_preset_only_profile_fills_base_url(tmp_path, monkeypatch):
    """档案只写 provider 不写 base_url：会话内切换不得把端点清空。"""
    wb = _wb_home(tmp_path, monkeypatch)
    wb.profiles.upsert("preset-only", "", "sk", "glm-4.6", provider="zai")
    wb.set_profile("preset-only")
    assert wb.cfg.base_url == "https://open.bigmodel.cn/api/paas/v4"


def test_reasoning_effort_reaches_llm_config(tmp_path):
    from lithe_cli.agent import build_llm

    wb = _wb(tmp_path, [])
    wb.dispatch("/reasoning high")
    llm = build_llm(wb.cfg)
    assert llm.reasoning_effort == "high"
    wb.dispatch("/reasoning off")
    assert build_llm(wb.cfg).reasoning_effort is None


def test_set_listing_includes_reasoning_hint(tmp_path):
    wb = _wb(tmp_path, [])
    r = wb.dispatch("/set")
    text = "".join(t for _, t in r.messages)
    assert "reasoning-effort" in text and "/reasoning" in text


def test_undo_targets_last_session_run(tmp_path):
    wb = _wb(tmp_path, WRITE_THEN_ANSWER + [{"content": "完成"}])
    wb.open()
    asyncio.run(wb.run_turn("把 a.txt 写为 hi"))
    target = tmp_path / "ws" / "a.txt"
    assert target.exists()

    async def do_undo():
        await wb._undo(None)

    asyncio.run(do_undo())
    assert not target.exists()
