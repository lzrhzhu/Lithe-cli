"""Pure text builders for the Textual interface.

Keeping display formatting separate from the application/event loop makes
the sidebar and chrome easy to test without mounting a Textual app.  The
historical imports from :mod:`lithe_cli.ttui` remain available there.

The sidebar is a list of collapsible sections (:data:`SIDEBAR_SECTIONS`):
each carries a one-line *summary* that stays visible when the body is
folded away (click the heading in the TUI), so the numbers worth a
glance — session, model, token spend — never require unfolding.

Assistant replies arrive as Markdown. :func:`markdown_text_lines` and
:func:`markdown_text` turn a reply into styled Rich ``Text`` lines
(headings, emphasis, code fences, lists, quotes, links) — built as
``Text`` spans, never markup strings, so model data containing
``[dim]``-style brackets still renders verbatim instead of raising or
being eaten by a markup parser.
"""

from __future__ import annotations

import re

from rich.console import Group
from rich.markup import escape
from rich.syntax import Syntax
from rich.text import Text

from .tui import TuiState
from .ui import display_width, truncate, wrap_text

# Stable section ids, in sidebar order. The TUI mounts one heading + one
# body widget per id at compose time; renderers walk the same order.
# There is deliberately no run-status section: the live "thinking" lane
# in the conversation (spinner + elapsed, duration under the output once
# the turn ends) owns run state, so the sidebar never repeats it.
# "keys" starts folded: the F-key map is reference material, not worth
# screen estate by default — there is no footer to mirror it either;
# one click (or it stays collapsed) when needed.
SIDEBAR_SECTIONS = ("session", "workspace", "model", "tools", "todos",
                    "usage", "keys")


