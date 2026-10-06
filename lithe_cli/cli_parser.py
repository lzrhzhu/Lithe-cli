"""Argument parser construction for the :mod:`lithe_cli` command line."""

from __future__ import annotations

import argparse

from lithe import __version__ as lithe_version

from . import __version__


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
        sp.add_argument(
            "--profile",
            help="endpoint profile name (env LITHE_PROFILE; default: active "
            "profile from lithe config --list)",
        )
        sp.add_argument("--workspace", help="agent workspace dir (default: cwd)")
        sp.add_argument("--store", help="run store dir (default: ~/.lithe/runs)")
        sp.add_argument("--user", help="user id for the store (default: cli)")
        sp.add_argument("--download", action="store_true",
                        help="grant the agent download_file (network fetch)")
        sp.add_argument(
            "--code", action="store_true",
            help="grant run_code/run_file (sandboxed Python; "
            "bwrap when available, passthrough otherwise)",
        )
        sp.add_argument(
            "--shell", action="store_true",
            help="grant run_command (native shell with current-user host access)",
        )
        sp.add_argument(
            "--skills", metavar="DIR",
            help="markdown skill dir (default: $LITHE_HOME/skills "
            'when it exists; "" disables)',
        )
        sp.add_argument("--mcp", metavar="SPEC",
                        help="MCP servers as JSON array/object or @file (env LITHE_MCP)")
        sp.add_argument("--vision", action="store_true",
                        help="grant image_info/analyze_image (vision on the main endpoint)")
        sp.add_argument(
            "--document", action="store_true",
            help="grant document_info/analyze_document "
            "(PDF/OOXML reading on the main endpoint)",
        )
        sp.add_argument(
            "--subagents", action=argparse.BooleanOptionalAction, default=True,
            help="grant delegate/delegate_parallel on the default roster "
            "(researcher/coder/operator). Default: on — the roster only "
            "reuses already-enabled tools; multiplies token spend "
            "(--no-subagents disables)",
        )
        sp.add_argument(
            "--document-format", metavar="DIALECT", default=None,
            choices=["inline-file", "files-api", "none"],
            help="document block dialect for analyze_document: "
            "inline-file = OpenRouter family (incl. self-built same-format "
            "routers), files-api = strict OpenAI two-step upload "
            "(default: profile field document_format, else provider preset)",
        )
        color = sp.add_mutually_exclusive_group()
        color.add_argument("--color", action="store_true", help="force colored output")
        color.add_argument("--no-color", action="store_true", help="disable colored output")
        sp.add_argument("--verbose", "-v", action="store_true",
                        help="show usage/reasoning events")

    def runtime(sp: argparse.ArgumentParser) -> None:
        common(sp)
        sp.add_argument("--stream", action="store_true", help="stream tokens as they generate")
        sp.add_argument(
            "--no-setup", action="store_true",
            help="skip the first-run endpoint wizard (fail hard "
            "when unconfigured instead of prompting)",
        )
        sp.add_argument("--max-steps", type=int, default=35,
                        help="tool-loop step budget (default: 35)")
        sp.add_argument("--context-window", type=int, default=None,
                        help="model context window, for fullness gauges")
        sp.add_argument("--timeout", type=float, default=180.0,
                        help="per-call timeout in seconds")
        sp.add_argument("--attempts", type=int, default=2,
                        help="per-call retry attempts")
        sp.add_argument(
            "--sleep-429", dest="sleep_429", type=float, default=2.0,
            help="base backoff seconds between 429 retries, jittered and "
            "scaled by attempt (default: 2.0; 0 retries immediately)",
        )
        sp.add_argument(
            "--sleep-err", dest="sleep_err", type=float, default=1.0,
            help="base backoff seconds between other retryable failures "
            "(5xx / network), jittered (default: 1.0; 0 retries immediately)",
        )
        sp.add_argument("--temperature", type=float, default=None,
                        help="sampling temperature (default: profile field, else endpoint default)")
        sp.add_argument(
            "--max-output-tokens", dest="max_tokens", type=int, default=None,
            help="per-call output token cap (default: profile max_tokens, "
            "else endpoint default); NOT the run budget --max-tokens",
        )
        sp.add_argument(
            "--max-cost", type=float, default=None,
            help="run cost budget in USD: the run ends with status "
            "budget_exceeded once cumulative cost crosses it (needs a "
            "pricing table on endpoints that report no usage cost)",
        )
        sp.add_argument(
            "--max-tokens", dest="max_total_tokens", type=int, default=None,
            help="run total-token budget: ends with budget_exceeded once "
            "cumulative tokens cross it",
        )
        sp.add_argument(
            "--reasoning-effort", metavar="LEVEL", default=None,
            help="reasoning intensity (off/minimal/low/medium/high, model "
            "dependent; profile field reasoning_effort; in-session /reasoning)",
        )

    run_p = sub.add_parser("run", help="run one task")
    runtime(run_p)
    run_p.add_argument("task", help="the task for the agent")

    chat_p = sub.add_parser("chat", help="interactive session")
    runtime(chat_p)
    chat_p.add_argument("-c", "--continue", dest="cont", action="store_true",
                        help="resume the most recent conversation")
    chat_p.add_argument("--resume", metavar="ID|标题",
                        help="resume a specific conversation (id or unique title prefix)")
    chat_p.add_argument("--title", help="title for a new conversation")

    sessions_p = sub.add_parser("sessions", help="list conversations")
    common(sessions_p)
    sessions_p.add_argument("--limit", type=int, default=30)
    sessions_p.add_argument("--rename", nargs=2, metavar=("ID", "TITLE"))
    sessions_p.add_argument("--delete", metavar="ID")
    sessions_p.add_argument(
        "--export", metavar="ID|标题",
        help="print a conversation as plain text (pipe it: lithe sessions "
        "--export 12 | pbcopy)",
    )

    models_p = sub.add_parser("models", help="list the endpoint's models (GET /models)")
    common(models_p)
    models_p.add_argument("--cached", action="store_true",
                          help="show only the cached list, no network")

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

    config_p = sub.add_parser("config", help="interactive endpoint setup (first-run wizard)")
    config_p.add_argument("--show", action="store_true",
                          help="print the active profile's config (key masked) and exit")
    config_p.add_argument("--list", action="store_true",
                          help="list saved profiles and exit")
    config_p.add_argument("--use", metavar="NAME", help="switch the active profile")
    config_p.add_argument("--model", metavar="NAME",
                          help="set the active profile's default model")

    return p
