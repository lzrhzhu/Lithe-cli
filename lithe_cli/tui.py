"""Persistent full-screen interface for chat and one-shot runs.

One prompt_toolkit ``Application`` owns the terminal for the whole
session: a conversation pane (task, tool feed, streaming assistant text)
in its own scrollable Window, a status sidebar (run state, tool calls,
todos, token / cost totals) in a separate Window beside it, an input line
and a status bar. The conversation uses an application-managed selectable
buffer, so its copy action is confined to that pane; terminal-native
selection remains terminal-controlled. F2 hides the sidebar entirely and
the conversation keeps a scroll offset (wheel, PgUp/PgDn, Home/End). Turns
stream into the panes and the screen stays up between runs — no falling
back to the raw console after a task finishes.

Everything degrades: without a TTY ``main`` never routes here, and the
pane composition is pure functions (``conversation_rows`` /
``sidebar_rows``) so they render identically under tests.
"""

from __future__ import annotations

import asyncio
import base64
import os
import shutil
import sys
import time

from prompt_toolkit.application import Application
from prompt_toolkit.application.current import get_app
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.clipboard import ClipboardData
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.document import Document
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import (
    BufferControl,
    Dimension,
    DynamicContainer,
    FormattedTextControl,
    HSplit,
    Layout,
    VSplit,
    Window,
)
from prompt_toolkit.lexers import Lexer
from prompt_toolkit.mouse_events import MouseEventType
from prompt_toolkit.styles import Style

from .commands import COMMANDS
from .ui import display_width, pad, tool_call_label, truncate, wrap_text

_TODO_MARKS = {"pending": "[ ]", "in_progress": "[~]", "completed": "[x]", "cancelled": "[-]"}
_TODO_CLASSES = {
    "pending": "",
    "in_progress": "todo-ip",
    "completed": "todo-done",
    "cancelled": "dim",
}
_STATUS_DONE = "done"

_STYLE = Style.from_dict(
    {
        "hdr": "bg:#172033 fg:#e2e8f0 bold",
        "foot": "bg:#111827 fg:#cbd5e1",
        "border": "fg:#475569",
        "title": "fg:#38bdf8 bold",
        "prompt": "fg:#38bdf8 bold",
        "user": "fg:#60a5fa bold",
        "assistant": "fg:#4ade80",
        "tool": "fg:#22d3ee",
        "ok": "fg:#4ade80",
        "err": "fg:#f87171 bold",
        "warn": "fg:#fbbf24",
        "dim": "fg:#94a3b8",
        "reason": "fg:#c084fc",
        "todo-ip": "fg:#fbbf24",
        "todo-done": "fg:#4ade80",
        "st-idle": "fg:#22d3ee bold",
        "st-run": "fg:#fbbf24 bold",
        "st-ok": "fg:#4ade80 bold",
        "st-bad": "fg:#f87171 bold",
    }
)

_WELCOME = (
    "输入任务开始对话；/help 查看命令。",
    "PgUp/PgDn 或滚轮回看；鼠标拖选对话后按 Ctrl+C 复制。",
    "Shift+拖拽是终端原生选区，可能跨栏；F2 可隐藏侧栏。",
)

_SCROLL_STEP = 3


def screen_supported() -> bool:
    """The TUI needs interactive input and output, never pipes or dumb terms."""
    return (
        sys.stdin.isatty()
        and sys.stdout.isatty()
        and os.environ.get("TERM") != "dumb"
    )


