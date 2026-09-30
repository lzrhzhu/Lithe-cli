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
    """The TUI needs a real terminal (never pipes, CI or dumb terms)."""
    return sys.stdout.isatty() and os.environ.get("TERM") != "dumb"


class TuiState:
    """Everything the frame renders; mutated by kernel events only."""

    def __init__(
        self,
        model: str,
        workspace: str,
        max_steps: int,
        todos: list[dict] | None = None,
    ):
        self.model = model
        self.workspace = workspace
        self.max_steps = max_steps
        self.todos = list(todos or [])
        self.running = False
        self.status = "就绪"
        self.step = 0
        self.tokens = 0
        self.cost = 0.0
        self.input_tokens = 0
        self.output_tokens = 0
        self.cached_tokens = 0
        self._turn_tokens = 0
        self._turn_cost = 0.0
        self._turn_input_tokens = 0
        self._turn_output_tokens = 0
        self._turn_cached_tokens = 0
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

    def say(self, cls: str, text: str) -> None:
        for line in str(text).splitlines() or [""]:
            self.feed.append((cls, line))
        del self.feed[:-400]
        self.invalidate()

    def invalidate(self) -> None:
        if self._app is not None and self._app.is_running:
            self._app.invalidate()

    # -- events -------------------------------------------------------------

    def reset_usage(self) -> None:
        self.tokens = 0
        self.cost = 0.0
        self.input_tokens = 0
        self.output_tokens = 0
        self.cached_tokens = 0
        self._turn_tokens = 0
        self._turn_cost = 0.0
        self._turn_input_tokens = 0
        self._turn_output_tokens = 0
        self._turn_cached_tokens = 0
        self.context_tokens = None
        self.context_window = None
        self.context_percent = None

    def on_event(self, ev: dict) -> None:
        kind = ev.get("type")
        if kind == "run_start":
            self.status = "思考中"
            self.step = 0
            self.scroll = 0
            self._turn_tokens = 0
            self._turn_cost = 0.0
            self._turn_input_tokens = 0
            self._turn_output_tokens = 0
            self._turn_cached_tokens = 0
            self.context_tokens = None
            self.context_window = None
            self.context_percent = None
            self.tools = []
            self.started = time.monotonic()
        elif kind == "step":
            self.step = int(ev.get("step") or 0)
            self.status = "思考中"
        elif kind == "assistant_delta":
            self.streaming += str(ev.get("text") or "")
        elif kind == "assistant":
            text = str(ev.get("text") or "")
            if text and not self.streaming:
                self.say("assistant", text)
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
            self.input_tokens += self._turn_input_tokens
            self.output_tokens += self._turn_output_tokens
            self.cached_tokens += self._turn_cached_tokens
            self.tokens += self._turn_tokens
            self.cost += self._turn_cost
            self._turn_tokens = 0
            self._turn_cost = 0.0
            self._turn_input_tokens = 0
            self._turn_output_tokens = 0
            self._turn_cached_tokens = 0
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
    lines = [
        (cls, f"● {state.status}"),
        ("dim", f"步骤 {state.step}/{state.max_steps} · {elapsed:.1f}s"),
        ("dim", f"模型 {state.model}"),
        ("dim", f"工作区 {state.workspace}"),
        ("dim", "◆ 工具"),
    ]
    if not state.tools:
        lines.append(("dim", "暂无调用"))
    for tool in state.tools[-4:]:
        mark = {"running": "…", "done": "✓", "failed": "✗"}.get(tool["status"], "·")
        suffix = f" {tool['elapsed']:.1f}s" if tool.get("elapsed") is not None else ""
        lines.append(("ok" if tool["status"] == "done" else "err" if tool["status"] == "failed" else "tool",
                      f"{mark} {tool['name']}{suffix}"))
    done = sum(todo.get("status") == "completed" for todo in state.todos)
    lines.append(("dim", f"◆ 待办 {done}/{len(state.todos)}"))
    if not state.todos:
        lines.append(("dim", "暂无任务清单"))
    for todo in state.todos[-5:]:
        mark = _TODO_MARKS.get(todo.get("status"), "[ ]")
        cls_todo = _TODO_CLASSES.get(todo.get("status"), "")
        lines.append((cls_todo, f"{mark} {todo.get('content', '')}"))
    input_tokens = state.input_tokens + state._turn_input_tokens
    output_tokens = state.output_tokens + state._turn_output_tokens
    cached_tokens = state.cached_tokens + state._turn_cached_tokens
    tokens = state.tokens + state._turn_tokens
    cost = state.cost + state._turn_cost
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
            ("", f"输入 {input_tokens:,} · 输出 {output_tokens:,}"),
            ("", f"缓存输入 {cached_tokens:,}"),
            ("", f"合计 {tokens:,} · ${cost:.4f}"),
            ("", f"上下文 {context}"),
        ]
    )
    return lines


