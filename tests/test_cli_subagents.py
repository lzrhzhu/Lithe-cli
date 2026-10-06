"""CLI wiring for the kernel's subagent delegation (--subagents).

The kernel bundle (lithe.bundles.subagents) is host data; the CLI grants it
via --subagents / /set subagents with a default roster (researcher / coder /
operator) whose tool lists filter against the actually-registered tools, so
capability flags shape the workers automatically.
"""
from __future__ import annotations

import asyncio
import json
import re

from lithe_cli.agent import (
    build_host,
    build_registry,
    build_system_prompt,
    register_subagents,
    tool_names,
)
from lithe_cli.config import Config
from lithe_cli.workbench import Workbench

from conftest import make_config


def _tc_call(name, args, cid="c1"):
    return {"id": cid, "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)}}


DELEGATE_SCRIPT = [
    # parent step 1: delegate a read-only lookup to the researcher
    {"tool_calls": [_tc_call(
        "delegate_parallel",
        {"tasks": [{"agent": "researcher", "task": "找一下 a.md 里写了什么"}]})]},
    # the child's own model call (shares the scripted transport)
    {"content": "a.md 的内容是待办清单。"},
    # parent step 2: final answer incorporating the child's report
    {"content": "子代理已确认：a.md 是待办清单。"},
]


def test_delegation_enabled_by_default_with_opt_out(tmp_path):
    """子代理委派默认包含（roster 只复用已启用工具，不新增能力），
    --no-subagents 显式退出。"""
    from lithe_cli.main import build_parser

    parser = build_parser()
    assert parser.parse_args(["run", "t"]).subagents is True
    assert parser.parse_args(["run", "--no-subagents", "t"]).subagents is False
    assert parser.parse_args(["run", "--subagents", "t"]).subagents is True

    cfg = make_config(tmp_path, DELEGATE_SCRIPT)
    assert cfg.subagents is True
    assert "delegate" in tool_names(cfg)
    assert "delegate" in build_system_prompt(Config())

    # opting out keeps the delegation pair off the registry-facing surface
    off = make_config(tmp_path / "off", [])
    off.subagents = False
    assert "delegate" not in tool_names(off)
    assert "delegate" not in build_system_prompt(off)


def test_parser_split_preserves_all_runtime_options():
    from lithe_cli.cli_parser import build_parser as parser_builder
    from lithe_cli.main import build_parser

    args = build_parser().parse_args([
        "run", "--no-subagents", "--no-setup", "--stream",
        "--max-steps", "7", "--context-window", "8192", "--timeout", "12",
        "--attempts", "4", "--temperature", "0.2", "--max-output-tokens", "99",
        "--max-cost", "1.5", "--max-tokens", "200", "--reasoning-effort", "low",
        "--sleep-429", "3.5", "--sleep-err", "0.5",
        "task",
    ])
    assert args.subagents is False
    assert args.no_setup and args.stream
    assert (args.max_steps, args.context_window, args.timeout, args.attempts) == (7, 8192, 12, 4)
    assert (args.temperature, args.max_tokens) == (0.2, 99)
    assert (args.max_cost, args.max_total_tokens, args.reasoning_effort) == (1.5, 200, "low")
    assert (args.sleep_429, args.sleep_err) == (3.5, 0.5)
    assert parser_builder().parse_args(["tools"]).command == "tools"


def test_delegation_runs_and_records_child_messages(tmp_path, capsys):
    cfg = make_config(tmp_path, DELEGATE_SCRIPT)
    cfg.subagents = True
    (cfg.workspace_dir).mkdir(parents=True, exist_ok=True)
    (cfg.workspace_dir / "a.md").write_text("- 待办", encoding="utf-8")

    from lithe_cli.agent import execute

    rid, done, host = asyncio.run(execute(cfg, "看看 a.md 是什么"))
    assert done["status"] == "done"
    out = capsys.readouterr().out
    assert "子代理已确认" in out
    # the child's messages are recorded under the run, tagged with the
    # delegation instance ("<agent>:<hex8>", kernel >= 0.1.4)
    rows = host.store.messages_for_run(rid, cfg.user_id)
    tagged = [r for r in rows
              if str(r.get("subagent") or "").startswith("researcher:")]
    assert tagged, "researcher's turns must be recorded with its instance tag"
    assert any("a.md" in (r.get("content") or "") for r in tagged)


def test_roster_adapts_to_capability_flags(tmp_path):
    cfg = make_config(tmp_path, [])
    cfg.subagents = True
    reg = build_registry(cfg)
    host = build_host(cfg, reg)
    engine = register_subagents(cfg, host, reg)
    assert engine.roster.ids() == ["researcher", "coder"]
    assert "delegate" in tool_names(cfg)
    # researcher is read-only and never gets the write tools
    assert "write_file" not in engine.trimmed_tools(
        engine.roster.get("researcher"), None) and \
        all(t["function"]["name"] != "write_file"
            for t in engine.trimmed_tools(engine.roster.get("researcher"), None))

    # shell capability adds the operator with run_command
    cfg2 = make_config(tmp_path, [])
    cfg2.shell = True
    cfg2.subagents = True
    reg2 = build_registry(cfg2)
    host2 = build_host(cfg2, reg2)
    engine2 = register_subagents(cfg2, host2, reg2)
    assert "operator" in engine2.roster.ids()
    op_names = [t["function"]["name"]
                for t in engine2.trimmed_tools(engine2.roster.get("operator"),
                                               None)]
    assert "run_command" in op_names


def test_subagent_prompt_prepends_environment_facts(tmp_path):
    """Subagents never see the orchestrator's system prompt — without the
    facts prepended they re-learn the workspace layout (nested repos,
    platform) by failed guesses, which is what this fixes."""
    from lithe import AgentContext

    cfg = make_config(tmp_path, [])
    cfg.subagents = True
    cfg.shell = True
    cfg.workspace_dir.mkdir(parents=True, exist_ok=True)
    (cfg.workspace_dir / "svc").mkdir()
    (cfg.workspace_dir / "svc" / ".git").mkdir()
    reg = build_registry(cfg)
    host = build_host(cfg, reg)
    engine = register_subagents(cfg, host, reg)
    prompt = engine.system_prompt(
        engine.roster.get("researcher"), AgentContext(run_id="r", user_id="u"))
    assert "宿主环境" in prompt
    assert "独立 git 仓库：svc" in prompt
    # the spec's own persona is intact after the prepended facts
    assert "只读检索子代理" in prompt


def test_set_subagents_toggles_and_warns(tmp_path):
    cfg = make_config(tmp_path, [])
    assert cfg.subagents is True  # default-on since the roster adds no powers
    wb = Workbench(cfg)
    keys = [row["key"] for row in wb.settings_rows()]
    assert "subagents" in keys
    # enabling from off warns about token amplification
    assert wb.dispatch("/set subagents off").changed
    r = wb.dispatch("/set subagents on")
    assert cfg.subagents is True
    assert any("token 花费" in text for cls, text in r.messages
               if cls == "warn")
    # /tools reflects the delegation pair without rebuilding a host
    r = wb.dispatch("/tools")
    listing = "".join(text for cls, text in r.messages)
    assert "delegate" in listing and "delegate_parallel" in listing
    r = wb.dispatch("/set subagents off")
    assert cfg.subagents is False
    r = wb.dispatch("/tools")
    assert "delegate" not in "".join(text for cls, text in r.messages)


def test_system_prompt_mentions_delegation():
    assert "delegate" in build_system_prompt(Config(subagents=True))


def test_delegation_renders_live_progress_and_records(tmp_path, capsys):
    """行模式下委派全程可见：调用标签带 roster、实时心跳带显示名、
    start/end 记录与 footer 委派计数。"""
    cfg = make_config(tmp_path, DELEGATE_SCRIPT)
    cfg.subagents = True
    cfg.workspace_dir.mkdir(parents=True, exist_ok=True)
    (cfg.workspace_dir / "a.md").write_text("- 待办", encoding="utf-8")

    from lithe_cli.agent import execute

    rid, done, host = asyncio.run(execute(cfg, "看看 a.md 是什么"))
    assert done["status"] == "done"
    out = capsys.readouterr().out
    # the parallel call line names the roster and the task count
    assert "researcher（1 项）" in out
    # live heartbeat: the worker's answer, labeled with its display name
    # plus the delegation-instance suffix (kernel >= 0.1.4) that keeps
    # same-agent parallel delegations distinguishable
    assert re.search(r"\[检索员·[0-9a-f]{4}\]", out)
    assert "a.md 的内容是待办清单" in out
    # the plain REPL still renders task lifecycle records after the live output
    assert re.search(r"▸ 检索员·[0-9a-f]{4}：找一下 a\.md 里写了什么", out)
    assert re.search(r"▪ 检索员·[0-9a-f]{4} · 完成", out)
    # the footer carries the delegation footprint
    assert "delegations 1" in out


def test_tool_call_labels_delegation():
    from lithe_cli.ui import tool_call_label

    label = tool_call_label("delegate", {"agent": "researcher",
                                         "task": "找一下 a.md 里写了什么"})
    assert label == "delegate · researcher：找一下 a.md 里写了什么"
    assert tool_call_label("delegate", {"agent": "researcher"}) == \
        "delegate · researcher"
    par = tool_call_label("delegate_parallel", {"tasks": [
        {"agent": "researcher", "task": "查 a"},
        {"agent": "coder", "task": "改 b"},
        {"agent": "researcher", "task": "查 c"},
    ]})
    assert par == "delegate_parallel · researcher、coder（3 项）"


def test_render_event_subagent_records(capsys):
    from lithe_cli.agent import render_event

    render_event({"type": "subagent_start", "agent": "researcher",
                  "display": "检索员", "task": "审查配置"})
    render_event({"type": "subagent_end", "agent": "researcher",
                  "display": "检索员", "status": "done", "steps": 4,
                  "changes": 2, "ok": True})
    out = capsys.readouterr().out
    assert "▸ 检索员：审查配置" in out
    assert "▪ 检索员 · 完成 · 4 步 · 2 处改动" in out


def test_render_event_subagent_progress(capsys):
    from lithe_cli.agent import render_event

    render_event({"type": "subagent_progress", "agent": "coder",
                  "display": "编码员", "event": {
                      "type": "tool_call", "name": "read_file",
                      "args": '{"path": "cfg.py"}'}})
    render_event({"type": "subagent_progress", "agent": "coder",
                  "display": "编码员", "event": {
                      "type": "tool_result", "ok": False,
                      "summary": "参数错误", "error": "文件不存在"}})
    out = capsys.readouterr().out
    assert "[编码员]" in out
    assert "read_file" in out and "cfg.py" in out
    assert "✗" in out and "文件不存在" in out


def test_footer_shows_delegation_and_context(capsys):
    from lithe_cli.ui import UI

    UI(color=False).footer({"status": "done", "steps": 3, "tokens": 900,
                            "subagent_delegations": 2,
                            "subagent_cost": 0.0123,
                            "context_percent": 42,
                            "duration_s": 10.0})
    out = capsys.readouterr().out
    assert "delegations 2" in out
    assert "sub-cost 0.0123" in out
    assert "ctx 42%" in out


def test_tui_folds_subagent_events():
    from lithe_cli.tui import TuiState

    state = TuiState("m", "/ws", 5)
    state.on_event({"type": "subagent_start", "agent": "researcher",
                    "display": "检索员", "task": "查 a.md",
                    "instance": "researcher:a111"})
    state.on_event({"type": "subagent_progress", "agent": "researcher",
                    "display": "检索员", "instance": "researcher:a111",
                    "event": {
                        "type": "tool_call", "name": "read_file",
                        "args": '{"path": "a.md"}'}})
    state.on_event({"type": "subagent_progress", "agent": "researcher",
                    "display": "检索员", "instance": "researcher:a111",
                    "event": {
                        "type": "tool_result", "ok": True,
                        "summary": "读取 a.md"}})
    state.on_event({"type": "subagent_end", "agent": "researcher",
                    "display": "检索员", "instance": "researcher:a111",
                    "status": "done", "steps": 2,
                    "changes": 0, "ok": True})
    state.on_event({"type": "done", "status": "done", "steps": 2,
                    "subagent_delegations": 1, "subagent_cost": 0.01})
    assert len(state.subagents) == 1
    block = state.subagents["researcher:a111"]
    assert block["task"] == "查 a.md"
    assert block["status"] == "done" and block["steps"] == 2
    lines = "\n".join(text for _, text in block["lines"])
    assert "read_file" in lines and "a.md" in lines
    assert "✓ 读取 a.md" in lines
    feed = "\n".join(text for _, text in state.feed)
    assert "委派 1 次" in feed and "子代理花费 $0.0100" in feed
    assert not any("检索员" in text for _, text in state.feed)
    # the live start dropped one position marker into the feed — the pane
    # mounts the card there, in the conversation's scroll flow
    from lithe_cli.tui import SUBAGENT_FEED_PREFIX

    assert (SUBAGENT_FEED_PREFIX + "researcher:a111", "") in state.feed
    assert block["in_feed"] is True


def test_tui_subagent_instances_and_history_restore():
    from lithe_cli.tui import TuiState, transcript_feed_lines

    state = TuiState("m", "/ws", 5)
    rows = [
        {"role": "user", "content": "检查 alpha", "subagent": "researcher:a111",
         "meta": {"subagent_task": {"agent": "researcher", "display": "检索员",
                                     "task": "检查 alpha", "status": "running"}}},
        {"role": "assistant", "content": "发现 alpha", "subagent": "researcher:a111"},
        {"role": "tool", "tool_name": "read_file", "content": "读取 alpha.txt",
         "subagent": "researcher:a111"},
        {"role": "assistant", "content": None, "subagent": "researcher:a111",
         "meta": {"subagent_task": {"agent": "researcher", "display": "检索员",
                                     "task": "检查 alpha", "status": "done",
                                     "steps": 2}}},
        {"role": "user", "content": "检查 beta", "subagent": "researcher:b222",
         "meta": {"subagent_task": {"agent": "researcher", "display": "检索员",
                                     "task": "检查 beta", "status": "failed"}}},
    ]
    state.feed.extend(transcript_feed_lines(rows))
    state.restore_subagents(rows)
    assert len(state.subagents) == 2
    assert state.subagents["researcher:a111"]["status"] == "done"
    assert state.subagents["researcher:a111"]["steps"] == 2
    assert "发现 alpha" in " ".join(
        text for _, text in state.subagents["researcher:a111"]["lines"]
    )
    assert state.subagents["researcher:b222"]["status"] == "failed"
    assert not any("alpha" in text or "beta" in text for _, text in state.feed)