class TuiState:
    """Everything the frame renders; mutated by kernel events only."""

    def __init__(
        self,
        model: str,
        workspace: str,
        max_steps: int,
        todos: list[dict] | None = None,
        *,
        profile: str = "",
        session_id: int | None = None,
        session_title: str = "",
    ):
        self.model = model
        self.profile = profile
        self.workspace = workspace
        self.max_steps = max_steps
        self.todos = list(todos or [])
        self.session_id = session_id
        self.session_title = session_title
        # Sessions shown in the sidebar; busy ones carry a live turn badge.
        self.sessions: list[dict] = []
        self.busy_sessions: set[int] = set()
        # Completion sources, kept fresh by the driver (run_screen).
        self.model_candidates: list[str] = []
        self.profile_names: list[str] = []
        # Modal picker ("sessions" | "model"): items are dicts with a kind
        # ("item"/"head"/"hint"), label and payload; cursor points at items.
        self.overlay: str | None = None
        self.overlay_title: str = ""
        self.overlay_items: list[dict] = []
        self.overlay_cursor: int = 0
        self.running = False
        self.status = "就绪"
        self.step = 0
        self.tokens = 0
        self.cost = 0.0
        self.input_tokens = 0
        self.output_tokens = 0
        self.cached_tokens = 0
        self.last_turn_cost = 0.0
        self._turn_tokens = 0
        self._turn_cost = 0.0
        self._turn_input_tokens = 0
        self._turn_output_tokens = 0
        self._turn_cached_tokens = 0
        self._done_seen = True
        self.context_tokens = None
        self.context_window = None
        self.context_percent = None
        self.tools: list[dict] = []
        self.feed: list[tuple[str, str]] = []
        self.streaming = ""
        self.scroll = 0
        self.show_sidebar = True
        self.started: float | None = None
        self.last_status: str | None = None
        self.stop: asyncio.Event | None = None
        self._app: Application | None = None
        self._conversation_buffer: Buffer | None = None
        self._conversation_control: BufferControl | None = None

    # -- feed ---------------------------------------------------------------

    def _conversation_width(self) -> int | None:
        if self._app is None or not self._app.is_running:
            return None
        cols, rows = _term_size(self._app)
        (width, _), _ = body_layout(self, cols, rows - 3)
        return max(1, width - 4)

    def _wrapped_rows(self, text: str) -> int:
        width = self._conversation_width()
        if width is None:
            return len(text.splitlines() or [""])
        return len(wrap_text(text, width))

    def say(self, cls: str, text: str) -> None:
        lines = str(text).splitlines() or [""]
        self.feed.extend((cls, line) for line in lines)
        if self.scroll:
            self.scroll += sum(self._wrapped_rows(line) for line in lines)
        self.invalidate()

    def invalidate(self) -> None:
        if self._app is not None and self._app.is_running:
            self._app.invalidate()

    # -- sessions / overlay ---------------------------------------------------

    def set_sessions(self, rows: list[dict], busy: set[int]) -> None:
        self.sessions = list(rows)
        self.busy_sessions = set(busy)
        self.invalidate()

    def open_overlay(self, name: str, title: str, items: list[dict]) -> None:
        self.overlay = name
        self.overlay_title = title
        self.overlay_items = items
        cursor = next((i for i, it in enumerate(items)
                       if it.get("kind") == "item"), 0)
        self.overlay_cursor = cursor
        self.invalidate()

    def close_overlay(self) -> None:
        self.overlay = None
        self.overlay_items = []
        self.overlay_cursor = 0
        self.invalidate()

    def overlay_move(self, delta: int) -> None:
        items = self.overlay_items
        if not items:
            return
        idx = self.overlay_cursor
        for _ in range(len(items)):
            idx = (idx + delta) % len(items)
            if items[idx].get("kind") == "item":
                break
        self.overlay_cursor = idx
        self.invalidate()

    def overlay_current(self) -> dict | None:
        if 0 <= self.overlay_cursor < len(self.overlay_items):
            item = self.overlay_items[self.overlay_cursor]
            return item if item.get("kind") == "item" else None
        return None

    # -- events -------------------------------------------------------------

    def reset_usage(self) -> None:
        self.tokens = 0
        self.cost = 0.0
        self.input_tokens = 0
        self.output_tokens = 0
        self.cached_tokens = 0
        self.last_turn_cost = 0.0
        self._turn_tokens = 0
        self._turn_cost = 0.0
        self._turn_input_tokens = 0
        self._turn_output_tokens = 0
        self._turn_cached_tokens = 0
        self._done_seen = True
        self.context_tokens = None
        self.context_window = None
        self.context_percent = None

    def _fold_turn(self) -> None:
        """Fold the current turn's usage into session totals and clear it."""
        self.input_tokens += self._turn_input_tokens
        self.output_tokens += self._turn_output_tokens
        self.cached_tokens += self._turn_cached_tokens
        self.tokens += self._turn_tokens
        self.cost += self._turn_cost
        self.last_turn_cost = self._turn_cost
        self._turn_tokens = 0
        self._turn_cost = 0.0
        self._turn_input_tokens = 0
        self._turn_output_tokens = 0
        self._turn_cached_tokens = 0

    def on_event(self, ev: dict) -> None:
        kind = ev.get("type")
        if kind == "run_start":
            self.status = "思考中"
            self.step = 0
            self.scroll = 0
            # A previous round that died without a done event (host-level
            # failure) still burned tokens: keep its measured usage instead
            # of silently dropping it when the accumulators reset.
            if not self._done_seen:
                self._fold_turn()
            self._done_seen = False
            self.context_tokens = None
            self.context_window = None
            self.context_percent = None
            self.tools = []
            self.started = time.monotonic()
        elif kind == "step":
            self.step = int(ev.get("step") or 0)
            self.status = "思考中"
        elif kind == "assistant_delta":
            text = str(ev.get("text") or "")
            if self.scroll and text:
                self.scroll += (
                    self._wrapped_rows(self.streaming + text)
                    - self._wrapped_rows(self.streaming)
                )
            self.streaming += text
        elif kind == "assistant":
            text = str(ev.get("text") or "")
            final_text = text or self.streaming
            streamed_rows = self._wrapped_rows(self.streaming) if self.streaming else 0
            if final_text:
                lines = final_text.splitlines() or [""]
                self.feed.extend(("assistant", line) for line in lines)
                if self.scroll:
                    final_rows = sum(self._wrapped_rows(line) for line in lines)
                    self.scroll += final_rows - streamed_rows
            self.streaming = ""
        elif kind == "tool_call":
            name = str(ev.get("name") or "tool")
            args = ev.get("args")
            args = args if isinstance(args, dict) else {}
            self.tools.append(
                {
                    "id": ev.get("id"),
                    "name": name,
                    "status": "running",
                    "t0": time.monotonic(),
                    "elapsed": None,
                }
            )
            self.say("tool", f"◆ {tool_call_label(name, args)}")
            self.status = f"工具 {name}"
        elif kind == "tool_result":
            self._settle_tool(ev.get("id"), bool(ev.get("ok")), ev.get("summary"))
        elif kind == "todo_change":
            self.todos = list(ev.get("new") or [])
            self.say("dim", f"□ 任务清单已更新（{len(self.todos)} 项）")
        elif kind == "usage":
            self._turn_input_tokens += int(ev.get("prompt_tokens") or 0)
            self._turn_output_tokens += int(ev.get("completion_tokens") or 0)
            self._turn_cached_tokens += int(ev.get("cached_tokens") or 0)
            self._turn_tokens += int(ev.get("total_tokens") or 0)
            self._turn_cost += float(ev.get("cost") or 0)
            if ev.get("context_tokens") is not None:
                self.context_tokens = int(ev["context_tokens"])
            if ev.get("context_window") is not None:
                self.context_window = int(ev["context_window"])
            if ev.get("context_percent") is not None:
                self.context_percent = ev["context_percent"]
        elif kind == "reasoning":
            digest = str(ev.get("text") or ev.get("summary") or "").replace("\n", " ")
            if digest:
                self.say("reason", f"~ {digest[:200]}")
        elif kind == "error":
            self.status = "失败"
            self.say("err", str(ev.get("message") or "运行出错"))
        elif kind == "cancelled":
            self.say("warn", "（已取消）")
        elif kind == "done":
            status = str(ev.get("status") or _STATUS_DONE)
            self.last_status = status
            self.status = {
                "done": "完成",
                "failed": "失败",
                "cancelled": "已取消",
                "max_steps": "步数上限",
                "budget_exceeded": "预算超限",
                "empty_response": "空响应",
            }.get(status, status)
            self.step = int(ev.get("steps") or self.step)
            if ev.get("prompt_tokens") is not None:
                self._turn_input_tokens = int(ev["prompt_tokens"])
            if ev.get("completion_tokens") is not None:
                self._turn_output_tokens = int(ev["completion_tokens"])
            if ev.get("tokens") is not None:
                self._turn_tokens = int(ev["tokens"])
            if ev.get("cost") is not None:
                self._turn_cost = float(ev["cost"])
            self._done_seen = True
            self._fold_turn()
            if ev.get("context_tokens") is not None:
                self.context_tokens = int(ev["context_tokens"])
            if ev.get("context_window") is not None:
                self.context_window = int(ev["context_window"])
            if ev.get("context_percent") is not None:
                self.context_percent = ev["context_percent"]
            if self.started is not None:
                self.status += f" · {time.monotonic() - self.started:.1f}s"
        self.invalidate()

    def _settle_tool(self, tool_id, ok: bool, summary) -> None:
        tool = next(
            (t for t in reversed(self.tools) if t["id"] == tool_id and t["status"] == "running"),
            None,
        )
        if tool is not None:
            tool["elapsed"] = time.monotonic() - tool["t0"]
            tool["status"] = "done" if ok else "failed"
            mark = "✓" if ok else "✗"
            label = str(summary or tool["name"]).replace("\n", " ")
            self.say("ok" if ok else "err", f"{mark} {label} {tool['elapsed']:.1f}s")


