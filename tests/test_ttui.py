"""Textual front-end tests — headless via App.run_test() (works on Windows)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from conftest import _tc, make_config

textual = pytest.importorskip("textual")

from textual.containers import VerticalScroll  # noqa: E402

from lithe_cli import clipboard as clipboard_mod  # noqa: E402

from lithe_cli.ttui import (  # noqa: E402
    ConfirmModal,
    LitheApp,
    PickerModal,
    SubagentCard,
    sidebar_markup,
)
from lithe_cli.ttui_render import prompt_info_text  # noqa: E402
from lithe_cli.tui import TuiState  # noqa: E402

WRITE_THEN_ANSWER = [
    {"tool_calls": [_tc("write_file", {"path": "a.txt", "content": "hi"}, "c1")]},
    {"content": "已写入 a.txt。"},
]

MD_ANSWER = [
    {"content": "## 计划\n先 **改代码**，再跑 `pytest` 验证。\n"
                "- [x] 分析\n- [ ] 实现\n```\nx = 1\n```\n> 引用原题"},
]


# -- copy: hermetic clipboard ------------------------------------------------

@pytest.fixture
def copied(monkeypatch):
    """Capture what the app sends to the clipboard, whichever channel."""
    sink: list[str] = []

    async def fake_osc52(app, text):
        sink.append(text)
        return True

    monkeypatch.setattr(clipboard_mod, "native_clipboard_command", lambda: None)
    monkeypatch.setattr(clipboard_mod, "_osc52", fake_osc52)
    return sink


class _RightClick:
    """Stand-in for Textual's MouseDown with button=3 (no driver needed)."""

    button = 3

    def __init__(self):
        self.stopped = False

    def stop(self):
        self.stopped = True


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

    side = sidebar_markup(state)
    assert "▾ 会话" in side and "#11" in side and "●" in side
    assert "▾ 模型" in side and "zhipu · glm-4.6" in side
    assert "▾ 用量" in side and "输入 100" in side and "合计 120" in side
    # 编辑区底部信息行带模型；按键表在侧栏，底栏已移除
    info = prompt_info_text(state)
    assert "glm-4.6" in info and "zhipu" in info
    assert "F3" not in info


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
            side = "\n".join(str(app.query_one(f"#side-body-{sid}").content)
                             for sid in ("usage",))
            assert "输入" in side
            cid = app.wb.current["id"]
            runs = app.wb.sessions.runs(cid)
            assert len(runs) == 1 and runs[0].conversation_id == cid

    asyncio.run(scenario())


def test_assistant_markdown_renders_not_raw_markers(tmp_path):
    """回复里的 Markdown 要渲染：标题去 # 加样式，强调去 **，代码块
    整块 pygments 高亮，列表归一为 •/○/✓，引用带 │；原始标记不得
    直接露出。"""

    async def scenario():
        from rich.syntax import Syntax

        app = _app(tmp_path, MD_ANSWER)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.query_one("#prompt").value = "给我计划"
            await pilot.press("enter")
            assert await _run_until_done(pilot, app), "turn did not finish"
            text = _conv_text(app)
            assert "▍ 计划" in text  # heading: bar + text, no ##
            assert "##" not in text and "**" not in text
            assert "`pytest`" not in text and "pytest 验证" in text
            assert "✓ 分析" in text and "○ 实现" in text
            assert "│ 引用原题" in text
            # 围栏代码：一个高亮渲染对象挂在会话区，内容保真
            codes = [getattr(c, "content", None)
                     for c in app.query_one("#conv").children]
            blocks = [c for c in codes if isinstance(c, Syntax)]
            assert blocks and blocks[0].code == "x = 1"
            # /copy 仍拿到原始 Markdown（渲染只影响显示，不影响数据）
            assert "## 计划" in app._scope_text("last")
            assert "```" in app._scope_text("last")

    asyncio.run(scenario())


def test_welcome_screen_shown_until_first_content(tmp_path):
    """开启即见的初始屏：空会话显示 logo/模型/工作区；第一条内容挂载后
    消失；切回空会话再现；它不进 feed（/copy 仍视为空对话）。"""

    async def scenario():
        app = _app(tmp_path, [])
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            # 1. 初始屏已挂载，内容带 logo 与工作区
            welcome = app.query_one("#welcome")
            text = str(welcome.content)
            assert "██╗" in text
            assert str(app.cfg.workspace_dir.resolve()) in text
            # /copy all：欢迎屏不算对话内容
            app.query_one("#prompt").value = "/copy all"
            await pilot.press("enter")
            await pilot.pause()
            assert "没有可复制" in _conv_text(app)
            # 2. 第一条内容 → 初始屏卸载
            app.state.say("user", "第一问")
            app._sync_feed()
            await pilot.pause()
            from textual.css.query import NoMatches

            with pytest.raises(NoMatches):
                app.query_one("#welcome")
            # 3. 切到新的空会话 → 初始屏回来（信息已随新状态刷新）
            second = app.wb.new_session(title="空会话")
            app._activate(second["id"])
            await pilot.pause()
            assert "空会话" in str(app.query_one("#welcome").content)

    asyncio.run(scenario())


def test_welcome_screen_not_shown_for_resumed_history(tmp_path):
    from lithe_cli.workbench import Workbench

    cfg = make_config(tmp_path, [])
    wb = Workbench(cfg)
    session = wb.new_session(title="有历史")
    cid = session["id"]
    from types import SimpleNamespace

    app = LitheApp(cfg, None, "chat",
                   args=SimpleNamespace(resume=str(cid), cont=False,
                                        title=None))

    async def scenario():
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            # 无消息的空历史仍然显示初始屏；有消息则直接看到内容
            app.state.say("assistant", "历史消息")
            app._sync_feed()
            await pilot.pause()
            from textual.css.query import NoMatches

            with pytest.raises(NoMatches):
                app.query_one("#welcome")
            assert "历史消息" in _conv_text(app)

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


