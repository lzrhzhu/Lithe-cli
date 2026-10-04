"""CLI wiring for the kernel's subagent delegation (--subagents).

The kernel bundle (lithe.bundles.subagents) is host data; the CLI grants it
via --subagents / /set subagents with a default roster (researcher / coder /
operator) whose tool lists filter against the actually-registered tools, so
capability flags shape the workers automatically.
"""
from __future__ import annotations

import asyncio
import json

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
    # the child's messages are recorded under the run, tagged by subagent id
    rows = host.store.messages_for_run(rid, cfg.user_id)
    tagged = [r for r in rows if r.get("subagent") == "researcher"]
    assert tagged, "researcher's turns must be recorded with its tag"
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