class _ConversationLexer(Lexer):
    def __init__(self):
        self.lines: list[list[tuple[str, str]]] = []

    def lex_document(self, document):
        lines = self.lines
        return lambda line: lines[line] if 0 <= line < len(lines) else []


class _SelectableConversationControl(BufferControl):
    def __init__(self, buffer: Buffer, lexer: Lexer, state: TuiState):
        super().__init__(buffer=buffer, lexer=lexer, focus_on_click=True)
        self._state = state

    def mouse_handler(self, mouse_event):
        if mouse_event.event_type == MouseEventType.SCROLL_UP:
            self._state.scroll += _SCROLL_STEP
            self._state.invalidate()
            return None
        if mouse_event.event_type == MouseEventType.SCROLL_DOWN:
            self._state.scroll = max(0, self._state.scroll - _SCROLL_STEP)
            self._state.invalidate()
            return None
        if mouse_event.event_type == MouseEventType.MOUSE_DOWN:
            get_app().layout.current_control = self
        return super().mouse_handler(mouse_event)


def _selection_text(text: str) -> str:
    lines = []
    for line in text.splitlines():
        if line.startswith(("╭", "╰")):
            continue
        if line.startswith("│ "):
            line = line[2:]
        if line.endswith(" │"):
            line = line[:-2]
        lines.append(line.rstrip())
    return "\n".join(lines).strip("\n")


def _send_clipboard(app: Application, text: str) -> None:
    app.clipboard.set_data(ClipboardData(text))
    app.output.write_raw(
        "\x1b]52;c;" + base64.b64encode(text.encode("utf-8")).decode("ascii") + "\x07"
    )
    app.output.flush()


# -- frame composition (pure; shared with tests) ---------------------------

def _status_class(status: str) -> str:
    if "失败" in status or "上限" in status or "超限" in status or "空响应" in status:
        return "st-bad"
    if status in ("就绪",):
        return "st-idle"
    if "完成" in status or "取消" in status:
        return "st-ok"
    return "st-run"


def _wrap_feed(lines: list[tuple[str, str]], inner: int) -> list[tuple[str, str]]:
    wrapped: list[tuple[str, str]] = []
    for cls, text in lines:
        wrapped.extend((cls, seg) for seg in wrap_text(text, inner))
    return wrapped


def _scroll_handler(state: TuiState, delta: int):
    def _on_mouse(event) -> None:
        state.scroll = max(0, state.scroll + delta)
        state.invalidate()

    return _on_mouse


def _box(
    title: str,
    wrapped: list[tuple[str, str]],
    width: int,
    height: int,
    handler=None,
) -> list[list]:
    """A bordered box of exactly *height* rows; *handler* rides content lines."""
    width = max(20, width)
    height = max(4, height)
    inner = max(1, width - 4)
    label = truncate(f" {title} ", inner)
    rows = [
        [
            ("class:border", "╭"),
            ("class:title", label),
            ("class:border", "─" * (width - display_width(label) - 2) + "╮"),
        ]
    ]
    shown = wrapped[-(height - 2):]
    if handler is None:
        body = [(f"class:{cls}" if cls else "", pad(seg, inner)) for cls, seg in shown]
    else:
        body = [
            (f"class:{cls}" if cls else "", pad(seg, inner), handler)
            for cls, seg in shown
        ]
    for seg in body:
        rows.append(
            [("class:border", "│ "), seg, ("class:border", " │")]
        )
    for _ in range(height - 2 - len(body)):
        rows.append([("class:border", "│ "), ("", " " * inner), ("class:border", " │")])
    rows.append([("class:border", "╰" + "─" * (inner + 2) + "╯")])
    return rows


def _panel(title: str, lines: list[tuple[str, str]], width: int, height: int) -> list[list]:
    inner = max(1, max(20, width) - 4)
    return _box(title, _wrap_feed(lines, inner), width, height)


