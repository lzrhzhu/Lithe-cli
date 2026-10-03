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
- ``_CHAT_KEYS`` — the ``/keys`` help text.

Pure data and formatting only — no terminal ownership of any kind.
"""

from __future__ import annotations

import asyncio
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

    def invalidate(self) -> None:
        """Presentation refresh hook. The Textual front-end repaints on its
        own event loop, so this is deliberately a no-op here."""

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
复制用终端原生选区（Shift+拖拽）；侧栏碍事时按 F2 隐藏。"""


def transcript_feed_lines(rows):
    """Stored session messages -> conversation feed rows (feed rebuild)."""
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
