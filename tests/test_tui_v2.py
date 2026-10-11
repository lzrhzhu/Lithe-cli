"""TUI: shared state folding + the Textual front-end's pure builders —
headless. The legacy prompt_toolkit screen is gone; these tests cover
what the shipped UI actually builds on."""

from __future__ import annotations

from lithe_cli.ttui import (
    completion_suggestions,
    sidebar_markup,
)
from lithe_cli.ttui_render import prompt_info_text
from lithe_cli.tui import (
    COPY_SCOPES,
    TuiState,
    describe_copy,
    feed_text,
    parse_copy_scope,
    transcript_feed_lines,
)


def test_parse_copy_scope_accepts_aliases_and_rejects_junk():
    assert parse_copy_scope("") == "last"
    assert parse_copy_scope("  last ") == "last"
    assert parse_copy_scope("全部") == "all"
    assert parse_copy_scope("工具") == "tools"
    assert parse_copy_scope("输入") == "user"
    assert parse_copy_scope("nonsense") is None
    assert set(COPY_SCOPES) == {"last", "all", "user", "tools"}


def test_feed_text_scopes_pick_the_right_rows():
    feed = [
        ("user", "把 a.txt 改一下"),
        ("tool", "◆ edit_file"),
        ("ok", "✓ 编辑 a.txt（+3 -1 行） 0.0s"),
        ("assistant", "已完成。"),
        ("assistant", "还要我提交吗？"),
        ("dim", "  · read_file：a.txt（12 行）"),
    ]
    assert feed_text(feed, "all").splitlines()[0] == "把 a.txt 改一下"
    assert feed_text(feed, "user") == "把 a.txt 改一下"
    # The whole trailing assistant run is one answer, not just its last row.
    assert feed_text(feed, "last") == "已完成。\n还要我提交吗？"
    tools = feed_text(feed, "tools")
    assert "◆ edit_file" in tools and "✓ 编辑 a.txt" in tools
    assert "已完成。" not in tools


def test_feed_text_skips_subagent_position_markers():
    """Markers are mounting instructions for the pane, not transcript text:
    /copy all must lift the conversation without blank marker lines."""
    from lithe_cli.tui import SUBAGENT_FEED_PREFIX

    feed = [
        ("user", "分工"),
        (SUBAGENT_FEED_PREFIX + "researcher:a111", ""),
        ("assistant", "已完成。"),
    ]
    assert feed_text(feed, "all") == "分工\n已完成。"


def test_feed_text_last_does_not_walk_into_earlier_answers():
    feed = [
        ("assistant", "第一轮回答"),
        ("user", "追问"),
        ("assistant", "第二轮回答"),
    ]
    assert feed_text(feed, "last") == "第二轮回答"


def test_feed_text_streaming_text_is_the_newest_answer():
    feed = [("assistant", "上一轮")]
    # The in-flight answer is not in the feed yet: it *is* the last answer.
    assert feed_text(feed, "last", extra="正在写的回答") == "正在写的回答"
    # ...and it is appended as a tail for the whole-conversation copy.
    assert feed_text(feed, "all", extra="正在写的回答") == \
        "上一轮\n正在写的回答"


def test_feed_text_empty_feed_yields_empty_text():
    assert feed_text([], "last") == ""
    assert feed_text([], "all") == ""
    assert describe_copy("a\nb") == "2 行 · 3 字符"


def test_sidebar_sections_show_session_and_model():
    state = TuiState("glm-4.6", "/ws", 5, profile="zhipu",
                     session_id=12, session_title="重构计划")
    state.set_sessions([
        {"id": 12, "title": "重构计划", "n_runs": 3, "last_status": "done"},
        {"id": 11, "title": "bugfix", "n_runs": 7, "last_status": "done"},
        {"id": 10, "title": "调研", "n_runs": 2, "last_status": "cancelled"},
    ], busy={11})
    text = sidebar_markup(state)
    assert "▾ 会话" in text and "#12 重构计划" in text
    assert "#11" in text and "●" in text  # busy badge on the other session
    assert "#10" in text
    assert "▾ 模型" in text and "zhipu · glm-4.6" in text
    assert "F4" in text and "F3" in text
    assert "▾ 工具" in text
    # 运行状态不在侧栏：它由对话区的 thinking 通道与底栏承载
    assert "▾ 运行" not in text


def test_sidebar_without_session_shows_hint():
    state = TuiState("m", "/ws", 5)
    assert "单次运行" in sidebar_markup(state)


def test_sidebar_shows_the_workspace_path():
    """侧栏工作区段：完整路径按分隔符换行展示；折叠后标题带截断摘要。"""
    from lithe_cli.ui import display_width

    from lithe_cli.ttui_render import sidebar_sections

    state = TuiState("m", r"I:\Lithe\lithe-cli", 5)
    text = sidebar_markup(state)
    assert "▾ 工作区" in text
    assert r"I:\Lithe\lithe-cli" in text
    # 长路径换行不丢字符：拼接各行应还原完整路径，且不超侧栏内宽
    state.workspace = r"C:\Users\someone\AppData\Local\Temp\tmpabc123\ws"
    ws = next(s for s in sidebar_sections(state) if s["id"] == "workspace")
    assert "".join(ws["lines"]) == state.workspace
    for line in ws["lines"]:
        assert display_width(line) <= 34
    # 折叠态：▸ 标题行携带截断的路径摘要
    folded = next(ln for ln in
                  sidebar_markup(state, collapsed={"workspace"}).splitlines()
                  if "工作区" in ln)
    assert "▸" in folded and "…" in folded
    # 空工作区也不空段
    state.workspace = ""
    assert "（未知）" in sidebar_markup(state)


