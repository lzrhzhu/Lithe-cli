"""The default full-screen front-end (Textual).

Selected by default for `lithe chat` / `lithe run` on an interactive
terminal; the earlier prompt_toolkit screen stays available as
``--ui prompt`` (``LITHE_UI=prompt``). Same Workbench backend as that
screen — this module only replaces the presentation layer. Layout, modals,
scrolling and repaint are Textual's.

What is intentionally reused from the prompt_toolkit screen
(:mod:`lithe_cli.tui`): ``TuiState`` (kernel-event folding, usage
accounting), ``transcript_feed_lines`` (store → feed rows). The
conversation pane renders *from* the state's feed, so both front-ends show
identical content.

Input niceties: ``/``-commands complete inline (suggestions above the
prompt, Tab accepts the first), and ↑/↓ recall the persistent chat history
(shared with the plain REPL through prompt_toolkit's FileHistory store).

Known gap versus the prompt_toolkit screen: no in-app mouse-drag selection
/ OSC 52 copy — use the terminal's native Shift+drag selection instead
(hide the sidebar with F2 first if it gets in the way).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Input, Label, ListItem, ListView, Static

from .tui import TuiState, transcript_feed_lines

_MAX_FEED = 400  # mounted conversation lines before trimming the oldest


# -- pure text builders (shared with tests) -----------------------------------

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
    if state.reasoning_effort:
        out.append(f"[dim]推理 {state.reasoning_effort} · F6 切换[/]")
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
    out.append(f"上下文 {context}")
    return "\n".join(out)


def footer_text(state: TuiState) -> str:
    if state.running:
        return f" ● {state.status} · Ctrl+C 取消 · F3 会话 · F4 模型 · F6 推理 "
    return (f" ● {state.status} · Enter 发送 · F2 侧栏 · F3 会话 · F4 模型"
            f" · F5 设置 · F6 推理 · /help ")


# -- input: completion + persistent history ------------------------------------

def completion_suggestions(
    value: str,
    commands: dict,
    models: list[str],
    profiles: list[str],
    session_ids: list[str],
) -> list[str]:
    """Suggest completions for a half-typed input line (pure; tested).

    ``/prefix`` completes command names; ``/model | /profile | /resume``
    complete their first argument (cached models, saved profiles, session
    ids). Empty result hides the suggestion strip.
    """
    if not value.startswith("/"):
        return []
    if " " not in value:
        prefix = value.rstrip()
        return [
            f"/{name} "
            for name in sorted(commands)
            if prefix == "/" or f"/{name}".startswith(prefix)
        ][:6]
    head, _, arg = value.partition(" ")
    command = head[1:].lower()
    words: list[str] = []
    if command == "model":
        words = models
    elif command == "profile":
        words = profiles
    elif command == "resume":
        words = session_ids
    return [w for w in words if w.startswith(arg) and w != arg][:6]


class HistoryInput(Input):
    """Input with ↑/↓ recall over a persistent FileHistory store."""

    BINDINGS = [
        Binding("up", "history_prev", "上一条", show=False, priority=True),
        Binding("down", "history_next", "下一条", show=False, priority=True),
        Binding("tab", "accept_suggestion", "采纳", show=False,
                priority=True),
    ]

    def __init__(self, *args, file_history=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.file_history = file_history
        self._lines: list[str] = []
        self._pos: int | None = None
        self._draft = ""
        self.suggestion: list[str] = []
        self.reload_history()

    def reload_history(self) -> None:
        self._lines = []
        if self.file_history is not None:
            try:
                self._lines = list(self.file_history.load_history_strings())
            except OSError:
                self._lines = []
        self._pos = None
        self._draft = ""

    def record(self, line: str) -> None:
        line = line.strip()
        if not line:
            return
        if not self._lines or self._lines[-1] != line:
            self._lines.append(line)
            if self.file_history is not None:
                try:
                    self.file_history.store_string(line)
                except OSError:
                    pass
        self._pos = None
        self._draft = ""

    def _apply(self, text: str, pos: int | None) -> None:
        self._pos = pos
        self.value = text
        self.cursor_position = len(text)

    def action_history_prev(self) -> None:
        if not self._lines:
            return
        if self._pos is None:
            self._draft = self.value
            self._apply(self._lines[-1], len(self._lines) - 1)
        elif self._pos > 0:
            self._apply(self._lines[self._pos - 1], self._pos - 1)

    def action_history_next(self) -> None:
        if self._pos is None:
            return
        if self._pos < len(self._lines) - 1:
            self._apply(self._lines[self._pos + 1], self._pos + 1)
        else:
            self._apply(self._draft, None)

    def action_accept_suggestion(self) -> None:
        if self.suggestion:
            accepted = self.suggestion[0]
            self.value = accepted
            self.cursor_position = len(accepted)
            self.suggestion = []


# -- workbench → app message ----------------------------------------------------

class WbEvent(Message):
    """One Workbench event, marshalled into the UI message pump."""

    def __init__(self, cid: int, ev: dict) -> None:
        super().__init__()
        self.cid = cid
        self.ev = ev


# -- pickers ---------------------------------------------------------------------

class PickerModal(ModalScreen):
    """Generic list picker: Enter selects, letters run extra actions,
    Esc/q closes. ``items`` are (payload, markup label) tuples."""

    BINDINGS = [
        Binding("escape", "dismiss_none", "关闭"),
        Binding("q", "dismiss_none", "关闭", show=False),
        Binding("n", "letter('n')", show=False),
        Binding("d", "letter('d')", show=False),
        Binding("r", "letter('r')", show=False),
        Binding("s", "letter('s')", show=False),
        # In-place toggle for the settings picker: flips the focused row
        # without dismissing, so several knobs change in one visit.
        Binding("space", "toggle", "切换（不关闭）", priority=True),
    ]

    def __init__(self, title: str, items: list[tuple[dict, str]],
                 hint: str = "", letter_actions: dict[str, str] | None = None,
                 live_toggle: Callable[[dict], str | None] | None = None):
        super().__init__()
        self.title = title
        self.items = items
        self.hint = hint
        self.letter_actions = letter_actions or {}
        self.live_toggle = live_toggle

    def compose(self) -> ComposeResult:
        with Vertical(id="picker"):
            yield Static(self.title, id="picker-title")
            yield ListView(
                *[ListItem(Label(label)) for _payload, label in self.items],
                id="picker-list",
            )
            if self.hint:
                yield Static(self.hint, id="picker-hint")

    def on_mount(self) -> None:
        self.query_one("#picker-list", ListView).focus()

    def _current_payload(self) -> dict | None:
        view = self.query_one("#picker-list", ListView)
        index = view.index if view.index is not None else 0
        if 0 <= index < len(self.items):
            return self.items[index][0]
        return None

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        view = self.query_one("#picker-list", ListView)
        index = view.children.index(event.item) if event.item in view.children \
            else None
        payload = self.items[index][0] if index is not None else None
        self.dismiss(("select", payload))

    def action_dismiss_none(self) -> None:
        self.dismiss(None)

    def action_letter(self, key: str) -> None:
        name = self.letter_actions.get(key)
        payload = self._current_payload()
        self.dismiss(("letter", name, payload) if name else None)

    def action_toggle(self) -> None:
        """Flip the focused row in place: the ``live_toggle`` callback
        applies the change and returns a rebuilt label (``None`` when the
        row is not toggleable); the modal stays open."""
        if self.live_toggle is None:
            return
        view = self.query_one("#picker-list", ListView)
        index = view.index if view.index is not None else 0
        if not 0 <= index < len(self.items):
            return
        payload = self.items[index][0]
        label = self.live_toggle(payload)
        if label is None:
            return
        self.items[index] = (payload, label)
        row = view.children[index]
        if row.children:
            row.children[0].update(label)


# -- the app ----------------------------------------------------------------------

class LitheApp(App):
    CSS = """
    #top { height: 1; background: #172033; color: #e2e8f0; }
    #body { height: 1fr; }
    #conv { width: 1fr; border: round #475569; padding: 0 1; }
    #side-wrap { width: 44; border: round #475569; padding: 0 1; }
    #prompt { border: round #475569; }
    #foot { height: 1; background: #111827; color: #cbd5e1; }
    #picker { width: 60%; height: auto; max-height: 80%;
              border: round #38bdf8; background: $surface; padding: 1 2; }
    #picker-title { color: #38bdf8; text-style: bold; }
    #picker-list { height: auto; max-height: 16; }
    #picker-hint { color: #94a3b8; }
    .user { color: #60a5fa; }
    .assistant { color: #4ade80; }
    .tool { color: #22d3ee; }
    .ok { color: #4ade80; }
    .err { color: #f87171; }
    .warn { color: #fbbf24; }
    .dim { color: #94a3b8; }
    .reason { color: #c084fc; }
    """

    BINDINGS = [
        Binding("f2", "toggle_sidebar", "侧栏", priority=True),
        Binding("f3", "open_sessions", "会话", priority=True),
        Binding("f4", "open_model", "模型", priority=True),
        Binding("f5", "open_set", "设置", priority=True),
        Binding("f6", "open_reasoning", "推理", priority=True),
        Binding("ctrl+c", "cancel_or_exit", "取消/退出", priority=True),
    ]

    def __init__(self, cfg, task: str | None, mode: str, args=None):
        super().__init__()
        self.cfg = cfg
        # not `self.task`: Textual's App reserves that attribute name
        self.initial_task = task
        self.mode = mode
        self.args = args
        self._shown = 0  # feed lines already mounted

    # -- composition ----------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Static("", id="top")
        with Horizontal(id="body"):
            with VerticalScroll(id="conv"):
                yield Static("", id="streaming", classes="assistant")
            with VerticalScroll(id="side-wrap"):
                yield Static("", id="side", markup=True)
        yield Static("", id="suggest")
        yield HistoryInput(placeholder="输入任务，/help 查看命令", id="prompt")
        yield Static("", id="foot")

    def on_mount(self) -> None:
        from lithe.bundles import JsonTodoStore

        from . import __version__
        from .agent import todo_store_path
        from .prompts import open_history
        from .workbench import Workbench

        self.wb = Workbench(self.cfg)
        self.todo_store = JsonTodoStore(todo_store_path(self.cfg))
        self.version = __version__
        prompt = self.query_one("#prompt", HistoryInput)
        if self.mode == "chat":
            try:
                prompt.file_history = open_history()
            except OSError:
                prompt.file_history = None
            prompt.reload_history()

        opening: list[tuple[str, str]] = []
        if self.mode == "chat":
            opening = self.wb.open(
                resume=getattr(self.args, "resume", None),
                continue_latest=bool(getattr(self.args, "cont", False)),
                title=getattr(self.args, "title", None),
            ).messages
        self.state = self._base_state(self._current_cid())
        self.states: dict[int, TuiState] = {}
        if self._current_cid() is not None:
            self.states[self._current_cid()] = self.state
        for cls, text in opening:
            self.state.say(cls, text)
        self.wb.subscribe(
            lambda cid, ev: self.post_message(WbEvent(cid, ev))
        )
        self._sync_feed()
        self._refresh_chrome()
        self.query_one("#prompt", Input).focus()
        if self.mode == "run" and self.initial_task:
            self.query_one("#prompt", Input).disabled = True
            asyncio.create_task(self._run_one_shot(self.initial_task))

    # -- state management (mirrors the prompt_toolkit driver) -----------------

    def _current_cid(self) -> int | None:
        return self.wb.current["id"] if getattr(self, "wb", None) \
            and self.wb.current else None

    def _base_state(self, cid: int | None) -> TuiState:
        title = ""
        if cid is not None:
            title = (self.wb.sessions.get(cid) or {}).get("title", "")
        return TuiState(
            self.cfg.model or "(scripted)",
            str(self.cfg.workspace_dir.resolve()),
            self.cfg.max_steps,
            self.todo_store.list(),
            profile=self.cfg.profile or "",
            session_id=cid,
            session_title=title,
        )

    def _refresh_meta(self) -> None:
        st = self.state
        st.model = self.cfg.model or "(scripted)"
        st.profile = self.cfg.profile or ""
        st.reasoning_effort = self.cfg.reasoning_effort
        if self.wb.current is not None:
            st.session_id = self.wb.current["id"]
            st.session_title = self.wb.current.get("title") or ""
        st.model_candidates = self.wb.model_candidates()
        st.profile_names = self.wb.profiles.names()
        st.set_sessions(self.wb.session_list(), self.wb.busy_ids())

    def _activate(self, cid: int) -> None:
        if cid not in self.states:
            st = self._base_state(cid)
            st.feed.extend(
                transcript_feed_lines(self.wb.sessions.transcript(cid))
            )
            usage = self.wb.sessions.usage_snapshot(cid)
            if usage.get("known"):
                st.input_tokens = usage["prompt_tokens"]
                st.output_tokens = usage["completion_tokens"]
                st.cached_tokens = usage["cached_tokens"]
                st.tokens = usage["total_tokens"]
            st.cost = usage["cost"]
            self.states[cid] = st
        self.state = self.states[cid]
        self._shown = 0
        conv = self.query_one("#conv", VerticalScroll)
        for child in list(conv.children):
            if child.id != "streaming":
                child.remove()
        self._refresh_meta()
        self._sync_feed()
        self._refresh_chrome()

    # -- conversation rendering (pure render-from-state) ----------------------

    def _sync_feed(self) -> None:
        conv = self.query_one("#conv", VerticalScroll)
        streaming = self.query_one("#streaming", Static)
        feed = self.state.feed
        while self._shown < len(feed):
            cls, text = feed[self._shown]
            self._shown += 1
            conv.mount(Static(text, classes=cls or "dim"),
                       before=streaming)
        non_stream = [c for c in conv.children if c.id != "streaming"]
        for stale in non_stream[:max(0, len(non_stream) - _MAX_FEED)]:
            stale.remove()
        if self.state.streaming:
            streaming.update(self.state.streaming)
        else:
            streaming.update("")
        conv.scroll_end(animate=False)

    def _refresh_chrome(self) -> None:
        self.query_one("#top", Static).update(
            header_text(self.state, getattr(self, "version", ""))
        )
        self.query_one("#side", Static).update(sidebar_markup(self.state))
        self.query_one("#foot", Static).update(footer_text(self.state))
        self.query_one("#side-wrap", VerticalScroll).display = \
            self.state.show_sidebar

    # -- workbench events -------------------------------------------------------

    def on_wb_event(self, message: WbEvent) -> None:
        cid, ev = message.cid, message.ev
        t = ev.get("type")
        if t == "session_busy":
            if cid == self._current_cid():
                self.state.running = True
                self.state.scroll = 0
                handle = self.wb.turns.get(cid) or {}
                self.state.stop = handle.get("stop")
            self._refresh_meta()
            self._refresh_chrome()
            return
        if t == "session_idle":
            if cid == self._current_cid():
                self.state.running = False
                self.state.stop = None
            self._refresh_meta()
            self._refresh_chrome()
            return
        if t == "command_output":
            self.state.say(ev.get("style") or "", ev.get("text", ""))
            self._sync_feed()
            self._refresh_meta()  # e.g. /models just rewrote cached_models
            self._refresh_chrome()
            return
        if t == "undo_done":
            self.state.say("ok", f"已撤销 {ev.get('reverted', 0)} 个操作"
                                 f"（run {ev.get('run_id')}）")
            self._sync_feed()
            return
        if cid in (-1, self._current_cid()):
            self.state.on_event(ev)
            self._sync_feed()
            self._refresh_chrome()

    # -- input -------------------------------------------------------------------

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "prompt":
            return
        from .commands import COMMANDS

        prompt = self.query_one("#prompt", HistoryInput)
        prompt.suggestion = completion_suggestions(
            event.value,
            COMMANDS,
            self.state.model_candidates,
            self.state.profile_names,
            [f"#{row['id']}" for row in self.state.sessions],
        )
        strip = self.query_one("#suggest", Static)
        if prompt.suggestion:
            strip.update("[dim]Tab 采纳 →[/] " +
                         "  ".join(prompt.suggestion[:4]))
            strip.display = True
        else:
            strip.update("")
            strip.display = False

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "prompt":
            return
        line = event.value.strip()
        event.input.value = ""
        self.query_one("#suggest", Static).display = False
        self.query_one("#prompt", HistoryInput).record(line)
        if not line or self.state.running:
            return
        if not line.startswith("/"):
            self.state.say("user", line)
            self._sync_feed()
        try:
            r = self.wb.dispatch(line)
        except SystemExit as exc:
            self.state.say("err", str(exc.code))
            self._sync_feed()
            return
        for cls, text in r.messages:
            self.state.say(cls, text)
        if line == "/help":
            from .tui import _CHAT_KEYS

            self.state.say("dim", _CHAT_KEYS)
        self._sync_feed()
        if r.toggle_sidebar:
            self.state.show_sidebar = not self.state.show_sidebar
        if r.action == "exit":
            self.exit()
            return
        if r.awaitable is not None:
            asyncio.create_task(r.awaitable())
        if r.overlay in ("sessions", "model", "set", "reasoning"):
            self._open_picker(r.overlay)
        if r.overlay == "rebuild" and self._current_cid() is not None:
            self._activate(self._current_cid())
        elif r.changed:
            self._refresh_meta()
            self._refresh_chrome()
        if r.action == "submit":
            self.state.running = True
            self.state.status = "思考中"
            self._refresh_chrome()
            asyncio.create_task(self.wb.submit(r.text))

    # -- pickers -------------------------------------------------------------------

    def _open_picker(self, name: str) -> None:
        if name == "sessions":
            items = []
            for row in self.wb.session_list():
                mark = "●" if row["id"] in self.wb.busy_ids() else (
                    "✓" if row.get("last_status") == "done" else "·")
                items.append((
                    {"conv_id": row["id"]},
                    f"#{row['id']} {mark} {row.get('n_runs', 0)}轮  "
                    f"{row.get('title') or '(无标题)'}",
                ))
            self.push_screen(PickerModal(
                "会话", items,
                "Enter 切换 · n 新建 · d 删除 · r 重命名 · Esc 关闭",
                letter_actions={"n": "_picker_new", "d": "_picker_delete",
                                "r": "_picker_rename"},
            ), self._sessions_picked)
        elif name == "model":
            items = []
            current = self.cfg.profile
            for profile in ([current] if current else []) + [
                    p for p in self.wb.profiles.names() if p != current]:
                ep = self.wb.profiles.endpoint(profile)
                items.append(({"kind": "head"}, f"[dim]── {profile} ──[/]"))
                if profile == current:
                    models = self.wb.model_candidates()
                else:
                    models = ([ep["model"]] if ep.get("model") else []) + [
                        m for m in ep.get("cached_models") or []
                        if m != ep.get("model")
                    ]
                for m in models:
                    active = profile == current and m == self.cfg.model
                    items.append((
                        {"model": m, "profile": profile},
                        f"{'[green]●[/] ' if active else ' '}{m}",
                    ))
            self.push_screen(PickerModal(
                "模型", items,
                "Enter 切换 · s 存为档案默认 · r 拉取列表 · Esc 关闭",
                letter_actions={"s": "_picker_save_model",
                                "r": "_picker_fetch_models"},
            ), self._model_picked)
        elif name == "set":
            items = []
            for row in self.wb.settings_rows():
                value = row["value"]
                if row["kind"] == "bool":
                    shown = "on" if value else "off"
                    items.append((
                        {"key": row["key"]},
                        f"{'[green]●[/] ' if value else '○ '}"
                        f"{row['key']:<10} {shown:<4}{row['label']}",
                    ))
                else:
                    shown = "off" if value is None else value
                    items.append((
                        {"kind": "hint"},
                        f"  {row['key']:<10} {shown!s:<6}{row['label']}"
                        f"（/set {row['key']} 值）",
                    ))
            self.push_screen(PickerModal(
                "运行设置", items,
                "空格 切换（不关闭）· Enter 切换并关闭 · 数值项用 /set 名称 值"
                " · Esc 关闭",
                live_toggle=self._toggle_setting_live,
            ), self._set_picked)
        elif name == "reasoning":
            from .workbench import REASONING_LEVELS

            current = self.cfg.reasoning_effort or "off"
            items = []
            for level in REASONING_LEVELS:
                active = level == current
                label = {"off": "关闭（不发送该字段）"}.get(level, level)
                items.append((
                    {"level": level},
                    f"{'[green]●[/] ' if active else ' '}{label}",
                ))
            self.push_screen(PickerModal(
                "推理强度", items,
                "Enter 应用 · s 存为档案默认 · Esc 关闭",
                letter_actions={"s": "_picker_save_reasoning"},
            ), self._reasoning_picked)

    def action_open_sessions(self) -> None:
        if self.mode == "chat" and not isinstance(self.screen, PickerModal):
            self._open_picker("sessions")

    def action_open_model(self) -> None:
        if not isinstance(self.screen, PickerModal):
            self._open_picker("model")

    def action_open_set(self) -> None:
        if not isinstance(self.screen, PickerModal):
            self._open_picker("set")

    def action_open_reasoning(self) -> None:
        if not isinstance(self.screen, PickerModal):
            self._open_picker("reasoning")

    def _reasoning_picked(self, result) -> None:
        if not result or result[0] != "select" or not result[1]:
            return
        r = self.wb.set_reasoning(result[1]["level"])
        for cls, text in r.messages:
            self.state.say(cls, text)
        self._refresh_meta()
        self._refresh_chrome()
        self._sync_feed()

    def _picker_save_reasoning(self, payload) -> None:
        if not payload:
            return
        r = self.wb.set_reasoning(payload.get("level", ""), save=True)
        for cls, text in r.messages:
            self.state.say(cls, text)
        self._refresh_meta()
        self._refresh_chrome()
        self._sync_feed()

    def _toggle_setting_live(self, payload) -> str | None:
        """F5 picker's in-place toggle (space): flip the knob via the same
        bare ``set_setting`` call /set makes, refresh the chrome, and hand
        back the row's rebuilt label; hints are not toggleable."""
        if not payload or payload.get("kind") == "hint":
            return None
        r = self.wb.set_setting(payload["key"])  # bare value toggles booleans
        for cls, text in r.messages:
            self.state.say(cls, text)
        if r.changed:
            self._refresh_meta()
            self._refresh_chrome()
        self._sync_feed()
        row = next(row for row in self.wb.settings_rows()
                   if row["key"] == payload["key"])
        value = row["value"]
        shown = "on" if value else "off"
        return (f"{'[green]●[/] ' if value else '○ '}"
                f"{row['key']:<10} {shown:<4}{row['label']}")

    def _set_picked(self, result) -> None:
        if not result or result[0] != "select" or not result[1]:
            return
        payload = result[1]
        if payload.get("kind") == "hint":
            return
        r = self.wb.set_setting(payload["key"])  # bare value toggles booleans
        for cls, text in r.messages:
            self.state.say(cls, text)
        if r.changed:
            self._refresh_meta()
            self._refresh_chrome()
        self._sync_feed()

    def _sessions_picked(self, result) -> None:
        if not result:
            return
        kind = result[0]
        if kind == "select" and result[1]:
            r = self.wb.switch_session(str(result[1]["conv_id"]))
            for cls, text in r.messages:
                self.state.say(cls, text)
            self._activate(self.wb.current["id"])
        elif kind == "letter" and result[1]:
            getattr(self, result[1])(result[2])

    def _model_picked(self, result) -> None:
        if not result:
            return
        if result[0] == "letter" and result[1]:
            getattr(self, result[1])(result[2])
            return
        if result[0] != "select" or not result[1]:
            return
        payload = result[1]
        if payload.get("kind") == "head":
            return
        if payload.get("profile") and payload["profile"] != self.cfg.profile:
            r = self.wb.set_profile(payload["profile"])
            for cls, text in r.messages:
                self.state.say(cls, text)
        r = self.wb.set_model(payload["model"])
        for cls, text in r.messages:
            self.state.say(cls, text)
        self._refresh_meta()
        self._refresh_chrome()
        self._sync_feed()

    # picker letter actions (called after the modal dismissed itself)
    def _picker_new(self, payload) -> None:
        self.wb.new_session()
        self.state.say("dim", "已开始新会话（/resume 可切回）")
        self._activate(self.wb.current["id"])

    def _picker_delete(self, payload) -> None:
        if not payload:
            return
        r = self.wb.delete_session(str(payload["conv_id"]))
        for cls, text in r.messages:
            self.state.say(cls, text)
        if self.wb.current is None:
            self.wb.new_session()
        self._activate(self.wb.current["id"])

    def _picker_rename(self, payload) -> None:
        prompt = self.query_one("#prompt", Input)
        prompt.value = "/rename "
        prompt.focus()

    def _picker_save_model(self, payload) -> None:
        if not payload or payload.get("kind") == "head":
            return
        if payload.get("profile") and payload["profile"] != self.cfg.profile:
            r = self.wb.set_profile(payload["profile"])
            for cls, text in r.messages:
                self.state.say(cls, text)
        r = self.wb.set_model(payload["model"], save=True)
        for cls, text in r.messages:
            self.state.say(cls, text)
        self._refresh_meta()
        self._refresh_chrome()
        self._sync_feed()

    def _picker_fetch_models(self, payload) -> None:
        asyncio.create_task(self.wb._fetch_and_report())

    # -- keys -----------------------------------------------------------------------

    def action_toggle_sidebar(self) -> None:
        self.state.show_sidebar = not self.state.show_sidebar
        self._refresh_chrome()

    def action_cancel_or_exit(self) -> None:
        if self.state.running:
            self.wb.cancel(self._current_cid())
            self.state.say("warn", "（正在取消本轮…）")
            self._sync_feed()
        else:
            self.exit()

    # -- one-shot run mode --------------------------------------------------------

    async def _run_one_shot(self, text: str) -> None:
        from .agent import execute

        stop = asyncio.Event()
        self.state.stop = stop
        self.state.running = True
        self.state.status = "思考中"
        self.state.say("user", text)
        self._sync_feed()
        self._refresh_chrome()
        try:
            await execute(self.cfg, text, on_event=self.state.on_event,
                          stop=stop)
        except Exception as exc:  # noqa: BLE001
            self.state.say("err", f"运行出错：{exc}")
        finally:
            self.state.running = False
            self.state.stop = None
            self._sync_feed()
            self._refresh_chrome()
            self.query_one("#prompt", Input).disabled = False


async def _driver(app: LitheApp) -> int:
    await app.run_async()
    return 0 if app.mode != "run" else (
        0 if app.state.last_status == "done" else 1)


def run_textual_screen(cfg, task: str | None, mode: str,
                       args: Any = None) -> int:
    """Drop-in Textual replacement for :func:`lithe_cli.tui.run_screen`."""
    app = LitheApp(cfg, task, mode, args)
    return asyncio.run(_driver(app))
