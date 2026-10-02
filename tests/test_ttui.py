"""Textual front-end tests — headless via App.run_test() (works on Windows)."""

from __future__ import annotations

import asyncio

import pytest

from conftest import _tc, make_config

textual = pytest.importorskip("textual")

from lithe_cli.ttui import LitheApp, footer_text, header_text, sidebar_markup  # noqa: E402
from lithe_cli.tui import TuiState  # noqa: E402

WRITE_THEN_ANSWER = [
    {"tool_calls": [_tc("write_file", {"path": "a.txt", "content": "hi"}, "c1")]},
    {"content": "已写入 a.txt。"},
]


def _app(tmp_path, responses, mode="chat"):
    cfg = make_config(tmp_path, responses)
    return LitheApp(cfg, "示例任务" if mode == "run" else None, mode)


async def _run_until_done(pilot, app, timeout=5.0):
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        await pilot.pause(0.05)
        if app.state.last_status == "done" and not app.state.running:
            return True
    return False


def _conv_text(app) -> str:

    return "\n".join(
        str(s.content) for s in app.query("#conv Static")
    )


def test_text_builders_show_sections_and_session():
    state = TuiState("glm-4.6", "/ws", 35, profile="zhipu",
                     session_id=12, session_title="重构计划")
    state.set_sessions([
        {"id": 12, "title": "重构计划", "n_runs": 3, "last_status": "done"},
        {"id": 11, "title": "bugfix", "n_runs": 7, "last_status": None},
    ], busy={11})
    state.on_event({"type": "run_start"})
    state.on_event({
        "type": "usage", "prompt_tokens": 100, "completion_tokens": 20,
        "cached_tokens": 5, "total_tokens": 120, "cost": 0.001,
        "context_tokens": 100, "context_window": 1000, "context_percent": 10.0,
    })
    state.on_event({"type": "done", "status": "done", "steps": 1,
                    "tokens": 120, "prompt_tokens": 100,
                    "completion_tokens": 20, "cost": 0.001})

    head = header_text(state, "1.0")
    assert "zhipu · glm-4.6" in head and "#12" in head and "重构计划" in head
    side = sidebar_markup(state)
    assert "◆ 会话" in side and "#11" in side and "●" in side
    assert "◆ 模型" in side and "zhipu · glm-4.6" in side
    assert "◆ 会话用量" in side and "输入 100" in side and "合计 120" in side
    foot = footer_text(state)
    assert "完成" in foot and "F3" in foot


def test_app_turn_streams_and_shows_result(tmp_path):
    async def scenario():
        app = _app(tmp_path, WRITE_THEN_ANSWER)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.query_one("#prompt").value = "把 a.txt 写为 hi"
            await pilot.press("enter")
            assert await _run_until_done(pilot, app), "turn did not finish"
            text = _conv_text(app)
            assert "把 a.txt 写为 hi" in text
            assert "已写入 a.txt" in text
            assert "edit" in text or "write" in text or "◆" in text
            side = app.query_one("#side").content
            assert "输入" in str(side)
            cid = app.wb.current["id"]
            runs = app.wb.sessions.runs(cid)
            assert len(runs) == 1 and runs[0].conversation_id == cid

    asyncio.run(scenario())


def test_f2_toggles_sidebar_without_residue(tmp_path):
    """The bug that motivated the Textual trial: toggling the sidebar must
    cleanly re-layout (Textual owns the repaint)."""

    async def scenario():
        app = _app(tmp_path, [])
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            wrap = app.query_one("#side-wrap")
            assert wrap.display is True
            await pilot.press("f2")
            await pilot.pause()
            assert wrap.display is False
            await pilot.press("f2")
            await pilot.pause()
            assert wrap.display is True
            # conversation pane re-expands to full width each time
            conv = app.query_one("#conv")
            assert conv.size.width > 50

    asyncio.run(scenario())


def test_session_picker_switches_sessions(tmp_path):
    async def scenario():
        app = _app(tmp_path, [{"content": "一"}])
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            first = app.wb.current["id"]
            # second session via /new
            app.query_one("#prompt").value = "/new 第二个"
            await pilot.press("enter")
            await pilot.pause()
            second = app.wb.current["id"]
            assert second != first

            from lithe_cli.ttui import PickerModal

            await pilot.press("f3")
            await pilot.pause()
            assert isinstance(app.screen, PickerModal)
            view = app.screen.query_one("#picker-list")
            assert len(view.children) == 2

            # cursor on the first entry (the other session), Enter switches
            view.index = 0
            await pilot.press("enter")
            await pilot.pause()
            assert app.wb.current["id"] == first
            # conversation pane rebuilt for the switched session
            assert app.state.session_id == first

    asyncio.run(scenario())


