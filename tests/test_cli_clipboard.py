"""Clipboard delivery: channel order, platform tools, file fallback.

Offline by construction: the native tools are faked (PATH lookups are
monkeypatched, the one real subprocess is this interpreter reading stdin)
and OSC 52 is stubbed, so nothing here touches the host clipboard.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from lithe_cli import clipboard


def _deliver(text: str, home: Path, app=None):
    """deliver_clipboard with an explicit home (the async seam it needs)."""
    return asyncio.run(clipboard.deliver_clipboard(text, app=app, home=home))


def test_native_command_matches_platform(monkeypatch):
    monkeypatch.setattr(clipboard.shutil, "which", lambda name: f"/x/{name}")

    # sys.platform is checked first, so both attributes must be set per case.
    monkeypatch.setattr(clipboard.sys, "platform", "darwin")
    monkeypatch.setattr(clipboard.os, "name", "posix")
    assert clipboard.native_clipboard_command() == ["pbcopy"]

    monkeypatch.setattr(clipboard.sys, "platform", "win32")
    monkeypatch.setattr(clipboard.os, "name", "nt")
    assert clipboard.native_clipboard_command() == ["clip"]

    monkeypatch.setattr(clipboard.sys, "platform", "linux")
    monkeypatch.setattr(clipboard.os, "name", "posix")
    # Wayland first, then X11 — the first tool that exists wins.
    assert clipboard.native_clipboard_command() == ["wl-copy"]


def test_native_command_none_when_nothing_installed(monkeypatch):
    monkeypatch.setattr(clipboard.shutil, "which", lambda name: None)
    assert clipboard.native_clipboard_command() is None


def test_clip_tool_gets_utf16_but_unix_tools_get_utf8(monkeypatch):
    """clip.exe reads UTF-16LE from a pipe (cmd's convention) while the
    Unix helpers read UTF-8; the encoding must follow the tool."""
    seen: list[bytes] = []

    class _Proc:
        returncode = 0

    def fake_run(argv, input=None, **kw):  # noqa: A002 — mirrors subprocess API
        seen.append(input)
        return _Proc()

    monkeypatch.setattr(clipboard.subprocess, "run", fake_run)
    assert clipboard.pipe_to_clipboard("中文", ["clip"])
    assert clipboard.pipe_to_clipboard("中文", ["pbcopy"])
    assert seen[0] == "中文".encode("utf-16-le")
    assert seen[1] == "中文".encode()


def test_pipe_reports_failure_without_raising(monkeypatch):
    class _Proc:
        returncode = 1

    monkeypatch.setattr(clipboard.subprocess, "run", lambda *a, **k: _Proc())
    assert clipboard.pipe_to_clipboard("x", ["pbcopy"]) is False

    def boom(*a, **k):
        raise OSError("no such tool")

    monkeypatch.setattr(clipboard.subprocess, "run", boom)
    assert clipboard.pipe_to_clipboard("x", ["pbcopy"]) is False


def test_pipe_runs_for_real_through_this_interpreter():
    """One end-to-end pipe (no clipboard involved): a process that reads
    stdin and exits 0 counts as a delivered copy."""
    code = "import sys; sys.stdin.buffer.read()"
    assert clipboard.pipe_to_clipboard("hello 中文",
                                       [sys.executable, "-c", code])


def test_write_clipboard_file_adds_newline_and_timestamps(tmp_path):
    path = clipboard.write_clipboard_file("第一行\n第二行", tmp_path)
    assert path is not None and path.parent == tmp_path
    assert path.name.startswith("clipboard-") and path.suffix == ".txt"
    assert path.read_text(encoding="utf-8") == "第一行\n第二行\n"


def test_write_clipboard_file_survives_an_unwritable_home(tmp_path):
    # A path whose parent is a *file* cannot be created: report, don't raise.
    blocker = tmp_path / "home"
    blocker.write_text("not a dir", encoding="utf-8")
    assert clipboard.write_clipboard_file("x", blocker / "sub") is None


def test_deliver_prefers_the_native_tool(monkeypatch, tmp_path):
    calls: list[str] = []
    monkeypatch.setattr(clipboard, "native_clipboard_command",
                        lambda: ["pbcopy"])
    monkeypatch.setattr(clipboard, "pipe_to_clipboard",
                        lambda text, argv: calls.append(text) or True)
    ok, channel = _deliver("hello", tmp_path)
    assert (ok, channel) == (True, "pbcopy")
    assert calls == ["hello"]


def test_deliver_falls_back_to_osc52_when_tools_are_missing(
        monkeypatch, tmp_path):
    monkeypatch.setattr(clipboard, "native_clipboard_command", lambda: None)
    copied: list[str] = []

    async def fake_osc52(app, text):
        copied.append(text)
        return True

    monkeypatch.setattr(clipboard, "_osc52", fake_osc52)
    ok, channel = _deliver("hello", tmp_path, app=object())
    assert (ok, channel) == (True, "OSC 52")
    assert copied == ["hello"]
    # No file was needed, so the fallback stayed unused.
    assert list(tmp_path.glob("clipboard-*")) == []


def test_deliver_falls_back_to_a_file_when_nothing_else_works(
        monkeypatch, tmp_path):
    monkeypatch.setattr(clipboard, "native_clipboard_command", lambda: None)

    async def no_osc52(app, text):
        return False

    monkeypatch.setattr(clipboard, "_osc52", no_osc52)
    ok, channel = _deliver("hello", tmp_path, app=object())
    assert ok is True
    assert Path(channel).read_text(encoding="utf-8") == "hello\n"


def test_deliver_reports_when_every_channel_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(clipboard, "native_clipboard_command", lambda: None)
    monkeypatch.setattr(clipboard, "write_clipboard_file", lambda *a, **k: None)
    ok, channel = _deliver("hello", tmp_path)
    assert ok is False and "不可用" in channel


def test_osc52_tolerates_apps_without_the_api():
    class _NoApi:
        pass

    class _Boom:
        def copy_to_clipboard(self, text):
            raise RuntimeError("no driver")

    class _Sync:
        def __init__(self):
            self.copied = []

        def copy_to_clipboard(self, text):
            self.copied.append(text)

    class _Async:
        def __init__(self):
            self.copied = []

        async def copy_to_clipboard(self, text):
            self.copied.append(text)

    assert asyncio.run(clipboard._osc52(_NoApi(), "x")) is False
    assert asyncio.run(clipboard._osc52(_Boom(), "x")) is False

    sync_app = _Sync()
    assert asyncio.run(clipboard._osc52(sync_app, "x")) is True
    assert sync_app.copied == ["x"]

    async_app = _Async()
    assert asyncio.run(clipboard._osc52(async_app, "x")) is True
    assert async_app.copied == ["x"]
