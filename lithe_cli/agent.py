"""Host wiring: tools, store, LLM config, and the event renderer.

Everything here maps lithe's host contract onto CLI defaults:

- workspace — the sandboxed file root the agent reads/writes (``--workspace``,
  default the current directory);
- store — ``JsonlRunStore`` under ``~/.lithe/runs`` (``--store``), the
  kernel's zero-database default;
- tools — the workspace bundle (read/write/edit/list/search/glob +
  apply_patch) and todos, plus opt-in capabilities: ``--code`` (sandboxed
  Python), ``--shell`` (native host commands), ``--skills`` (markdown skill
  library), ``--vision`` (image probe/analysis), ``--download`` (SSRF-guarded
  network fetch) and ``--mcp`` (external MCP servers);
- undo — the bundled tools register reverters, so ``lithe undo`` works with
  zero configuration.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from lithe import AgentContext, LLMConfig, ToolRegistry
from lithe.bundles import (
    AgentHost,
    JsonlRunStore,
    JsonTodoStore,
    Workspace,
    undo_run,
)
from lithe.bundles.command import register_command_tools
from lithe.bundles.download import register_download_tools
from lithe.bundles.patch import register_apply_patch_tool
from lithe.bundles.workspace import register_file_tools
from lithe.bundles.todos import register_todo_tools

from .config import Config
from .ui import CYAN, GREEN, YELLOW, ui

SYSTEM_PROMPT_BASE = (
    "你是运行在命令行里的助理，工作区是用户的当前目录。"
    "用提供的文件工具完成任务（读写前先读、谨慎修改）；修改已有文件时优先用 edit_file 或 apply_patch 做局部修改，"
    "仅在新建文件或确需整体重写时使用 write_file。"
    "只有用户明确要求计划/跟踪，或任务确有多个需要追踪的独立阶段时才调用 update_todos；"
    "普通问答、解释、单步操作和小改动不创建待办。已有清单仅在用户继续相关工作时更新，无关请求保持不变；"
    "更新前读取并保留相关的现有任务，条数按实际步骤确定，不使用固定数量。"
    "最后用简洁中文汇报结果。"
)


def todo_store_path(cfg: Config) -> Path:
    workspace = str(cfg.workspace_dir.expanduser().resolve())
    scope = json.dumps(
        {"user_id": str(cfg.user_id), "workspace": workspace},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(scope.encode("utf-8")).hexdigest()[:24]
    return cfg.store_dir.expanduser() / f"todos-{digest}.json"


def sandbox_backend() -> str:
    """bwrap when present (full isolation), else passthrough."""
    return "bwrap" if shutil.which("bwrap") else "passthrough"


def build_system_prompt(cfg: Config) -> str:
    """Base prompt plus one line per enabled capability."""
    extras = []
    if cfg.code:
        extras.append("可以用 run_code 执行 Python 来验证想法、计算或测试。")
    if cfg.shell:
        extras.append("可以用 run_command 在主机上运行系统命令；命令有当前用户权限且副作用不可撤销，执行前先确认必要性。")
    if cfg.download:
        extras.append("可以用 download_file 下载网络文件到工作区。")
    if cfg.skills_dir is not None:
        extras.append("可以先 load_skill 查看可用技能并按需加载规范。")
    if not extras:
        return SYSTEM_PROMPT_BASE
    return SYSTEM_PROMPT_BASE + "".join(extras)


def build_llm(cfg: Config) -> LLMConfig:
    return LLMConfig(
        model=cfg.model or "unused",
        base_url=cfg.base_url or "unused",
        api_key=cfg.api_key or "unused",
        timeout=cfg.timeout,
        attempts=cfg.attempts,
        stream=cfg.stream,
        context_window=cfg.context_window,
        transport=cfg.transport,
    )


def build_registry(cfg: Config) -> ToolRegistry:
    """Register the CLI's tool set against the configured workspace."""
    reg = ToolRegistry()

    def workspace_for(ctx) -> Workspace:
        return Workspace(cfg.workspace_dir.resolve())

    register_file_tools(reg, workspace_for)
    register_apply_patch_tool(reg, workspace_for)
    todo_store = JsonTodoStore(todo_store_path(cfg))
    register_todo_tools(reg, lambda ctx: todo_store)
    if cfg.download:
        register_download_tools(reg, workspace_for)
    if cfg.code:
        from lithe.bundles.sandbox import CodeRunner, register_code_tools

        runner = CodeRunner(sys.executable, backend=sandbox_backend())
        register_code_tools(reg, lambda ctx: str(cfg.workspace_dir.resolve()), runner)
    if cfg.shell:
        from lithe.bundles.command import CommandRunner

        register_command_tools(
            reg, lambda ctx: cfg.workspace_dir.resolve(), CommandRunner()
        )
    if cfg.skills_dir is not None:
        from lithe.bundles.skills import SkillLibrary, register_skill_tool

        register_skill_tool(reg, SkillLibrary(cfg.skills_dir))
    if cfg.vision:
        from lithe.bundles.images import register_image_tools

        register_image_tools(reg, workspace_for, llm_config=build_llm(cfg))
    return reg


