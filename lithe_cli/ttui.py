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
from textual.css.query import NoMatches
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
    ConfirmModal, ConversationPane, FoldHead, FoldRow, FormModal,
    HistoryInput, PickerModal, SectionHeader, SubagentCard, ThinkingRow,
    HistoryInputSubmitted, WbEvent, completion_suggestions, selected_text,
)
from .ttui_render import (
    SIDEBAR_SECTIONS,
    markdown_text,
    markdown_text_lines,
    prompt_info_text,
    section_header,
    sidebar_sections,
    welcome_markup,
)
from .ttui_render import sidebar_markup as sidebar_markup  # noqa: F401 — legacy re-export

_MAX_FEED = 400  # mounted conversation lines before trimming the oldest

# -- the app ----------------------------------------------------------------------

class LitheApp(App):
    # Kilo-style chrome: no box borders anywhere — the panes separate by
    # background color alone (the sidebar sits on a darker band, no
    # divider line), information lives in the collapsible sidebar
    # sections and the conversation's thinking lane, not a header bar
    # or a footer status strip.
    CSS = """
    Screen { background: #14101f; }
    #body { height: 1fr; }
    #main-column { width: 1fr; height: 1fr; }
    #conv { width: 1fr; height: 1fr; padding: 0 2; }
    /* Subagent cards live inline in the conversation: a one-line heading
       when collapsed (plus the left rail), search + transcript expanded. */
    .subagent-card { height: auto; margin: 0 0 1 0; padding: 0 1 0 0;
                     border-left: solid #3b3358; }
    .subagent-heading { height: 1; width: 1fr; border: none; min-width: 0;
                        background: transparent; color: #c084fc;
                        text-align: left; content-align: left middle; }
    .subagent-search { height: 1; margin: 0 1; border: none; padding: 0 1; }
    .subagent-output { height: auto; margin: 0 1; color: #cfc8e2; }
    /* The sidebar separates from the conversation by background alone:
       a darker band, deliberately no divider line. */
    #side-wrap { width: 40; background: #0d0a16; padding: 0 1; }
    .side-head { height: 1; color: #e7e1f3; margin-top: 1; }
    #side-wrap > .side-head:first-of-type { margin-top: 0; }
    .side-body { padding: 0 0 0 2; color: #a49bc2; margin: 0 0 1 0; }
    #suggest { color: #a49bc2; padding: 0 2; }
    /* The prompt band: a solid violet-tinted raised band carrying the
       input plus the dim endpoint info line at its bottom — there is no
       footer status bar below it. */
    #prompt-row { height: auto; padding: 0 1; margin: 0 1;
                  background: #1c1631; }
    #prompt-main { height: auto; }
    #prompt-glyph { width: 1; color: #c084fc; }
    #prompt { height: auto; min-height: 3; max-height: 10; width: 1fr;
              border: none; padding: 0 0 0 1; background: transparent; }
    #prompt-info { height: 1; color: #a49bc2; padding: 0 0 0 2; }
    /* One breathing line between feed blocks (class transitions: user →
       tool → answer → notices); inside a block lines stay dense. */
    .block-gap { margin-top: 1; }
    /* Collapsible folds: heading always visible, body only when
       expanded. */
    .fold-head { height: 1; color: #c084fc; }
    .fold-body { margin: 0 1 0 2; color: #b1a2e8; }
    /* The live thinking lane (one per pane, below the feed, above the
       streaming slot): violet heading — spinner + "thinking" while the
       turn runs, "thought · Ns" / a dim meta line under the output after. */
    #thinking { height: auto; margin-top: 1; }
    #picker { width: 60%; height: auto; max-height: 80%;
              border: round #a78bfa; background: $surface; padding: 1 2; }
    #picker-title { color: #a78bfa; text-style: bold; }
    #picker-list { height: auto; max-height: 16; }
    #picker-hint { color: #a49bc2; }
    #picker-command { color: #f87171; }
    #form-fields { height: auto; }
    .form-row { height: 3; margin: 0 0 1 0; }
    .form-row Label { width: 11; padding: 0 1 0 0; color: #a49bc2; }
    .form-row Input { width: 1fr; }
    Button.form-choice { width: 1fr; height: 3; border: round #3b3358;
                         background: #14101f; color: #e7e1f3; padding: 0 1;
                         min-width: 0; content-align: left middle; }
    /* Kilo-style user lines: the reader's own words sit on a solid
       violet panel spanning the pane — background only, no frame — so
       quoted input reads as a block between the model's plain output;
       one breathing line above each message. */
    .user { color: #e7e1f3; width: 1fr; background: #272040;
            padding: 0 1; }
    .user-first { margin-top: 1; }
    .assistant { color: $text; }
    .tool { color: #c4b5fd; }
    .ok { color: #4ade80; }
    .err { color: #f87171; }
    .warn { color: #fbbf24; }
    .dim { color: #a49bc2; }
    .reason { color: #c084fc; }
    /* The first-screen block: shown only while the conversation is
       empty; the first real row removes it (never part of the feed). */
    .welcome { margin: 1 0; }
    """

    BINDINGS = [
        Binding("f2", "toggle_sidebar", "侧栏", priority=True),
        Binding("f3", "open_sessions", "会话", priority=True),
        Binding("f4", "open_model", "模型", priority=True),
        Binding("f5", "open_set", "设置", priority=True),
        Binding("f6", "open_reasoning", "推理", priority=True),
        Binding("f7", "open_endpoint", "端点", priority=True),
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
        # sidebar sections folded away (usage: its summary already carries
        # the numbers; keys: reference material, not worth screen estate)
        self._collapsed: set[str] = {"usage", "keys"}
        # feed mounting state: block-gap tracks the previous row's class
        self._last_cls: str | None = None

    # -- composition ----------------------------------------------------------

    def compose(self) -> ComposeResult:
        with Horizontal(id="body"):
            with Vertical(id="main-column"):
                with ConversationPane(id="conv"):
                    # The thinking lane mounts once between the feed and
                    # the streaming slot: rows above it are settled output,
                    # the streaming reply below it is the newest text.
                    yield ThinkingRow(id="thinking")
                    yield Static("", id="streaming", classes="assistant",
                                 markup=False)
            with VerticalScroll(id="side-wrap"):
                # one heading + one body widget per section, mounted once
                # and updated in place (click a heading to fold its body)
                for sid in SIDEBAR_SECTIONS:
                    yield SectionHeader("", id=f"side-head-{sid}", section=sid,
                                        markup=True, classes="side-head")
                    yield Static("", id=f"side-body-{sid}", markup=True,
                                 classes="side-body")
        yield Static("", id="suggest")
        with Vertical(id="prompt-row"):
            with Horizontal(id="prompt-main"):
                yield Static("❯", id="prompt-glyph", markup=False)
                yield HistoryInput(placeholder="输入任务，/help 查看命令",
                                   id="prompt")
            yield Static("", id="prompt-info", markup=True)

    def on_mount(self) -> None:
        from .prompts import open_history
        from .workbench import Workbench

        self.wb = Workbench(self.cfg)
        # Destructive run_command approval rides a modal; the turn task and
        # the app share one loop, so the middleware can await the human.
        self.wb.approver = self.confirm_command
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
        # Live turn heartbeat: kernel events alone leave the screen
        # static during long model calls, so the thinking lane spins on
        # its own fast timer (glyph + live elapsed from
        # TuiState.elapsed()) while a turn runs.
        self.set_interval(0.12, self._tick_thinking)
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
        self._sync_endpoint_view(st)
        return st

    def _sync_endpoint_view(self, st: TuiState) -> None:
        """Endpoint display fields for the sidebar's 模型 section."""
        st.provider = self.cfg.provider or ""
        st.temperature = self.cfg.temperature
        st.max_output = self.cfg.max_tokens
        st.dialect = ""
        if self.cfg.profile:
            try:
                ep = self.wb.profiles.endpoint(self.cfg.profile)
            except SystemExit:
                ep = {}
            st.dialect = self.wb._endpoint_dialect(ep)
        elif st.provider:
            st.dialect = self.wb._endpoint_dialect(
                {"provider": st.provider})

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
        self._sync_endpoint_view(st)
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
        """Unmount feed lines and subagent cards, keep the chrome slots
        (streaming reply + thinking lane)."""
        conv = self.query_one("#conv", ConversationPane)
        for child in list(conv.children):
            if child.id not in ("streaming", "thinking"):
                child.remove()
        # the feed remounts from scratch next: block-gap tracking restarts
        # with it (the thinking lane's state is refreshed separately)
        self._last_cls = None

    # -- conversation rendering (pure render-from-state) ----------------------

    def _sync_feed(self) -> None:
        conv = self.query_one("#conv", ConversationPane)
        streaming = self.query_one("#streaming", Static)
        thinking = self.query_one("#thinking", ThinkingRow)
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
                    conv.mount(card, before=thinking)
                    card.call_after_refresh(card.set_data, block)
                continue
            if cls == "user":
                # The reader's own words: contiguous rows form one solid
                # background band (no frame), breathing room above the
                # first line via .user-first's margin.
                run = [text]
                while (self._shown < len(feed)
                       and feed[self._shown][0] == "user"):
                    run.append(feed[self._shown][1])
                    self._shown += 1
                self._last_cls = "user"
                for i, line in enumerate(run):
                    classes = "user" + (" user-first" if i == 0 else "")
                    conv.mount(Static(line, classes=classes, markup=False),
                               before=thinking)
                continue
            if cls == "assistant":
                # The reply is Markdown: contiguous assistant rows form one
                # block rendered through the span builder (headings,
                # emphasis, fences, lists). Rich Text carries the styling,
                # so no markup parsing ever touches model data — brackets
                # in the answer stay verbatim.
                run = [text]
                while (self._shown < len(feed)
                       and feed[self._shown][0] == "assistant"):
                    run.append(feed[self._shown][1])
                    self._shown += 1
                classes = "assistant"
                if self._last_cls is not None and self._last_cls != "assistant":
                    classes += " block-gap"
                self._last_cls = "assistant"
                for line in markdown_text_lines("\n".join(run)):
                    conv.mount(Static(line, classes=classes, markup=False),
                               before=thinking)
                continue
            # markup=False: model/tool text is data, not Textual markup —
            # a "[x]" in an answer must render, not parse (and it is what
            # /copy sends to the clipboard verbatim).
            classes = cls or "dim"
            if self._last_cls is not None and cls != self._last_cls:
                classes += " block-gap"  # breathing room between blocks
            self._last_cls = cls
            conv.mount(Static(text, classes=classes, markup=False),
                       before=thinking)
        # Trim the oldest mounted rows, but never a card: a long-running
        # subagent's block must not vanish mid-flight just because the
        # orchestrator talked a lot after delegating. The thinking lane
        # and streaming slot are chrome, not feed rows.
        non_feed = [c for c in conv.children
                    if c.id not in ("streaming", "thinking")]
        stale = [c for c in non_feed[:max(0, len(non_feed) - _MAX_FEED)]
                 if not isinstance(c, SubagentCard)]
        for old in stale:
            old.remove()
        # Welcome screen: mounted only while the feed holds nothing to
        # show — the first real content (or a switch into a session with
        # history) takes it away. Keyed on the feed, not the widget tree:
        # _clear_conversation's removals land asynchronously, so children
        # may still list stale rows mid-switch. It lives in the widget
        # tree only, so feed readers (/copy, export, replay) never see it.
        welcome = self._welcome_widget()
        if self.state.feed and welcome is not None:
            welcome.remove()
        elif not self.state.feed and welcome is None:
            conv.mount(Static(self._welcome_text(), id="welcome",
                              classes="welcome", markup=True),
                       before=thinking)
        if self.state.streaming:
            streaming.update(markdown_text(self.state.streaming))
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

    def _welcome_widget(self) -> Static | None:
        """The mounted welcome block, if any (it exists only while the
        conversation is empty)."""
        try:
            return self.query_one("#welcome", Static)
        except NoMatches:
            return None

    def _welcome_text(self) -> str:
        from lithe import __version__ as kernel_version

        from . import __version__

        return welcome_markup(self.state, __version__, kernel_version)

    def _refresh_chrome(self) -> None:
        for sec in sidebar_sections(self.state):
            folded = sec["id"] in self._collapsed
            self.query_one(f"#side-head-{sec['id']}", SectionHeader).update(
                section_header(sec, folded))
            body = self.query_one(f"#side-body-{sec['id']}", Static)
            body.update("\n".join(sec["lines"]))
            body.display = not folded
        self.query_one("#prompt-info", Static).update(
            prompt_info_text(self.state))
        self.query_one("#side-wrap", VerticalScroll).display = \
            self.state.show_sidebar
        self._refresh_thinking()
        welcome = self._welcome_widget()
        if welcome is not None:
            welcome.update(self._welcome_text())

    def _refresh_thinking(self) -> None:
        """Sync the conversation's thinking lane from the state.

        Running: spinner + "thinking" + the live elapsed. Settled: a
        "thought · Ns" fold (click re-expands the digests) when the turn
        thought, otherwise a dim "model · Ns" meta line — either way the
        turn's duration lands under its output. Nothing to show yet: the
        lane hides (it is chrome, never a feed row).
        """
        st = self.state
        try:
            row = self.query_one("#thinking", ThinkingRow)
        except NoMatches:
            return
        row.body_text = "\n\n".join(st.thinking)
        if st.running:
            live = st.elapsed()
            row.set_live(fmt_duration(live) if live is not None else "")
        elif st.thinking:
            tail = (f" · {fmt_duration(st.last_turn_duration)}"
                    if st.last_turn_duration is not None else "")
            row.settle("thought", tail)
        elif st.last_turn_duration is not None:
            row.settle(st.model,
                       f" · {fmt_duration(st.last_turn_duration)}", meta=True)
        else:
            row.display = False

    def _toggle_section(self, sid: str) -> None:
        """Fold/unfold one sidebar section (click on its heading)."""
        if sid in self._collapsed:
            self._collapsed.discard(sid)
        else:
            self._collapsed.add(sid)
        self._refresh_chrome()

    def on_click(self, event) -> None:
        widget = event.widget
        if isinstance(widget, SectionHeader) and widget.section:
            event.stop()
            self._toggle_section(widget.section)
        elif isinstance(widget, FoldHead) and isinstance(widget.parent, FoldRow):
            event.stop()
            widget.parent.toggle()

    def _tick_thinking(self) -> None:
        """Spinner heartbeat while a turn runs: rotate the thinking lane's
        glyph and resync it from the state — long model calls emit no
        kernel events (and one-shot runs bypass the workbench event
        pump entirely), so without this the lane would sit frozen."""
        if not self.state.running:
            return
        try:
            self.query_one("#thinking", ThinkingRow).frame += 1
        except NoMatches:
            return
        self._refresh_thinking()

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
            if t == "run_start":
                # New turn: the thinking lane restarts collapsed (its
                # digests and mode resync right after via on_event).
                self.query_one("#thinking", ThinkingRow).expanded = False
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
        elif name == "endpoint":
            from .profiles import masked_key
            from .ui import truncate

            items = []
            current = self.cfg.profile
            for pname in self.wb.profiles.names():
                try:
                    ep = self.wb.profiles.endpoint(pname)
                except SystemExit:
                    continue
                mark = "[green]●[/]" if pname == current else " "
                dialect = self.wb._endpoint_dialect(ep) or "手写端点"
                url = truncate(ep.get("base_url") or "（preset 补齐）", 34)
                key = masked_key(ep.get("api_key") or "")
                items.append((
                    {"profile": pname},
                    f"{mark} {pname} · {ep.get('model') or '—'}\n"
                    f"    [dim]{dialect} · {url} · key {key}[/]",
                ))
            if not items:
                items.append((
                    {"kind": "head"},
                    "[dim]（没有已保存档案；n 新建）[/]",
                ))
            self.push_screen(PickerModal(
                "端点档案", items,
                "Enter 切换 · n 新建 · e 编辑 · p 改 provider · d 删除 · Esc 关闭",
                letter_actions={"n": "_picker_new_profile",
                                "e": "_picker_edit_profile",
                                "p": "_picker_profile_provider",
                                "d": "_picker_delete_profile"},
            ), self._endpoint_picked)

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

    def action_open_endpoint(self) -> None:
        if not isinstance(self.screen, PickerModal):
            self._open_picker("endpoint")

    def _endpoint_picked(self, result) -> None:
        if not result:
            return
        kind = result[0]
        if kind == "letter" and result[1]:
            getattr(self, result[1])(result[2])
            return
        if kind != "select" or not result[1]:
            return
        payload = result[1]
        if payload.get("kind") == "head":
            return
        r = self.wb.set_profile(payload["profile"])
        for cls, text in r.messages:
            self.state.say(cls, text)
        self._refresh_meta()
        self._refresh_chrome()
        self._sync_feed()

    # F7 表单：新建/编辑档案
    def _picker_new_profile(self, payload) -> None:
        self._open_profile_form(None)

    def _picker_edit_profile(self, payload) -> None:
        name = (payload or {}).get("profile")
        if not name:
            return
        self._open_profile_form(name)

    # F7 › d：删除档案（确认后）
    def _picker_delete_profile(self, payload) -> None:
        name = (payload or {}).get("profile")
        if not name:
            return
        self.push_screen(ConfirmModal(
            f"删除端点档案 {name}？保存的 key、默认模型与收藏一并移除",
            title="⚠ 删除端点档案",
        ), lambda ok: self._profile_delete_confirmed(name, ok))

    def _profile_delete_confirmed(self, name: str, ok: bool) -> None:
        if ok:
            r = self.wb.delete_profile(name)
            for cls, text in r.messages:
                self.state.say(cls, text)
            self._refresh_meta()
            self._refresh_chrome()
            self._sync_feed()
        self._open_picker("endpoint")  # reopened either way: list refreshes

    def _open_profile_form(self, edit_name: str | None) -> None:
        try:
            from lithe.bundles.providers import PRESETS, known_providers
        except ImportError:
            PRESETS, known_providers = {}, []
        provider_choices = ["none", *known_providers()]
        preset_urls = {n: p.get("base_url") or ""
                       for n, p in PRESETS.items()}
        ep: dict = {}
        if edit_name:
            try:
                ep = self.wb.profiles.endpoint(edit_name)
            except SystemExit:
                ep = {}

        def _on_change(field: str, value: str) -> None:
            if field != "provider":
                return
            # provider 换档时：base_url 为空或恰好是另一 preset 的官方
            # URL → 跟着换成新 preset 的；用户手写的路由不动。
            try:
                form = self.screen
                if not isinstance(form, FormModal):
                    return
                current = form.values().get("base_url") or ""
                if current and current not in preset_urls.values():
                    return
                form.set_value("base_url", preset_urls.get(value, ""))
            except Exception:
                pass

        fields = [
            {"name": "profile", "label": "档案名",
             "kind": "text", "default": edit_name or "",
             "placeholder": "如 zhipu / my-router"},
            {"name": "provider", "label": "Provider", "kind": "choice",
             "choices": provider_choices,
             "default": ep.get("provider") or "none"},
            {"name": "base_url", "label": "Base URL",
             "kind": "text", "default": ep.get("base_url") or "",
             "placeholder": "自建路由填自己的 URL"},
            {"name": "api_key", "label": "API key",
             "kind": "password", "default": ep.get("api_key") or ""},
            {"name": "model", "label": "模型",
             "kind": "text", "default": ep.get("model") or "",
             "required": False,
             "placeholder": "可留空；保存后 F4 或 /models 拉取再选"},
        ]
        self.push_screen(FormModal(
            "新建端点档案" if not edit_name else f"编辑档案 {edit_name}",
            fields,
            "Enter 下一项（末项保存）· Provider 行 Enter 换选项 · 模型可留空"
            "（之后 /models 拉取）· Esc 取消 · provider=none 时 base_url 自填",
            on_change=_on_change,
        ), self._profile_form_saved)

    def _profile_form_saved(self, result) -> None:
        if not result or result[0] != "submit":
            return
        v = result[1]
        r = self.wb.save_profile(
            v.get("profile", ""), v.get("provider", ""),
            v.get("base_url", ""), v.get("api_key", ""),
            v.get("model", ""))
        for cls, text in r.messages:
            self.state.say(cls, text)
        self._refresh_meta()
        self._refresh_chrome()
        self._sync_feed()

    # F7 › p：快改 provider 类型
    def _picker_profile_provider(self, payload) -> None:
        name = (payload or {}).get("profile")
        if not name:
            return
        try:
            from lithe.bundles.providers import known_providers
        except ImportError:
            known_providers = []
        items = []
        for choice in ["none", *known_providers()]:
            label = "none（手写端点）" if choice == "none" else choice
            items.append(({"provider": choice}, f" {label}"))
        self.push_screen(PickerModal(
            f"{name} 的 provider 类型", items,
            "Enter 应用 · Esc 取消",
        ), lambda r: self._provider_choice_picked(name, r))

    def _provider_choice_picked(self, name: str, result) -> None:
        if not result or result[0] != "select" or not result[1]:
            return
        r = self.wb.change_profile_provider(name, result[1]["provider"])
        for cls, text in r.messages:
            self.state.say(cls, text)
        self._refresh_meta()
        self._refresh_chrome()
        self._sync_feed()

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