def test_chrome_is_borderless_kilo_style(tmp_path):
    """无顶栏、无边框：聊天区/侧栏/输入框不再画 box，信息走侧栏与底栏。
    侧栏与会话区之间也不用分隔线——靠更深的底色区分两个区域。"""

    async def scenario():
        from textual.css.query import NoMatches

        app = _app(tmp_path, [])
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            with pytest.raises(NoMatches):
                app.query_one("#top")
            for sel in ("#conv", "#prompt"):
                assert not app.query_one(sel).styles.border, \
                    f"{sel} should carry no box border"
            # no divider either: the sidebar has no border at all and sits
            # on a background distinct from the conversation area's
            edges = app.query_one("#side-wrap").styles.border
            assert not any(edge[0] for edge in
                           (edges.top, edges.right, edges.bottom,
                            edges.left)), "侧栏不应再画分隔线"
            side_bg = app.query_one("#side-wrap").styles.background
            page_bg = app.screen.styles.background
            assert side_bg != page_bg and side_bg.rgb is not None, \
                "侧栏底色应与会话区不同"

    asyncio.run(scenario())


def test_sidebar_sections_fold_by_click(tmp_path):
    """点击段落标题折叠/展开；用量段默认折叠且摘要（输入/输出/费用）留在标题行。"""

    async def scenario():
        from lithe_cli.ttui_widgets import SectionHeader

        app = _app(tmp_path, [])
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            head = app.query_one("#side-head-usage", SectionHeader)
            body = app.query_one("#side-body-usage")
            assert body.display is False  # folded by default
            label = str(head.content)
            assert "▸" in label and "用量" in label
            # every section has a heading mounted
            from lithe_cli.ttui_render import SIDEBAR_SECTIONS

            for sid in SIDEBAR_SECTIONS:
                assert app.query_one(f"#side-head-{sid}")
                assert app.query_one(f"#side-body-{sid}")
            # click the usage heading → expands, body lines visible
            await pilot.click("#side-head-usage")
            await pilot.pause()
            assert body.display is True
            assert "▾" in str(head.content)
            assert "缓存输入" in str(body.content)
            # click again → folds back with the summary inline
            await pilot.click("#side-head-usage")
            await pilot.pause()
            assert body.display is False
            assert "▸" in str(head.content)
            # keys 段同样默认折叠（摘要保留 F2–F7 范围）
            khead = app.query_one("#side-head-keys", SectionHeader)
            kbody = app.query_one("#side-body-keys")
            assert kbody.display is False
            assert "F2–F7" in str(khead.content)

    asyncio.run(scenario())


def test_thinking_lane_spins_expands_and_settles(tmp_path):
    """思考走对话区的 thinking 通道：运行中 spinner + thinking 字样 + 计时，
    点击展开思考内容；结束后在输出下方收成 thought · 时长（无思考内容时是
    model · 时长 的暗色元信息行）；新一轮开始时复位。"""

    async def scenario():
        from lithe_cli.ttui_widgets import ThinkingRow

        app = _app(tmp_path, [])
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            row = app.query_one("#thinking", ThinkingRow)
            assert row.display is False  # 没有回合时通道隐藏

            # 回合开始：spinner + thinking 字样，默认收起
            app.state.say("user", "问题")
            app.state.on_event({"type": "run_start"})
            app.state.running = True
            app._sync_feed()
            app._refresh_chrome()
            await pilot.pause()
            assert row.display is True and row.active is True
            head = str(row.query_one(".fold-head").content)
            assert "thinking" in head
            assert any(frame in head for frame in ThinkingRow.FRAMES)
            body = row.query_one(".fold-body")
            assert body.display is False

            # 思考摘要在运行中累积；点击 thinking 展开
            app.state.on_event({"type": "reasoning", "summary": "先分析输入"})
            app.state.on_event({"type": "reasoning", "summary": "再规划步骤"})
            app._refresh_chrome()
            await pilot.click(".fold-head")
            await pilot.pause()
            assert row.expanded is True
            text = str(body.content)
            assert "先分析输入" in text and "再规划步骤" in text

            # 结束：通道在输出下方收成 thought · 时长（可再点击收放）
            app.state.say("assistant", "答案")
            app.state.on_event({"type": "done", "status": "done",
                                "steps": 1, "duration_s": 4.0})
            app.state.running = False
            app._sync_feed()
            app._refresh_chrome()
            await pilot.pause()
            assert row.active is False and row.display is True
            head = str(row.query_one(".fold-head").content)
            assert "thought" in head and "4.0s" in head
            # 通道位于答案行的下方、流式槽之上（输出的信息下方）
            order = [type(c).__name__ for c in app.query_one("#conv").children]
            assert order.index("ThinkingRow") == len(order) - 2
            await pilot.click(".fold-head")
            await pilot.pause()
            assert row.expanded is False and body.display is False

            # 无思考内容的回合：暗色 model · 时长 元信息行，点击不展开
            app.state.on_event({"type": "run_start"})
            app.state.on_event({"type": "done", "status": "done",
                                "steps": 1, "duration_s": 2.5})
            app.state.running = False
            app._refresh_chrome()
            await pilot.pause()
            head = str(row.query_one(".fold-head").content)
            assert "thought" not in head
            assert app.state.model in head and "2.5s" in head
            await pilot.click(".fold-head")
            await pilot.pause()
            assert row.expanded is False

    asyncio.run(scenario())