def build_host(cfg: Config, reg: ToolRegistry) -> AgentHost:
    """Build the AgentHost; the store lands under cfg.store_dir."""
    store = JsonlRunStore(cfg.store_dir)
    prompt = build_system_prompt(cfg)
    return AgentHost(
        reg,
        build_llm(cfg),
        store,
        max_steps=cfg.max_steps,
        build_system_prompt=lambda ctx, mode, anchor: prompt,
    )


def render_event(
    ev: dict,
    verbose: bool = False,
    stream: bool = False,
    pending: dict | None = None,
) -> None:
    """Render one runtime event onto the terminal.

    ``pending`` maps tool-call ids to their start time so results can show
    elapsed seconds. ``stream`` suppresses the full-text echo of the final
    answer when its tokens were already streamed.
    """
    t = ev.get("type")
    pending = pending if pending is not None else {}
    if t == "assistant_delta":
        ui.delta(ev.get("text", ""))
        pending["_deltas"] = pending.get("_deltas", 0) + 1
    elif t == "assistant":
        text = ev.get("text") or ""
        # Streaming already showed every token; echoing the whole answer
        # again would duplicate it.
        if text and not (stream and pending.get("_deltas")):
            ui.assistant(text)
    elif t == "tool_call":
        args = ev.get("args") or {}
        ui.tool_call(str(ev.get("name")), args)
        if ev.get("id") is not None:
            pending[ev["id"]] = time.monotonic()
    elif t == "tool_result":
        summary = (ev.get("summary") or "").replace("\n", " ")
        started = pending.pop(ev.get("id"), None) if ev.get("id") else None
        elapsed = (time.monotonic() - started) if started is not None else None
        ui.tool_result(bool(ev.get("ok")), summary, elapsed)
    elif t == "error":
        ui.error(str(ev.get("message", "")))
    elif t == "cancelled":
        ui.warn("已取消")
    elif verbose and t == "usage":
        print(
            ui.usage(
                ev.get("prompt_tokens"),
                ev.get("completion_tokens"),
                ev.get("context_tokens"),
                ev.get("context_percent"),
            )
        )
    elif verbose and t == "reasoning":
        digest = (ev.get("summary") or "").replace("\n", " ")
        if digest:
            print(ui.reasoning(digest[:120]))


async def execute(
    cfg: Config,
    task: str,
    history: list[dict] | None = None,
    run_id: str | None = None,
    on_event=None,
    stop=None,
) -> tuple[str, dict, Any]:
    """Run one task end-to-end; returns (run_id, done_event, host).

    ``on_event`` (the full-screen TUI supplies one) receives every kernel
    event and silences the line-oriented printing below, including the
    footer — the caller renders from the events instead. ``stop`` is the
    kernel's cancellation handle, surfaced so Ctrl+C can cancel a turn
    without killing the process.
    """
    reg = build_registry(cfg)
    host = build_host(cfg, reg)
    rid = run_id or uuid.uuid4().hex[:12]
    ctx = AgentContext(run_id=rid, user_id=cfg.user_id)
    manager = None
    if cfg.mcp_servers:
        from lithe.bundles.mcp import MCPManager

        manager = MCPManager(cfg.mcp_servers)
        try:
            report = await manager.attach(reg)
        except Exception as exc:  # degrade gracefully, like the bundle
            ui.error(f"MCP attach 失败：{exc}")
            report = {}
        for name, res in report.items():
            if isinstance(res, list):
                print(ui.s(f"  ⚙ MCP {name}: {len(res)} 个工具", CYAN))
            else:
                ui.error(f"MCP {name}: {res}")
    ev: dict = {}
    pending: dict[str, float] = {}
    try:
        async for ev in host.run(ctx, task, history=history, stop=stop):
            if on_event is not None:
                on_event(ev)
            else:
                render_event(ev, cfg.verbose, cfg.stream, pending)
        if ev.get("type") == "done" and on_event is None:
            if cfg.stream and pending.get("_deltas"):
                print()  # close the streaming line before the footer
            ui.footer(ev)
    finally:
        if manager is not None:
            await manager.close()
    return rid, ev, host


async def undo(cfg: Config, run_id: str) -> int:
    """Revert a run's mutations with the bundled tools' reverters."""
    reg = build_registry(cfg)
    host = build_host(cfg, reg)
    report = await undo_run(host, run_id, cfg.user_id)
    mark = ui.s("✓", GREEN) if report.reverted else ui.s("·", YELLOW)
    print(f"{mark} 已撤销 {report.reverted} 个操作（run {run_id}）")
    return 0