def sidebar_sections(state: TuiState) -> list[dict]:
    """Ordered ``{"id", "title", "summary", "lines"}`` sidebar sections."""
    sections: list[dict] = []

    # -- 会话 --------------------------------------------------------------
    lines: list[str] = []
    if state.session_id is None:
        summary = "单次运行"
        lines.append("[dim]（单次运行，无会话）[/]")
    else:
        summary = f"#{state.session_id}"
        dot = "[yellow]●[/]" if state.session_id in state.busy_sessions else "·"
        lines.append(f"#{state.session_id} {state.session_title or '无标题'} {dot}")
        shown = 0
        for row in state.sessions:
            if shown >= 3:
                break
            if row.get("id") == state.session_id:
                continue
            shown += 1
            mark = "[yellow]●[/]" if row["id"] in state.busy_sessions else (
                "[green]✓[/]" if row.get("last_status") == "done" else "·")
            lines.append(f"[dim] #{row['id']} {row.get('title') or ''} "
                         f"{mark} {row.get('n_runs', 0)}轮[/]")
        lines.append("[dim]F3 切换 · /new 新建[/]")
    sections.append({"id": "session", "title": "会话",
                     "summary": summary, "lines": lines})

    # -- 工作区 --------------------------------------------------------------
    # The resolved workspace path (where every tool path lands), wrapped
    # to the sidebar's inner width; folded, the heading keeps a truncated
    # copy of the path visible.
    ws = state.workspace or ""
    lines = _wrap_path(ws, 34) if ws else []
    if not lines:
        lines = ["（未知）"]
    sections.append({"id": "workspace", "title": "工作区",
                     "summary": truncate(ws, 22) if ws else "",
                     "lines": lines})

    # -- 模型 --------------------------------------------------------------
    lines = []
    label = f"{state.profile} · {state.model}" if state.profile else state.model
    lines.append(f"[cyan]{label}[/]")
    if state.dialect:
        lines.append(f"[dim]{state.dialect} · F7 端点[/]")
    else:
        lines.append("[dim]F7 端点管理[/]")
    if state.reasoning_effort:
        lines.append(f"[dim]推理 {state.reasoning_effort} · F6 切换[/]")
    sampling = []
    if state.temperature is not None:
        sampling.append(f"温度 {state.temperature:g}")
    if state.max_output:
        sampling.append(f"输出上限 {state.max_output:,}")
    if sampling:
        lines.append(f"[dim]{' · '.join(sampling)} · /set 调整[/]")
    if state.context_window:
        lines.append(f"[dim]窗口 {state.context_window:,} · F4 切换[/]")
    else:
        lines.append("[dim]F4 或 /model 切换[/]")
    sections.append({"id": "model", "title": "模型",
                     "summary": label, "lines": lines})

    # -- 工具 --------------------------------------------------------------
    lines = []
    if not state.tools:
        lines.append("[dim]暂无调用[/]")
    for tool in state.tools[-4:]:
        mark = {"running": "…", "done": "✓", "failed": "✗"}.get(tool["status"], "·")
        tcolor = "green" if tool["status"] == "done" else (
            "red" if tool["status"] == "failed" else "cyan")
        suffix = f" {tool['elapsed']:.1f}s" if tool.get("elapsed") is not None else ""
        lines.append(f"[{tcolor}]{mark} {tool['name']}{suffix}[/]")
    sections.append({"id": "tools", "title": "工具",
                     "summary": (f"最近 {min(len(state.tools), 4)} 项"
                                 if state.tools else "暂无"),
                     "lines": lines})

    # -- 待办 --------------------------------------------------------------
    done = sum(todo.get("status") == "completed" for todo in state.todos)
    lines = []
    if not state.todos:
        lines.append("[dim]暂无任务清单[/]")
    marks = {"pending": "[ ]", "in_progress": "[~]",
              "completed": "[x]", "cancelled": "[-]"}
    tcolors = {"pending": "dim", "in_progress": "yellow",
               "completed": "green", "cancelled": "dim"}
    for todo in state.todos[-5:]:
        status = todo.get("status")
        lines.append(f"[{tcolors.get(status, 'dim')}]"
                     f"{marks.get(status, '[ ]')} {todo.get('content', '')}[/]")
    sections.append({"id": "todos", "title": "待办",
                     "summary": f"{done}/{len(state.todos)}", "lines": lines})

    # -- 用量 --------------------------------------------------------------
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
    lines = []
    lines.append(f"输入 {input_tokens:,}")
    lines.append(f"输出 {output_tokens:,}")
    lines.append(f"缓存输入 {cached_tokens:,}")
    lines.append(f"合计 {tokens:,}")
    lines.append(f"本轮费用 ${turn_cost:.4f}")
    lines.append(f"累计费用 ${cost:.4f}")
    if state.max_cost is not None:
        lines.append(f"[dim]成本预算 ${cost:.4f} / ${state.max_cost:.2f}[/]")
    if state.max_total_tokens is not None:
        lines.append(f"[dim]token 预算 {tokens:,} / {state.max_total_tokens:,}[/]")
    lines.append(f"上下文 {context}")
    summary = f"输入 {input_tokens:,} · 输出 {output_tokens:,} · ${turn_cost:.4f}"
    if state.context_percent is not None:
        summary += f" · 上下文 {state.context_percent}%"
    sections.append({"id": "usage", "title": "用量",
                     "summary": summary, "lines": lines})

    # -- 按键 --------------------------------------------------------------
    # The full key map lives here, folded away by default (its summary
    # keeps the range visible) — with the footer gone, this section is
    # the only key reference.
    lines = [
        "[dim]F2 侧栏 · F3 会话 · F4 模型[/]",
        "[dim]F5 设置 · F6 推理 · F7 端点[/]",
        "[dim]Enter 发送 · Ctrl+J 换行 · Tab 采纳[/]",
        "[dim]Esc 关弹层 · Ctrl+C 取消/退出 · /help 命令[/]",
    ]
    sections.append({"id": "keys", "title": "按键",
                     "summary": "F2–F7 · /help", "lines": lines})
    return sections


