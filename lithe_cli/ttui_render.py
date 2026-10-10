"""Pure text builders for the Textual interface.

Keeping display formatting separate from the application/event loop makes
the sidebar and chrome easy to test without mounting a Textual app.  The
historical imports from :mod:`lithe_cli.ttui` remain available there.
"""

from __future__ import annotations

from .tui import TuiState
from .ui import fmt_duration


def header_text(state: TuiState, version: str) -> str:
    who = f"{state.profile} · {state.model}" if state.profile else state.model
    if state.session_id is not None:
        who = f"▣ #{state.session_id} {state.session_title or '无标题'} │ {who}"
    return f" lithe {version} · {who} · {state.workspace}"


def sidebar_markup(state: TuiState) -> str:
    out: list[str] = []
    out.append("[dim]◆ 会话[/]")
    if state.session_id is None:
        out.append("[dim]（单次运行，无会话）[/]")
    else:
        dot = "[yellow]●[/]" if state.session_id in state.busy_sessions else "·"
        out.append(f"#{state.session_id} {state.session_title or '无标题'} {dot}")
        shown = 0
        for row in state.sessions:
            if shown >= 3:
                break
            if row.get("id") == state.session_id:
                continue
            shown += 1
            mark = "[yellow]●[/]" if row["id"] in state.busy_sessions else (
                "[green]✓[/]" if row.get("last_status") == "done" else "·")
            out.append(f"[dim] #{row['id']} {row.get('title') or ''} "
                       f"{mark} {row.get('n_runs', 0)}轮[/]")
        out.append("[dim]F3 切换 · /new 新建[/]")
    out.append("[dim]◆ 模型[/]")
    label = f"{state.profile} · {state.model}" if state.profile else state.model
    out.append(f"[cyan]{label}[/]")
    if state.dialect:
        out.append(f"[dim]{state.dialect} · F7 端点[/]")
    else:
        out.append("[dim]F7 端点管理[/]")
    if state.reasoning_effort:
        out.append(f"[dim]推理 {state.reasoning_effort} · F6 切换[/]")
    sampling = []
    if state.temperature is not None:
        sampling.append(f"温度 {state.temperature:g}")
    if state.max_output:
        sampling.append(f"输出上限 {state.max_output:,}")
    if sampling:
        out.append(f"[dim]{' · '.join(sampling)} · /set 调整[/]")
    if state.context_window:
        out.append(f"[dim]窗口 {state.context_window:,} · F4 切换[/]")
    else:
        out.append("[dim]F4 或 /model 切换[/]")
    out.append("[dim]◆ 运行[/]")
    if state.running:
        status_color = "yellow"
    elif state.status in ("就绪",) or "完成" in state.status or "取消" in state.status:
        status_color = "green"
    else:
        status_color = "red"
    out.append(f"[{status_color}]● {state.status}[/]")
    out.append(f"[dim]步骤 {state.step}/{state.max_steps}[/]")
    live = state.elapsed()
    if state.running:
        if live is not None:
            out.append(f"[yellow]已进行 {fmt_duration(live)}[/]")
    elif state.last_turn_duration is not None:
        out.append(f"[dim]本轮耗时 {fmt_duration(state.last_turn_duration)}[/]")
    out.append("[dim]◆ 工具[/]")
    if not state.tools:
        out.append("[dim]暂无调用[/]")
    for tool in state.tools[-4:]:
        mark = {"running": "…", "done": "✓", "failed": "✗"}.get(tool["status"], "·")
        tcolor = "green" if tool["status"] == "done" else (
            "red" if tool["status"] == "failed" else "cyan")
        suffix = f" {tool['elapsed']:.1f}s" if tool.get("elapsed") is not None else ""
        out.append(f"[{tcolor}]{mark} {tool['name']}{suffix}[/]")
    done = sum(todo.get("status") == "completed" for todo in state.todos)
    out.append(f"[dim]◆ 待办 {done}/{len(state.todos)}[/]")
    if not state.todos:
        out.append("[dim]暂无任务清单[/]")
    marks = {"pending": "[ ]", "in_progress": "[~]",
             "completed": "[x]", "cancelled": "[-]"}
    tcolors = {"pending": "dim", "in_progress": "yellow",
               "completed": "green", "cancelled": "dim"}
    for todo in state.todos[-5:]:
        status = todo.get("status")
        out.append(f"[{tcolors.get(status, 'dim')}]"
                   f"{marks.get(status, '[ ]')} {todo.get('content', '')}[/]")
    input_tokens = state.input_tokens + state._turn_input_tokens
    output_tokens = state.output_tokens + state._turn_output_tokens
    cached_tokens = state.cached_tokens + state._turn_cached_tokens
    tokens = state.tokens + state._turn_tokens
    cost = state.cost + state._turn_cost
    turn_cost = (state._turn_cost if state.running or not state._done_seen
                 else state.last_turn_cost)
    context = "—"
    if state.context_tokens is not None:
        context = f"{state.context_tokens:,}"
        if state.context_window:
            context += f" / {state.context_window:,}"
        if state.context_percent is not None:
            context += f" ({state.context_percent}%)"
    out.append("[dim]◆ 会话用量[/]")
    out.append(f"输入 {input_tokens:,}")
    out.append(f"输出 {output_tokens:,}")
    out.append(f"缓存输入 {cached_tokens:,}")
    out.append(f"合计 {tokens:,}")
    out.append(f"本轮费用 ${turn_cost:.4f}")
    out.append(f"累计费用 ${cost:.4f}")
    if state.max_cost is not None:
        out.append(f"[dim]成本预算 ${cost:.4f} / ${state.max_cost:.2f}[/]")
    if state.max_total_tokens is not None:
        out.append(f"[dim]token 预算 {tokens:,} / {state.max_total_tokens:,}[/]")
    out.append(f"上下文 {context}")
    return "\n".join(out)


def footer_text(state: TuiState) -> str:
    if state.running:
        live = state.elapsed()
        timer = f" · {fmt_duration(live)}" if live is not None else ""
        return (f" ● {state.status}{timer} · Ctrl+C 取消 · F3 会话"
                f" · F4 模型 · F6 推理 · F7 端点 ")
    return (f" ● {state.status} · Enter 发送 · Ctrl+Enter 换行 · F2 侧栏"
            f" · F3 会话 · F4 模型"
            f" · F5 设置 · F6 推理 · F7 端点 · /help ")
