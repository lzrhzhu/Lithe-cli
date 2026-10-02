"""TUI v2: sections, pickers, completion, transcript rebuild — headless."""

from __future__ import annotations

from prompt_toolkit.document import Document

from lithe_cli.tui import (
    SlashCompleter,
    TuiState,
    _header_row,
    models_overlay_items,
    picker_rows,
    sessions_overlay_items,
    settings_overlay_items,
    transcript_feed_lines,
)
from lithe_cli.ui import display_width


def test_sidebar_sections_show_session_and_model():
    from lithe_cli.tui import _sidebar_lines

    state = TuiState("glm-4.6", "/ws", 5, profile="zhipu",
                     session_id=12, session_title="重构计划")
    state.set_sessions([
        {"id": 12, "title": "重构计划", "n_runs": 3, "last_status": "done"},
        {"id": 11, "title": "bugfix", "n_runs": 7, "last_status": "done"},
        {"id": 10, "title": "调研", "n_runs": 2, "last_status": "cancelled"},
    ], busy={11})
    text = "\n".join(t for _, t in _sidebar_lines(state))
    assert "◆ 会话" in text and "#12 重构计划" in text
    assert "#11" in text and "●" in text  # busy badge on the other session
    assert "#10" in text
    assert "◆ 模型" in text and "zhipu · glm-4.6" in text
    assert "F4" in text and "F3" in text
    assert "◆ 运行" in text and "◆ 工具" in text


def test_sidebar_without_session_shows_hint():
    from lithe_cli.tui import _sidebar_lines

    state = TuiState("m", "/ws", 5)
    text = "\n".join(t for _, t in _sidebar_lines(state))
    assert "单次运行" in text


def test_header_shows_profile_and_session():
    state = TuiState("glm-4.6", "/ws", 5, profile="zhipu",
                     session_id=12, session_title="重构计划")
    row = _header_row(state, 100, "1.0.0")
    text = "".join(f[1] for f in row)
    assert "zhipu · glm-4.6" in text and "#12" in text and "重构计划" in text
    assert display_width(text) <= 100
    # narrow terminals still fit
    row = _header_row(state, 30, "1.0.0")
    assert display_width("".join(f[1] for f in row)) <= 30


def test_overlay_navigation_wraps_and_skips_hints():
    state = TuiState("m", "/ws", 5)
    items = [
        {"kind": "item", "label": "a"},
        {"kind": "hint", "label": "hint"},
        {"kind": "item", "label": "b"},
    ]
    state.open_overlay("sessions", "会话", items)
    assert state.overlay_current()["label"] == "a"
    state.overlay_move(1)
    assert state.overlay_current()["label"] == "b"  # skipped the hint
    state.overlay_move(1)  # wraps past the trailing hint back to "a"
    assert state.overlay_current()["label"] == "a"
    state.close_overlay()
    assert state.overlay is None and state.overlay_current() is None


def test_sessions_overlay_items_marks_busy_and_orders():
    items = sessions_overlay_items(
        [
            {"id": 12, "title": "重构", "n_runs": 3, "last_status": "done"},
            {"id": 11, "title": "后台", "n_runs": 1, "last_status": None},
        ],
        busy={11},
    )
    labels = [i["label"] for i in items if i["kind"] == "item"]
    assert any("#12" in lb and "✓" in lb for lb in labels)
    assert any("#11" in lb and "●" in lb for lb in labels)
    assert items[-1]["kind"] == "hint" and "Enter" in items[-1]["label"]


def test_models_overlay_items_groups_by_profile():
    items = models_overlay_items(
        ["zhipu", "or"],
        {"zhipu": {"model": "glm-4.6", "cached_models": ["glm-4.6", "air"]},
         "or": {"model": "sonnet-4"}},
        current_profile="zhipu",
        current_model="glm-4.6",
        current_candidates=["glm-4.6", "air"],
    )
    heads = [i["label"] for i in items if i["kind"] == "head"]
    assert heads == ["── zhipu ──", "── or ──"]  # current profile first
    active = [i for i in items if i.get("active")]
    assert len(active) == 1 and active[0]["model"] == "glm-4.6"
    or_models = [i["model"] for i in items
                 if i["kind"] == "item" and i["profile"] == "or"]
    assert or_models == ["sonnet-4"]