def _wrap_path(path: str, width: int) -> list[str]:
    """Wrap a path to *width* display columns, preferring separator
    boundaries (``C:\\a\\b\\`` / ``c\\d``) over mid-token breaks; a single
    segment wider than a line falls back to the CJK-aware hard wrap."""
    lines: list[str] = []
    cur = ""
    for seg in (s for s in re.split(r"(?<=[\\/])", path) if s):
        if display_width(seg) > width:
            if cur:
                lines.append(cur)
                cur = ""
            lines.extend(wrap_text(seg, width))
        elif not cur or display_width(cur) + display_width(seg) <= width:
            cur += seg
        else:
            lines.append(cur)
            cur = seg
    if cur:
        lines.append(cur)
    return lines or [path]


def section_header(sec: dict, folded: bool) -> str:
    """The one-line heading: a bold ▾/▸ + title (kilo-style contrast
    against the regular-weight body lines), with the summary kept dim
    and inline when folded (todos keep their count either way)."""
    arrow = "▸" if folded else "▾"
    title = sec["title"]
    if sec["summary"] and (folded or sec["id"] == "todos"):
        return f"[b]{arrow} {title}[/b] [dim]{sec['summary']}[/]"
    return f"[b]{arrow} {title}[/b]"


def sidebar_markup(state: TuiState,
                   collapsed: frozenset[str] | set[str] = frozenset()) -> str:
    out: list[str] = []
    for sec in sidebar_sections(state):
        folded = sec["id"] in collapsed
        out.append(section_header(sec, folded))
        if not folded:
            out.extend(sec["lines"])
    return "\n".join(out)


def prompt_info_text(state: TuiState) -> str:
    """The dim line at the bottom of the editing area (kilo-style): the
    effective model with its profile and, when set, the reasoning effort
    — the endpoint truth at a glance where you type. There is no footer
    status bar; every field is escaped data, never markup."""
    label = f"{state.model} · {state.profile}" if state.profile \
        else state.model
    out = f"[dim]{escape(label)}[/]"
    if state.reasoning_effort:
        out += f"[dim] · 推理 {escape(state.reasoning_effort)}[/]"
    return out


# -- markdown rendering (assistant replies) --------------------------------------
#
# Chat models answer in Markdown. Rendering it as raw text shows every
# ``##`` and ``**`` marker; parsing it as Textual markup would explode on
# the brackets models love to emit. The middle path: a small CommonMark
# subset → Rich ``Text`` spans builder — styled output, zero markup
# parsing, model data stays verbatim wherever it is not one of the
# constructs below.

_MD_FENCE_RE = re.compile(r"^\s*(?P<tick>`{3,}|~{3,})(?P<info>.*)$")
_MD_HEAD_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_MD_RULE_RE = re.compile(r"^ {0,3}((?:-\s*){3,}$|(?:\*\s*){3,}$|(?:_\s*){3,}$)")
_MD_QUOTE_RE = re.compile(r"^\s{0,3}>\s?(.*)$")
_MD_TASK_RE = re.compile(r"^(\s*)[-*+]\s+\[([ xX])\]\s+(.*)$")
_MD_BULLET_RE = re.compile(r"^(\s*)[-*+]\s+(.+)$")
_MD_NUM_RE = re.compile(r"^(\s*)(\d{1,9})[.)]\s+(.+)$")
# Inline constructs, tried in order: code, ***bold italic***, **bold** /
# __bold__, *italic* / _italic_ (never inside words — `2*3*4` and
# `snake_case` stay literal), ~~strike~~, [label](url). Lazy matches keep
# a stray unmatched marker literal.
_MD_INLINE_RE = re.compile(
    r"`(?P<code>[^`\n]+)`"
    r"|(?P<tri>\*\*\*[^*\n]+?\*\*\*)"
    r"|(?P<bold>\*\*[^*\n]+?\*\*|__[^_\n]+?__)"
    r"|(?P<ital>(?<!\w)\*[^*\s\n][^*\n]*?\*(?!\w)|(?<!\w)_[^_\s\n][^_\n]*?_(?!\w))"
    r"|(?P<strike>~~[^~\n]+~~)"
    r"|(?P<link>\[(?P<ltxt>[^\]\n]*)\]\((?P<lurl>[^)\s]+)\))"
)

