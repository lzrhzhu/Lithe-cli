"""Slash commands: one registry, two frontends.

``Workbench.dispatch`` returns an :class:`ActionResult`; the plain REPL prints
``messages`` and runs ``awaitable`` synchronously, the TUI prints the same
messages into the conversation feed, opens ``overlay`` pickers and schedules
the awaitable as a task. Help text and completion words come from
:data:`COMMANDS`, so the two frontends can never drift apart.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from collections.abc import Callable

# The /copy scope vocabulary lives with the feed it extracts from, so the
# completion hint, the help line and the parser can never drift apart.
from .tui import COPY_SCOPE_HELP

# name → (args hint, one-line description); order is the help order.
COMMANDS: dict[str, tuple[str, str]] = {
    "help": ("", "显示这条帮助"),
    "new": ("[标题]", "开始新会话（当前会话保留，可随时 /resume 回来）"),
    "sessions": ("", "列出会话（F3 同效）"),
    "resume": ("[编号|标题]", "切换会话；不带参数等同 /sessions"),
    "rename": ("标题", "重命名当前会话"),
    "model": ("[名称|档案:模型|--save]", "查看/切换模型（F4 同效）；常用区置顶，档案:模型 跨档案切换；--save 存为档案默认"),
    "models": ("[all]", "从端点拉取模型列表并缓存；all 并行拉取全部档案（限定名显示）"),
    "reasoning": ("[级别|--save]", "查看/设置推理强度（F6 同效）；off/minimal/low/medium/high，下一轮生效"),
    "profile": ("[名称]", "查看/切换 provider 档案（列表含 provider · 协议标注）"),
    "fav": ("[档案:模型]", "查看/收藏常用模型；/model 列表与 F4 选择器顶部直达"),
    "set": ("[名称] [值]", "查看/调整运行设置（F5 同效）；面板内空格连切不关闭、Enter 切换并关闭，下一轮生效"),
    "tools": ("", "列出已注册的工具"),
    "copy": (f"[{COPY_SCOPE_HELP}]", "复制对话文本到系统剪贴板（右键同效）；默认最后一条回答"),
    "undo": ("[run]", "撤销当前会话最近一轮（或指定 run）的文件改动"),
    "sidebar": ("", "显示/隐藏右侧状态栏（F2 同效）"),
    "exit": ("", "退出（等同 /quit）"),
}


def help_text() -> list[tuple[str, str]]:
    lines = ["可用命令："]
    width = max(len(name) for name in COMMANDS)
    for name, (args, desc) in COMMANDS.items():
        lines.append(f"  /{name:<{width}} {args:<12} {desc}")
    lines.append("")
    lines.append("会话内随时可切模型/切会话，下一轮生效；运行中的轮次不受影响。")
    return [("dim", ln) for ln in lines]


@dataclass
class ActionResult:
    """What a dispatched line means to the calling frontend."""

    action: str = "none"  # none | submit | exit
    text: str = ""  # submit payload
    messages: list[tuple[str, str]] = field(default_factory=list)
    overlay: str | None = None  # TUI picker: "sessions" | "model" | "set"
    toggle_sidebar: bool = False
    changed: bool = False  # model/session changed → refresh banner/sidebar
    # /copy: which slice of the feed the front-end should put on the
    # clipboard. The workbench only parses it — the text itself lives in
    # the front-end (feed in the TUI, store transcript in the plain REPL).
    copy_scope: str | None = None
    # Zero-arg coroutine the frontend must run: plain → asyncio.run,
    # TUI → create_task (so /models fetches never block the screen).
    awaitable: Callable[[], Any] | None = None

    def say(self, cls: str, text: str) -> None:
        self.messages.append((cls, text))
