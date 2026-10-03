"""TUI: shared state folding + the Textual front-end's pure builders —
headless. The legacy prompt_toolkit screen is gone; these tests cover
what the shipped UI actually builds on."""

from __future__ import annotations

from lithe_cli.ttui import (
    completion_suggestions,
    footer_text,
    header_text,
    sidebar_markup,
)
from lithe_cli.tui import TuiState, transcript_feed_lines


def test_sidebar_sections_show_session_and_model():
    state = TuiState("glm-4.6", "/ws", 5, profile="zhipu",
                     session_id=12, session_title="重构计划")
    state.set_sessions([
        {"id": 12, "title": "重构计划", "n_runs": 3, "last_status": "done"},
        {"id": 11, "title": "bugfix", "n_runs": 7, "last_status": "done"},
        {"id": 10, "title": "调研", "n_runs": 2, "last_status": "cancelled"},
    ], busy={11})
    text = sidebar_markup(state)
    assert "◆ 会话" in text and "#12 重构计划" in text
    assert "#11" in text and "●" in text  # busy badge on the other session
    assert "#10" in text
    assert "◆ 模型" in text and "zhipu · glm-4.6" in text
    assert "F4" in text and "F3" in text
    assert "◆ 运行" in text and "◆ 工具" in text


def test_sidebar_without_session_shows_hint():
    state = TuiState("m", "/ws", 5)
    assert "单次运行" in sidebar_markup(state)


def test_header_shows_profile_and_session():
    state = TuiState("glm-4.6", "/ws", 5, profile="zhipu",
                     session_id=12, session_title="重构计划")
    text = header_text(state, "1.0.0")
    assert "zhipu · glm-4.6" in text and "#12" in text and "重构计划" in text


def test_sidebar_marks_running_and_failed_tools():
    state = TuiState("m", "/ws", 5)
    state.on_event({"type": "run_start"})
    state.on_event({"type": "tool_call", "id": "c1", "name": "read_file",
                    "args": {"path": "x"}})
    state.on_event({"type": "tool_result", "id": "c1", "ok": False,
                    "summary": "读取失败"})
    text = sidebar_markup(state)
    assert "✗" in text and "read_file" in text
    assert "◆ 待办 0/0" in text


def test_footer_switches_hints_by_running_state():
    state = TuiState("m", "/ws", 5)
    idle = footer_text(state)
    assert "Enter 发送" in idle and "/help" in idle
    state.running = True
    busy = footer_text(state)
    assert "Ctrl+C 取消" in busy and "F3" in busy and "F4" in busy


def test_completion_suggestions_cover_commands_and_args():
    state = TuiState("m", "/ws", 5)
    state.model_candidates = ["glm-4.6", "glm-4.5-air"]
    state.profile_names = ["zhipu", "openrouter"]
    state.sessions = [{"id": 3}, {"id": 7}]
    from lithe_cli.commands import COMMANDS

    got = completion_suggestions("/mo", COMMANDS,
                                 state.model_candidates, state.profile_names,
                                 [f"#{row['id']}" for row in state.sessions])
    assert "/model" in [g.rstrip() for g in got]
    assert "/models" in [g.rstrip() for g in got]

    got = completion_suggestions("/model glm-4", COMMANDS,
                                 state.model_candidates, state.profile_names,
                                 [])
    assert "glm-4.6" in got and "glm-4.5-air" in got

    got = completion_suggestions("/profile open", COMMANDS,
                                 [], state.profile_names, [])
    assert got == ["openrouter"]

    got = completion_suggestions("/resume #", COMMANDS,
                                 [], [], ["#3", "#7"])
    assert got == ["#3", "#7"]

    assert completion_suggestions("普通输入", COMMANDS, [], [], []) == []


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


def test_state_event_folding_feeds_and_usage():
    state = TuiState("model-x", "/ws", 12)
    state.on_event({"type": "run_start"})
    state.on_event({"type": "assistant_delta", "text": "部分"})
    state.on_event({"type": "assistant", "text": "最终回答"})
    state.on_event({"type": "reasoning", "summary": "思考片段"})
    state.on_event({"type": "cancelled"})
    assert ("assistant", "最终回答") in state.feed
    assert state.streaming == ""
    assert any(cls == "reason" and "思考片段" in text for cls, text in state.feed)
    assert any(cls == "warn" for cls, _ in state.feed)
