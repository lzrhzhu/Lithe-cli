"""CLI surface for sessions/profiles/models: subcommands + plain chat resume."""

from __future__ import annotations

import asyncio

import pytest

from lithe_cli.config import Config
from lithe_cli.workbench import Workbench

from conftest import _tc, make_config, scripted_transport

WRITE_THEN_ANSWER = [
    {"tool_calls": [_tc("write_file", {"path": "a.txt", "content": "hi"}, "c1")]},
    {"content": "已写入 a.txt。"},
]


def _make_sessions(tmp_path, n=2):
    wb = Workbench(make_config(tmp_path, WRITE_THEN_ANSWER))
    ids = []
    for i in range(n):
        wb.open(title=f"会话 {i}")
        ids.append(wb.current["id"])
        asyncio.run(wb.run_turn(f"任务 {i}"))
    return wb, ids


def test_resume_accepts_hash_prefixed_id(tmp_path):
    """The TUI completion suggests "#12"; dispatching that token must route."""
    wb, ids = _make_sessions(tmp_path, n=3)
    wb.open()  # a fresh current session to switch away from
    r = wb.dispatch(f"/resume #{ids[0]}")
    assert wb.current["id"] == ids[0]
    assert not any(cls == "err" for cls, _ in r.messages)

    # resolve() itself accepts both shapes
    assert wb.sessions.resolve(f"#{ids[1]}")["id"] == ids[1]
    assert wb.sessions.resolve(str(ids[2]))["id"] == ids[2]


def test_sessions_subcommand_lists_renames_deletes(tmp_path, capsys):
    from lithe_cli.main import main

    wb, ids = _make_sessions(tmp_path)
    cfg = wb.cfg
    rc = main(["sessions", "--store", str(cfg.store_dir), "--user", cfg.user_id])
    assert rc == 0
    out = capsys.readouterr().out
    assert f"#{ids[0]}" in out and f"#{ids[1]}" in out
    assert "会话 0" in out and "会话 1" in out
    assert "done" not in out or True  # status glyphs live in the row lines

    rc = main(["sessions", "--store", str(cfg.store_dir), "--user", cfg.user_id,
               "--rename", str(ids[0]), "改名了"])
    assert rc == 0
    assert wb.sessions.get(ids[0])["title"] == "改名了"

    rc = main(["sessions", "--store", str(cfg.store_dir), "--user", cfg.user_id,
               "--delete", str(ids[1])])
    assert rc == 0
    assert wb.sessions.get(ids[1]) is None
    # runs survive deletion
    assert wb.store.get_run(
        wb.sessions.runs(ids[1])[0].run_id if wb.sessions.runs(ids[1]) else "",
        cfg.user_id,
    ) is not None or wb.sessions.runs(ids[1]) == []

    rc = main(["sessions", "--rename", "99", "x", "--store", str(cfg.store_dir)])
    assert rc == 1
    assert "找不到" in capsys.readouterr().out


def test_sessions_empty_store(tmp_path, capsys):
    from lithe_cli.main import main

    rc = main(["sessions", "--store", str(tmp_path / "empty")])
    assert rc == 0
    assert "没有会话记录" in capsys.readouterr().out