def test_block_gap_between_feed_blocks_and_taller_prompt(tmp_path):
    """类切换的行间距 block-gap；输入框最小高度 3 行。"""

    async def scenario():
        app = _app(tmp_path, [])
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.state.say("user", "第一问")
            app.state.say("user", "第一问（续）")
            app.state.say("tool", "◆ read_file")
            app.state.say("assistant", "答")
            app._sync_feed()
            await pilot.pause()
            rows = [c for c in app.query_one("#conv").children
                    if c.id not in ("streaming", "thinking")]
            gap = ["block-gap" in r.classes for r in rows]
            # 同类相邻行不空行；类切换处空一行
            assert gap == [False, False, True, True]
            # 输入框最小高度 3 行（CSS min-height 落到布局尺寸上）
            prompt = app.query_one("#prompt")
            assert prompt.size.height >= 3

    asyncio.run(scenario())


def test_kilo_style_tints_prompt_user_lines_and_workspace(tmp_path):
    """kilo 风格：输入框是比页面亮的紫色实底色带；用户发言是纯背景色块
    （无边框、近白偏紫文字、上方留白）；侧栏挂出工作区路径段。"""

    async def scenario():
        app = _app(tmp_path, [])
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            # 1. 输入框色带：实底、比页面底色亮（不再是半透明叠加）
            tint = app.query_one("#prompt-row").styles.background
            page = app.screen.styles.background
            assert tint.a == 1, "输入框应为实底色带"
            assert tint != page and tint.rgb is not None
            # 2. 用户发言：纯背景色块（无框）+ 通栏宽度 + 近白偏紫文字
            app.state.say("user", "用户问一句")
            app.state.say("user", "第二行")
            app.state.say("dim", "（间隔行）")
            app.state.say("user", "单独一条")
            app._sync_feed()
            await pilot.pause()
            users = [c for c in app.query_one("#conv").children
                     if "user" in c.classes]
            assert len(users) == 3
            assert all(str(u.styles.width) == "1fr" for u in users)
            assert all(u.styles.background.a == 1
                       and u.styles.background != page for u in users)
            assert all(u.styles.color.rgb == (0xe7, 0xe1, 0xf3)
                       for u in users)
            # 无边框：只靠背景成块
            assert all(not any(u.styles.border[i][0] for i in range(4))
                       for u in users)
            # 每条消息首行上方留白（间距增大），中间行不加
            assert users[0].styles.margin[0] == 1
            assert users[1].styles.margin[0] == 0
            assert users[2].styles.margin[0] == 1
            # 3. 侧栏工作区段：路径挂载（换行不丢字符，拼接可还原）
            body = app.query_one("#side-body-workspace")
            assert "".join(str(body.content).splitlines()) \
                == app.state.workspace
            assert app.state.workspace == \
                str(app.cfg.workspace_dir.resolve())
            # 标题加粗、正文缩进 2 列
            head = app.query_one("#side-head-workspace")
            assert "[b]" in str(head.content)
            assert body.styles.padding[3] == 2

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


def test_todos_are_loaded_per_conversation_on_new_and_switch(tmp_path):
    async def scenario():
        app = _app(tmp_path, [])
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            from lithe.bundles import JsonTodoStore
            from lithe_cli.agent import todo_store_path

            first = app.wb.current["id"]
            JsonTodoStore(todo_store_path(app.cfg, first)).replace([
                {"content": "第一会话任务", "status": "pending"},
            ])

            second_conv = app.wb.new_session()
            app._activate(second_conv["id"])
            assert app.state.todos == []

            JsonTodoStore(todo_store_path(app.cfg, second_conv["id"])).replace([
                {"content": "第二会话任务", "status": "pending"},
            ])
            app.wb.current = app.wb.sessions.get(first)
            app._activate(first)
            assert [todo["content"] for todo in app.state.todos] == ["第一会话任务"]

    asyncio.run(scenario())


def test_confirm_modal_mounts_and_answers(tmp_path):
    """回归：ConfirmModal 的 compose 必须可挂载（同父级 id 唯一）并能
    y/n 应答——早前两行同用 id="picker-hint"，破坏性命令审批一弹出
    就 MountError 崩溃。"""

    async def scenario():
        app = _app(tmp_path, [])
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            got: list[bool] = []
            app.push_screen(ConfirmModal("rm -rf build"), got.append)
            await pilot.pause()
            assert isinstance(app.screen, ConfirmModal)
            assert app.screen.query_one("#picker-command")
            await pilot.press("y")
            await pilot.pause()
            assert got == [True]

            got.clear()
            app.push_screen(ConfirmModal("del /s build"), got.append)
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            assert got == [False]

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


def test_run_mode_ctrl_c_cancels_one_shot_turn(tmp_path):
    """One-shot mode has no workbench turn; Ctrl+C must still cancel via the
    run's own stop handle (the README promise), not be a silent no-op."""
    async def scenario():
        release = asyncio.Event()

        class _Hang:
            async def complete(self, client, **kw):
                await release.wait()
                return {"content": "late", "tool_calls": [], "usage": {}}

        cfg = make_config(tmp_path, [])
        cfg.transport = _Hang()
        app = LitheApp(cfg, "长任务", "run")
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.2)
            assert app.state.running, "one-shot turn should be in flight"
            stop = app.state.stop
            assert stop is not None
            await pilot.press("ctrl+c")
            assert stop.is_set(), "Ctrl+C must reach the run's stop handle"
            assert "正在取消" in _conv_text(app)
            assert app.is_running, "first Ctrl+C cancels, not exits"
            # let the hung call return; the run then ends cancelled
            release.set()
            for _ in range(100):
                await pilot.pause(0.05)
                if not app.state.running:
                    break
            assert not app.state.running
            assert app.state.last_status in ("cancelled", "done")

    asyncio.run(scenario())