_HEAD_STYLES = {1: "bold #e9d5ff", 2: "bold #c4b5fd",
                3: "bold #e7e1f3", 4: "bold #e7e1f3",
                5: "bold #cfc8e2", 6: "bold #cfc8e2"}


def _md_inline(text: str) -> Text:
    """One line with inline markdown rendered to Text spans."""
    out = Text()
    pos = 0
    for m in _MD_INLINE_RE.finditer(text):
        if m.start() > pos:
            out.append(text[pos:m.start()])
        if m.group("code") is not None:
            out.append(m.group("code"), "#ddd6fe")
        elif m.group("tri") is not None:
            out.append(m.group("tri")[3:-3], "bold italic")
        elif m.group("bold") is not None:
            out.append(m.group("bold")[2:-2], "bold")
        elif m.group("ital") is not None:
            out.append(m.group("ital")[1:-1], "italic")
        elif m.group("strike") is not None:
            out.append(m.group("strike")[2:-2], "strike")
        else:
            label, url = m.group("ltxt"), m.group("lurl")
            out.append(label or url, "underline #c084fc")
        pos = m.end()
    if pos < len(text):
        out.append(text[pos:])
    return out


def _fence_lang(info: str) -> str:
    """The language tag off a fence info string (``python``,
    ``{.python #id}`` attribute form, or empty → plain text)."""
    lang = info.strip().split()[0] if info.strip() else ""
    lang = lang.lstrip("{").rstrip("}").lstrip(".")
    return lang or "text"


def _code_block(lines: list[str], lang: str) -> Syntax:
    """One fenced block as a pygments-highlighted renderable.

    ``ansi_dark`` rides the terminal's own 16-color palette and paints no
    background, matching the borderless chrome; unknown language names
    fall back to plain text inside rich (verified), never raise.
    Highlighting the whole block at once keeps multi-line tokens
    (triple-quoted strings, decorators) correctly colored.
    """
    return Syntax("\n".join(lines), lang, theme="ansi_dark",
                  word_wrap=False)


def markdown_text_lines(text: str) -> list[Text | Syntax]:
    """A reply rendered as styled terminal lines (CommonMark subset).

    Headings keep their text but lose the ``#`` markers (a colored bar
    and weight carry the level); emphasis/link markers disappear into
    spans; fenced code becomes one pygments-highlighted block; lists
    normalize to ``•``/``n.``/task glyphs; quotes get a rail; blank
    lines between paragraphs render as nothing (code fences keep them
    verbatim). Anything unrecognized — including square brackets that
    look like markup — passes through verbatim.
    """
    out: list[Text | Syntax] = []
    code_buf: list[str] = []
    code_lang = ""
    in_fence = False
    for raw in text.split("\n"):
        fence = _MD_FENCE_RE.match(raw)
        if fence:
            if in_fence:
                if code_buf:
                    out.append(_code_block(code_buf, code_lang))
                code_buf = []
                in_fence = False
            else:
                in_fence = True
                code_lang = _fence_lang(fence.group("info"))
            continue  # the fences themselves render as nothing
        if in_fence:
            code_buf.append(raw)
            continue
        if not raw.strip():
            # Blank lines between paragraphs render as nothing — terminal
            # rows are tall, so even one blank row reads as a wide gap.
            # Blank lines inside fences are buffered verbatim above.
            continue
        m = _MD_HEAD_RE.match(raw)
        if m:
            level = len(m.group(1))
            body = _md_inline(m.group(2))
            body.stylize(_HEAD_STYLES[level])
            head = Text("▍ " if level <= 2 else "",
                        style=_HEAD_STYLES[level])
            head.append_text(body)
            out.append(head)
            continue
        if _MD_RULE_RE.match(raw):
            out.append(Text("─" * 24, style="#3b3358"))
            continue
        m = _MD_QUOTE_RE.match(raw)
        if m:
            quote = _md_inline(m.group(1))
            quote.stylize("italic #a49bc2")
            rail = Text("│ ", style="italic #a49bc2")
            rail.append_text(quote)
            out.append(rail)
            continue
        m = _MD_TASK_RE.match(raw)
        if m:
            done = m.group(2).lower() == "x"
            mark = Text(f"{m.group(1)}{'✓' if done else '○'} ",
                        style="#4ade80" if done else "#a49bc2")
            mark.append_text(_md_inline(m.group(3)))
            out.append(mark)
            continue
        m = _MD_BULLET_RE.match(raw)
        if m:
            bullet = Text(f"{m.group(1)}• ", style="#c4b5fd")
            bullet.append_text(_md_inline(m.group(2)))
            out.append(bullet)
            continue
        m = _MD_NUM_RE.match(raw)
        if m:
            num = Text(f"{m.group(1)}{m.group(2)}. ", style="#c4b5fd")
            num.append_text(_md_inline(m.group(3)))
            out.append(num)
            continue
        out.append(_md_inline(raw))
    if in_fence and code_buf:
        # Unterminated fence (mid-stream, or the model forgot the close):
        # highlight what is there — the next render passes fix it up.
        out.append(_code_block(code_buf, code_lang))
    return out