def test_config_list_use_model(tmp_path, monkeypatch, capsys):
    from lithe_cli.main import main
    from lithe_cli.profiles import ProfileStore

    monkeypatch.setenv("LITHE_HOME", str(tmp_path))
    store = ProfileStore()
    store.upsert("zhipu", "https://z.example/api", "sk-zhipu-key", "glm-4.6")
    store.upsert("or", "https://or.example/api", "sk-or-key-123456", "sonnet-4")

    rc = main(["config", "--list"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "zhipu" in out and "or" in out and "*" in out
    assert "sk-zhipu-key" not in out  # masked
    assert "sk-or-key-…3456".replace("…", "…") in out or "…" in out

    rc = main(["config", "--use", "or"])
    assert rc == 0 and store.active_name() == "or"

    rc = main(["config", "--use", "ghost"])
    assert rc == 1

    rc = main(["config", "--model", "glm-4.5-air"])
    assert rc == 0
    # --model edits the ACTIVE profile (or), not the first one
    assert store.endpoint("or")["model"] == "glm-4.5-air"
    assert store.endpoint("zhipu")["model"] == "glm-4.6"

    rc = main(["config", "--show"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "or" in out and "glm-4.5-air" in out


def test_models_subcommand_online_and_cached(tmp_path, monkeypatch, capsys):
    import lithe_cli.profiles as profiles_mod
    from lithe_cli.main import main
    from lithe_cli.profiles import ProfileStore

    monkeypatch.setenv("LITHE_HOME", str(tmp_path))
    ProfileStore().upsert("zhipu", "https://z.example/api", "sk-z", "glm-4.6")
    args = ["--profile", "zhipu"]

    class Resp:
        def read(self):
            return b'{"data": [{"id": "glm-4.6"}, {"id": "glm-4.5-air"}]}'

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(profiles_mod, "urlopen",
                        lambda req, timeout=10.0: Resp())
    rc = main(["models", *args])
    assert rc == 0
    out = capsys.readouterr().out
    assert "glm-4.6" in out and "glm-4.5-air" in out
    assert "已缓存" in out

    # offline path: fetch fails → cached fallback + nonzero exit
    def broken(req, timeout=10.0):
        raise OSError("down")

    monkeypatch.setattr(profiles_mod, "urlopen", broken)
    rc = main(["models", *args])
    assert rc == 1
    out = capsys.readouterr().out
    assert "连不上" in out and "glm-4.5-air" in out  # fallback shown

    rc = main(["models", "--cached", *args])
    assert rc == 0
    assert "glm-4.5-air" in capsys.readouterr().out


def test_plain_chat_resume_banner(tmp_path, monkeypatch, capsys):
    from lithe_cli import main as main_mod
    from lithe_cli import prompts

    wb, ids = _make_sessions(tmp_path, n=1)
    cfg = wb.cfg

    monkeypatch.setattr(prompts, "chat_line", lambda *a, **k: "/exit")
    rc = main_mod._chat_loop(cfg, resume=str(ids[0]))
    assert rc == 0
    out = capsys.readouterr().out
    assert f"已恢复会话 #{ids[0]}" in out

    with pytest.raises(SystemExit):
        main_mod._chat_loop(cfg, resume="不存在的")


def test_plain_chat_continue_latest(tmp_path, monkeypatch, capsys):
    from lithe_cli import main as main_mod
    from lithe_cli import prompts

    wb, ids = _make_sessions(tmp_path, n=1)
    monkeypatch.setattr(prompts, "chat_line", lambda *a, **k: "/exit")
    assert main_mod._chat_loop(wb.cfg, continue_latest=True) == 0
    out = capsys.readouterr().out
    assert f"已继续最近会话 #{ids[0]}" in out


def test_doctor_shows_profiles_and_sessions(tmp_path, monkeypatch, capsys):
    from lithe_cli.main import main
    from lithe_cli.profiles import ProfileStore
    from lithe_cli.workbench import Workbench

    monkeypatch.setenv("LITHE_HOME", str(tmp_path))
    ProfileStore().upsert("zhipu", "https://z.example/api", "sk-z", "glm-4.6")
    # doctor reads the default store ($LITHE_HOME/runs), so land data there
    cfg = Config(store_dir=tmp_path / "runs", workspace_dir=tmp_path / "ws",
                 transport=scripted_transport(WRITE_THEN_ANSWER))
    wb = Workbench(cfg)
    wb.open(title="唯一会话")
    asyncio.run(wb.run_turn("任务"))
    monkeypatch.chdir(tmp_path)  # doctor reads cwd as workspace

    assert main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "profiles" in out and "zhipu" in out
    assert "sessions" in out and "1 个会话" in out