def test_mid_turn_text_steers_and_commands_still_work(tmp_path):
    """Plain text typed mid-turn steers the running turn (queued into the
    kernel inbox); slash commands dispatch while a turn runs. When the
    model call never yields a step boundary, leftovers are reported."""
    async def scenario():
        release = asyncio.Event()

        class _Hang:
            async def complete(self, client, **kw):
                await release.wait()
                return {"content": "ok", "tool_calls": [], "usage": {}}

        cfg = make_config(tmp_path, [])
        cfg.transport = _Hang()
        app = LitheApp(cfg, None, "chat")
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            inp = app.query_one("#prompt")
            inp.value = "第一个任务"
            await pilot.press("enter")
            await pilot.pause(0.2)
            assert app.state.running
            # plain text mid-turn: queued for injection, not restored
            inp.value = "补充说明"
            await pilot.press("enter")
            await pilot.pause(0.1)
            assert "已排队" in _conv_text(app)
            assert app.query_one("#prompt").value == ""
            # slash command mid-turn: dispatches normally
            inp.value = "/help"
            await pilot.press("enter")
            await pilot.pause(0.1)
            text = _conv_text(app)
            assert "命令" in text or "/model" in text
            # the hung call never reaches a step boundary: the queued text
            # cannot be injected and is honestly reported as dropped
            release.set()
            for _ in range(100):
                await pilot.pause(0.05)
                if not app.state.running:
                    break
            assert not app.state.running
            assert "未能在本轮结束前注入" in _conv_text(app)

    asyncio.run(scenario())


async def _settle_bottom(pilot, conv):
    """Give the follow anchor cycles to land: mounting grows the pane's
    extent only after a layout refresh, so scroll position converges over
    a few pauses (robust on slow CI)."""
    import time

    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        await pilot.pause(0.02)
        if conv.scroll_y >= conv.max_scroll_y - 1:
            return


def test_completed_turn_does_not_duplicate_the_feed(tmp_path):
    """Regression: session_idle used to reload the store transcript on top
    of the live-mounted rows, so every finished turn appeared twice."""

    async def scenario():
        app = _app(tmp_path, WRITE_THEN_ANSWER)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.query_one("#prompt").value = "把 a.txt 写为 hi"
            await pilot.press("enter")
            assert await _run_until_done(pilot, app), "turn did not finish"
            await pilot.pause(0.3)  # let the idle event land
            conv = app.query_one("#conv")
            mounted = [str(c.content) for c in conv.children
                       if c.id not in ("streaming", "thinking")]
            assert len(mounted) == len(set(mounted)), "rows must not double"
            from lithe_cli.tui import SUBAGENT_FEED_PREFIX

            expected = [text for cls, text in app.state.feed
                        if not cls.startswith(SUBAGENT_FEED_PREFIX)]
            assert mounted == expected

    asyncio.run(scenario())


def test_subagent_cards_mount_and_search_independently(tmp_path):
    async def scenario():
        app = _app(tmp_path, [])
        async with app.run_test() as pilot:
            state = app.state
            state.say("user", "先调研再实现")
            state.on_event({"type": "subagent_start", "agent": "researcher",
                            "display": "检索员", "task": "找 alpha",
                            "instance": "researcher:a111"})
            state.on_event({"type": "subagent_progress", "agent": "researcher",
                            "display": "检索员", "instance": "researcher:a111",
                            "event": {"type": "tool_result", "ok": True,
                                      "summary": "发现 alpha.txt"}})
            state.say("dim", "调研完成，开始实现")
            state.on_event({"type": "subagent_start", "agent": "coder",
                            "display": "编码员", "task": "实现 beta",
                            "instance": "coder:b222"})
            state.on_event({"type": "subagent_progress", "agent": "coder",
                            "display": "编码员", "instance": "coder:b222",
                            "event": {"type": "tool_result", "ok": True,
                                      "summary": "修改 beta.py"}})
            app._sync_feed()
            await pilot.pause()
            conv = app.query_one("#conv")
            cards = list(app.query(SubagentCard))
            assert len(cards) == 2
            assert {card.instance for card in cards} == {
                "researcher:a111", "coder:b222"
            }
            # Cards live inside the conversation pane, in chronology with
            # the feed lines around them — they scroll with the transcript,
            # not in a region of their own below it.
            assert all(card.parent is conv for card in cards)
            kinds = ["user" if "user" in c.classes else
                     "card" if isinstance(c, SubagentCard) else "line"
                     for c in conv.children
                     if c.id not in ("streaming", "thinking")]
            assert kinds == ["user", "card", "line", "card"]
            # Collapsed = exactly one row (heading only, no borders/margins)
            collapsed = next(c for c in cards
                             if c.instance == "coder:b222")
            assert collapsed.region.height == 1
            card = next(c for c in cards if c.instance == "researcher:a111")
            card._expanded = True
            card.set_data(state.subagents[card.instance])
            await pilot.pause()
            search = card.query_one(".subagent-search")
            search.value = "alpha"
            await pilot.pause()
            output = str(card.query_one(".subagent-output").content)
            assert "alpha" in output
            assert "beta" not in output
            other = next(c for c in cards if c.instance == "coder:b222")
            assert other._search == ""

    asyncio.run(scenario())


