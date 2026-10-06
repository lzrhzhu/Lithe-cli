"""Run budgets (--max-cost/--max-tokens + profile pricing) and steering.

The kernel's AgentHost budgets end a run with status="budget_exceeded";
the CLI wires them as flags + /set knobs, with profile-level pricing making
max_cost real on endpoints that report no usage cost. Steering queues
mid-turn user texts into the kernel inbox, injected at step boundaries.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from lithe_cli.agent import build_host, build_llm, build_registry, execute
from lithe_cli.config import load_config
from lithe_cli.ttui import sidebar_markup
from lithe_cli.tui import TuiState
from lithe_cli.workbench import Workbench

from conftest import make_config


def _tc_call(name, args, cid="c1"):
    return {"id": cid, "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)}}


WRITE_THEN_ANSWER = [
    {"tool_calls": [_tc_call("write_file", {"path": "a.txt", "content": "hi"})]},
    {"content": "已写入。"},
]


def test_run_token_budget_ends_with_budget_exceeded(tmp_path):
    cfg = make_config(tmp_path, WRITE_THEN_ANSWER)
    cfg.max_total_tokens = 100  # scripted usage reports 120 per call
    rid, done, host = asyncio.run(execute(cfg, "写 a.txt"))
    assert rid
    assert done["status"] == "budget_exceeded"
    assert host.max_total_tokens == 100


def test_run_cost_budget_uses_profile_pricing(tmp_path):
    # scripted usage carries no cost field: without a price table the
    # budget can never trigger (cost stays 0)
    cfg = make_config(tmp_path, WRITE_THEN_ANSWER)
    cfg.pricing = {"prompt": 3000.0, "completion": 15000.0}  # $/1M
    cfg.max_cost = 0.5
    rid, done, _ = asyncio.run(execute(cfg, "写 a.txt"))
    assert rid
    assert done["status"] == "budget_exceeded"
    # 100 prompt × $3k/1M + 20 completion × $15k/1M = 0.30 + 0.30
    assert done["cost"] == pytest.approx(0.6, abs=1e-6)


def test_budgets_and_pricing_round_trip_from_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("LITHE_HOME", str(tmp_path))
    (tmp_path / "config.json").write_text(
        '{"version": 2, "active": "p", "profiles": {"p": {'
        '"api_key": "sk", "base_url": "https://x/api", "model": "m", '
        '"pricing": {"prompt": 3, "completion": 15}, '
        '"temperature": 0.4, "max_tokens": 2048, '
        '"extra_body": {"enable_thinking": true}, '
        '"default_headers": {"X-Title": "lithe"}}}}',
        encoding="utf-8")

    class Args:
        api_key = base_url = model = profile = store = workspace = user = None
        skills = mcp = None
        download = code = shell = vision = color = no_color = verbose = False
        subagents = document = False
        temperature = max_tokens = max_cost = max_total_tokens = None

    cfg = load_config(Args())
    assert cfg.pricing == {"prompt": 3, "completion": 15}
    assert cfg.temperature == 0.4 and cfg.max_tokens == 2048
    assert cfg.extra_body == {"enable_thinking": True}
    assert cfg.default_headers == {"X-Title": "lithe"}
    llm = build_llm(cfg)
    assert llm.pricing == {"prompt": 3, "completion": 15}
    assert llm.temperature == 0.4 and llm.max_tokens == 2048
    assert llm.extra_body == {"enable_thinking": True}
    assert llm.default_headers == {"X-Title": "lithe"}
    # flag beats profile for the sampling knobs (0.0 is a real value)
    Args.temperature = 0.0
    Args.max_tokens = 512
    cfg2 = load_config(Args())
    assert cfg2.temperature == 0.0 and cfg2.max_tokens == 512


def test_preset_extra_body_merges_under_profile_fields(tmp_path):
    cfg = make_config(tmp_path, [])
    cfg.provider = "openrouter"
    cfg.extra_body = {"route": "fallback"}
    llm = build_llm(cfg)
    # explicit key wins; preset siblings survive
    assert llm.extra_body.get("route") == "fallback"


def test_retry_backoff_reaches_llm_config(tmp_path):
    """重试必须带退避基数：立即原样重发只会再撞同一个限流——一次瞬时
    上游错误就能连杀委派与编排者的下一次调用（会话 #54 的失败形状）。
    默认非零，且 profile 字段可覆盖。"""
    cfg = make_config(tmp_path, [])
    assert cfg.sleep_429 == 2.0 and cfg.sleep_err == 1.0
    llm = build_llm(cfg)
    assert llm.attempts == 2
    assert llm.sleep_429 == 2.0 and llm.sleep_err == 1.0
    cfg.sleep_429, cfg.sleep_err = 5.0, 0.0
    llm = build_llm(cfg)
    assert llm.sleep_429 == 5.0 and llm.sleep_err == 0.0


def test_set_budget_and_sampling_knobs(tmp_path):
    cfg = make_config(tmp_path, [])
    wb = Workbench(cfg)
    r = wb.dispatch("/set max-cost 1.5")
    assert cfg.max_cost == 1.5
    assert any("1.5" in t for _, t in r.messages)
    assert wb.dispatch("/set max-cost off").messages
    assert cfg.max_cost is None
    wb.dispatch("/set max-tokens 5000")
    assert cfg.max_total_tokens == 5000
    wb.dispatch("/set max-tokens off")
    assert cfg.max_total_tokens is None
    # temperature 0 is a valid setting, not rejected as non-positive
    wb.dispatch("/set temperature 0")
    assert cfg.temperature == 0.0
    wb.dispatch("/set temperature off")
    assert cfg.temperature is None
    wb.dispatch("/set max-output-tokens 1024")
    assert cfg.max_tokens == 1024
    # budgets reach the host on the next build
    cfg.max_cost, cfg.max_total_tokens = 2.0, 9000
    host = build_host(cfg, build_registry(cfg))
    assert host.max_cost == 2.0 and host.max_total_tokens == 9000


def test_sidebar_shows_budget_progress_lines():
    state = TuiState("m", "/ws", 35)
    state.on_event({"type": "run_start"})
    state.on_event({
        "type": "usage", "prompt_tokens": 100, "completion_tokens": 20,
        "cached_tokens": 0, "total_tokens": 120, "cost": 0.01,
    })
    side = sidebar_markup(state)
    assert "预算" not in side  # no caps set → no lines
    state.max_cost = 1.0
    state.max_total_tokens = 10_000
    side = sidebar_markup(state)
    assert "成本预算 $0.0100 / $1.00" in side
    assert "token 预算 120 / 10,000" in side


def test_budget_status_maps_to_chinese_label():
    state = TuiState("m", "/ws", 35)
    state.on_event({"type": "done", "status": "budget_exceeded", "steps": 1})
    assert state.status == "预算超限"


# --------------------------------------------------------------------------- #
# steering
# --------------------------------------------------------------------------- #

class _GateTransport:
    """Holds the first model call open until the gate is set, so the test
    can steer mid-run deterministically; captures every call's kwargs."""

    def __init__(self):
        self.gate = asyncio.Event()
        self.stage = 0
        self.calls: list[dict] = []

    async def complete(self, client, **kw):
        self.calls.append(kw)
        if self.stage == 0:
            self.stage = 1
            await self.gate.wait()
            return {"content": "",
                    "tool_calls": [_tc_call("read_file", {"path": "a.txt"})],
                    "usage": {}}
        return {"content": "完成", "tool_calls": [], "usage": {}}


