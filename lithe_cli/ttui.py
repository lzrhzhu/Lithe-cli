"""The full-screen front-end (Textual).

The only interactive UI: `lithe chat` / `lithe run` on an interactive
terminal always launch it; without a TTY `main` falls back to the plain
per-line REPL. Layout, modals, scrolling and repaint are Textual's.

What it builds on from :mod:`lithe_cli.tui`: ``TuiState`` (kernel-event
folding, usage accounting), ``transcript_feed_lines`` (store → feed
rows). The conversation pane renders *from* the state's feed.

Input niceties: ``/``-commands complete inline (suggestions above the
prompt, Tab accepts the first), and ↑/↓ recall the persistent chat history
(shared with the plain REPL through prompt_toolkit's FileHistory store).

Copying: mouse reporting means the terminal's own drag-select and menu
never reach it, so the app brings its own — right-click opens a copy menu,
``/copy`` takes the whole conversation (or the last answer, the user's
lines, the tool output), and ``Ctrl+C`` copies a Textual text selection
when there is one before it falls back to cancel/exit. Delivery is
layered (platform tool → OSC 52 → file) in :mod:`lithe_cli.clipboard`;
Shift+drag still works in terminals that keep native selection.
"""

from __future__ import annotations

import asyncio

from typing import Any

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Static, TextArea

from .clipboard import deliver_clipboard, write_clipboard_file
from .config import lithe_home
from .tui import (
    COPY_SCOPE_LABELS,
    COPY_SCOPES,
    SUBAGENT_FEED_PREFIX,
    TuiState,
    describe_copy,
    feed_text,
    transcript_feed_lines,
)
from .ui import fmt_duration as fmt_duration  # noqa: F401 — legacy re-export
from . import ttui_widgets as _widgets
from .ttui_widgets import (
    ConfirmModal, ConversationPane, HistoryInput, PickerModal, SubagentCard,
    HistoryInputSubmitted, WbEvent, completion_suggestions, selected_text,
)
from .ttui_render import footer_text, header_text, sidebar_markup

_MAX_FEED = 400  # mounted conversation lines before trimming the oldest

# -- the app ----------------------------------------------------------------------

