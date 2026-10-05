"""System-clipboard delivery for the CLI's copy commands.

Deliberately dependency-free and front-end-free: the Textual screen, the
plain per-line REPL and the tests all go through
:func:`deliver_clipboard`, which tries three channels in order —

1. the platform's own tool (``pbcopy`` / ``clip`` / ``wl-copy`` /
   ``xclip`` / ``xsel``): the only channel that writes the *real* system
   clipboard no matter what the terminal implements;
2. OSC 52 through the caller's app (Textual's ``copy_to_clipboard``):
   covers terminals without those tools, including remote sessions —
   ``app`` is optional because only a full-screen front-end has one;
3. a plain file under ``$LITHE_HOME`` (``clipboard-<ts>.txt``), so a copy
   is never a silent no-op on a terminal where neither works.

Everything is best-effort: failures come back as a reason string instead
of an exception, because a copy the user cannot find is worth reporting,
not crashing on.
"""

from __future__ import annotations

import inspect
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .config import lithe_home

_TIMEOUT = 5  # seconds: a wedged clipboard helper must not freeze the UI


def native_clipboard_command() -> list[str] | None:
    """The installed platform clipboard tool, if any (a pure PATH lookup)."""
    if sys.platform == "darwin":
        candidates = [["pbcopy"]]
    elif os.name == "nt":
        candidates = [["clip"]]
    else:
        candidates = [
            ["wl-copy"],
            ["xclip", "-selection", "clipboard"],
            ["xsel", "-i", "--clipboard"],
        ]
    for argv in candidates:
        if shutil.which(argv[0]):
            return argv
    return None


def pipe_to_clipboard(text: str, argv: list[str]) -> bool:
    """Feed *text* to a clipboard tool over stdin; ``False`` on any failure.

    ``clip.exe`` reads UTF-16LE from a pipe (cmd's convention), the Unix
    helpers UTF-8.
    """
    name = Path(argv[0]).name.lower()
    encoding = "utf-16-le" if name.startswith("clip") else "utf-8"
    try:
        proc = subprocess.run(
            argv, input=text.encode(encoding), capture_output=True,
            timeout=_TIMEOUT,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def write_clipboard_file(text: str, home: Path | None = None) -> Path | None:
    """Last-resort copy: the text as ``$LITHE_HOME/clipboard-<ts>.txt``."""
    base = Path(home) if home is not None else lithe_home()
    try:
        base.mkdir(parents=True, exist_ok=True)
        path = base / f"clipboard-{time.strftime('%Y%m%d-%H%M%S')}.txt"
        path.write_text(text if text.endswith("\n") else text + "\n",
                        encoding="utf-8")
    except OSError:
        return None
    return path


async def deliver_clipboard(
    text: str, *, app: Any = None, home: Path | None = None
) -> tuple[bool, str]:
    """Copy *text* by the best available channel; returns ``(ok, channel)``.

    ``channel`` is what the user should be told: the tool that took it
    (``pbcopy``), ``OSC 52``, or the fallback file's path — and when
    nothing worked, the reason.
    """
    argv = native_clipboard_command()
    if argv is not None and pipe_to_clipboard(text, argv):
        return True, argv[0]
    if app is not None and await _osc52(app, text):
        return True, "OSC 52"
    path = write_clipboard_file(text, home)
    if path is not None:
        return True, str(path)
    return False, ("系统剪贴板不可用（未找到 pbcopy/clip/wl-copy/xclip，"
                   "终端也不接受 OSC 52，且写入 $LITHE_HOME 失败）")


async def _osc52(app: Any, text: str) -> bool:
    """Textual's clipboard write, tolerant of sync or coroutine flavours.

    The release-switch that renamed pieces of the selection API is exactly
    why this goes through ``getattr``: an app that cannot copy must fall
    through to the next channel, not raise out of a key handler.
    """
    copy = getattr(app, "copy_to_clipboard", None)
    if not callable(copy):
        return False
    try:
        result = copy(text)
        if inspect.isawaitable(result):
            await result
    except Exception:  # noqa: BLE001 — no driver/headless: try the next channel
        return False
    return True