def test_resumed_session_mounts_persisted_subagent_cards(tmp_path):
    from lithe.bundles.store.protocol import StoredMessage
    from lithe_cli.workbench import Workbench

    cfg = make_config(tmp_path, [])
    wb = Workbench(cfg)
    session = wb.new_session(title="恢复子任务")
    cid = session["id"]
    run_id = "persisted-subagent-run"
    wb.store.create_run(run_id, cfg.user_id, "检查项目",
                        conversation_id=cid)
    wb.store.add_message(StoredMessage(
        role="user", content="开始检查项目", run_id=run_id,
        user_id=cfg.user_id,
    ))
    wb.store.add_message(StoredMessage(
        role="user", content="检查 README", run_id=run_id,
        user_id=cfg.user_id, subagent="researcher:abcd",
        meta={"subagent_task": {"agent": "researcher", "display": "检索员",
                                "task": "检查 README", "instance": "researcher:abcd",
                                "status": "done", "steps": 3}},
    ))
    wb.store.add_message(StoredMessage(
        role="assistant", content="README 包含使用说明", run_id=run_id,
        user_id=cfg.user_id, subagent="researcher:abcd",
    ))
    wb.store.add_message(StoredMessage(
        role="assistant", content="结论：README 已检查", run_id=run_id,
        user_id=cfg.user_id,
    ))
    app = LitheApp(cfg, None, "chat",
                   args=SimpleNamespace(resume=str(cid), cont=False, title=None))

    async def scenario():
        # wide terminal: the one-line heading truncates the task to the
        # pane width, so give it room to show "检查 README" in full on
        # every runner (the assertion would be width-dependent otherwise)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            cards = list(app.query(SubagentCard))
            assert len(cards) == 1
            card = cards[0]
            assert card.instance == "researcher:abcd"
            assert "检查 README" in str(card.query_one(".subagent-heading").label)
            assert app.state.subagents[card.instance]["status"] == "done"
            assert any("README 包含使用说明" in line
                       for _cls, line in app.state.subagents[card.instance]["lines"])
            assert not any("检查 README" in line for _cls, line in app.state.feed)
            # The card mounts where the delegation sat in the stored
            # transcript — between the orchestrator's own rows, not
            # appended after them.
            conv = app.query_one("#conv")
            order = [type(c).__name__ for c in conv.children
                     if c.id not in ("streaming", "thinking")]
            pos = order.index("SubagentCard")
            assert 0 < pos < len(order) - 1
            assert order[pos - 1] == order[pos + 1] == "Static"

    asyncio.run(scenario())


def test_sync_feed_follows_only_when_at_bottom(tmp_path):
    """Scrolling up to re-read earlier output sticks; new events must not
    yank the reader back to the bottom."""
    async def scenario():
        app = _app(tmp_path, [])
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            for i in range(60):
                app.state.say("dim", f"line {i}")
            app._sync_feed()
            await pilot.pause()
            conv = app.query_one("#conv", VerticalScroll)
            assert conv.max_scroll_y > 0
            await _settle_bottom(pilot, conv)
            assert conv.scroll_y >= conv.max_scroll_y - 1  # followed down
            conv.scroll_to(y=0, animate=False)
            await pilot.pause()
            assert conv.scroll_y == 0
            for i in range(10):
                app.state.say("dim", f"more {i}")
            app._sync_feed()
            await pilot.pause()
            assert conv.scroll_y == 0, "reading position must stick"
            conv.scroll_end(animate=False)
            await _settle_bottom(pilot, conv)
            app.state.say("dim", "tail")
            app._sync_feed()
            await _settle_bottom(pilot, conv)
            assert conv.scroll_y >= conv.max_scroll_y - 1  # follows again

    asyncio.run(scenario())


def test_completion_suggestions_commands_and_args():
    from lithe_cli.commands import COMMANDS
    from lithe_cli.ttui import completion_suggestions

    got = completion_suggestions("/mo", COMMANDS, [], [], [])
    assert got[:2] == ["/model ", "/models "]
    # "/" lists commands alphabetically, so /copy now leads the list
    assert completion_suggestions("/", COMMANDS, [], [], [])[:2] == \
        ["/copy ", "/exit "]
    assert completion_suggestions("/co", COMMANDS, [], [], []) == ["/copy "]
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


def test_prompt_submits_multiline_text_and_ctrl_enter_inserts_newline(tmp_path):
    async def scenario():
        app = _app(tmp_path, [{"content": "收到完整内容"}])
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            prompt = app.query_one("#prompt")
            prompt.focus()

            await pilot.press("a", "ctrl+enter", "b")
            await pilot.pause()
            assert prompt.text == "a\nb"

            # Ctrl+J is what LF becomes in Textual — the byte most
            # terminals actually deliver for Ctrl+Enter (a plain CR is
            # indistinguishable from Enter, so it cannot be the newline
            # key). Shift+Enter covers enhanced (kitty/CSI-u) reporting.
            prompt.value = ""
            await pilot.press("a", "ctrl+j", "b", "shift+enter", "c")
            await pilot.pause()
            assert prompt.text == "a\nb\nc"

            prompt.value = "a\nb"
            await pilot.press("enter")
            assert await _run_until_done(pilot, app), "multiline turn did not finish"
            assert "a\nb" in _conv_text(app)
            assert "收到完整内容" in _conv_text(app)

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


def test_f6_opens_reasoning_picker_and_applies(tmp_path):
    """Regression: F6 crashed with AttributeError (wb.REASONING_LEVELS)."""

    async def scenario():
        app = _app(tmp_path, [])
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.press("f6")
            await pilot.pause()
            assert isinstance(app.screen, PickerModal)
            levels = [p["level"] for p, _ in app.screen.items]
            assert levels == ["off", "minimal", "low", "medium", "high"]
            for _ in range(4):  # off -> high
                await pilot.press("down")
            await pilot.press("enter")
            await pilot.pause()
            assert app.cfg.reasoning_effort == "high"

    asyncio.run(scenario())