class LitheApp(App):
    CSS = """
    #top { height: 1; background: #172033; color: #e2e8f0; }
    #body { height: 1fr; }
    #main-column { width: 1fr; height: 1fr; }
    #conv { width: 1fr; height: 1fr; border: round #475569; padding: 0 1; }
    /* Subagent cards live inline in the conversation: a one-line heading
       when collapsed (plus the left rail), search + transcript expanded. */
    .subagent-card { height: auto; margin: 0 0 1 0; padding: 0 1 0 0;
                     border-left: round #475569; }
    .subagent-heading { height: 1; width: 1fr; border: none; min-width: 0;
                        background: #1b2438; color: #fbbf24;
                        text-align: left; content-align: left middle; }
    .subagent-search { height: 3; margin: 0 1 0 1; border: round #334155; }
    .subagent-output { height: auto; margin: 0 1 0 1; color: #cbd5e1; }
    #side-wrap { width: 44; border: round #475569; padding: 0 1; }
    #prompt { height: auto; max-height: 8; border: round #475569; }
    #foot { height: 1; background: #111827; color: #cbd5e1; }
    #picker { width: 60%; height: auto; max-height: 80%;
              border: round #38bdf8; background: $surface; padding: 1 2; }
    #picker-title { color: #38bdf8; text-style: bold; }
    #picker-list { height: auto; max-height: 16; }
    #picker-hint { color: #94a3b8; }
    #picker-command { color: #f87171; }
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
        self._cancel_asked = False  # first Ctrl+C cancels, second exits
        self._pending_selection = ""  # selection captured at right-click time
        # model-picker view state: favorites-only filter (letter f)
        self._model_fav_only = False

    # -- composition ----------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Static("", id="top", markup=False)
        with Horizontal(id="body"):
            with Vertical(id="main-column"):
                with ConversationPane(id="conv"):
                    yield Static("", id="streaming", classes="assistant",
                                 markup=False)
            with VerticalScroll(id="side-wrap"):
                yield Static("", id="side", markup=True)
        yield Static("", id="suggest")
        yield HistoryInput(placeholder="输入任务，/help 查看命令", id="prompt")
        yield Static("", id="foot", markup=False)

    def on_mount(self) -> None:
        from . import __version__
        from .prompts import open_history
        from .workbench import Workbench

        self.wb = Workbench(self.cfg)
        # Destructive run_command approval rides a modal; the turn task and
        # the app share one loop, so the middleware can await the human.
        self.wb.approver = self.confirm_command
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
            cid = self._current_cid()
            self._load_session_history(self.state, cid)
            self.states[cid] = self.state
        for cls, text in opening:
            self.state.say(cls, text)
        self.wb.subscribe(
            lambda cid, ev: self.post_message(WbEvent(cid, ev))
        )
        self._sync_feed()
        self._refresh_chrome()
        self.query_one("#prompt", HistoryInput).focus()
        # Live turn timer: kernel events alone leave the screen static
        # during long model calls, so the chrome repaints on a heartbeat
        # while a turn runs (elapsed comes from TuiState.elapsed()).
        self.set_interval(0.5, self._tick_elapsed)
        if self.mode == "run" and self.initial_task:
            self.query_one("#prompt", HistoryInput).disabled = True
            asyncio.create_task(self._run_one_shot(self.initial_task))

    # -- state management --------------------------------------------------------

    def _current_cid(self) -> int | None:
        return self.wb.current["id"] if getattr(self, "wb", None) \
            and self.wb.current else None

    def _base_state(self, cid: int | None) -> TuiState:
        from lithe.bundles import JsonTodoStore

        from .agent import todo_store_path

        title = ""
        if cid is not None:
            title = (self.wb.sessions.get(cid) or {}).get("title", "")
        st = TuiState(
            self.cfg.model or "(scripted)",
            str(self.cfg.workspace_dir.resolve()),
            self.cfg.max_steps,
            JsonTodoStore(todo_store_path(self.cfg, cid)).list(),
            profile=self.cfg.profile or "",
            session_id=cid,
            session_title=title,
        )
        st.max_cost = self.cfg.max_cost
        st.max_total_tokens = self.cfg.max_total_tokens
        return st

    def _load_session_history(self, state: TuiState, cid: int) -> None:
        """Load both the orchestrator feed and subagent cards on first open.

        The feed is built from the interleaved history in one pass, so each
        subagent's marker row (and with it its card) sits where the
        delegation actually happened, not pinned below the conversation.
        """
        history = self.wb.sessions.transcript(
            cid, last=0, include_subagents=True
        )
        rows = transcript_feed_lines(history)
        state.feed.extend(rows[-200:])
        state.restore_subagents(history)
        usage = self.wb.sessions.usage_snapshot(cid)
        if usage.get("known"):
            state.input_tokens = usage["prompt_tokens"]
            state.output_tokens = usage["completion_tokens"]
            state.cached_tokens = usage["cached_tokens"]
            state.tokens = usage["total_tokens"]
        state.cost = usage["cost"]

    def action_refresh_session_history(self) -> None:
        """Reload durable transcript for the active session."""
        cid = self._current_cid()
        if cid is None:
            return
        self.state.feed.clear()
        self.state.subagents.clear()
        self._load_session_history(self.state, cid)
        self._shown = 0
        self._clear_conversation()
        self._sync_feed()

    def _refresh_meta(self) -> None:
        st = self.state
        st.model = self.cfg.model or "(scripted)"
        st.profile = self.cfg.profile or ""
        st.reasoning_effort = self.cfg.reasoning_effort
        st.max_steps = self.cfg.max_steps
        st.max_cost = self.cfg.max_cost
        st.max_total_tokens = self.cfg.max_total_tokens
        if self.wb.current is not None:
            st.session_id = self.wb.current["id"]
            st.session_title = self.wb.current.get("title") or ""
        # completion lane: favorites + recents as qualified refs, then the
        # current profile's bare model ids; the live model always completes
        # (env-only endpoints have no profile section to carry it).
        cands = [
            f"{e['profile']}:{e['model']}"
            for e in self.wb.model_entries()
            if e["section"] in ("fav", "recent")
        ] + [
            e["model"] for e in self.wb.model_entries()
            if e["section"] == "profile" and e["group"] == self.cfg.profile
        ]
        if self.cfg.model and self.cfg.model not in cands:
            cands.insert(0, self.cfg.model)
        st.model_candidates = cands
        st.profile_names = self.wb.profiles.names()
        st.set_sessions(self.wb.session_list(), self.wb.busy_ids())

    def _activate(self, cid: int) -> None:
        from lithe.bundles import JsonTodoStore

        from .agent import todo_store_path

        if cid not in self.states:
            st = self._base_state(cid)
            self._load_session_history(st, cid)
            self.states[cid] = st
            self.state = st
            self._shown = 0
            self._clear_conversation()
            self._sync_feed()
        else:
            self.state = self.states[cid]
            if cid not in self.wb.busy_ids():
                # Returning to an idle session: rebuild from the persisted
                # store, because a turn that finished while another session
                # was active never fed events into this state (and the live
                # feed keeps transient notices a store rebuild would drop).
                # A busy session keeps its live cache so the running turn
                # keeps rendering.
                self.action_refresh_session_history()
        # Reload from the conversation's persisted store when returning to it:
        # a background turn or /undo may have changed its todos meanwhile.
        self.states[cid].todos = JsonTodoStore(
            todo_store_path(self.cfg, cid)
        ).list()
        self._refresh_meta()
        self._refresh_chrome()

    def _clear_conversation(self) -> None:
        """Unmount feed lines and subagent cards, keep the streaming slot."""
        conv = self.query_one("#conv", ConversationPane)
        for child in list(conv.children):
            if child.id != "streaming":
                child.remove()

    # -- conversation rendering (pure render-from-state) ----------------------

    def _sync_feed(self) -> None:
        conv = self.query_one("#conv", ConversationPane)
        streaming = self.query_one("#streaming", Static)
        # Follow the feed only when the reader is already at (or near) the
        # bottom: scrolling up to re-read earlier output mid-run must stick,
        # not be yanked back to the bottom on every event.
        at_bottom = conv.scroll_y >= conv.max_scroll_y - 1
        feed = self.state.feed
        while self._shown < len(feed):
            cls, text = feed[self._shown]
            self._shown += 1
            if cls.startswith(SUBAGENT_FEED_PREFIX):
                # A subagent card mounts where the delegation began, so it
                # scrolls with the conversation instead of squatting in a
                # fixed region below it. Data refresh rides the pass below.
                instance = cls[len(SUBAGENT_FEED_PREFIX):]
                block = self.state.subagents.get(instance)
                if block is not None:
                    card = SubagentCard(instance, block)
                    conv.mount(card, before=streaming)
                    card.call_after_refresh(card.set_data, block)
                continue
            # markup=False: model/tool text is data, not Textual markup —
            # a "[x]" in an answer must render, not parse (and it is what
            # /copy sends to the clipboard verbatim).
            conv.mount(Static(text, classes=cls or "dim", markup=False),
                       before=streaming)
        # Trim the oldest mounted rows, but never a card: a long-running
        # subagent's block must not vanish mid-flight just because the
        # orchestrator talked a lot after delegating.
        non_stream = [c for c in conv.children if c.id != "streaming"]
        stale = [c for c in non_stream[:max(0, len(non_stream) - _MAX_FEED)]
                 if not isinstance(c, SubagentCard)]
        for old in stale:
            old.remove()
        if self.state.streaming:
            streaming.update(self.state.streaming)
        else:
            streaming.update("")
        for child in conv.children:
            if isinstance(child, SubagentCard):
                child._block["expanded"] = child._expanded
                child._block["search"] = child._search
        self.call_after_refresh(self._refresh_subagent_lines)
        if at_bottom:
            conv.scroll_end(animate=False)
            # The pane's extent only grows once the newly mounted lines lay
            # out (next refresh). Anchoring again after that refresh keeps
            # the follow exact: otherwise the scroll lands one refresh short
            # of the bottom and the next event's at-bottom check mis-reads
            # "still reading up there" and stops following for good.
            conv.call_after_refresh(conv.scroll_end, animate=False)

    def _refresh_subagent_lines(self) -> None:
        """Refresh mounted card transcripts after each event, preserving UI."""
        try:
            container = self.query_one("#conv", ConversationPane)
        except Exception:
            return
        for card in container.children:
            if isinstance(card, SubagentCard):
                block = self.state.subagents.get(card.instance)
                if block is not None:
                    card.set_data(block)

    def _refresh_chrome(self) -> None:
        self.query_one("#top", Static).update(
            header_text(self.state, getattr(self, "version", ""))
        )
        self.query_one("#side", Static).update(sidebar_markup(self.state))
        self.query_one("#foot", Static).update(footer_text(self.state))
        self.query_one("#side-wrap", VerticalScroll).display = \
            self.state.show_sidebar

    def _tick_elapsed(self) -> None:
        """Heartbeat for the running turn's live timer (see on_mount)."""
        if self.state.running:
            self._refresh_chrome()

    # -- workbench events -------------------------------------------------------

    def on_wb_event(self, message: WbEvent) -> None:
        cid, ev = message.cid, message.ev
        t = ev.get("type")
        if t == "session_busy":
            if cid == self._current_cid():
                self.state.running = True
                handle = self.wb.turns.get(cid) or {}
                self.state.stop = handle.get("stop")
            self._refresh_meta()
            self._refresh_chrome()
            return
        if t == "session_idle":
            if cid == self._current_cid():
                # No feed rebuild here: the live feed (transient steering
                # notices, elapsed times, subagent cards) is richer than a
                # store reload; _activate rebuilds from the store when the
                # reader returns to the session.
                self.state.running = False
                self.state.stop = None
                self._cancel_asked = False
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
            from lithe.bundles import JsonTodoStore

            from .agent import todo_store_path

            if cid in self.states:
                self.states[cid].todos = JsonTodoStore(
                    todo_store_path(self.cfg, cid)
                ).list()
            if cid == self._current_cid():
                self.state.say("ok", f"已撤销 {ev.get('reverted', 0)} 个操作"
                                     f"（run {ev.get('run_id')}）")
                self._sync_feed()
            return
        if cid in (-1, self._current_cid()):
            self.state.on_event(ev)
            self._sync_feed()
            self._refresh_chrome()

    # -- input -------------------------------------------------------------------

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        if event.text_area.id != "prompt":
            return
        from .commands import COMMANDS

        prompt = self.query_one("#prompt", HistoryInput)
        prompt.completions = completion_suggestions(
            event.text_area.text,
            COMMANDS,
            self.state.model_candidates,
            self.state.profile_names,
            [f"#{row['id']}" for row in self.state.sessions],
        )
        strip = self.query_one("#suggest", Static)
        if prompt.completions:
            strip.update("[dim]Tab 采纳 →[/] " +
                         "  ".join(prompt.completions[:4]))
            strip.display = True
        else:
            strip.update("")
            strip.display = False

    async def on_history_input_submitted(
        self, event: HistoryInputSubmitted,
    ) -> None:
        if event.input.id != "prompt":
            return
        line = event.value.strip()
        event.input.value = ""
        self.query_one("#suggest", Static).display = False
        self.query_one("#prompt", HistoryInput).record(line)
        if not line:
            return
        if self.state.running and not line.startswith("/"):
            # Plain text mid-turn steers the running turn: queued into the
            # kernel's inbox and injected as a user message at the next
            # step boundary (user_injected event renders the line). When
            # no turn is actually draining (one-shot mode), fall back to
            # putting the text back in the input box. Slash commands still
            # dispatch below — the workbench supports them mid-turn.
            if self.wb.steer(line, self._current_cid()):
                from .ui import truncate
                self.state.say(
                    "dim", f"（已排队，将在当前步骤后注入：{truncate(line, 48)}）")
                self._sync_feed()
                return
            self.query_one("#prompt", HistoryInput).value = line
            self.state.say("warn", "本轮还在进行中，未发送；文本已放回输入框，命令仍可用")
            self._sync_feed()
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
        if r.copy_scope:
            # /copy: the front-end owns the text (the workbench only
            # resolved the scope), so the copy happens here.
            await self._copy_scope(r.copy_scope)
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
            entries = self.wb.model_entries(fav_only=self._model_fav_only)
            section = group = None
            current = self.cfg.profile
            for entry in entries:
                # fav/recent heads render once per section; profile heads
                # once per profile group
                changed = ((entry["section"] != section)
                           if entry["section"] in ("fav", "recent")
                           else ((entry["section"], entry["group"])
                                 != (section, group)))
                if changed:
                    section, group = entry["section"], entry["group"]
                    if section == "fav":
                        head = "[dim]── ★ 常用 ──[/]"
                    elif section == "recent":
                        head = "[dim]── 最近 ──[/]"
                    else:
                        try:
                            ep = self.wb.profiles.endpoint(group)
                        except SystemExit:
                            ep = {}
                        dialect = self.wb._endpoint_dialect(ep)
                        tag = f"（{dialect}）" if dialect else ""
                        mark = "▶ " if group == current else ""
                        head = f"[dim]── {mark}{group}{tag} ──[/]"
                    items.append(({"kind": "head"}, head))
                active = (entry["profile"] == current
                          and entry["model"] == self.cfg.model)
                star = "★" if entry["favorite"] else " "
                label = (f"{entry['profile']}:{entry['model']}"
                         if section in ("fav", "recent") else entry["model"])
                items.append((
                    {"model": entry["model"], "profile": entry["profile"]},
                    f"{'[green]●[/] ' if active else ' '}{star} {label}",
                ))
            if not items:
                items.append(({"kind": "head"},
                              "[dim]（没有收藏；f 切回全量列表，行内 a 收藏）[/]"))
            self.push_screen(PickerModal(
                "模型", items,
                "Enter 切换 · a 收藏 · f 只看收藏 · s 存为默认 · r 拉取 · Esc 关闭",
                letter_actions={"s": "_picker_save_model",
                                "r": "_picker_fetch_models",
                                "a": "_picker_toggle_fav",
                                "f": "_picker_fav_filter"},
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
        prompt = self.query_one("#prompt", HistoryInput)
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

    def _picker_toggle_fav(self, payload) -> None:
        """Letter a: star/unstar the focused row, then reopen the picker so
        the ★ marks (and section membership) refresh in place."""
        if not payload or payload.get("kind") == "head":
            return
        ref = f"{payload['profile']}:{payload['model']}"
        r = self.wb.fav_command(ref)
        for cls, text in r.messages:
            self.state.say(cls, text)
        self._sync_feed()
        self._open_picker("model")

    def _picker_fav_filter(self, payload) -> None:
        """Letter f: collapse the (possibly huge) grouped list to the
        favorites lane and back."""
        self._model_fav_only = not self._model_fav_only
        self._open_picker("model")

    # -- copy ----------------------------------------------------------------------

    def _scope_text(self, scope: str) -> str:
        """The feed slice /copy hands to the clipboard (streaming included)."""
        return feed_text(self.state.feed, scope, extra=self.state.streaming)

    async def _copy_text(self, text: str, label: str) -> bool:
        """One copy through the layered channels (see lithe_cli.clipboard)."""
        ok, channel = await deliver_clipboard(text, app=self, home=lithe_home())
        if ok:
            self.state.say("ok", f"已复制{label}"
                                 f"（{describe_copy(text)} → {channel}）")
        else:
            self.state.say("err", f"复制{label}失败：{channel}")
        self._sync_feed()
        return ok

    async def _copy_scope(self, scope: str) -> bool:
        label = COPY_SCOPE_LABELS.get(scope, scope)
        text = self._scope_text(scope)
        if not text.strip():
            self.state.say("warn", f"没有可复制的{label}（对话还是空的）")
            self._sync_feed()
            return False
        return await self._copy_text(text, label)

    async def _copy_file(self) -> bool:
        """Export the whole conversation under $LITHE_HOME (no clipboard)."""
        text = self._scope_text("all")
        path = write_clipboard_file(text, lithe_home())
        if path is None:
            self.state.say("err", "导出失败：无法写入 $LITHE_HOME")
        else:
            self.state.say("ok", "已导出整段对话"
                                 f"（{describe_copy(text)}）→ {path}")
        self._sync_feed()
        return path is not None

    def open_copy_menu(self) -> None:
        """The right-click copy menu (also the app-level right-click hook)."""
        selection = selected_text(self.screen, self)
        items: list[tuple[dict, str]] = []
        if selection:
            items.append(({"selection": True},
                          f"复制选中文本（{describe_copy(selection)}）"))
        for scope in COPY_SCOPES:
            items.append(({"scope": scope}, f"复制{COPY_SCOPE_LABELS[scope]}"))
        items.append(({"file": True}, "整段对话存为文件（$LITHE_HOME）"))
        # The modal takes the selection with it, so keep it here for the
        # callback instead of re-reading it after the dismiss.
        self._pending_selection = selection
        self.push_screen(
            PickerModal("复制", items, "Enter 复制 · Esc 关闭"),
            self._copy_picked,
        )

    def _copy_picked(self, result) -> None:
        if not result or result[0] != "select" or not result[1]:
            return
        payload = result[1]
        if payload.get("selection"):
            if self._pending_selection:
                asyncio.create_task(
                    self._copy_text(self._pending_selection, "选中文本"))
            return
        if payload.get("file"):
            asyncio.create_task(self._copy_file())
            return
        asyncio.create_task(self._copy_scope(payload.get("scope") or "last"))

    def on_mouse_down(self, event) -> None:
        """Right-click outside the conversation pane (header, sidebar,
        footer) gets the same copy menu — mouse reporting leaves the
        terminal's own menu unreachable in here."""
        if getattr(event, "button", 0) != 3:
            return
        if isinstance(self.screen, PickerModal):
            return
        self.open_copy_menu()

    # -- keys -----------------------------------------------------------------------

    def action_toggle_sidebar(self) -> None:
        self.state.show_sidebar = not self.state.show_sidebar
        self._refresh_chrome()

    def action_cancel_or_exit(self) -> None:
        text = selected_text(self.screen, self)
        if text:
            # A native-Ctrl+C reflex over a drag selection: copy it instead
            # of cancelling the turn (press again with nothing selected to
            # cancel/exit). Absent a selection API this is a no-op and the
            # documented cancel/exit behaviour stands.
            asyncio.create_task(self._copy_text(text, "选中文本"))
            return
        if self.state.running:
            if not self.wb.cancel(self._current_cid()):
                # One-shot run mode drives no workbench turn, so wb.cancel
                # has nothing to stop — fall back to the run's own stop
                # handle (the README's "Ctrl+C cancels the current turn").
                stop = getattr(self.state, "stop", None)
                if stop is not None:
                    stop.set()
            if self._cancel_asked:
                self.exit()  # second Ctrl+C during the same turn: force exit
                return
            self._cancel_asked = True
            self.state.say("warn", "（正在取消本轮，再按一次 Ctrl+C 退出）")
            self._sync_feed()
        else:
            self.exit()

    # -- one-shot run mode --------------------------------------------------------

    async def confirm_command(self, command: str) -> bool:
        """Await a human y/n on a destructive command (guard middleware).

        Bounded: an abandoned modal times out as a refusal instead of
        wedging the turn forever; any UI failure also refuses.
        """
        fut: asyncio.Future[bool] = asyncio.get_running_loop().create_future()

        def _resolved(result) -> None:
            if not fut.done():
                fut.set_result(bool(result))

        try:
            self.push_screen(ConfirmModal(command), _resolved)
        except Exception:  # noqa: BLE001 — a broken UI denies, never runs
            return False
        try:
            return await asyncio.wait_for(fut, timeout=300)
        except Exception:  # noqa: BLE001 — timeout / cancelled → refuse
            return False

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
                          stop=stop, approver=self.confirm_command)
        except Exception as exc:  # noqa: BLE001
            self.state.say("err", f"运行出错：{exc}")
        finally:
            self.state.running = False
            self.state.stop = None
            self._cancel_asked = False
            self._sync_feed()
            self._refresh_chrome()
            self.query_one("#prompt", HistoryInput).disabled = False


async def _driver(app: LitheApp) -> int:
    await app.run_async()
    return 0 if app.mode != "run" else (
        0 if app.state.last_status == "done" else 1)


def run_textual_screen(cfg, task: str | None, mode: str,
                       args: Any = None) -> int:
    """The full-screen driver for `lithe chat` (mode='chat') and `lithe run`."""
    app = LitheApp(cfg, task, mode, args)
    return asyncio.run(_driver(app))


def __getattr__(name: str):
    """Forward extracted widget names for existing ``lithe_cli.ttui`` users."""
    try:
        return getattr(_widgets, name)
    except AttributeError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None