def _header_row(state: TuiState, width: int, version: str) -> list:
    chip = f" {state.status} "
    left = truncate(
        f" lithe {version} · {state.model} · {state.workspace}",
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


def build_app(state: TuiState, mode: str, version: str, on_line) -> tuple[Application, Buffer]:
    """Assemble the full-screen Application; the input line drives *on_line*.

    The conversation pane owns a selectable BufferControl, so in-app mouse
    selection and copying cannot include the sidebar. The conversation keeps
    a scroll offset (PgUp/PgDn/Home/End and the mouse wheel); F2 hides the
    sidebar for terminals where native selection is preferred."""
    kb = KeyBindings()
    conversation_buffer = Buffer(read_only=True)
    conversation_lexer = _ConversationLexer()
    conversation_control = _SelectableConversationControl(
        conversation_buffer, conversation_lexer, state
    )
    state._conversation_buffer = conversation_buffer
    state._conversation_control = conversation_control

    @kb.add("c-c", eager=True)
    def _cancel_or_copy(event):
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

    buffer = Buffer(
        history=_chat_history() if mode == "chat" else None,
        multiline=False,
        read_only=Condition(lambda: state.running or mode == "run"),
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
            conversation_buffer.set_document(Document(text), bypass_readonly=True)

    def _side_text():
        cols, rows = _term_size(state._app)
        _, side = body_layout(state, cols, rows - 3)
        fragments = []
        if side is not None:
            for row in sidebar_rows(state, side[0], side[1]):
                fragments.extend(row)
                fragments.append(("", "\n"))
        return FormattedText(fragments[:-1] if fragments else "")

    def _body():
        cols, rows = _term_size(state._app)
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
        if mode == "run":
            hint = " q 退出 · F2 侧栏 " if not state.running else " Ctrl+C 取消 · PgUp/PgDn 滚动 "
        elif state.running:
            hint = " Ctrl+C 取消本轮 · PgUp/PgDn 滚动 "
        else:
            hint = " Enter 发送 · F2 侧栏 · /help 命令 "
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


_CHAT_HELP = """可用命令：
  /help    显示这段帮助
  /tools   列出当前注册的工具
  /sidebar 显示/隐藏右侧状态栏（F2 同效）
  /new     清空会话历史，重新开始
  /exit    退出（等同 /quit）

按键：PgUp/PgDn·滚轮 滚动对话，Home/End 跳到顶/底。
鼠标拖选左侧对话后按 Ctrl+C 复制，仅会复制对话选区；
Shift+拖拽交由终端原生选区处理，可能同时选中右侧内容。"""


async def run_screen(cfg, task: str | None, mode: str) -> int:
    """Full-screen driver for `lithe chat` (mode='chat') and `lithe run`."""
    from lithe.bundles import JsonlRunStore, JsonTodoStore

    from . import __version__
    from .agent import build_registry, execute, todo_store_path

    todo_store = JsonTodoStore(todo_store_path(cfg))
    state = TuiState(
        cfg.model or "(scripted)",
        str(cfg.workspace_dir.resolve()),
        cfg.max_steps,
        todo_store.list(),
    )
    store = JsonlRunStore(cfg.store_dir)
    run_ids: list[str] = []
    current: dict = {"task": None}

    def _slash(line: str) -> bool:
        if line in ("/exit", "/quit"):
            state._app.exit()
            return True
        if line == "/help":
            state.say("dim", _CHAT_HELP)
            return True
        if line == "/sidebar":
            state.show_sidebar = not state.show_sidebar
            state.say("dim", "（侧栏已隐藏，F2 或 /sidebar 恢复）" if not state.show_sidebar else "（侧栏已恢复）")
            return True
        if line == "/tools":
            names = sorted(build_registry(cfg).names())
            state.say("dim", "已注册工具：" + "、".join(names))
            return True
        if line == "/new":
            run_ids.clear()
            state.reset_usage()
            state.say("dim", "（已清空会话历史与用量统计）")
            return True
        return False

    async def _turn(line: str) -> None:
        stop = asyncio.Event()
        state.stop = stop
        state.running = True
        state.started = time.monotonic()
        state.invalidate()
        try:
            history = store.messages_for_runs(run_ids, cfg.user_id) if run_ids else None
            rid, done, _ = await execute(
                cfg, line, history=history, on_event=state.on_event, stop=stop
            )
            run_ids.append(rid)
            if done.get("status") != "done":
                state.say("warn", f"（本轮状态：{done.get('status')}）")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            state.say("err", f"运行出错：{exc}")
        finally:
            state.running = False
            state.stop = None
            state.invalidate()

    def on_line(text: str) -> None:
        line = text.strip()
        if not line or state.running:
            return
        state.say("user", line)
        if line.startswith("/") and _slash(line):
            return
        current["task"] = asyncio.create_task(_turn(line))

    app, _ = build_app(state, mode, __version__, on_line)
    state._app = app
    if mode == "run" and task:

        async def _start() -> None:
            state.say("user", task)
            await _turn(task)

        asyncio.create_task(_start())
    await app.run_async()
    if current["task"] is not None and not current["task"].done():
        current["task"].cancel()
    if mode == "run":
        return 0 if state.last_status == "done" else 1
    return 0