def test_f5_settings_picker_toggles_bool(tmp_path):
    """Regression: F5 crashed rendering the reasoning-effort row (None
    value), and Enter on a bool row sent the literal token 'toggle',
    which set_setting rejects — every toggle errored with 是开关."""

    async def scenario():
        app = _app(tmp_path, [])
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.press("f5")
            await pilot.pause()
            assert isinstance(app.screen, PickerModal)
            keys = [p.get("key") for p, _ in app.screen.items]
            assert keys[0] == "shell"  # first bool row
            assert app.cfg.shell is False
            await pilot.press("enter")  # toggle shell on
            await pilot.pause()
            assert app.cfg.shell is True
            assert "shell：off → on" in _conv_text(app)

    asyncio.run(scenario())


def test_f5_space_toggles_in_place_without_closing(tmp_path):
    """Regression: Enter was the only toggle and it dismissed the picker,
    so flipping several knobs meant reopening F5 per knob. Space now
    flips the focused row in place and the modal stays open."""

    async def scenario():
        app = _app(tmp_path, [])
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.press("f5")
            await pilot.pause()
            assert isinstance(app.screen, PickerModal)
            await pilot.press("space")  # shell off -> on
            await pilot.pause()
            assert app.cfg.shell is True
            assert isinstance(app.screen, PickerModal), "空格不得关闭设置面板"
            await pilot.press("down")
            await pilot.press("space")  # code off -> on
            await pilot.pause()
            assert app.cfg.code is True and app.cfg.shell is True
            assert isinstance(app.screen, PickerModal)
            # the flipped row's label reflects the new state in place
            labels = [label for _payload, label in app.screen.items]
            assert any("shell" in text and "●" in text for text in labels)

    asyncio.run(scenario())


def test_command_output_refreshes_model_candidates(tmp_path):
    """Regression: /models rewrote cached_models in the profile, but the
    session's F4 list and /model completion stayed stale until an
    unrelated event happened to call _refresh_meta."""

    async def scenario():
        app = _app(tmp_path, [])
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.cfg.model = "fresh-model"
            app.wb._emit(-1, {"type": "command_output", "style": "ok",
                              "text": "端点返回 2 个模型"})
            await pilot.pause()
            assert app.state.model == "fresh-model"
            assert app.state.model_candidates[0] == "fresh-model"
            assert "端点返回" in _conv_text(app)

    asyncio.run(scenario())


# --- copy: right-click, /copy, Ctrl+C over a selection, verbatim text --------


def test_right_click_on_the_conversation_opens_the_copy_menu(
        tmp_path, copied):
    """Mouse reporting swallows the terminal's own context menu, so the
    pane must answer button=3 itself (and stop the event, or the app
    handler would open a second menu)."""

    async def scenario():
        app = _app(tmp_path, [])
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.state.say("assistant", "答案第一行")
            app._sync_feed()
            await pilot.pause()

            event = _RightClick()
            app.query_one("#conv").on_mouse_down(event)
            await pilot.pause()
            assert event.stopped, "pane handler must consume the click"
            assert isinstance(app.screen, PickerModal)
            payloads = [payload for payload, _ in app.screen.items]
            assert [p.get("scope") for p in payloads if "scope" in p] == \
                ["last", "all", "user", "tools"]
            assert not any("selection" in p for p in payloads)

            app.screen.query_one("#picker-list").index = 1  # 复制整段对话
            await pilot.press("enter")
            await pilot.pause()
            assert copied and "答案第一行" in copied[-1]
            assert "已复制整段对话" in _conv_text(app)

    asyncio.run(scenario())


def test_right_click_offers_the_selection_when_there_is_one(
        tmp_path, copied, monkeypatch):
    async def scenario():
        app = _app(tmp_path, [])
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            monkeypatch.setattr("lithe_cli.ttui.selected_text",
                                lambda *a: "拖选出来的两行")
            app.state.say("assistant", "答案")
            app._sync_feed()
            app.open_copy_menu()
            await pilot.pause()
            payloads = [payload for payload, _ in app.screen.items]
            assert payloads[0] == {"selection": True}
            await pilot.press("enter")
            await pilot.pause()
            assert copied[-1] == "拖选出来的两行"
            assert "已复制选中文本" in _conv_text(app)

    asyncio.run(scenario())


def test_copy_command_copies_the_last_answer(tmp_path, copied):
    async def scenario():
        app = _app(tmp_path, [])
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.state.say("user", "把 a.txt 改一下")
            app.state.say("assistant", "已经改好了。")
            app.state.say("assistant", "还要提交吗？")
            app._sync_feed()
            await pilot.pause()

            prompt = app.query_one("#prompt")
            prompt.value = "/copy"
            await pilot.press("enter")
            await pilot.pause()
            assert copied[-1] == "已经改好了。\n还要提交吗？"

            prompt.value = "/copy user"
            await pilot.press("enter")
            await pilot.pause()
            assert copied[-1] == "把 a.txt 改一下"

    asyncio.run(scenario())


def test_copy_command_rejects_an_unknown_scope(tmp_path, copied):
    async def scenario():
        app = _app(tmp_path, [])
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.query_one("#prompt").value = "/copy 乱七八糟"
            await pilot.press("enter")
            await pilot.pause()
            text = _conv_text(app)
            assert "用法" in text and "/copy" in text
            assert copied == [], "a bad scope must not copy anything"

    asyncio.run(scenario())


def test_copy_on_an_empty_conversation_says_so(tmp_path, copied):
    async def scenario():
        app = _app(tmp_path, [])
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.query_one("#prompt").value = "/copy all"
            await pilot.press("enter")
            await pilot.pause()
            assert "没有可复制" in _conv_text(app)
            assert copied == []

    asyncio.run(scenario())