def conversation_rows(state: TuiState, width: int, height: int) -> list[list]:
    """The conversation pane, windowed by *state.scroll* (0 = follow the tail)."""
    inner = max(1, max(20, width) - 4)
    wrapped = _wrap_feed(_conversation_lines(state), inner)
    visible = max(1, height - 2)
    max_scroll = max(0, len(wrapped) - visible)
    state.scroll = min(state.scroll, max_scroll)
    start = max(0, len(wrapped) - visible - state.scroll)
    window = wrapped[start:start + visible]
    return _box("对话", window, width, height, handler=_scroll_handler(state, _SCROLL_STEP))


def sidebar_rows(state: TuiState, width: int, height: int) -> list[list]:
    return _panel("运行状态", _sidebar_lines(state), width, height)


def _conversation_lines(state: TuiState) -> list[tuple[str, str]]:
    lines = list(state.feed)
    if state.streaming:
        lines.append(("assistant", state.streaming))
    if not lines:
        lines = [("dim", text) for text in _WELCOME]
    return lines


def _sidebar_lines(state: TuiState) -> list[tuple[str, str]]:
    cls = _status_class(state.status)
    elapsed = (
        time.monotonic() - state.started
        if state.started is not None and state.running
        else 0.0
    )
    lines: list[tuple[str, str]] = []
    # -- 会话 ----------------------------------------------------------------
    lines.append(("dim", "◆ 会话"))
    if state.session_id is None:
        lines.append(("dim", "（单次运行，无会话）"))
    else:
        dot = "●" if state.session_id in state.busy_sessions else "·"
        title = truncate(state.session_title or "无标题", 18)
        lines.append(("", f"#{state.session_id} {title} {dot}"))
        shown = 0
        for row in state.sessions:
            if shown >= 3:
                break
            if row.get("id") == state.session_id:
                continue
            shown += 1
            mark = "●" if row["id"] in state.busy_sessions else (
                "✓" if row.get("last_status") == "done" else "·")
            other = truncate(str(row.get("title") or ""), 14)
            lines.append(
                ("dim", f" #{row['id']} {other} {mark} {row.get('n_runs', 0)}轮")
            )
        lines.append(("dim", "F3 切换 · /new 新建"))
    # -- 模型 ----------------------------------------------------------------
    lines.append(("dim", "◆ 模型"))
    label = f"{state.profile} · {state.model}" if state.profile else state.model
    lines.append(("", truncate(label, 30)))
    if state.context_window:
        lines.append(("dim", f"窗口 {state.context_window:,} · F4 切换"))
    else:
        lines.append(("dim", "F4 或 /model 切换"))
    # -- 运行 ----------------------------------------------------------------
    lines.append(("dim", "◆ 运行"))
    lines.append((cls, f"● {state.status}"))
    lines.append(("dim", f"步骤 {state.step}/{state.max_steps} · {elapsed:.1f}s"))
    # -- 工具 ----------------------------------------------------------------
    lines.append(("dim", "◆ 工具"))
    if not state.tools:
        lines.append(("dim", "暂无调用"))
    for tool in state.tools[-4:]:
        mark = {"running": "…", "done": "✓", "failed": "✗"}.get(tool["status"], "·")
        suffix = f" {tool['elapsed']:.1f}s" if tool.get("elapsed") is not None else ""
        lines.append(("ok" if tool["status"] == "done" else "err" if tool["status"] == "failed" else "tool",
                      f"{mark} {tool['name']}{suffix}"))
    # -- 待办 ----------------------------------------------------------------
    done = sum(todo.get("status") == "completed" for todo in state.todos)
    lines.append(("dim", f"◆ 待办 {done}/{len(state.todos)}"))
    if not state.todos:
        lines.append(("dim", "暂无任务清单"))
    for todo in state.todos[-5:]:
        mark = _TODO_MARKS.get(todo.get("status"), "[ ]")
        cls_todo = _TODO_CLASSES.get(todo.get("status"), "")
        lines.append((cls_todo, f"{mark} {todo.get('content', '')}"))
    # -- 会话用量 --------------------------------------------------------------
    input_tokens = state.input_tokens + state._turn_input_tokens
    output_tokens = state.output_tokens + state._turn_output_tokens
    cached_tokens = state.cached_tokens + state._turn_cached_tokens
    tokens = state.tokens + state._turn_tokens
    cost = state.cost + state._turn_cost
    if state.running or not state._done_seen:
        turn_cost = state._turn_cost
    else:
        turn_cost = state.last_turn_cost
    context = "—"
    if state.context_tokens is not None:
        context = f"{state.context_tokens:,}"
        if state.context_window:
            context += f" / {state.context_window:,}"
        if state.context_percent is not None:
            context += f" ({state.context_percent}%)"
    lines.extend(
        [
            ("dim", "◆ 会话用量"),
            ("", f"输入 {input_tokens:,}"),
            ("", f"输出 {output_tokens:,}"),
            ("", f"缓存输入 {cached_tokens:,}"),
            ("", f"合计 {tokens:,}"),
            ("", f"本轮费用 ${turn_cost:.4f}"),
            ("", f"累计费用 ${cost:.4f}"),
            ("", f"上下文 {context}"),
        ]
    )
    return lines


def _header_row(state: TuiState, width: int, version: str) -> list:
    chip = f" {state.status} "
    who = f"{state.profile} · {state.model}" if state.profile else state.model
    if state.session_id is not None:
        title = truncate(state.session_title or "无标题", 16)
        who = f"▣ #{state.session_id} {title} │ {who}"
    left = truncate(
        f" lithe {version} · {who} · {state.workspace}",
        width - display_width(chip) - 1,
    )
    return [
        ("class:hdr", pad(left, width - display_width(chip))),
        (f"class:hdr class:{_status_class(state.status)}", chip),
    ]