def test_sidebar_headings_bold_bodies_regular():
    """kilo 风格排版：标题行加粗、摘要置灰；正文行不加粗。"""
    state = TuiState("m", "/ws", 5, session_id=12, session_title="t")
    for line in sidebar_markup(state).splitlines():
        if "▾" in line or "▸" in line:
            assert "[b]" in line, f"标题行应加粗：{line}"
        elif line.strip():
            assert not line.startswith("[b]"), f"正文行不得加粗：{line}"


# -- markdown rendering of assistant replies -------------------------------------

from lithe_cli.ttui_render import (  # noqa: E402
    markdown_text,
    markdown_text_lines,
    welcome_markup,
)


def test_welcome_markup_shows_logo_model_workspace():
    state = TuiState("glm-4.6", r"I:\Lithe\proj", 35, profile="zhipu",
                     session_id=12, session_title="重构计划")
    text = welcome_markup(state, "0.2.1", "0.1.9")
    assert "██╗" in text  # the logo
    assert "lithe-cli 0.2.1 · lithe 0.1.9" in text
    assert "zhipu · glm-4.6" in text
    assert r"I:\Lithe\proj" in text
    assert "#12 重构计划" in text
    assert "/help" in text and "F7 端点" in text
    # 无会话时不显示会话行
    bare = TuiState("m", "/ws", 5)
    assert "● 会话" not in welcome_markup(bare, "0.2.1", "0.1.9")


def test_welcome_markup_escapes_brackets_in_data():
    """模型名/路径里的方括号是数据不是 markup：渲染不得报错，且最终
    显示里方括号原样保留。"""
    from rich.console import Console

    state = TuiState("[weird] model", r"D:\[work]\proj", 5,
                     session_title="[t]")
    text = welcome_markup(state, "0.2.1", "0.1.9")
    console = Console(file=open("nul", "w", encoding="utf-8"), record=True,
                      width=80)
    console.print(text, markup=True)  # invalid styles would raise here
    shown = console.export_text()
    assert r"D:\[work]\proj" in shown
    assert "[weird] model" in shown


def _plain(lines):
    return [str(t) for t in lines]


def test_markdown_headings_lose_hash_markers():
    lines = markdown_text_lines("## 实施方案\n### 细节\n正文")
    assert _plain(lines) == ["▍ 实施方案", "细节", "正文"]
    # 标题层级落在样式上（h1/h2 带 ▍ 前缀，h3+ 纯加粗）
    assert "# 实施方案" not in _plain(lines)


def test_markdown_inline_emphasis_and_code():
    lines = markdown_text_lines("改 **核心** 与 `run()`，见[文档](https://x.io/d)")
    assert _plain(lines) == ["改 核心 与 run()，见文档"]
    text = lines[0]
    spans = sorted((s.start, s.end, str(s.style)) for s in text.spans)
    assert any("bold" in st for _s, _e, st in spans)
    assert any("#ddd6fe" in st for _s, _e, st in spans)  # inline code
    assert any("underline" in st for _s, _e, st in spans)  # link label


def test_markdown_fences_lists_quotes_rules():
    from rich.syntax import Syntax
    from rich.text import Text

    reply = "先看：\n```python\nx = 1\n```\n- 甲\n- 乙\n1. 第一\n> 引用\n---"
    items = markdown_text_lines(reply)
    # 围栏代码整块变成一个高亮渲染对象（非逐行 Text）
    assert [type(i) for i in items] == \
        [Text, Syntax, Text, Text, Text, Text, Text]
    assert items[1].code == "x = 1"
    assert [str(t) for t in items if isinstance(t, Text)] == \
        ["先看：", "• 甲", "• 乙", "1. 第一", "│ 引用", "─" * 24]


def test_markdown_code_fences_highlight_and_degrade():
    from rich.syntax import Syntax

    # 语言标签带花括号属性写法也能取到语言名
    items = markdown_text_lines("```{.js}\nlet a = 1\n```")
    assert isinstance(items[0], Syntax) and items[0].code == "let a = 1"
    # 未知语言不炸（rich 回退纯文本），代码内容保真
    items = markdown_text_lines("```totally-unknown\nkeep me\n```")
    assert isinstance(items[0], Syntax) and items[0].code == "keep me"
    # 无语言标签 → text；空代码块不产生渲染对象
    assert isinstance(markdown_text_lines("```\nx\n```")[0], Syntax)
    assert markdown_text_lines("```\n```") == []
    # 未闭合围栏（流式中途/模型漏写）：已有内容照样高亮
    items = markdown_text_lines("```python\npartial = True")
    assert isinstance(items[0], Syntax) and items[0].code == "partial = True"