def test_ctrl_c_copies_a_selection_instead_of_cancelling(
        tmp_path, copied, monkeypatch):
    """The native Ctrl+C reflex over a drag selection must not kill the
    turn; with nothing selected Ctrl+C still cancels."""
    monkeypatch.setattr("lithe_cli.ttui.selected_text",
                        lambda *a: "选中了这一段")

    async def scenario():
        app = _app(tmp_path, [])
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await pilot.press("ctrl+c")
            await pilot.pause()
            assert copied == ["选中了这一段"]
            assert app.is_running, "Ctrl+C over a selection must not exit"
            assert "已复制选中文本" in _conv_text(app)

            # No selection: the documented exit path is untouched.
            monkeypatch.setattr("lithe_cli.ttui.selected_text", lambda *a: "")
            await pilot.press("ctrl+c")
            await pilot.pause()

    asyncio.run(scenario())


def test_feed_text_is_renderable_verbatim_not_as_markup(tmp_path, copied):
    """Model output is data: a "[dim]" in an answer must reach both the
    screen and the clipboard exactly as written (markup parsing would
    swallow it, and every caller reads widget content back for /copy)."""
    answer = "用 [dim] 标灰，用 [/] 收尾"

    async def scenario():
        app = _app(tmp_path, [])
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.state.say("assistant", answer)
            app._sync_feed()
            await pilot.pause()
            rendered = [str(s.content) for s in app.query("#conv Static")]
            assert answer in rendered
            prompt = app.query_one("#prompt")
            prompt.value = "/copy"
            await pilot.press("enter")
            await pilot.pause()
            assert copied[-1] == answer
    asyncio.run(scenario())


# -- F7 端点管理：表单 + 选择器 ------------------------------------------------------

def test_sidebar_shows_endpoint_dialect_and_prompt_info():
    state = TuiState("glm-4.6", "/ws", 35, profile="zhipu")
    state.dialect = "zai · chat"
    state.temperature = 0.7
    state.max_output = 8192
    side = sidebar_markup(state)
    assert "zai · chat · F7 端点" in side
    assert "温度 0.7" in side and "输出上限 8,192" in side
    # 按键表在侧栏的 按键 段（底部不再有状态栏）
    assert "F7 端点" in side and "F2 侧栏" in side
    # 编辑区信息行带模型与档案，不带 F 键提示
    info = prompt_info_text(state)
    assert "glm-4.6 · zhipu" in info and "F7" not in info
    # 无 dialect 时仍有 F7 入口提示
    state.dialect = ""
    assert "F7 端点管理" in sidebar_markup(state)


def test_form_modal_advances_cycles_and_submits():
    changes: list = []

    async def scenario():
        fields = [
            {"name": "profile", "label": "档案名", "kind": "text"},
            {"name": "provider", "label": "Provider", "kind": "choice",
             "choices": ["none", "anthropic", "zai"], "default": "none"},
            {"name": "base_url", "label": "Base URL", "kind": "text"},
            {"name": "api_key", "label": "API key", "kind": "password"},
            {"name": "model", "label": "模型", "kind": "text"},
        ]

        def on_change(field, value):
            changes.append((field, value))
            if field == "provider":
                form.set_value("base_url", "https://preset.example/v1")

        got: list = []
        from lithe_cli.ttui_widgets import FormModal
        from textual.app import App as TextualApp

        class Host(TextualApp):
            pass

        app = Host()
        form = FormModal("测试表单", fields, hint="提示", on_change=on_change)
        async with app.run_test(size=(80, 24)) as pilot:
            app.push_screen(form, got.append)
            await pilot.pause()
            # 档案名 → Provider（Enter 前进）
            await pilot.press(*"myrouter")
            await pilot.press("enter")
            # Provider 行 Enter 循环：none → anthropic，并预填 base_url
            await pilot.press("enter")
            assert changes[-1] == ("provider", "anthropic")
            # base_url 已预填，Tab 跳过 → api_key → model → 末项 Enter 提交
            await pilot.press("tab")
            await pilot.press("enter")
            await pilot.press(*"sk-demo")
            await pilot.press("enter")
            await pilot.press(*"glm-4.6")
            await pilot.press("enter")
            await pilot.pause()
        assert got and got[0][0] == "submit"
        values = got[0][1]
        assert values["profile"] == "myrouter"
        assert values["provider"] == "anthropic"
        assert values["base_url"] == "https://preset.example/v1"
        assert values["api_key"] == "sk-demo"
        assert values["model"] == "glm-4.6"

    asyncio.run(scenario())


def test_form_modal_blocks_blank_required_fields():
    from lithe_cli.ttui_widgets import FormModal
    from textual.app import App as TextualApp
    from textual.widgets import Static as TextStatic

    class Host(TextualApp):
        pass

    async def scenario():
        fields = [
            {"name": "profile", "label": "档案名", "kind": "text"},
            {"name": "model", "label": "模型", "kind": "text"},
        ]
        got: list = []
        app = Host()
        form = FormModal("测试表单", fields, hint="提示")
        async with app.run_test(size=(80, 24)) as pilot:
            app.push_screen(form, got.append)
            await pilot.pause()
            form.action_submit()  # 空表单直接提交：提示缺项，不 dismiss
            await pilot.pause()
            assert got == []
            assert "还需填写" in str(
                form.query_one("#picker-hint", TextStatic).content)
            # 填完再提交成功（set_value 同步 _values，直接改 .value 只进 widget）
            form.set_value("profile", "p")
            form.set_value("model", "m")
            form.action_submit()
            await pilot.pause()
        assert got and got[0] == ("submit", {"profile": "p", "model": "m"})

    asyncio.run(scenario())