def test_steering_injects_at_step_boundary(tmp_path):
    cfg = make_config(tmp_path, [])
    tr = _GateTransport()
    cfg.transport = tr
    cfg.workspace_dir.mkdir(parents=True, exist_ok=True)
    (cfg.workspace_dir / "a.txt").write_text("hi", encoding="utf-8")

    async def scenario():
        inbox: asyncio.Queue = asyncio.Queue()
        events: list[dict] = []
        task = asyncio.get_running_loop().create_task(execute(
            cfg, "读一下 a.txt", on_event=events.append, inbox=inbox))
        for _ in range(200):
            await asyncio.sleep(0.01)
            if tr.calls:
                break
        assert tr.calls, "first model call never started"
        inbox.put_nowait("顺便也看看 b.txt 存不存在")
        tr.gate.set()
        rid, done, _ = await task
        assert done["status"] == "done"
        # injected text reaches the second model call's messages
        second = tr.calls[1]["messages"]
        assert any(m.get("role") == "user"
                   and "b.txt" in (m.get("content") or "")
                   for m in second), second
        # the injection was announced as an event and persisted as a row
        assert any(ev.get("type") == "user_injected"
                   and "b.txt" in ev.get("text", "") for ev in events)
        rows = _messages_of(rid, cfg)
        assert any(r.get("role") == "user" and "b.txt" in (r.get("content") or "")
                   for r in rows)

    asyncio.run(scenario())


def _messages_of(rid, cfg):
    from lithe.bundles import JsonlRunStore

    return JsonlRunStore(cfg.store_dir).messages_for_run(rid, cfg.user_id)


def test_workbench_steer_routes_and_reports_leftovers(tmp_path):
    cfg = make_config(tmp_path, [])
    tr = _GateTransport()
    cfg.transport = tr
    cfg.workspace_dir.mkdir(parents=True, exist_ok=True)
    (cfg.workspace_dir / "a.txt").write_text("hi", encoding="utf-8")
    wb = Workbench(cfg)
    wb.open()

    async def scenario():
        got: list[dict] = []
        wb.subscribe(lambda cid, ev: got.append(ev))
        task = asyncio.get_running_loop().create_task(wb.run_turn("读 a.txt"))
        for _ in range(200):
            await asyncio.sleep(0.01)
            if tr.calls:
                break
        assert wb.steer("改看 c.txt") is True
        assert wb.steer("   ") is False  # blank never queues
        tr.gate.set()
        await task
        # injected (announced) + the model saw it on the next call
        assert any(ev.get("type") == "user_injected" for ev in got)
        assert any("c.txt" in (m.get("content") or "")
                   for m in tr.calls[1]["messages"])
        # turn over: steer has nothing to route to
        assert wb.steer("x") is False

    asyncio.run(scenario())


def test_workbench_reports_uninjectable_leftovers(tmp_path):
    cfg = make_config(tmp_path, [])
    release = asyncio.Event()

    class _Hang:
        async def complete(self, client, **kw):
            await release.wait()
            return {"content": "ok", "tool_calls": [], "usage": {}}

    cfg.transport = _Hang()
    wb = Workbench(cfg)
    wb.open()

    async def scenario():
        got: list[dict] = []
        wb.subscribe(lambda cid, ev: got.append(ev))
        task = asyncio.get_running_loop().create_task(wb.run_turn("任务"))
        for _ in range(200):
            await asyncio.sleep(0.01)
            if wb.busy_ids():
                break
        assert wb.steer("补充") is True
        release.set()  # the hung call returns; run ends with no step 2
        await task
        assert any("未能在本轮结束前注入" in (ev.get("text") or "")
                   for ev in got)

    asyncio.run(scenario())