def test_settings_overlay_items_toggles_and_hints():
    items = settings_overlay_items([
        {"key": "shell", "label": "run_command（宿主 shell）", "kind": "bool",
         "value": True},
        {"key": "vision", "label": "图像理解", "kind": "bool", "value": False},
        {"key": "max-steps", "label": "步数上限", "kind": "int", "value": 35},
    ])
    selectable = [i for i in items if i["kind"] == "item"]
    assert [i["key"] for i in selectable] == ["shell", "vision"]
    assert "●" in selectable[0]["label"] and "on" in selectable[0]["label"]
    assert "○" in selectable[1]["label"] and "off" in selectable[1]["label"]
    # 数值项不可选，以 hint 呈现并带用法提示
    hints = [i["label"] for i in items if i["kind"] == "hint"]
    assert any("max-steps" in h and "35" in h and "/set" in h for h in hints)
    assert "Enter" in items[-1]["label"]


def test_picker_rows_fit_and_highlight_cursor():
    state = TuiState("m", "/ws", 5)
    items = [{"kind": "item", "label": f"选项 {i}"} for i in range(20)]
    items.append({"kind": "hint", "label": "Enter 确认"})
    state.open_overlay("sessions", "会话", items)
    for w, h in ((60, 12), (40, 9), (100, 30)):
        rows = picker_rows(state, w, h)
        assert len(rows) == h
        for row in rows:
            assert display_width("".join(f[1] for f in row)) == w
    # the cursor row is visible even at the top of a long list
    body = "\n".join("".join(f[1] for f in row)
                     for row in picker_rows(state, 60, 12))
    assert "▸ 选项 0" in body

    # moving to the bottom keeps it visible too
    state.overlay_move(1)
    body = "\n".join("".join(f[1] for f in row)
                     for row in picker_rows(state, 60, 12))
    assert "▸ 选项 1" in body


def test_slash_completer_completes_commands_and_args():
    state = TuiState("m", "/ws", 5)
    state.model_candidates = ["glm-4.6", "glm-4.5-air"]
    state.profile_names = ["zhipu", "openrouter"]
    state.sessions = [{"id": 3}, {"id": 7}]
    c = SlashCompleter(state)

    got = [x.text for x in c.get_completions(Document("/mo"), None)]
    assert "/model " in got and "/models " in got

    got = [x.text for x in c.get_completions(Document("/model glm-4"), None)]
    assert "glm-4.6" in got and "glm-4.5-air" in got

    got = [x.text for x in c.get_completions(Document("/profile open"), None)]
    assert got == ["openrouter"]

    got = [x.text for x in c.get_completions(Document("/resume #"), None)]
    assert got == ["#3", "#7"]

    assert list(c.get_completions(Document("普通输入"), None)) == []


def test_transcript_feed_lines_maps_roles():
    rows = [
        {"role": "user", "content": "改一下 a.txt"},
        {"role": "assistant", "content": "",
         "tool_calls": [{"function": {"name": "edit_file"}}]},
        {"role": "tool", "content": "编辑 a.txt（+1 行）", "tool_name": "edit_file"},
        {"role": "assistant", "content": "完成"},
        # legacy string-encoded tool_calls still parse
        {"role": "assistant", "content": "",
         "tool_calls": '[{"function": {"name": "read_file"}}]'},
    ]
    feed = transcript_feed_lines(rows)
    assert ("user", "改一下 a.txt") in feed
    assert ("assistant", "完成") in feed
    assert any(cls == "tool" and "edit_file" in text for cls, text in feed)
    assert any(cls == "tool" and "read_file" in text for cls, text in feed)
    assert any(cls == "dim" and "edit_file" in text for cls, text in feed)


def test_build_app_accepts_overlay_callbacks():
    import os

    import pytest

    if os.name == "nt":
        pytest.skip("constructing a full prompt_toolkit Application "
                    "needs a console screen buffer (POSIX PTY only)")
    from lithe_cli.tui import build_app

    state = TuiState("m", "/ws", 5)
    picked = []
    keyed = []
    app, buffer = build_app(
        state, "chat", "0.7.0", lambda text: None,
        on_overlay_select=lambda name, item: picked.append((name, item)),
        on_overlay_key=lambda name, key: keyed.append((name, key)),
    )
    assert app is not None and buffer is not None
    # F3/F4 and the overlay navigation bindings are registered
    names = {str(b.handler.__name__ if b.handler else "")
             for b in app.key_bindings.bindings}
    assert any("overlay" in n for n in names)
    # the read-only condition follows the overlay state
    assert not buffer.read_only()
    state.open_overlay("sessions", "会话",
                       [{"kind": "item", "label": "x"}])
    assert buffer.read_only()
    state.close_overlay()
    assert not buffer.read_only()