def test_model_picker_and_command_switch_model(tmp_path, monkeypatch):
    monkeypatch.setenv("LITHE_HOME", str(tmp_path / "home"))
    from lithe_cli.profiles import ProfileStore

    async def scenario():
        ProfileStore().upsert("zhipu", "https://z.example/api", "sk-z",
                              "glm-4.6", 128000)
        ProfileStore().cache_models("zhipu", ["glm-4.6", "glm-4.5-air"])
        app = _app(tmp_path, [])
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.cfg.profile = "zhipu"
            app.cfg.model = "glm-4.6"
            app._refresh_meta()

            from lithe_cli.ttui import PickerModal

            await pilot.press("f4")
            await pilot.pause()
            assert isinstance(app.screen, PickerModal)
            # index 0 = head row, 1 = glm-4.6 (active), 2 = glm-4.5-air
            app.screen.query_one("#picker-list").index = 2
            await pilot.press("enter")
            await pilot.pause()
            assert app.cfg.model == "glm-4.5-air"
            assert "glm-4.5-air" in _conv_text(app)

            # slash command path also works
            app.query_one("#prompt").value = "/model glm-4.6"
            await pilot.press("enter")
            await pilot.pause()
            assert app.cfg.model == "glm-4.6"

    asyncio.run(scenario())


def test_ctrl_c_exits_when_idle(tmp_path):
    async def scenario():
        app = _app(tmp_path, [])
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await pilot.press("ctrl+c")
            await pilot.pause()
            assert not app.is_running

    asyncio.run(scenario())


def test_run_mode_one_shot(tmp_path):
    async def scenario():
        app = _app(tmp_path, WRITE_THEN_ANSWER, mode="run")
        async with app.run_test(size=(100, 30)) as pilot:
            assert await _run_until_done(pilot, app), "one-shot did not finish"
            assert app.state.last_status == "done"
            text = _conv_text(app)
            assert "示例任务" in text and "已写入 a.txt" in text

    asyncio.run(scenario())


def test_completion_suggestions_commands_and_args():
    from lithe_cli.commands import COMMANDS
    from lithe_cli.ttui import completion_suggestions

    got = completion_suggestions("/mo", COMMANDS, [], [], [])
    assert got[:2] == ["/model ", "/models "]
    assert completion_suggestions("/", COMMANDS, [], [], [])[:1] == ["/exit "]
    assert completion_suggestions("普通输入", COMMANDS, [], [], []) == []
    assert completion_suggestions(
        "/model glm-4.5", COMMANDS, ["glm-4.6", "glm-4.5-air"], [], []
    ) == ["glm-4.5-air"]
    assert completion_suggestions(
        "/profile z", COMMANDS, [], ["zhipu", "openrouter"], []
    ) == ["zhipu"]
    assert completion_suggestions(
        "/resume #", COMMANDS, [], [], ["#1", "#12"]
    ) == ["#1", "#12"]
    # trailing space = about to type the argument → show all candidates
    assert completion_suggestions(
        "/model ", COMMANDS, ["a", "b"], [], []
    ) == ["a", "b"]


def test_app_completion_strip_and_history_recall(tmp_path, monkeypatch):
    monkeypatch.setenv("LITHE_HOME", str(tmp_path / "home"))

    async def scenario():
        app = _app(tmp_path, [])
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            prompt = app.query_one("#prompt")
            strip = app.query_one("#suggest")
            # typing / shows command suggestions; Tab accepts the first
            prompt.value = "/mod"
            await pilot.pause()
            assert strip.display is True
            assert "/model " in str(strip.content)
            await pilot.press("tab")
            await pilot.pause()
            assert prompt.value == "/model "
            # a plain line lands in history; ↑ recalls it after clearing
            prompt.value = "第一条"
            await pilot.press("enter")
            await pilot.pause()
            assert prompt.value == ""
            await pilot.press("up")
            await pilot.pause()
            assert prompt.value == "第一条"
            await pilot.press("up")  # only one stored line: stays put
            await pilot.pause()
            assert prompt.value == "第一条"
            await pilot.press("down")  # back to the empty draft
            await pilot.pause()
            assert prompt.value == ""

    asyncio.run(scenario())


def test_history_persists_across_processes(tmp_path, monkeypatch):
    monkeypatch.setenv("LITHE_HOME", str(tmp_path / "home"))

    async def first():
        app = _app(tmp_path, [])
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            prompt = app.query_one("#prompt")
            prompt.value = "跨进程的历史"
            await pilot.press("enter")
            await pilot.pause()

    asyncio.run(first())

    async def second():
        app = _app(tmp_path, [])
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            prompt = app.query_one("#prompt")
            assert prompt._lines[-1] == "跨进程的历史"
            await pilot.press("up")
            await pilot.pause()
            assert prompt.value == "跨进程的历史"

    asyncio.run(second())


def test_resolve_ui_defaults_to_textual(monkeypatch):
    from lithe_cli.main import _resolve_ui

    class Args:
        ui = None

    monkeypatch.delenv("LITHE_UI", raising=False)
    assert _resolve_ui(Args()) == "textual"
    monkeypatch.setenv("LITHE_UI", "prompt")
    assert _resolve_ui(Args()) == "prompt"
    args = Args()
    args.ui = "prompt"
    monkeypatch.setenv("LITHE_UI", "textual")
    assert _resolve_ui(args) == "prompt"  # flag beats env
    monkeypatch.setenv("LITHE_UI", "garbage")
    assert _resolve_ui(Args()) == "textual"  # unknown values fall back