def body_layout(state: TuiState, width: int, height: int):
    """Pane geometry: (conversation w×h, sidebar w×h | None)."""
    height = max(4, height)
    if not state.show_sidebar:
        return (width, height), None
    if width >= 76:
        side_w = min(44, max(30, width // 3))
        return (width - side_w, height), (side_w, height)
    if height < 8:
        return (width, height), None
    conv_h = max(4, height * 3 // 5)
    return (width, conv_h), (width, height - conv_h)


def compose_footer(state: TuiState, width: int, running_hint: str) -> list:
    """Status + key hints only; usage lives in the sidebar, not down here."""
    dot = f"class:foot class:{_status_class(state.status)}"
    left = f" ● {state.status} "
    room = width - display_width(running_hint) - display_width(left) - 2
    text = left + (" " * max(1, room)) if room >= 1 else truncate(left, width - display_width(running_hint) - 1)
    return [(dot, " "), ("class:foot", pad(text, width - display_width(running_hint))), ("class:foot", running_hint)]


# -- overlay pickers (pure item/row builders; shared with tests) -------------

def sessions_overlay_items(session_rows: list[dict], busy: set[int]) -> list[dict]:
    """Items for the session picker: one selectable row per conversation."""
    items = []
    for row in session_rows:
        mark = "●" if row["id"] in busy else (
            "✓" if row.get("last_status") == "done" else "·")
        title = row.get("title") or "(无标题)"
        items.append({
            "kind": "item",
            "label": f"#{row['id']} {mark} {row.get('n_runs', 0)}轮  {title}",
            "conv_id": row["id"],
        })
    if not items:
        items.append({"kind": "hint", "label": "（暂无会话，按 n 新建）"})
    items.append({"kind": "hint",
                  "label": "Enter 切换 · n 新建 · d 删除 · Esc 关闭"})
    return items


def models_overlay_items(
    profile_names: list[str],
    endpoints: dict[str, dict],
    current_profile: str | None,
    current_model: str | None,
    current_candidates: list[str],
) -> list[dict]:
    """Items for the model picker: current profile first, then the rest."""
    items: list[dict] = []
    order = ([current_profile] if current_profile else []) + [
        p for p in profile_names if p != current_profile
    ]
    if not order:
        items.append({"kind": "hint",
                      "label": "（无已保存档案；/model 名称 直接切换）"})
    for profile in order:
        ep = endpoints.get(profile) or {}
        items.append({"kind": "head", "label": f"── {profile} ──"})
        if profile == current_profile:
            models = list(current_candidates)
        else:
            models = []
            if ep.get("model"):
                models.append(ep["model"])
            for name in ep.get("cached_models") or []:
                if name not in models:
                    models.append(name)
        for name in models:
            active = profile == current_profile and name == current_model
            items.append({
                "kind": "item",
                "label": f"{'●' if active else ' '} {name}",
                "model": name,
                "profile": profile,
                "active": active,
            })
        if not models:
            items.append({"kind": "hint", "label": "  （无缓存模型，r 拉取）"})
    items.append({"kind": "hint",
                  "label": "Enter 切换 · s 存为档案默认 · r 拉取列表 · Esc 关闭"})
    return items


def picker_rows(state: TuiState, width: int, height: int) -> list[list]:
    """The modal picker panel that replaces the body while an overlay is open.

    Long lists window around the cursor row (like the conversation pane
    follows its tail): the cursor is always visible, whether at the top of
    a long list or the bottom.
    """
    inner = max(1, max(20, width) - 4)
    wrapped: list[tuple[str, str]] = []
    spans: list[tuple[int, int, bool]] = []  # (start, end, is_cursor)
    for i, item in enumerate(state.overlay_items):
        is_item = item.get("kind") == "item"
        cls = "" if is_item else "dim"
        label = item.get("label", "")
        if is_item and i == state.overlay_cursor:
            cls = "tool"
            label = f"▸ {label}"
        start = len(wrapped)
        wrapped.extend((cls, seg) for seg in wrap_text(label, inner))
        spans.append((start, len(wrapped), cls == "tool"))
    visible = max(1, height - 2)
    window_start = max(0, len(wrapped) - visible)
    for start, end, is_cursor in spans:
        if is_cursor:
            if end <= window_start:  # cursor above the window → pin to top
                window_start = start
            elif start >= window_start + visible:  # below → pin to bottom
                window_start = max(0, end - visible)
            break
    window = wrapped[window_start:window_start + visible]
    return _box(state.overlay_title or "选择", window, width, height)


class SlashCompleter(Completer):
    """Completes ``/``-commands and their known arguments (models, profiles)."""

    def __init__(self, state: TuiState):
        self.state = state

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        if not text.startswith("/"):
            return
        if " " not in text:
            for name in sorted(COMMANDS):
                if f"/{name}".startswith(text):
                    args = COMMANDS[name][0]
                    yield Completion(
                        f"/{name} ", start_position=-len(text),
                        display=f"/{name} {args}".rstrip(),
                    )
            return
        head, _, arg = text.partition(" ")
        command = head[1:].lower()
        words: list[str] = []
        if command == "model":
            words = self.state.model_candidates
        elif command == "profile":
            words = self.state.profile_names
        elif command == "resume":
            words = [f"#{row['id']}" for row in self.state.sessions]
        for word in words:
            if word.startswith(arg) and word != arg:
                yield Completion(word, start_position=-len(arg))


# -- application ------------------------------------------------------------

def _chat_history():
    """Persistent chat history (prompt_toolkit FileHistory), shared with the plain REPL."""
    from .prompts import open_history

    return open_history()


def _term_size(app: Application | None) -> tuple[int, int]:
    if app is not None and app.is_running:
        size = app.output.get_size()
        return size.columns, size.rows
    cols, rows = shutil.get_terminal_size((100, 30))
    return cols, rows


def build_app(
    state: TuiState,
    mode: str,
    version: str,
    on_line,
    on_overlay_select=None,
    on_overlay_key=None,
) -> tuple[Application, Buffer]:
    """Assemble the full-screen Application; the input line drives *on_line*.

    The conversation pane owns a selectable BufferControl, so in-app mouse
    selection and copying cannot include the sidebar. The conversation keeps
    a scroll offset (PgUp/PgDn/Home/End and the mouse wheel); F2 hides the
    sidebar for terminals where native selection is preferred. F3/F4 open
    the session/model pickers — modal panels that swap the body area; their
    Enter/letter keys are routed to *on_overlay_select* / *on_overlay_key*
    (both optional; without them the pickers are read-only views).
    """
    kb = KeyBindings()
    conversation_buffer = Buffer(read_only=True)
    conversation_lexer = _ConversationLexer()
    conversation_control = _SelectableConversationControl(
        conversation_buffer, conversation_lexer, state
    )
    state._conversation_buffer = conversation_buffer
    state._conversation_control = conversation_control

    def _select(item: dict | None) -> None:
        if item is not None and on_overlay_select is not None:
            on_overlay_select(state.overlay, item)

    def _key(action: str) -> None:
        if on_overlay_key is not None:
            on_overlay_key(state.overlay, action)

    overlay_open = Condition(lambda: state.overlay is not None)

    @kb.add("c-c", eager=True)
    def _cancel_or_copy(event):
        if state.overlay is not None:
            state.close_overlay()
            return
        if (
            event.app.layout.current_control is conversation_control
            and conversation_buffer.selection_state is not None
        ):
            data = conversation_buffer.copy_selection()
            text = _selection_text(data.text)
            if not text.strip():
                state.say("warn", "选区没有可复制的对话文字")
            elif len(text.encode("utf-8")) > 100_000:
                state.say("warn", "选区超过终端剪贴板大小限制")
            else:
                try:
                    _send_clipboard(event.app, text)
                except (OSError, RuntimeError, AttributeError) as exc:
                    state.say("err", f"复制失败：{exc}")
                else:
                    state.say("ok", "已复制选中的对话内容")
            return
        if state.running and state.stop is not None:
            state.stop.set()
            state.say("warn", "（正在取消本轮…）")
        else:
            event.app.exit()

    if mode == "run":

        @kb.add("q", eager=True)
        @kb.add("enter", eager=True)
        def _quit(event):
            if not state.running:
                event.app.exit()

    def _page(dy: int):
        def _scroll(event):
            _, rows = _term_size(state._app)
            step = max(1, (rows - 5) // 2)
            state.scroll = max(0, state.scroll + dy * step)
            state.invalidate()

        return _scroll

    kb.add("pageup")(_page(1))
    kb.add("pagedown")(_page(-1))
    kb.add("home")(lambda event: (setattr(state, "scroll", 10**9), state.invalidate()))
    kb.add("end")(lambda event: (setattr(state, "scroll", 0), state.invalidate()))

    def _toggle_sidebar(event):
        state.show_sidebar = not state.show_sidebar
        state.invalidate()

    kb.add("f2")(_toggle_sidebar)

    # -- pickers: modal navigation while an overlay is open -------------------
    kb.add("f3")(lambda event: on_line("/sessions"))
    kb.add("f4")(lambda event: on_line("/model"))

    @kb.add("escape", filter=overlay_open, eager=True)
    @kb.add("q", filter=overlay_open, eager=True)
    def _overlay_close(event):
        state.close_overlay()

    @kb.add("up", filter=overlay_open, eager=True)
    @kb.add("c-p", filter=overlay_open, eager=True)
    def _overlay_up(event):
        state.overlay_move(-1)

    @kb.add("down", filter=overlay_open, eager=True)
    @kb.add("c-n", filter=overlay_open, eager=True)
    def _overlay_down(event):
        state.overlay_move(1)

    @kb.add("enter", filter=overlay_open, eager=True)
    def _overlay_enter(event):
        _select(state.overlay_current())

    @kb.add("n", filter=overlay_open, eager=True)
    @kb.add("d", filter=overlay_open, eager=True)
    @kb.add("r", filter=overlay_open, eager=True)
    @kb.add("s", filter=overlay_open, eager=True)
    def _overlay_letter(event):
        _key(event.key)

    buffer = Buffer(
        history=_chat_history() if mode == "chat" else None,
        multiline=False,
        completer=SlashCompleter(state) if mode == "chat" else None,
        complete_while_typing=Condition(lambda: state.overlay is None),
        read_only=Condition(
            lambda: state.running or mode == "run" or state.overlay is not None
        ),
        accept_handler=lambda buff: (on_line(buff.text), False)[1],
    )

    def _header():
        cols, _ = _term_size(state._app)
        return FormattedText(_header_row(state, cols, version))

    def _refresh_conversation(width: int, height: int) -> None:
        rows = conversation_rows(state, width, height)
        styled_lines = [
            [(str(fragment[0]), str(fragment[1])) for fragment in row]
            for row in rows
        ]
        conversation_lexer.lines = styled_lines
        text = "\n".join("".join(value for _, value in row) for row in styled_lines)
        if conversation_buffer.text != text:
            conversation_buffer.set_document(
                Document(text, cursor_position=len(text)),
                bypass_readonly=True,
            )

    def _side_text():
        cols, rows = _term_size(state._app)
        _, side = body_layout(state, cols, rows - 3)
        fragments = []
        if side is not None:
            for row in sidebar_rows(state, side[0], side[1]):
                fragments.extend(row)
                fragments.append(("", "\n"))
        return FormattedText(fragments[:-1] if fragments else "")

    def _overlay_text():
        cols, rows = _term_size(state._app)
        fragments = []
        for row in picker_rows(state, cols, rows - 3):
            fragments.extend(row)
            fragments.append(("", "\n"))
        return FormattedText(fragments[:-1] if fragments else "")

    def _body():
        cols, rows = _term_size(state._app)
        if state.overlay is not None:
            # the picker replaces the body: unambiguous modal focus, no
            # mouse-selection interplay with the hidden panes
            return Window(
                FormattedTextControl(_overlay_text, show_cursor=False),
                wrap_lines=False,
            )
        (cw, ch), side = body_layout(state, cols, rows - 3)
        _refresh_conversation(cw, ch)
        conv = Window(
            conversation_control,
            width=Dimension.exact(cw),
            height=Dimension.exact(ch),
            wrap_lines=False,
        )
        if side is None:
            return conv
        side_win = Window(
            FormattedTextControl(_side_text, show_cursor=False),
            width=Dimension.exact(side[0]),
            height=Dimension.exact(side[1]),
            wrap_lines=False,
        )
        if side[0] >= 30 and side[1] == ch:
            return VSplit([conv, side_win])
        return HSplit([conv, side_win])

    def _footer():
        cols, _ = _term_size(state._app)
        if state.overlay is not None:
            hint = " ↑↓ 选择 · Enter 确认 · Esc 关闭 "
        elif mode == "run":
            hint = " q 退出 · F2 侧栏 " if not state.running else " Ctrl+C 取消 · PgUp/PgDn 滚动 "
        elif state.running:
            hint = " Ctrl+C 取消本轮 · F3 会话 · F4 模型 "
        else:
            hint = " Enter 发送 · F2 侧栏 · F3 会话 · F4 模型 · /help 命令 "
        return FormattedText(compose_footer(state, cols, hint))

    def _prompt_text():
        return FormattedText([("class:prompt", "lithe ❯ ")])

    header_win = Window(FormattedTextControl(_header, show_cursor=False), height=Dimension.exact(1))
    body_container = DynamicContainer(_body)
    input_row = VSplit(
        [
            Window(FormattedTextControl(_prompt_text, show_cursor=False), width=Dimension.exact(8)),
            Window(BufferControl(buffer=buffer), wrap_lines=False),
        ]
    )
    footer_win = Window(FormattedTextControl(_footer, show_cursor=False), height=Dimension.exact(1))
    app = Application(
        layout=Layout(HSplit([header_win, body_container, input_row, footer_win])),
        key_bindings=kb,
        style=_STYLE,
        full_screen=True,
        mouse_support=True,
    )
    app.layout.focus(buffer)
    return app, buffer


_CHAT_KEYS = """按键：PgUp/PgDn·滚轮 滚动对话，Home/End 跳到顶/底。
F2 侧栏 · F3 会话选择器 · F4 模型选择器（Esc 关闭）。
鼠标拖选左侧对话后按 Ctrl+C 复制，仅会复制对话选区；
Shift+拖拽交由终端原生选区处理，可能同时选中右侧内容。"""

_SAY_CLASSES = {"user", "assistant", "err", "warn", "ok", "dim", "tool",
                "reason"}


def transcript_feed_lines(rows):
    """Stored session messages -> conversation feed rows (TUI rebuild)."""
    import json as _json

    out = []
    for m in rows:
        role = m.get("role")
        content = (m.get("content") or "").strip()
        if role == "user" and content:
            out.append(("user", content))
        elif role == "assistant":
            calls = m.get("tool_calls")
            if isinstance(calls, str):
                try:
                    calls = _json.loads(calls)
                except (ValueError, TypeError):
                    calls = None
            if isinstance(calls, list) and calls:
                names = "、".join(
                    (c.get("function") or {}).get("name", "?")
                    for c in calls if isinstance(c, dict)
                )
                out.append(("tool", "◆ " + names))
            if content:
                out.append(("assistant", content))
        elif role == "tool":
            summary = (m.get("content") or "").replace("\n", " ")
            out.append(("dim", "  · " + (m.get("tool_name") or "工具") + "："
                        + truncate(summary, 60)))
    return out


async def run_screen(cfg, task, mode, args=None):
    """Full-screen driver for `lithe chat` (mode='chat') and `lithe run`.

    Chat is Workbench-driven: persistent sessions (F3 picker / /resume),
    live model switching (F4 picker / /model), background turns keep their
    badge when you switch away, and switching back rebuilds the pane from
    the store. Run mode stays a one-shot: one turn, then q to leave.
    """
    import time as _time

    from lithe.bundles import JsonTodoStore

    from . import __version__
    from .agent import execute, todo_store_path
    from .workbench import Workbench

    todo_store = JsonTodoStore(todo_store_path(cfg))
    wb = Workbench(cfg)

    opening = []
    if mode == "chat":
        opening = wb.open(
            resume=getattr(args, "resume", None),
            continue_latest=bool(getattr(args, "cont", False)),
            title=getattr(args, "title", None),
        ).messages

    states = {}

    def _current_cid():
        return wb.current["id"] if wb.current else None

    def _base_state(cid):
        title = ""
        if cid is not None:
            title = (wb.sessions.get(cid) or {}).get("title", "")
        return TuiState(
            cfg.model or "(scripted)",
            str(cfg.workspace_dir.resolve()),
            cfg.max_steps,
            todo_store.list(),
            profile=cfg.profile or "",
            session_id=cid,
            session_title=title,
        )

    def _refresh_meta(st):
        st.model = cfg.model or "(scripted)"
        st.profile = cfg.profile or ""
        if wb.current is not None:
            st.session_id = wb.current["id"]
            st.session_title = wb.current.get("title") or ""
        st.model_candidates = wb.model_candidates()
        st.profile_names = wb.profiles.names()
        st.set_sessions(wb.session_list(), wb.busy_ids())

    def _state_for(cid):
        if cid not in states:
            st = _base_state(cid)
            for cls, text in transcript_feed_lines(wb.sessions.transcript(cid)):
                st.feed.append((cls, text))
            usage = wb.sessions.usage_snapshot(cid)
            if usage.get("known"):
                st.input_tokens = usage["prompt_tokens"]
                st.output_tokens = usage["completion_tokens"]
                st.cached_tokens = usage["cached_tokens"]
                st.tokens = usage["total_tokens"]
            st.cost = usage["cost"]
            states[cid] = st
        _refresh_meta(states[cid])
        return states[cid]

    state = _state_for(_current_cid()) if _current_cid() is not None \
        else _base_state(None)
    for cls, text in opening:
        state.say(cls if cls in _SAY_CLASSES else "dim", text)

    def _activate(cid):
        nonlocal state
        state = _state_for(cid)
        state._app = app
        state.scroll = 0
        state.invalidate()

    def _say_result(r):
        for cls, text in r.messages:
            state.say(cls if cls in _SAY_CLASSES else "dim", text)

    # -- workbench events -> active state ------------------------------------

    def _on_workbench_event(cid, ev):
        t = ev.get("type")
        if t == "session_busy":
            if cid == _current_cid():
                state.running = True
                state.started = _time.monotonic()
                state.stop = (wb.turns.get(cid) or {}).get("stop")
            state.set_sessions(wb.session_list(), wb.busy_ids())
            return
        if t == "session_idle":
            if cid == _current_cid():
                state.running = False
                state.stop = None
            state.set_sessions(wb.session_list(), wb.busy_ids())
            return
        if t == "command_output":
            state.say(ev.get("style") if ev.get("style") in _SAY_CLASSES
                      else "dim", ev.get("text", ""))
            return
        if t == "undo_done":
            state.say("ok", "已撤销 " + str(ev.get("reverted", 0)) + " 个操作"
                            "（run " + str(ev.get("run_id")) + "）")
            return
        if cid in (-1, _current_cid()):
            state.on_event(ev)

    wb.subscribe(_on_workbench_event)

    # -- pickers -------------------------------------------------------------

    def _open_picker(name):
        if name == "sessions":
            state.open_overlay(
                "sessions", "会话",
                sessions_overlay_items(wb.session_list(), wb.busy_ids()),
            )
        elif name == "model":
            endpoints = {}
            for p in wb.profiles.names():
                try:
                    endpoints[p] = wb.profiles.endpoint(p)
                except SystemExit:
                    pass
            state.open_overlay(
                "model", "模型",
                models_overlay_items(
                    wb.profiles.names(), endpoints,
                    cfg.profile, cfg.model, wb.model_candidates(),
                ),
            )

    def _after_switch():
        cid = _current_cid()
        if cid is not None:
            _activate(cid)

    def on_overlay_select(name, item):
        state.close_overlay()
        if not item:
            return
        if name == "sessions":
            _say_result(wb.switch_session(str(item["conv_id"])))
            _after_switch()
        elif name == "model":
            if item.get("profile") and item["profile"] != cfg.profile:
                _say_result(wb.set_profile(item["profile"]))
            _say_result(wb.set_model(item["model"]))
            _refresh_meta(state)

    def on_overlay_key(name, key):
        if name == "sessions":
            if key == "n":
                state.close_overlay()
                wb.new_session()
                state.say("dim", "已开始新会话（/resume 可切回）")
                _after_switch()
            elif key == "d":
                item = state.overlay_current()
                state.close_overlay()
                if item:
                    _say_result(wb.delete_session(str(item["conv_id"])))
                    if wb.current is None:
                        wb.new_session()
                    _after_switch()
            elif key == "r":
                state.close_overlay()
                if buffer is not None:
                    buffer.text = "/rename "
        elif name == "model":
            if key == "r":
                state.close_overlay()
                on_line("/models")
            elif key == "s":
                item = state.overlay_current()
                state.close_overlay()
                if item:
                    if item.get("profile") and item["profile"] != cfg.profile:
                        _say_result(wb.set_profile(item["profile"]))
                    _say_result(wb.set_model(item["model"], save=True))
                    _refresh_meta(state)

    # -- input ----------------------------------------------------------------

    def on_line(text):
        line = text.strip()
        if not line or state.running:
            return
        if not line.startswith("/"):
            state.say("user", line)
        try:
            r = wb.dispatch(line)
        except SystemExit as exc:
            state.say("err", str(exc.code))
            return
        _say_result(r)
        if line == "/help":
            state.say("dim", _CHAT_KEYS)
        if r.toggle_sidebar:
            state.show_sidebar = not state.show_sidebar
            state.say("dim", "（侧栏已隐藏，F2 或 /sidebar 恢复）"
                      if not state.show_sidebar else "（侧栏已恢复）")
        if r.action == "exit":
            state._app.exit()
            return
        if r.awaitable is not None:
            asyncio.create_task(r.awaitable())
        if r.overlay in ("sessions", "model"):
            _open_picker(r.overlay)
        if r.overlay == "rebuild":
            _after_switch()
        elif r.changed:
            _refresh_meta(state)
        if r.action == "submit":
            state.running = True  # immediate feedback; session_busy confirms
            state.started = _time.monotonic()
            state.scroll = 0
            state.invalidate()
            asyncio.create_task(wb.submit(r.text))

    # -- one-shot run mode ----------------------------------------------------

    async def _one_shot(text):
        stop = asyncio.Event()
        state.stop = stop
        state.running = True
        state.started = _time.monotonic()
        state.invalidate()
        try:
            await execute(cfg, text, on_event=state.on_event, stop=stop)
        except Exception as exc:  # noqa: BLE001
            state.say("err", "运行出错：" + str(exc))
        finally:
            state.running = False
            state.stop = None
            state.invalidate()

    app, buffer = build_app(state, mode, __version__, on_line,
                            on_overlay_select=on_overlay_select,
                            on_overlay_key=on_overlay_key)
    state._app = app
    if mode == "run" and task:

        async def _start():
            state.say("user", task)
            await _one_shot(task)

        asyncio.create_task(_start())
    await app.run_async()
    return 0 if mode != "run" else (0 if state.last_status == "done" else 1)
