"""Reusable Textual widgets and modal dialogs used by the full-screen UI."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.content import Content
from textual.css.query import NoMatches
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, ListItem, ListView, Static, TextArea

from .ui import display_width, truncate

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
    if "\n" in value or not value.startswith("/"):
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


class HistoryInput(TextArea):
    """Multiline prompt with history recall and persistent FileHistory."""

    BINDINGS = [
        Binding("up", "history_prev", "上一条", show=False, priority=True),
        Binding("down", "history_next", "下一条", show=False, priority=True),
        Binding("tab", "accept_suggestion", "采纳", show=False,
                priority=True),
        Binding("enter", "submit", "发送", show=False, priority=True),
        Binding("ctrl+enter", "insert_newline", "换行", show=False,
                priority=True),
    ]

    def __init__(self, *args, file_history=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.file_history = file_history
        self._lines: list[str] = []
        self._pos: int | None = None
        self._draft = ""
        self.completions: list[str] = []
        self.reload_history()

    @property
    def value(self) -> str:
        """Input-compatible alias used by the workbench and UI code."""
        return self.text

    @value.setter
    def value(self, text: str) -> None:
        self.text = text

    def action_submit(self) -> None:
        self.post_message(HistoryInputSubmitted(self, self.text))

    def action_insert_newline(self) -> None:
        self.insert("\n")

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
        if not line.strip():
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
        lines = text.split("\n")
        self.move_cursor((len(lines) - 1, len(lines[-1])))

    def action_history_prev(self) -> None:
        # Keep normal vertical editing inside a multiline draft; history is
        # recalled only when the cursor is already on its first line.
        if self.cursor_location[0] > 0:
            self.action_cursor_up()
            return
        if not self._lines:
            return
        if self._pos is None:
            self._draft = self.value
            self._apply(self._lines[-1], len(self._lines) - 1)
        elif self._pos > 0:
            self._apply(self._lines[self._pos - 1], self._pos - 1)

    def action_history_next(self) -> None:
        if self.cursor_location[0] < len(self.text.split("\n")) - 1:
            self.action_cursor_down()
            return
        if self._pos is None:
            return
        if self._pos < len(self._lines) - 1:
            self._apply(self._lines[self._pos + 1], self._pos + 1)
        else:
            self._apply(self._draft, None)

    def action_accept_suggestion(self) -> None:
        if self.completions:
            accepted = self.completions[0]
            self.value = accepted
            self.move_cursor((0, len(accepted)))
            self.completions = []


class HistoryInputSubmitted(Message):
    """A multiline prompt submission, kept separate from TextArea.Changed."""

    def __init__(self, input: HistoryInput, value: str) -> None:
        super().__init__()
        self.input = input
        self.value = value


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


class SubagentCard(Vertical):
    """A collapsible, independently searchable subagent transcript."""

    def __init__(self, instance: str, block: dict | None = None, **kwargs) -> None:
        safe_id = "subagent-" + "".join(
            c if c.isalnum() or c in "-_" else "-" for c in instance
        )
        super().__init__(id=safe_id, classes="subagent-card", **kwargs)
        self.instance = instance
        self._expanded = bool((block or {}).get("expanded"))
        self._search = str((block or {}).get("search") or "")
        self._lines: list[tuple[str, str]] = []
        self._block: dict = {}
        self._heading = ""
        if block:
            self.update_data(block)

    def compose(self) -> ComposeResult:
        yield Button("", classes="subagent-heading", variant="default")
        yield Input(placeholder="搜索此子任务…", classes="subagent-search")
        yield Static("", classes="subagent-output", markup=False)

    def update_data(self, block: dict) -> None:
        same_block = self._block is block
        if self.is_mounted and same_block:
            block["expanded"] = self._expanded
            block["search"] = self._search
        self._block = block
        self._lines = list(block.get("lines") or [])
        if not (self.is_mounted and same_block):
            self._expanded = bool(block.get("expanded"))
            self._search = str(block.get("search") or "")

    def set_data(self, block: dict) -> None:
        self.update_data(block)
        try:
            heading = self.query_one(".subagent-heading", Button)
        except NoMatches:
            # The card sits in its parent's children but its compose()
            # children are not mounted yet — a sibling subagent's event
            # syncing this card inside that window hit here. The data is
            # already stored; on_mount re-renders once the card is live.
            return
        display = str(block.get("display") or block.get("agent") or "子代理")
        suffix = str(block.get("instance") or "").rsplit(":", 1)[-1][:4]
        task = str(block.get("task") or "（任务描述载入中）").replace("\n", " ")
        status = str(block.get("status") or "running")
        status_label = {
            "running": "运行中", "done": "完成", "max_steps": "步数上限",
            "failed": "失败", "cancelled": "已取消", "budget_exceeded": "预算超限",
        }.get(status, status)
        status_mark = {
            "running": "●", "done": "✓", "failed": "✗", "cancelled": "○",
        }.get(status, "·")
        details = []
        if block.get("steps") is not None:
            details.append(f"{block['steps']} 步")
        if block.get("changes"):
            details.append(f"{block['changes']} 处改动")
        detail = " · " + " · ".join(details) if details else ""
        # One collapsed line: identity + status, then the task squeezed into
        # whatever width remains. The budget is measured on the heading
        # button itself (the card's rail and padding shrink it) and covers
        # the arrow prefix, the button's pad and the " · " join, so the
        # ellipsis — not the pane — ends the line.
        head = f"{status_mark} {display}·{suffix} · {status_label}{detail}"
        width = heading.content_size.width or self.content_size.width or 64
        self._heading = \
            f"{head} · {truncate(task, max(4, width - display_width(head) - 8))}"
        heading.label = self.heading_label()
        search = self.query_one(".subagent-search", Input)
        output = self.query_one(".subagent-output", Static)
        if search.value != self._search:
            search.value = self._search
        search.display = self._expanded
        output.display = self._expanded
        if self._expanded:
            self._render_lines(search.value, output)

    def heading_label(self) -> Content:
        """The heading as verbatim content — task text is model data, and
        Button labels parse square-bracket markup (``[/]`` would raise)."""
        arrow = "▾" if self._expanded else "▸"
        return Content.from_rich_text(Text(f"{arrow} {self._heading}"))

    def refresh_lines(self) -> None:
        """Refresh transcript text without resetting UI-local search state."""
        if not self.is_mounted:
            return
        self._lines = list(self._block.get("lines") or [])
        search = self.query_one(".subagent-search", Input)
        output = self.query_one(".subagent-output", Static)
        if self._expanded:
            self._render_lines(search.value, output)

    def _render_lines(self, query: str, output: Static) -> None:
        needle = query.casefold().strip()
        lines = [text for _cls, text in self._lines
                 if not needle or needle in text.casefold()]
        output.update("\n".join(lines) if lines else ("（没有匹配内容）" if needle else "（暂无子任务输出）"))

    def on_mount(self) -> None:
        self.set_data(self._block)

    def on_resize(self) -> None:
        """The one-line heading truncates to the laid-out width, which is
        still 0 at mount time — re-render once the card has its real size
        (and again if the terminal is resized)."""
        if self._block:
            self.set_data(self._block)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if "subagent-heading" not in event.button.classes:
            return
        self._expanded = not self._expanded
        self._block["expanded"] = self._expanded
        heading = self.query_one(".subagent-heading", Button)
        heading.label = self.heading_label()
        search = self.query_one(".subagent-search", Input)
        output = self.query_one(".subagent-output", Static)
        search.display = self._expanded
        output.display = self._expanded
        if self._expanded:
            self._render_lines(search.value, output)

    def on_input_changed(self, event: Input.Changed) -> None:
        if "subagent-search" in event.input.classes:
            self._search = event.value
            self._block["search"] = event.value
            self._render_lines(event.value,
                               self.query_one(".subagent-output", Static))


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
        # model picker: favorite the focused row / favorites-only filter.
        # Gated by each picker's letter_actions, inert elsewhere.
        Binding("a", "letter('a')", show=False),
        Binding("f", "letter('f')", show=False),
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


# -- forms ------------------------------------------------------------------------

class FormModal(ModalScreen):
    """A labeled-field form modal (the endpoint editor behind F7).

    Fields are dicts: ``{"name", "label", "kind": "text" | "password" |
    "choice", "default", "placeholder", "choices"}``. Enter advances to
    the next field and submits on the last; choice fields cycle their
    options on Enter or Space; Tab / ↑ / ↓ move between fields; Esc or
    q cancels. ``on_change(field_name, value)`` fires after every value
    change so the host can react (the provider field re-defaults
    base_url to the chosen preset's official URL). Dismissals:
    ``("submit", values-dict)`` or ``None``.
    """

    BINDINGS = [
        Binding("escape", "cancel", "取消"),
        Binding("q", "cancel", "取消", show=False),
        Binding("down", "next_field", show=False, priority=True),
        Binding("up", "prev_field", show=False, priority=True),
        # Enter is owned by the form, not the widgets: Button's own
        # two-phase key activation is version-flaky (press every other
        # key in some builds), so one priority binding dispatches —
        # choice rows cycle, text rows advance / submit. Mouse clicks
        # keep the Button.Pressed path.
        Binding("enter", "enter_key", show=False, priority=True),
    ]

    def __init__(self, title: str, fields: list[dict], hint: str = "",
                 on_change: Callable[[str, str], None] | None = None):
        super().__init__()
        self.title = title
        self.fields = fields
        self.hint = hint
        self.on_change = on_change
        self._values: dict[str, str] = {
            f["name"]: str(f.get("default") or "") for f in fields}
        self._choices: dict[str, list[str]] = {
            f["name"]: list(f.get("choices") or [])
            for f in fields if f.get("kind") == "choice"}

    def compose(self) -> ComposeResult:
        with Vertical(id="picker"):
            yield Static(self.title, id="picker-title")
            with Vertical(id="form-fields"):
                for f in self.fields:
                    with Horizontal(classes="form-row"):
                        yield Label(f["label"], classes="form-label")
                        if f.get("kind") == "choice":
                            yield Button("", classes="form-choice",
                                         id=f"form-{f['name']}")
                        else:
                            yield Input(
                                value=self._values[f["name"]],
                                placeholder=f.get("placeholder") or "",
                                password=f.get("kind") == "password",
                                id=f"form-{f['name']}",
                            )
            yield Static(self.hint, id="picker-hint")

    def on_mount(self) -> None:
        self._sync_choice_buttons()
        if self.fields:
            self._focus(0)

    # -- field plumbing ---------------------------------------------------------

    def _field_index(self, widget_id: str | None) -> int | None:
        if not widget_id:
            return None
        for i, f in enumerate(self.fields):
            if f"form-{f['name']}" == widget_id:
                return i
        return None

    def _focus(self, index: int) -> None:
        f = self.fields[index]
        self.query_one(f"#form-{f['name']}").focus()

    def _sync_choice_buttons(self) -> None:
        for f in self.fields:
            if f.get("kind") != "choice":
                continue
            btn = self.query_one(f"#form-{f['name']}", Button)
            value = self._values[f["name"]]
            btn.label = f"{value} ▸" if value else "（空）"

    def _changed(self, name: str, value: str) -> None:
        if self.on_change is not None:
            try:
                self.on_change(name, value)
            except Exception:  # noqa: BLE001 — host callbacks must not kill the form
                pass

    def set_value(self, name: str, value: str) -> None:
        """Host-side field update (e.g. re-default base_url) — text fields."""
        f = next((x for x in self.fields if x["name"] == name), None)
        if f is None or f.get("kind") == "choice":
            return
        self._values[name] = value
        try:
            self.query_one(f"#form-{name}", Input).value = value
        except NoMatches:
            pass

    def values(self) -> dict[str, str]:
        return dict(self._values)

    # -- events ------------------------------------------------------------------

    def on_input_submitted(self, event: Input.Submitted) -> None:
        idx = self._field_index(event.input.id)
        if idx is None:
            return
        name = self.fields[idx]["name"]
        self._values[name] = event.value
        self._changed(name, event.value)
        if idx >= len(self.fields) - 1:
            self.action_submit()
        else:
            self._focus(idx + 1)

    def on_input_changed(self, event: Input.Changed) -> None:
        idx = self._field_index(event.input.id)
        if idx is not None:
            self._values[self.fields[idx]["name"]] = event.value

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Mouse path for choice rows (keyboard goes through
        :meth:`action_enter_key`)."""
        idx = self._field_index(event.button.id)
        if idx is None or self.fields[idx].get("kind") != "choice":
            return
        self._cycle_choice(self.fields[idx])

    def _cycle_choice(self, field: dict) -> None:
        opts = self._choices[field["name"]]
        cur = self._values[field["name"]]
        if not opts:
            return
        nxt = opts[(opts.index(cur) + 1) % len(opts)] if cur in opts \
            else opts[0]
        self._values[field["name"]] = nxt
        self._sync_choice_buttons()
        self._changed(field["name"], nxt)

    # -- actions -------------------------------------------------------------------

    def action_enter_key(self) -> None:
        """One Enter, dispatched by field kind (see BINDINGS note)."""
        idx = self._field_index(self.focused.id if self.focused else None)
        if idx is None:
            return
        field = self.fields[idx]
        if field.get("kind") == "choice":
            self._cycle_choice(field)
            return
        value = getattr(self.focused, "value", "") or ""
        self._values[field["name"]] = value
        self._changed(field["name"], value)
        if idx >= len(self.fields) - 1:
            self.action_submit()
        else:
            self._focus(idx + 1)

    def action_next_field(self) -> None:
        idx = self._field_index(self.focused.id if self.focused else None)
        if idx is not None and idx < len(self.fields) - 1:
            self._focus(idx + 1)

    def action_prev_field(self) -> None:
        idx = self._field_index(self.focused.id if self.focused else None)
        if idx is not None and idx > 0:
            self._focus(idx - 1)

    def action_submit(self) -> None:
        # stop at the first blank required field: the hint names it
        missing = [f["label"] for f in self.fields
                   if f.get("kind") != "choice" and f.get("required", True)
                   and not (self._values.get(f["name"]) or "").strip()]
        if missing:
            self.query_one("#picker-hint", Static).update(
                f"还需填写：{'、'.join(missing)}（Esc 取消）")
            return
        self.dismiss(("submit", self.values()))

    def action_cancel(self) -> None:
        self.dismiss(None)


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