def test_markdown_drops_blank_lines_between_paragraphs():
    """行间距：段间空行不渲染（终端行高偏高，空行观感过宽）；代码块内的
    空行原样保留。"""
    reply = "第一段\n\n\n\n第二段\n\n\n结尾\n\n"
    assert _plain(markdown_text_lines(reply)) == ["第一段", "第二段", "结尾"]
    assert _plain(markdown_text_lines("\n\n只剩内容\n\n")) == ["只剩内容"]
    # 围栏内空行属于代码，不动
    items = markdown_text_lines("```\na\n\nb\n```")
    assert items[0].code == "a\n\nb"


def test_markdown_task_lists_render_as_glyphs():
    plain = _plain(markdown_text_lines("- [ ] 待办\n- [x] 已完成"))
    assert plain == ["○ 待办", "✓ 已完成"]


def test_markdown_leaves_non_markdown_data_verbatim():
    """方括号、未闭合标记、词内星号/下划线一律原样（数据不是标记）。"""
    line = "用 [dim] 标灰 [/]，2*3*4，snake_case，未闭合 ** 星"
    assert _plain(markdown_text_lines(line)) == [line]
    # 纯文本逐行保真（空行不渲染，不增行、不删字符、不加样式）
    plain_text = "已写入 a.txt。\n\n第二行 2*3"
    assert _plain(markdown_text_lines(plain_text)) == \
        ["已写入 a.txt。", "第二行 2*3"]
    assert str(markdown_text("abc")) == "abc"


def test_markdown_nested_and_numbered_lists_keep_indent():
    plain = _plain(markdown_text_lines("- 外层\n  - 内层\n   3. 序号"))
    assert plain == ["• 外层", "  • 内层", "   3. 序号"]


def test_sidebar_sections_collapse_with_inline_summary():
    """折叠段只剩一行：▸ + 标题 + 摘要（用量段把关键数字带在标题上）。"""
    state = TuiState("m", "/ws", 5)
    state.on_event({"type": "run_start"})
    state.on_event({"type": "usage", "prompt_tokens": 1200,
                    "completion_tokens": 340, "total_tokens": 1540,
                    "cost": 0.0123, "context_tokens": 100,
                    "context_window": 1000, "context_percent": 10.0})
    folded = sidebar_markup(state, collapsed={"usage"})
    assert "▸ 用量" in folded
    assert "输入 1,200" in folded and "输出 340" in folded
    assert "$0.0123" in folded and "上下文 10.0%" in folded
    assert "缓存输入" not in folded  # body folded away
    assert "▾ 用量" in sidebar_markup(state)  # default: all expanded
    # 待办的计数两种状态都在标题上（标题加粗、计数置灰，同一行）
    todos_line = next(line for line in sidebar_markup(state).splitlines()
                      if "待办" in line)
    assert "▾" in todos_line and "0/0" in todos_line
    folded_todos = next(line for line in
                        sidebar_markup(state, collapsed={"todos"}).splitlines()
                        if "待办" in line)
    assert "▸" in folded_todos and "0/0" in folded_todos


def test_render_builders_keep_the_legacy_ttui_import_path():
    from lithe_cli.ttui import sidebar_markup as legacy_sidebar
    from lithe_cli.ttui_render import prompt_info_text, sidebar_markup

    state = TuiState("m", "/ws", 5)
    assert legacy_sidebar is sidebar_markup
    assert legacy_sidebar(state) == sidebar_markup(state)
    assert prompt_info_text(state)  # 底栏移除后由信息行接替


def test_sidebar_marks_running_and_failed_tools():
    state = TuiState("m", "/ws", 5)
    state.on_event({"type": "run_start"})
    state.on_event({"type": "tool_call", "id": "c1", "name": "read_file",
                    "args": {"path": "x"}})
    state.on_event({"type": "tool_result", "id": "c1", "ok": False,
                    "summary": "读取失败"})
    text = sidebar_markup(state)
    assert "✗" in text and "read_file" in text
    todos_line = next(line for line in text.splitlines() if "待办" in line)
    assert "▾" in todos_line and "0/0" in todos_line


def test_prompt_info_lists_model_profile_and_reasoning():
    state = TuiState("glm-4.6", "/ws", 5, profile="zhipu")
    text = prompt_info_text(state)
    assert "glm-4.6" in text and "zhipu" in text
    assert "推理" not in text  # 未设置思考强度时不显示
    state.reasoning_effort = "high"
    assert "推理 high" in prompt_info_text(state)
    # 键位表仍在侧栏 按键 段（底部没有状态栏了）
    side = sidebar_markup(state)
    assert "▾ 按键" in side
    for key in ("F2 侧栏", "F3 会话", "F4 模型", "F5 设置", "F6 推理",
                "F7 端点", "Ctrl+J 换行", "/help"):
        assert key in side


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
    # reasoning 摘要进 thinking 通道（对话区的思考折叠），不进 feed
    assert state.thinking == ["思考片段"]
    assert not any(cls == "reason" for cls, _ in state.feed)
    assert any(cls == "warn" for cls, _ in state.feed)
