"""Shared presentation state for the CLI front-ends.

The legacy prompt_toolkit full-screen application that used to live here
is gone: `lithe chat` / `lithe run` on an interactive terminal always
launch the Textual front-end (:mod:`lithe_cli.ttui`), and without a TTY
``main`` falls back to the plain per-line REPL. What remains is what the
remaining front-ends build on:

- :func:`screen_supported` — the interactive-terminal gate;
- :class:`TuiState` — kernel-event folding (feed, tools, usage
  accounting) the Textual app renders from;
- :func:`transcript_feed_lines` — stored session messages → feed rows;
- :func:`parse_copy_scope` / :func:`feed_text` — what ``/copy`` lifts off
  the feed (right-click and Ctrl+C over a selection go through the same
  extraction in the Textual front-end);
- ``_CHAT_KEYS`` — the ``/keys`` help text.

Pure data and formatting only — no terminal ownership of any kind.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time

from .ui import fmt_duration, tool_call_label, truncate

_STATUS_DONE = "done"


def screen_supported() -> bool:
    """The full-screen UI needs interactive input and output, never pipes
    or dumb terms."""
    return (
        sys.stdin.isatty()
        and sys.stdout.isatty()
        and os.environ.get("TERM") != "dumb"
    )


class TuiState:
    """Everything the front-end renders; mutated by kernel events only."""

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
        # Completion sources, kept fresh by the front-end.
        self.model_candidates: list[str] = []
        self.profile_names: list[str] = []
        self.running = False
        self.status = "就绪"
        self.step = 0
        self.tokens = 0
        self.cost = 0.0
        self.input_tokens = 0
        self.output_tokens = 0
        self.cached_tokens = 0
        self.last_turn_cost = 0.0
        self.last_turn_duration: float | None = None
        self._turn_tokens = 0
        self._turn_cost = 0.0
        self._turn_input_tokens = 0
        self._turn_output_tokens = 0
        self._turn_cached_tokens = 0
        self._done_seen = True
        self.context_tokens = None
        self.context_window = None
        self.context_percent = None
        self.reasoning_effort: str | None = None
        # Run budgets (for the sidebar progress lines); None = no cap.
        self.max_cost: float | None = None
        self.max_total_tokens: int | None = None
        self.tools: list[dict] = []
        self.feed: list[tuple[str, str]] = []
        # One independently searchable transcript per delegation instance.
        # The kernel already gives each parallel invocation a unique tag.
        self.subagents: dict[str, dict] = {}
        self.streaming = ""
        self.show_sidebar = True
        self.started: float | None = None
        self.last_status: str | None = None
        self.stop: asyncio.Event | None = None

    # -- feed ---------------------------------------------------------------

    def say(self, cls: str, text: str) -> None:
        lines = str(text).splitlines() or [""]
        self.feed.extend((cls, line) for line in lines)
        self.invalidate()

    def _subagent(self, instance: str, *, agent: str = "?",
                  display: str = "", task: str = "") -> dict:
        """Return/create one delegation card, keyed by its unique instance."""
        key = str(instance or agent or "unknown")
        return self.subagents.setdefault(key, {
            "instance": key, "agent": agent, "display": display or agent,
            "task": task, "status": "running", "steps": None,
            "changes": 0, "lines": [], "expanded": False, "search": "",
        })

    def add_subagent_line(self, instance: str, cls: str, text: str,
                          **identity) -> None:
        block = self._subagent(instance, **identity)
        block["lines"].append((cls, str(text)))
        self.invalidate()

    def restore_subagents(self, rows: list[dict]) -> None:
        """Rebuild delegation cards from instance-tagged persisted messages."""
        self.subagents.clear()
        for row in rows:
            instance = str(row.get("subagent") or "")
            if not instance:
                continue
            meta = row.get("meta")
            if isinstance(meta, str):
                try:
                    meta = json.loads(meta)
                except (ValueError, TypeError):
                    meta = None
            task_info = meta.get("subagent_task") if isinstance(meta, dict) else None
            if isinstance(task_info, dict):
                self._subagent(
                    instance, agent=str(task_info.get("agent") or "?"),
                    display=str(task_info.get("display") or ""),
                    task=str(task_info.get("task") or ""),
                ).update({k: task_info[k] for k in
                          ("status", "steps", "changes") if k in task_info})
                continue
            block = self._subagent(instance)
            role = row.get("role")
            content = str(row.get("content") or "").strip()
            if role == "assistant":
                calls = row.get("tool_calls")
                if isinstance(calls, str):
                    try:
                        calls = json.loads(calls)
                    except (ValueError, TypeError):
                        calls = None
                if isinstance(calls, list):
                    for call in calls:
                        fn = call.get("function") if isinstance(call, dict) else None
                        if isinstance(fn, dict):
                            name = str(fn.get("name") or "tool")
                            block["lines"].append(("tool", f"◆ {name}"))
                if content:
                    block["lines"].append(("assistant", content))
            elif role == "tool" and content:
                name = str(row.get("tool_name") or "工具")
                block["lines"].append(("dim", f"{name}：{content}"))
        self.invalidate()

    def invalidate(self) -> None:
        """Presentation refresh hook. The Textual front-end repaints on its
        own event loop, so this is deliberately a no-op here."""

    def elapsed(self) -> float | None:
        """Live seconds since the running turn started (None when idle or
        before the first ``run_start``). Frontends pair this with a periodic
        repaint so the timer advances even while the model call itself
        emits no events."""
        if self.running and self.started is not None:
            return max(0.0, time.monotonic() - self.started)
        return None

    # -- sessions -------------------------------------------------------------

    def set_sessions(self, rows: list[dict], busy: set[int]) -> None:
        self.sessions = list(rows)
        self.busy_sessions = set(busy)
        self.invalidate()

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
            self.streaming += text
        elif kind == "assistant":
            text = str(ev.get("text") or "")
            final_text = text or self.streaming
            if final_text:
                lines = final_text.splitlines() or [""]
                self.feed.extend(("assistant", line) for line in lines)
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
            self._settle_tool(ev.get("id"), bool(ev.get("ok")),
                              ev.get("summary"), ev.get("error"))
        elif kind == "todo_change":
            self.todos = list(ev.get("new") or [])
            done = sum(1 for t in self.todos if t.get("status") == "completed")
            # The list belongs in the conversation feed, not only the
            # sidebar: the sidebar can be hidden (F2) and caps at the last
            # few items, which is exactly how "todos were created but I
            # can't see them" happens.
            self.say("dim", f"▤ 任务清单已更新（{done}/{len(self.todos)} 完成）")
            marks = {"pending": "[ ]", "in_progress": "[~]",
                     "completed": "[x]", "cancelled": "[-]"}
            row_cls = {"in_progress": "warn", "completed": "ok"}
            for n, item in enumerate(self.todos, 1):
                status = str(item.get("status") or "pending")
                self.say(row_cls.get(status, "dim"),
                         f"  {n}. {marks.get(status, '[ ]')} "
                         f"{item.get('content', '')}")
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
        elif kind == "user_injected":
            # Steering: a queued user text the kernel injected mid-run at a
            # step boundary; render it as a user line so the transcript
            # shows why the model's course changed.
            self.say("user", str(ev.get("text") or ""))
        elif kind == "subagent_start":
            instance = str(ev.get("instance") or ev.get("agent") or "unknown")
            block = self._subagent(
                instance, agent=str(ev.get("agent") or "?"),
                display=str(ev.get("display") or ""),
                task=str(ev.get("task") or ""),
            )
            block.update({"agent": str(ev.get("agent") or block["agent"]),
                          "display": str(ev.get("display") or block["display"]),
                          "task": str(ev.get("task") or block["task"])})
            self.invalidate()
        elif kind == "subagent_end":
            instance = str(ev.get("instance") or ev.get("agent") or "unknown")
            block = self._subagent(
                instance, agent=str(ev.get("agent") or "?"),
                display=str(ev.get("display") or ""),
            )
            status = str(ev.get("status") or "")
            block.update(status=status or block["status"],
                         steps=ev.get("steps"), changes=ev.get("changes", 0))
            self.invalidate()
        elif kind == "subagent_progress":
            # Live worker output belongs to its independent instance block.
            instance = str(ev.get("instance") or ev.get("agent") or "unknown")
            block = self._subagent(
                instance, agent=str(ev.get("agent") or "?"),
                display=str(ev.get("display") or ""),
            )
            inner = ev.get("event") or {}
            t = inner.get("type")
            if t == "tool_call":
                name = str(inner.get("name") or "")
                args = inner.get("args")
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except (ValueError, TypeError):
                        args = {}
                args = args if isinstance(args, dict) else {}
                label = f"◆ {tool_call_label(name, args)}"
                self.add_subagent_line(instance, "tool", label,
                                       agent=block["agent"],
                                       display=block["display"])
            elif t == "tool_result":
                ok = bool(inner.get("ok"))
                label = str(inner.get("summary") or "").replace("\n", " ")
                if not ok and inner.get("error"):
                    detail = str(inner["error"]).replace("\n", " ")
                    if detail and label and (detail in label or label in detail):
                        label = detail if len(detail) > len(label) else label
                    elif detail:
                        label = f"{label} · {detail}" if label else detail
                self.add_subagent_line(instance, "ok" if ok else "err",
                                       f"{'✓' if ok else '✗'} {label}",
                                       agent=block["agent"],
                                       display=block["display"])
            elif t == "assistant":
                text = str(inner.get("text") or "").replace("\n", " ").strip()
                if text:
                    self.add_subagent_line(instance, "dim", text[:200],
                                           agent=block["agent"],
                                           display=block["display"])
            elif t == "error":
                msg = str(inner.get("message") or "出错").replace("\n", " ")
                self.add_subagent_line(instance, "err", msg[:200],
                                       agent=block["agent"],
                                       display=block["display"])
            elif t == "cancelled":
                self.add_subagent_line(instance, "warn", "已取消",
                                       agent=block["agent"],
                                       display=block["display"])
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
            if ev.get("subagent_delegations"):
                # Delegation footprint in the feed: count + subagent-only
                # spend (the folded cost above already includes it).
                extra = f"委派 {ev['subagent_delegations']} 次"
                if ev.get("subagent_cost"):
                    extra += f" · 子代理花费 ${float(ev['subagent_cost']):.4f}"
                self.say("dim", extra)
            if ev.get("context_tokens") is not None:
                self.context_tokens = int(ev["context_tokens"])
            if ev.get("context_window") is not None:
                self.context_window = int(ev["context_window"])
            if ev.get("context_percent") is not None:
                self.context_percent = ev["context_percent"]
            # Turn length: prefer the kernel's duration_s (covers the whole
            # run incl. prompt assembly); fall back to the local run_start
            # timer for envelopes that predate the field.
            if ev.get("duration_s") is not None:
                self.last_turn_duration = float(ev["duration_s"])
            elif self.started is not None:
                self.last_turn_duration = time.monotonic() - self.started
            if self.last_turn_duration is not None:
                self.status += f" · {fmt_duration(self.last_turn_duration)}"
        self.invalidate()

    def _settle_tool(self, tool_id, ok: bool, summary, error=None) -> None:
        tool = next(
            (t for t in reversed(self.tools) if t["id"] == tool_id and t["status"] == "running"),
            None,
        )
        if tool is None:
            return
        tool["elapsed"] = time.monotonic() - tool["t0"]
        tool["status"] = "done" if ok else "failed"
        label = str(summary or tool["name"]).replace("\n", " ")
        if not ok and error:
            # Same rule as the plain CLI: a failed call's summary ("参数错误")
            # without the diagnostic is not readable output; when one text
            # contains the other, keep only the longer one.
            detail = str(error).replace("\n", " ")
            if detail and label and (detail in label or label in detail):
                label = detail if len(detail) > len(label) else label
            elif detail:
                label = f"{label} · {detail}" if label else detail
        mark = "✓" if ok else "✗"
        self.say("ok" if ok else "err", f"{mark} {label} {tool['elapsed']:.1f}s")


_CHAT_KEYS = """按键：PgUp/PgDn 或鼠标滚轮 滚动对话，Home/End 跳到顶/底。
F2 侧栏 · F3 会话选择器 · F4 模型选择器 · F5 设置 · F6 推理强度（Esc 关闭）。
复制：右键菜单 或 /copy（默认最后一条回答；/copy all|user|tools）→ 系统剪贴板，
终端工具缺失时自动退到 OSC 52，再不行落到 $LITHE_HOME 的 clipboard-*.txt。
终端原生选区（Shift+拖拽）仍可用：选中后 Ctrl+C 复制，不再取消本轮；侧栏碍事时按 F2 隐藏。"""


# -- /copy: which feed rows land on the clipboard --------------------------------

# The scopes /copy accepts; ``last`` is the answer just given.
COPY_SCOPES = ("last", "all", "user", "tools")

COPY_SCOPE_HELP = "|".join(COPY_SCOPES)

COPY_SCOPE_LABELS = {
    "last": "最后一条回答",
    "all": "整段对话",
    "user": "我的输入",
    "tools": "工具输出",
}

_COPY_SCOPE_ALIASES = {
    "": "last", "last": "last", "answer": "last", "回答": "last",
    "all": "all", "全部": "all", "对话": "all",
    "user": "user", "me": "user", "输入": "user", "我": "user",
    "tools": "tools", "tool": "tools", "工具": "tools",
}


def parse_copy_scope(arg: str) -> str | None:
    """Normalise a ``/copy`` argument to one of :data:`COPY_SCOPES`.

    ``None`` means "not a scope" so the caller can report the usage instead
    of silently copying something unexpected.
    """
    return _COPY_SCOPE_ALIASES.get(str(arg or "").strip().lower())


def feed_text(feed: list[tuple[str, str]], scope: str = "all",
              extra: str = "") -> str:
    """Plain text of the conversation, for the clipboard (pure; tested).

    ``last`` is the newest assistant block — the answer just given —
    ``user`` the lines the user typed, ``tools`` the tool call/result
    lines, ``all`` the whole feed. ``extra`` is the text still streaming:
    it is not in the feed yet, which makes it *the* newest answer for
    ``last`` and a tail to append for ``all``.
    """
    extra = str(extra or "").rstrip("\n")
    if scope == "last" and extra.strip():
        return extra.strip("\n")
    if scope == "user":
        lines = [text for cls, text in feed if cls == "user"]
    elif scope == "tools":
        lines = [text for cls, text in feed if cls in ("tool", "ok", "err")]
    elif scope == "last":
        lines = _last_answer(feed)
    else:
        lines = [text for _cls, text in feed]
    text = "\n".join(lines).strip("\n")
    if extra and scope == "all":
        text = f"{text}\n{extra}".strip("\n")
    return text


def _last_answer(feed: list[tuple[str, str]]) -> list[str]:
    """The trailing run of assistant rows (one answer, gap-free)."""
    end = next(
        (i for i in range(len(feed) - 1, -1, -1)
         if feed[i][0] == "assistant" and str(feed[i][1]).strip()),
        None,
    )
    if end is None:
        return []
    start = end
    while start > 0 and feed[start - 1][0] == "assistant":
        start -= 1
    return [text for _cls, text in feed[start:end + 1]]


def describe_copy(text: str) -> str:
    """``N 行 · M 字符`` — the confirmation tail after a finished copy."""
    return f"{len(text.splitlines())} 行 · {len(text)} 字符"


def transcript_feed_lines(rows):
    """Stored session messages -> conversation feed rows (feed rebuild)."""
    import json as _json

    out = []
    for m in rows:
        # Subagent transcripts are rendered in their instance cards, never
        # folded into the orchestrator's shared conversation stream.
        if m.get("subagent"):
            continue
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
