"""Sessions: the CLI's named conversations over the kernel's store.

A session *is* a kernel conversation row (``{"id", "user_id", "title",
"meta"}``) plus the runs attached to it. Nothing session-shaped lives outside
the run store: messages persist incrementally per turn, runs carry the
``conversation_id``, and every query below is a store read — so a session
survives process restarts with zero sidecar state.

Identification: a session is addressed by its numeric id or by a unique title
prefix (``/resume refactor`` when the title is "重构计划" is fine if it
resolves; ambiguity is an error, not a guess).
"""

from __future__ import annotations

from typing import Any

from lithe.bundles import JsonlRunStore

TITLE_LIMIT = 30


def auto_title(task: str, limit: int = TITLE_LIMIT) -> str:
    """First task line, squeezed to *limit* display characters."""
    from .ui import truncate

    first = (task or "").strip().splitlines()[0] if task and task.strip() else ""
    return truncate(first, limit) or "新会话"


class SessionManager:
    def __init__(self, store: JsonlRunStore, user_id: str):
        self.store = store
        self.user_id = user_id

    # -- lifecycle ------------------------------------------------------------

    def new(self, title: str | None = None, meta: dict | None = None) -> dict:
        return self.store.create_conversation(
            self.user_id, title or "新会话", meta=meta
        )

    def get(self, conversation_id: int) -> dict | None:
        return self.store.get_conversation(conversation_id, self.user_id)

    def resolve(self, ident: str | int) -> dict | None:
        """Numeric id or unique title prefix → conversation row."""
        text = str(ident).strip()
        if not text:
            return None
        if text.isdigit():
            return self.get(int(text))
        matches = [
            c for c in self.list(limit=1000)
            if (c["title"] or "").startswith(text)
        ]
        if len(matches) == 1:
            return self.get(matches[0]["id"])
        return None  # zero or several hits: caller decides the message

    def resolve_exact(self, ident: str | int) -> tuple[dict | None, str]:
        """Like resolve but explains itself: (row | None, error message)."""
        row = self.resolve(ident)
        if row is not None:
            return row, ""
        text = str(ident).strip()
        if not text.isdigit():
            hits = [
                c["title"] for c in self.list(limit=1000)
                if (c["title"] or "").startswith(text)
            ]
            if len(hits) > 1:
                return None, f"标题前缀 {text!r} 匹配多个会话：{'、'.join(hits)}"
        return None, f"找不到会话 {text}（lithe-cli sessions 查看）"

    def latest(self) -> dict | None:
        rows = self.list(limit=1)
        return self.get(rows[0]["id"]) if rows else None

    # -- queries --------------------------------------------------------------

    def list(self, limit: int = 40) -> list[dict]:
        """Conversation summaries, newest activity first (kernel v2 store)."""
        return self.store.conversation_summaries(self.user_id, limit=limit)

    def summary(self, conversation_id: int) -> dict | None:
        for row in self.list(limit=1000):
            if row["id"] == conversation_id:
                return row
        return None

    def runs(self, conversation_id: int) -> list:
        return self.store.runs_for_conversation(conversation_id, self.user_id)

    def history(self, conversation_id: int) -> list[dict]:
        """All session messages as replay rows (see kernel replay_messages)."""
        return self.store.messages_for_conversation(
            conversation_id, self.user_id
        )

    def transcript(self, conversation_id: int, last: int = 200) -> list[dict]:
        """Recent messages for display (TUI rebuild), oldest first."""
        rows = self.history(conversation_id)
        return rows[-last:] if last else rows

    def usage_snapshot(self, conversation_id: int) -> dict:
        """Cumulative usage from stored run finals (None fields = unknown)."""
        totals = {"prompt_tokens": 0, "completion_tokens": 0,
                  "cached_tokens": 0, "total_tokens": 0, "cost": 0.0,
                  "known": False}
        for r in self.runs(conversation_id):
            totals["cost"] += r.cost or 0.0
            if r.total_tokens is not None:
                totals["known"] = True
                totals["total_tokens"] += r.total_tokens
                totals["prompt_tokens"] += r.prompt_tokens or 0
                totals["completion_tokens"] += r.completion_tokens or 0
                totals["cached_tokens"] += r.cached_tokens or 0
        return totals

    # -- mutations ------------------------------------------------------------

    def rename(self, conversation_id: int, title: str) -> None:
        self.store.rename_conversation(conversation_id, self.user_id, title)

    def delete(self, conversation_id: int) -> bool:
        """Soft-delete; runs/messages stay (log/undo keep working)."""
        return bool(self.store.delete_conversation(conversation_id, self.user_id))

    def set_meta(self, conversation_id: int, patch: dict[str, Any]) -> None:
        self.store.update_conversation_meta(conversation_id, self.user_id, patch)


def format_session_rows(rows: list[dict], active_id: int | None,
                        busy: set[int] | None = None) -> list[str]:
    """One line per session for pickers and `lithe sessions` (plain text)."""
    busy = busy or set()
    out = []
    for r in rows:
        mark = "*" if r["id"] == active_id else " "
        dot = "●" if r["id"] in busy else ("✓" if r["last_status"] == "done"
                                           else "·")
        title = r["title"] or "(无标题)"
        stamp = ""
        if r.get("updated_at") is not None:
            import datetime

            stamp = datetime.datetime.fromtimestamp(
                r["updated_at"]
            ).strftime("%m-%d %H:%M")
        out.append(
            f"{mark}#{r['id']:<4} {dot} {r['n_runs']:>3} 轮  {stamp:<12} {title}"
        )
    return out or ["（暂无会话；/new 或直接输入开始）"]