def markdown_text(text: str) -> Text | Group:
    """The same rendering as one renderable (the streaming slot): a
    single Text when every line is text, else a Group stacking the
    highlighted code blocks between the lines."""
    items = markdown_text_lines(text)
    if all(isinstance(item, Text) for item in items):
        return Text("\n").join(items)
    return Group(*items)


# -- welcome screen ----------------------------------------------------------------
#
# What a fresh, empty conversation shows instead of a blank pane: the
# logo, the effective endpoint/model and workspace, and the handful of
# keys worth knowing on first contact. It lives only in the TUI layer —
# never in the feed — so /copy, export and session replay never see it,
# and the first real row replaces it.

_WELCOME_LOGO = (
    "██╗     ██╗████████╗██╗  ██╗███████╗",
    "██║     ██║╚══██╔══╝██║  ██║██╔════╝",
    "██║     ██║   ██║   ███████║█████╗  ",
    "██║     ██║   ██║   ██╔══██║██╔══╝  ",
    "███████╗██║   ██║   ██║  ██║███████╗",
    "╚══════╝╚═╝   ╚═╝   ╚═╝  ╚═╝╚══════╝",
)


def welcome_markup(state: TuiState, cli_version: str,
                   kernel_version: str) -> str:
    """The first-screen block (Textual markup; every dynamic field is
    escaped — model names and paths are data, not markup)."""
    lines = [f"[#c084fc]{row}[/]" for row in _WELCOME_LOGO]
    lines.append("")
    lines.append(f"[dim]lithe-cli {escape(cli_version)} · "
                 f"lithe {escape(kernel_version)}[/]")
    label = f"{state.profile} · {state.model}" if state.profile \
        else state.model
    lines.append(f"[#c084fc]◆ 模型[/] {escape(label)}   [dim]F4 切换[/]")
    lines.append(f"[#c084fc]⌂ 工作区[/] {escape(state.workspace)}")
    if state.session_id is not None:
        lines.append(f"[#c084fc]● 会话[/] #{state.session_id} "
                     f"{escape(state.session_title or '新会话')}")
    lines.append("")
    lines.append("[dim]输入任务开始 · /help 查看命令 · "
                 "F2 侧栏 · F3 会话 · F7 端点[/]")
    lines.append("[dim]试试：总结 README.md 的要点，或清理 src 里的 TODO[/]")
    return "\n".join(lines)
