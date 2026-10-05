"""Reusable Textual widgets and modal dialogs used by the full-screen UI."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Input, Label, ListItem, ListView, Static

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


# -- clipboard hooks -------------------------------------------------------------

def selected_text(screen: Any, app: Any = None) -> str:
    """The current Textual text selection, if this release has one.

    The selection API moved names between Textual releases (and is absent
    in the headless test driver), so every candidate is probed and a miss
    simply means "no selection" — callers keep their old behaviour.
    """
    for holder in (screen, app):
        if holder is None:
            continue
        getter = getattr(holder, "get_selected_text", None)
        if not callable(getter):
            continue
        try:
            text = getter()
        except Exception:  # noqa: BLE001 — no selection model: not fatal
            continue
        if isinstance(text, str) and text.strip():
            return text
    return ""


class ConversationPane(VerticalScroll):
    """The conversation pane.

    Right-click is handled here rather than on the app: mouse reporting is
    on, so the terminal's own context menu never opens inside the screen,
    and the pane is where a reader aims when they want text out of it.
    """

    def on_mouse_down(self, event) -> None:
        if getattr(event, "button", 0) != 3:
            return
        stop = getattr(event, "stop", None)
        if callable(stop):  # keep the app's own handler from firing twice
            stop()
        self.app.open_copy_menu()


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


# -- command approval --------------------------------------------------------

class ConfirmModal(ModalScreen):
    """y/n on a destructive run_command: y allows, n/Esc refuses."""

    BINDINGS = [
        Binding("y", "allow", "允许"),
        Binding("n", "refuse", "拒绝"),
        Binding("escape", "refuse", "拒绝", show=False),
    ]

    def __init__(self, command: str):
        super().__init__()
        self.command = command

    def compose(self) -> ComposeResult:
        with Vertical(id="picker"):
            yield Static("⚠ 破坏性命令需确认", id="picker-title")
            # 命令行与按键说明各用各的 id：Textual 要求同一父级下 id 唯一，
            # 早前两处都叫 picker-hint，push_screen 时直接 MountError。
            yield Static(self.command, id="picker-command")
            yield Static("y 允许执行 · n/Esc 拒绝", id="picker-hint")

    def action_allow(self) -> None:
        self.dismiss(True)

    def action_refuse(self) -> None:
        self.dismiss(False)


