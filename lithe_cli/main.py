"""The `lithe` command-line entry point.

Subcommands:
  run TASK     one-shot task against the configured endpoint
  chat         interactive session (history carried across turns)
  tools        list the tools this CLI registers
  runs         list stored runs
  log RUN_ID   show a stored run's messages and actions
  undo RUN_ID  revert a run's file/todo mutations
  doctor       show the effective configuration and capability status
  config       interactive endpoint setup (first-run wizard; --show to peek)

Endpoint config: LITHE_API_KEY / LITHE_BASE_URL / LITHE_MODEL (flags
override env, env overrides the wizard-saved $LITHE_HOME/config.json).
On a TTY, run/chat with no endpoint at all launch the setup wizard
(--no-setup keeps the hard refusal for scripting). Storage lands in
~/.lithe/runs (LITHE_HOME to move it); the agent's workspace is the
current directory unless --workspace says else.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Any

from lithe import __version__ as lithe_version
from lithe.bundles import JsonlRunStore

from . import __version__
from . import prompts
from .agent import build_registry, execute, sandbox_backend, undo
from .config import config_file, load_config, require_endpoint
from .prompts import open_history
from .tui import screen_supported
from .ui import BOLD, CYAN, GREEN, RED, YELLOW, configure, truncate, ui


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="lithe",
        description="Command-line interface for the lithe agent kernel.",
    )
    p.add_argument(
        "--version",
        action="version",
        version=f"lithe-cli {__version__} (lithe {lithe_version})",
    )
    sub = p.add_subparsers(dest="command")

    def common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--api-key", help="endpoint API key (env LITHE_API_KEY)")
        sp.add_argument("--base-url", help="endpoint base URL (env LITHE_BASE_URL)")
        sp.add_argument("--model", help="model name (env LITHE_MODEL)")
        sp.add_argument("--workspace", help="agent workspace dir (default: cwd)")
        sp.add_argument("--store", help="run store dir (default: ~/.lithe/runs)")
        sp.add_argument("--user", help="user id for the store (default: cli)")
        sp.add_argument(
            "--download",
            action="store_true",
            help="grant the agent download_file (network fetch)",
        )
        sp.add_argument(
            "--code",
            action="store_true",
            help="grant run_code/run_file (sandboxed Python; "
            "bwrap when available, passthrough otherwise)",
        )
        sp.add_argument(
            "--shell",
            action="store_true",
            help="grant run_command (native shell with current-user host access)",
        )
        sp.add_argument(
            "--skills",
            metavar="DIR",
            help="markdown skill dir (default: $LITHE_HOME/skills "
            'when it exists; "" disables)',
        )
        sp.add_argument(
            "--mcp",
            metavar="SPEC",
            help="MCP servers as JSON array/object or @file (env LITHE_MCP)",
        )
        sp.add_argument(
            "--vision",
            action="store_true",
            help="grant image_info/analyze_image (vision on the main endpoint)",
        )
        color = sp.add_mutually_exclusive_group()
        color.add_argument("--color", action="store_true", help="force colored output")
        color.add_argument(
            "--no-color", action="store_true", help="disable colored output"
        )
        sp.add_argument(
            "--verbose", "-v", action="store_true", help="show usage/reasoning events"
        )

    def runtime(sp: argparse.ArgumentParser) -> None:
        common(sp)
        sp.add_argument(
            "--stream", action="store_true", help="stream tokens as they generate"
        )
        sp.add_argument(
            "--no-setup",
            action="store_true",
            help="skip the first-run endpoint wizard (fail hard "
            "when unconfigured instead of prompting)",
        )
        sp.add_argument(
            "--max-steps",
            type=int,
            default=35,
            help="tool-loop step budget (default: 35)",
        )
        sp.add_argument(
            "--context-window",
            type=int,
            default=None,
            help="model context window, for fullness gauges",
        )
        sp.add_argument(
            "--timeout", type=float, default=180.0, help="per-call timeout in seconds"
        )
        sp.add_argument(
            "--attempts", type=int, default=2, help="per-call retry attempts"
        )

    run_p = sub.add_parser("run", help="run one task")
    runtime(run_p)
    run_p.add_argument("task", help="the task for the agent")

    chat_p = sub.add_parser("chat", help="interactive session")
    runtime(chat_p)

    tools_p = sub.add_parser("tools", help="list registered tools")
    common(tools_p)

    runs_p = sub.add_parser("runs", help="list stored runs")
    common(runs_p)
    runs_p.add_argument("--limit", type=int, default=30)

    log_p = sub.add_parser("log", help="show a run's messages and actions")
    common(log_p)
    log_p.add_argument("run_id")

    undo_p = sub.add_parser("undo", help="revert a run's mutations")
    common(undo_p)
    undo_p.add_argument("run_id")

    sub.add_parser("doctor", help="show configuration and capability status")

    config_p = sub.add_parser(
        "config", help="interactive endpoint setup (first-run wizard)"
    )
    config_p.add_argument(
        "--show",
        action="store_true",
        help="print the saved config (key masked) and exit",
    )

    return p


def _cmd_tools(cfg: Any) -> int:
    reg = build_registry(cfg)
    rows = []
    for name in sorted(reg.names()):
        spec = reg.spec(name)
        cat = spec.category.name if spec.category is not None else "?"
        desc = (spec.description or "").splitlines()[0]
        rows.append([name, cat.upper(), truncate(desc, 64)])
    print(ui.table(["tool", "category", "description"], rows))
    return 0


def _cmd_runs(cfg: Any, limit: int) -> int:
    store = JsonlRunStore(cfg.store_dir)
    runs = store.list_runs(cfg.user_id, limit=limit)
    if not runs:
        print(ui.s(f"（{cfg.store_dir} 下没有 run 记录）", YELLOW))
        return 0
    rows = []
    for r in reversed(runs):  # newest last, like a log
        task = (r.task or "").replace("\n", " ")
        rows.append([r.run_id, r.status, str(r.steps), f"{r.cost:.4f}", truncate(task, 60)])
    print(
        ui.table(
            ["run", "status", "steps", "cost", "task"],
            rows,
            cell_styles=[ui.s, ui.status, ui.s, ui.s, ui.s],
        )
    )
    return 0


def _cmd_log(cfg: Any, run_id: str) -> int:
    store = JsonlRunStore(cfg.store_dir)
    run = store.get_run(run_id, cfg.user_id)
    if run is None:
        print(ui.s(f"找不到 run {run_id}（user={cfg.user_id}）", RED))
        return 1
    print(
        f"run {ui.s(run.run_id, CYAN)}  status={ui.status(run.status)}  "
        f"steps={run.steps}  model={run.model}"
    )
    print(f"task: {run.task}")
    if run.final:
        print(f"final: {run.final[:500]}")
    print(ui.section("messages"))
    for row in store.messages_for_run(run_id, cfg.user_id):
        role = row.get("role", "?")
        content = row.get("content") or ""
        calls = row.get("tool_calls")
        extra = ui.s(f" tool_calls={len(calls)}", CYAN) if calls else ""
        print(f"[{ui.role(role)}] {str(content)[:300]}{extra}")
    print(ui.section("actions"))
    actions = store.list_actions(run_id, cfg.user_id)
    if not actions:
        print("（无）")
    for a in actions:
        print(f"#{a.id} {a.kind:<14} {a.target:<40} {a.status}")
    return 0


def _cmd_doctor() -> int:
    from .config import default_skills_dir, load_saved_endpoint

    class _Args:  # doctor reuses load_config over env-only defaults
        api_key = base_url = model = store = workspace = user = None
        skills = mcp = None
        download = code = shell = vision = color = no_color = verbose = False

    cfg = load_config(_Args())
    print(ui.s(f"lithe-cli {__version__}（lithe {lithe_version}）", CYAN, BOLD))

    def mask(key: str | None) -> str:
        if not key:
            return ui.s("未设置", RED)
        shown = key if len(key) <= 8 else f"{key[:5]}…{key[-4:]}"
        return shown

    if cfg.has_endpoint:
        endpoint = f"{cfg.model} @ {cfg.base_url}（key: {mask(cfg.api_key)}）"
    else:
        endpoint = ui.s(
            "未配置（run/chat 会进入交互设置，或先运行 lithe-cli config）", RED
        )
    print(ui.kv("endpoint", endpoint))

    saved = load_saved_endpoint()
    if saved:
        state = f"已保存 {saved['model']} @ {saved['base_url']}"
    else:
        state = ui.s("未保存（lithe-cli config 可交互配置）", YELLOW)
    print(ui.kv("config", f"{config_file()}（{state}）"))

    try:
        n_runs = len(JsonlRunStore(cfg.store_dir).list_runs(cfg.user_id, limit=10_000))
        store_line = f"{cfg.store_dir}（{n_runs} 个 run）"
    except OSError as exc:
        store_line = ui.s(f"{cfg.store_dir}（读取失败：{exc}）", RED)
    print(ui.kv("store", store_line))
    print(ui.kv("workspace", str(cfg.workspace_dir)))

    backend = sandbox_backend()
    if backend == "bwrap":
        sandbox_line = ui.s("bwrap（隔离执行）", GREEN)
    else:
        sandbox_line = ui.s("passthrough（未找到 bwrap，无隔离）", RED)
    print(ui.kv("sandbox", sandbox_line + "；--code 启用 run_code"))
    command_line = "已启用（run_command 可执行主机命令）" if cfg.shell else "未启用（--shell 显式授权）"
    print(ui.kv("shell", command_line))

    skills = default_skills_dir()
    if cfg.skills_dir is not None:
        print(ui.kv("skills", f"{cfg.skills_dir}（load_skill 已启用）"))
    elif skills.is_dir():
        print(ui.kv("skills", f"{skills} 目录存在但未加载（--skills 指定）"))
    else:
        print(ui.kv("skills", "未配置"))

    if cfg.mcp_servers:
        names = ", ".join(s.name for s in cfg.mcp_servers)
        print(ui.kv("mcp", f"{len(cfg.mcp_servers)} 个服务器：{names}"))
    else:
        print(ui.kv("mcp", "未配置（--mcp 或环境变量 LITHE_MCP）"))
    print(ui.kv("color", "on" if ui.color else "off"))
    return 0


def _cmd_run(cfg: Any, task: str) -> int:
    _, done, _ = asyncio.run(execute(cfg, task))
    return 0 if done.get("status") == "done" else 1


def _cmd_config(show: bool) -> int:
    """`lithe-cli config [--show]`: run or inspect the endpoint wizard."""
    from .config import load_saved_endpoint
    from .setup import run_setup_wizard, stdin_is_interactive

    saved = load_saved_endpoint()
    if show:
        if saved:
            key = saved["api_key"]
            masked = key if len(key) <= 8 else f"{key[:5]}…{key[-4:]}"
            print(ui.kv("config", str(config_file())))
            print(ui.kv("base_url", saved["base_url"]))
            print(ui.kv("api_key", masked))
            print(ui.kv("model", saved["model"]))
        else:
            print(
                ui.s(
                    f"（{config_file()} 尚未创建；不带 --show 运行本命令进入交互配置）",
                    YELLOW,
                )
            )
        return 0
    if not stdin_is_interactive():
        print(
            ui.s(
                "交互配置需要终端（当前 stdin 不是 TTY）。\n"
                "请改用环境变量：LITHE_API_KEY / LITHE_BASE_URL / LITHE_MODEL。",
                RED,
            )
        )
        return 1
    return 0 if run_setup_wizard() is not None else 1


_CHAT_HELP = """可用命令：
  /help   显示这段帮助
  /tools  列出当前注册的工具
  /new    清空会话历史，重新开始
  /exit   退出（等同 /quit）"""


def _chat_loop(cfg: Any) -> int:
    store = JsonlRunStore(cfg.store_dir)
    run_ids: list[str] = []
    input_history = open_history()  # FileHistory under $LITHE_HOME, shared runs
    print(ui.banner(__version__, cfg.model or "(scripted)", str(cfg.workspace_dir)))
    while True:
        try:
            # prompt_toolkit owns the terminal: wide-char safe, paste-safe,
            # persistent history — never input()/readline
            line = prompts.chat_line(ui.prompt(), input_history).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        except UnicodeDecodeError:
            print(
                ui.s(
                    "\n（这行输入无法按 UTF-8 解码，已丢弃；"
                    "请把终端与 locale 设为 UTF-8（如 export LANG=C.UTF-8）后重试。）",
                    RED,
                )
            )
            continue
        if not line:
            continue
        if line in ("/exit", "/quit", "exit", "quit"):
            break
        if line == "/help":
            print(_CHAT_HELP)
            continue
        if line == "/tools":
            _cmd_tools(cfg)
            continue
        if line == "/new":
            run_ids.clear()
            print(ui.s("（已清空会话历史）", YELLOW))
            continue
        if line.startswith("/"):
            print(ui.s(f"未知命令 {line}（/help 查看可用命令）", RED))
            continue
        history = store.messages_for_runs(run_ids, cfg.user_id) if run_ids else None
        try:
            # One asyncio.run per turn: each run is self-contained, and the
            # blocking input() stays out of async context.
            rid, done, _ = asyncio.run(execute(cfg, line, history=history))
        except KeyboardInterrupt:
            print("\n（已中断本轮；输入继续）")
            continue
        run_ids.append(rid)
        if done.get("status") != "done":
            print(ui.s(f"（本轮状态：{done.get('status')}）", RED))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command is None:
        build_parser().print_help()
        return 2
    cfg = load_config(args)
    configure(cfg.color)
    if args.command == "run":
        require_endpoint(cfg, interactive=not args.no_setup)
        if screen_supported():
            from .tui import run_screen

            return asyncio.run(run_screen(cfg, args.task, "run"))
        return _cmd_run(cfg, args.task)
    if args.command == "chat":
        require_endpoint(cfg, interactive=not args.no_setup)
        if screen_supported():
            from .tui import run_screen

            return asyncio.run(run_screen(cfg, None, "chat"))
        return _chat_loop(cfg)
    if args.command == "tools":
        return _cmd_tools(cfg)
    if args.command == "runs":
        return _cmd_runs(cfg, args.limit)
    if args.command == "log":
        return _cmd_log(cfg, args.run_id)
    if args.command == "undo":
        return asyncio.run(undo(cfg, args.run_id))
    if args.command == "doctor":
        return _cmd_doctor()
    if args.command == "config":
        return _cmd_config(args.show)
    return 2


if __name__ == "__main__":
    sys.exit(main())
