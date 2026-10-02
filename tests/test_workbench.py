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
