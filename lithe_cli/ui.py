"""Zero-dependency terminal rendering for the lithe CLI.

Color policy: on only when stdout is a TTY, ``NO_COLOR`` is unset
(https://no-color.org) and ``TERM`` is not ``dumb``; ``--color`` /
``--no-color`` override everything. Every helper degrades to plain text
when color is off, so piped output and test capture stay clean.

Widths are computed with East-Asian widths in mind (task text is often
Chinese), so tables stay aligned where a bare ``len()`` would not.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
import unicodedata

RESET = "\x1b[0m"
BOLD = "\x1b[1m"
DIM = "\x1b[2m"
RED = "\x1b[31m"
GREEN = "\x1b[32m"
YELLOW = "\x1b[33m"
BLUE = "\x1b[34m"
MAGENTA = "\x1b[35m"
CYAN = "\x1b[36m"

# Terminal states worth coloring differently (everything else stays plain).
_OK_STATES = frozenset({"done"})
_BAD_STATES = frozenset({"failed", "error", "budget_exceeded", "max_steps"})
_WARN_STATES = frozenset({"cancelled", "running"})

# Subagent end-states (kernel RunStats.status) → display text, shared by the
# line renderer and the TUI feed.
SUBAGENT_STATUS = {
    "done": "完成",
    "failed": "失败",
    "cancelled": "已取消",
    "budget_exceeded": "预算超限",
    "max_steps": "步数上限",
}

_ROLE_STYLES = {
    "user": BLUE,
    "assistant": GREEN,
    "tool": CYAN,
    "system": MAGENTA,
}


def default_color() -> bool:
    """Auto-detect: a TTY, no NO_COLOR, and TERM not "dumb"."""
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("TERM", "") == "dumb":
        return False
    return bool(sys.stdout.isatty())


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def display_width(text: str) -> int:
    """Column width of *text* counting East-Asian glyphs as double; ANSI
    escape sequences are transparent."""
    clean = _ANSI_RE.sub("", text)
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in clean)


def pad(text: str, width: int) -> str:
    """Left-aligned *text* padded to *width* display columns."""
    return text + " " * max(0, width - display_width(text))


def truncate(text: str, width: int) -> str:
    """Cut *text* to *width* display columns, appending an ellipsis."""
    if display_width(text) <= width:
        return text
    out = ""
    used = 0
    for ch in text:
        w = 2 if unicodedata.east_asian_width(ch) in "WF" else 1
        if used + w > width - 1:
            break
        out += ch
        used += w
    return out + "…"


def wrap_text(text: str, width: int) -> list[str]:
    """Wrap *text* to *width* display columns (CJK-aware, newline-aware)."""
    if width < 1:
        return []
    lines: list[str] = []
    current = ""
    used = 0
    for ch in str(text):
        if ch == "\n":
            lines.append(current)
            current = ""
            used = 0
            continue
        w = 2 if unicodedata.east_asian_width(ch) in "WF" else 1
        if used + w > width and current:
            lines.append(current)
            current = ""
            used = 0
        if w <= width:
            current += ch
            used += w
    if current or not lines:
        lines.append(current)
    return lines


def tool_call_label(name: str, args: dict) -> str:
    path = str(args.get("path") or args.get("file_path") or "").strip()
    if name in {"read_file", "write_file", "edit_file"}:
        action = {"read_file": "读取", "write_file": "写入", "edit_file": "局部修改"}[name]
        return f"{name} · {action} {path}" if path else name
    if name == "apply_patch":
        patch = str(args.get("patch_text") or "")
        paths = [
            line.partition(":")[2].strip()
            for line in patch.splitlines()
            if line.startswith(("*** Update File:", "*** Add File:", "*** Delete File:"))
        ]
        files = "、".join(dict.fromkeys(paths))
        return f"{name} · {files}" if files else f"{name} · 文件补丁"
    if name == "delegate":
        agent = str(args.get("agent") or "").strip()
        task = str(args.get("task") or "").strip().replace("\n", " ")
        if agent and task:
            return f"{name} · {agent}：{truncate(task, 60)}"
        if agent:
            return f"{name} · {agent}"
        return name
    if name == "delegate_parallel":
        tasks = args.get("tasks")
        if isinstance(tasks, list) and tasks:
            agents = [str(t.get("agent") or "?") if isinstance(t, dict) else "?"
                      for t in tasks]
            roster = "、".join(dict.fromkeys(agents))
            return f"{name} · {roster}（{len(tasks)} 项）"
        return name
    for key in ("query", "pattern", "agent", "command", "url"):
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return f"{name} · {value.strip()[:80]}"
    return name


def fmt_duration(seconds: float | None) -> str:
    """Human wall-clock length: ``12.3s`` / ``2m05s`` / ``1h04m``.

    Empty string for ``None`` so callers can drop the field silently.
    """
    if seconds is None:
        return ""
    seconds = max(0.0, float(seconds))
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes = int(seconds // 60)
    sec = int(round(seconds % 60))
    if minutes < 60:
        return f"{minutes}m{sec:02d}s"
    return f"{minutes // 60}h{minutes % 60:02d}m"


class UI:
    """All terminal output goes through here; ``color`` gates every code."""

    def __init__(self, color: bool = False, width: int | None = None):
        self.color = bool(color)
        self._width = width

    @property
    def width(self) -> int:
        return max(12, self._width or shutil.get_terminal_size((100, 24)).columns)

    def s(self, text: str, *styles: str) -> str:
        """Wrap *text* in *styles* when color is on and text is non-empty."""
        if not self.color or not text or not styles:
            return text
        return "".join(styles) + text + RESET

    # -- structural ---------------------------------------------------------

    def rule(self, char: str = "─", width: int = 60) -> str:
        return self.s(char * min(width, self.width), DIM)

    def kv(self, label: str, value: str) -> str:
        return f"{self.s(pad(label, 11), CYAN, BOLD)} {value}"

    def section(self, title: str) -> str:
        width = min(50, self.width)
        label = f"── {title} "
        if display_width(label) > width:
            return self.s(truncate(label, width), DIM)
        return self.s(label + "─" * (width - display_width(label)), DIM)

    def table(
        self, headers: list[str], rows: list[list[str]], cell_styles: list | None = None
    ) -> str:
        """Aligned, terminal-width-aware table with display-width styling."""
        widths = [display_width(h) for h in headers]
        for row in rows:
            for i, cell in enumerate(row[:len(headers)]):
                widths[i] = max(widths[i], display_width(cell))
        spacing = "  " if self.width >= 3 * len(headers) else " "
        available = max(1, self.width - display_width(spacing) * (len(headers) - 1))
        overflow = max(0, sum(widths) - available)
        for i in range(len(widths) - 1, -1, -1):
            floor = min(widths[i], max(1, display_width(headers[i])))
            reduction = min(overflow, widths[i] - floor)
            widths[i] -= reduction
            overflow -= reduction
        for i in range(len(widths) - 1, -1, -1):
            reduction = min(overflow, widths[i] - 1)
            widths[i] -= reduction
            overflow -= reduction
        shown_headers = [truncate(h, widths[i]) for i, h in enumerate(headers)]
        head = self.s(
            spacing.join(pad(shown_headers[i], widths[i]) for i in range(len(headers))),
            BOLD,
            DIM,
        )
        sep = self.s(spacing.join("─" * w for w in widths), DIM)
        lines = [head, sep]
        for row in rows:
            cells = []
            for i, cell in enumerate(row[:len(headers)]):
                shown = truncate(cell, widths[i])
                styled = cell_styles[i](shown) if cell_styles and cell_styles[i] else shown
                cells.append(pad(styled, widths[i]))
            lines.append(spacing.join(cells))
        return "\n".join(lines)

    def status(self, text: str) -> str:
        if text in _OK_STATES:
            return self.s(text, GREEN)
        if text in _BAD_STATES:
            return self.s(text, RED)
        if text in _WARN_STATES:
            return self.s(text, YELLOW)
        return text

    def role(self, text: str) -> str:
        return self.s(text, _ROLE_STYLES.get(text, DIM))

    # -- events -------------------------------------------------------------

    def delta(self, text: str) -> None:
        print(text, end="", flush=True)

    def assistant(self, text: str) -> None:
        print(self.s(text, GREEN))

    def tool_call(self, name: str, args: dict) -> None:
        label = tool_call_label(name, args if isinstance(args, dict) else {})
        prefix = f"  ⚒ {name} "
        detail = truncate(label[len(name):].strip(" ·"), max(8, self.width - display_width(prefix)))
        print(f"  {self.s('⚒', CYAN)} {self.s(name, CYAN, BOLD)} {self.s(detail, DIM)}")

    def tool_result(self, ok: bool, summary: str, elapsed: float | None) -> None:
        mark = self.s("✓", GREEN) if ok else self.s("✗", RED)
        tail = self.s(f" {elapsed:.1f}s", DIM) if elapsed is not None else ""
        label = truncate(summary, max(8, self.width - 8 - display_width(tail)))
        print(f"  {mark} {self.s(label, GREEN if ok else RED)}{tail}")

    _TODO_MARKS = {"pending": "[ ]", "in_progress": "[~]",
                   "completed": "[x]", "cancelled": "[-]"}
    _TODO_COLORS = {"in_progress": YELLOW, "completed": GREEN}

    def todo_change(self, items: list) -> None:
        """Print the full task list after an ``update_todos`` replace.

        The tool's one-line summary only carries the count; without this
        block the user never sees what the agent actually planned.
        """
        done = sum(1 for it in items if it.get("status") == "completed")
        head = f"任务清单（{done}/{len(items)} 完成）"
        print(f"  {self.s('▤', MAGENTA)} {self.s(head, MAGENTA, BOLD)}")
        for n, it in enumerate(items, 1):
            status = str(it.get("status") or "pending")
            mark = self._TODO_MARKS.get(status, "[ ]")
            color = self._TODO_COLORS.get(status)
            mark = self.s(mark, color) if color else mark
            content = truncate(str(it.get("content") or ""),
                               max(8, self.width - 12))
            print(f"     {n}. {mark} {content}")

    def error(self, msg: str) -> None:
        print(f"  {self.s('!', RED)} {self.s(msg, RED)}")

    def warn(self, msg: str) -> None:
        print(f"  {self.s('~', YELLOW)} {self.s(msg, YELLOW)}")

    def usage(self, prompt_tk, completion_tk, ctx_tk, ctx_pct) -> str:
        return self.s(
            f"  · tokens p{prompt_tk}/c{completion_tk} "
            f"ctx={ctx_tk or '?'}tk ({ctx_pct}%)",
            DIM,
        )

    def reasoning(self, digest: str) -> str:
        return self.s(f"  ~ {digest}", DIM)

    def footer(self, ev: dict) -> None:
        """The one-line wrap-up after a run: status · steps · tokens · cost · time."""
        parts = [self.status(str(ev.get("status", "?")))]
        if ev.get("steps") is not None:
            parts.append(f"steps {ev['steps']}")
        tokens = ev.get("tokens")
        if isinstance(tokens, dict):
            tokens = tokens.get("total_tokens")
        if tokens:
            parts.append(f"tokens {tokens}")
        cost = ev.get("cost")
        if cost:
            parts.append(f"cost {cost:.4f}")
        if ev.get("subagent_delegations"):
            parts.append(f"delegations {int(ev['subagent_delegations'])}")
        if ev.get("subagent_cost"):
            parts.append(f"sub-cost {float(ev['subagent_cost']):.4f}")
        if ev.get("context_percent") is not None:
            parts.append(f"ctx {ev['context_percent']}%")
        elif ev.get("context_tokens"):
            parts.append(f"ctx {ev['context_tokens']}tk")
        duration = fmt_duration(ev.get("duration_s"))
        if duration:
            parts.append(duration)
        print(f"{self.s('──', DIM)} {' · '.join(parts)}")

    # -- chat ---------------------------------------------------------------

    def banner(self, version: str, model: str, workspace: str) -> str:
        rows = [
            f"lithe {version} · command agent",
            f"◆ model      {model}",
            f"⌂ workspace  {workspace}",
            "/help 查看会话命令 · /exit 退出",
        ]
        inner = max(8, self.width - 4)
        rows = [truncate(row, inner - 2) for row in rows]
        width = max(display_width(row) for row in rows) + 2
        top = "╭" + "─" * width + "╮"
        bot = "╰" + "─" * width + "╯"
        body = "\n".join("│" + pad(" " + row, width) + "│" for row in rows)
        art = "\n".join((top, body, bot))
        return self.s(art, CYAN)

    def prompt(self) -> str:
        return self.s("lithe ❯ ", CYAN, BOLD)


# Module-level singleton; main() reconfigures it from flags/env.
ui = UI(default_color())


def configure(color: bool | None = None) -> UI:
    """(Re)configure the singleton; ``None`` means auto-detect."""
    ui.color = default_color() if color is None else bool(color)
    return ui