def test_f7_endpoint_picker_lists_and_switches(tmp_path, monkeypatch):
    monkeypatch.setenv("LITHE_HOME", str(tmp_path / "home"))

    async def scenario():
        app = _app(tmp_path, [{"content": "ok"}])
        async with app.run_test(size=(100, 30)) as pilot:
            app.wb.profiles.upsert("a", "https://a.example/api", "sk-a",
                                   "m-a")
            app.wb.profiles.upsert("b", "https://b.example/api", "sk-b",
                                   "m-b", provider="zai")
            app._refresh_meta()
            app.action_open_endpoint()
            await pilot.pause()
            assert isinstance(app.screen, PickerModal)
            assert app.screen.title == "端点档案"
            labels = [label for _payload, label in app.screen.items]
            assert any("zai · chat" in lab and "m-b" in lab for lab in labels)
            # 第二行（b）回车 → 切换档案，整套端点真相生效
            await pilot.press("down")
            await pilot.press("enter")
            await pilot.pause()
            assert app.cfg.profile == "b"
            assert app.cfg.base_url == "https://b.example/api"
            assert app.cfg.model == "m-b"
            assert app.state.dialect == "zai · chat"

    asyncio.run(scenario())


def test_f7_form_creates_profile_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setenv("LITHE_HOME", str(tmp_path / "home"))

    async def scenario():
        app = _app(tmp_path, [{"content": "ok"}])
        async with app.run_test(size=(100, 30)) as pilot:
            app.action_open_endpoint()
            await pilot.pause()
            # 空档案列表 → n 打开表单
            await pilot.press("n")
            await pilot.pause(0.2)
            from lithe_cli.ttui_widgets import FormModal

            assert isinstance(app.screen, FormModal)
            form = app.screen
            await pilot.press(*"myrouter")
            await pilot.press("enter")        # → Provider
            # 循环到 openrouter（选择列表按字母序，逐次 Enter 换档）
            for _ in range(14):
                if form.values()["provider"] == "openrouter":
                    break
                await pilot.press("enter")
                await pilot.pause()
            assert form.values()["provider"] == "openrouter"
            await pilot.press("tab")          # base_url（Tab 全选了预填的官方 URL）
            base = form.values()["base_url"]
            assert base == "https://openrouter.ai/api/v1"
            # 直接键入自己的路由 URL（Tab 进入时全选，输入即整体替换）
            await pilot.press(*"https://my-router/api/v1")
            await pilot.press("enter")        # → api_key
            await pilot.press(*"sk-or")
            await pilot.press("enter")        # → model
            await pilot.press(*"vendor/claude-sonnet-4")
            await pilot.press("enter")        # 末项 → 保存
            await pilot.pause()
            assert app.cfg.profile == "myrouter"
            assert app.cfg.provider == "openrouter"
            assert app.cfg.base_url == "https://my-router/api/v1"
            ep = app.wb.profiles.endpoint("myrouter")
            assert ep["provider"] == "openrouter"
            assert ep["model"] == "vendor/claude-sonnet-4"

    asyncio.run(scenario())


def test_f7_form_blank_model_creates_profile(tmp_path, monkeypatch):
    """模型留空也能建档：之后 F4//models 拉取列表再选。"""
    monkeypatch.setenv("LITHE_HOME", str(tmp_path / "home"))

    async def scenario():
        app = _app(tmp_path, [{"content": "ok"}])
        async with app.run_test(size=(100, 30)) as pilot:
            app.action_open_endpoint()
            await pilot.pause()
            await pilot.press("n")            # 空列表 → 新建表单
            await pilot.pause(0.2)
            from lithe_cli.ttui_widgets import FormModal

            assert isinstance(app.screen, FormModal)
            await pilot.press(*"bare")
            await pilot.press("enter")        # → Provider（保持 none）
            await pilot.press("tab")          # → base_url
            await pilot.press(*"https://bare.example/api")
            await pilot.press("enter")        # → api_key
            await pilot.press(*"sk-bare")
            await pilot.press("enter")        # → model（留空）
            await pilot.press("enter")        # 末项空值 → 直接保存（非必填）
            await pilot.pause()
            assert app.cfg.profile == "bare"
            assert "model" not in app.wb.profiles.endpoint("bare")
            assert any("/models" in line[1] for line in app.state.feed)

    asyncio.run(scenario())


def test_f7_edit_and_delete_letters(tmp_path, monkeypatch):
    """e 打开编辑表单（预填）；d 确认后删除并回到刷新后的列表。"""
    monkeypatch.setenv("LITHE_HOME", str(tmp_path / "home"))

    async def scenario():
        from lithe_cli.ttui_widgets import ConfirmModal, FormModal

        app = _app(tmp_path, [{"content": "ok"}])
        async with app.run_test(size=(100, 30)) as pilot:
            app.wb.profiles.upsert("a", "https://a.example/api", "sk-a",
                                   "m-a")
            app.wb.profiles.upsert("b", "https://b.example/api", "sk-b",
                                   "m-b")
            app.action_open_endpoint()
            await pilot.pause()
            # e：第一行（a）→ 编辑表单，字段预填
            await pilot.press("e")
            await pilot.pause(0.2)
            assert isinstance(app.screen, FormModal)
            assert app.screen.title == "编辑档案 a"
            assert app.screen.values()["base_url"] == "https://a.example/api"
            await pilot.press("escape")       # 取消编辑（picker 已随 e 关闭）
            await pilot.pause()
            app.action_open_endpoint()
            await pilot.pause()
            # d：删除第一行（a）→ 确认模态 → y 执行
            await pilot.press("d")
            await pilot.pause(0.2)
            assert isinstance(app.screen, ConfirmModal)
            await pilot.press("y")
            await pilot.pause(0.2)
            assert "a" not in app.wb.profiles.names()
            assert isinstance(app.screen, PickerModal)
            assert [p["profile"] for p, _ in app.screen.items] == ["b"]
            # 拒绝路径：d → n 取消，档案仍在
            await pilot.press("d")
            await pilot.pause(0.2)
            await pilot.press("n")
            await pilot.pause(0.2)
            assert "b" in app.wb.profiles.names()

    asyncio.run(scenario())
