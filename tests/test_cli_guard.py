"""lithe-cli: the dangerous-command guard is wired into build_registry.

End-to-end through registry dispatch: catastrophic commands never reach a
runner (safe to test — nothing executes), and the destructive tier asks
the injected approver. Kernel-side classification tables live in the
kernel's own test suite; this pins the CLI wiring. Sync tests drive
coroutines with asyncio.run, the suite's convention (no async plugin).
"""
from __future__ import annotations

import asyncio

from lithe import AgentContext

from lithe_cli.agent import build_registry, interactive_approver
from lithe_cli.config import Config


def _ctx() -> AgentContext:
    return AgentContext(run_id="r", user_id="u")


def test_guard_active_when_shell_enabled(tmp_path):
    cfg = Config(store_dir=tmp_path / "s", workspace_dir=tmp_path / "ws",
                 shell=True)
    reg = build_registry(cfg)
    res = asyncio.run(reg.dispatch("run_command",
                                   {"command": "rm -rf /"}, _ctx()))
    assert res.ok is False and res.summary == "危险命令已拦截"

    # destructive tier with no approver channel: denied with guidance
    res = asyncio.run(reg.dispatch("run_command",
                                   {"command": "rm -rf node_modules"}, _ctx()))
    assert res.ok is False and res.summary == "需用户审批"


def test_guard_asks_injected_approver(tmp_path):
    cfg = Config(store_dir=tmp_path / "s", workspace_dir=tmp_path / "ws",
                 shell=True)
    asked: list[str] = []

    async def approver(command: str) -> bool:
        asked.append(command)
        return False

    reg = build_registry(cfg, approver)
    res = asyncio.run(reg.dispatch(
        "run_command", {"command": "git push --force origin main"}, _ctx()))
    assert res.ok is False and res.summary == "用户已拒绝"
    assert asked == ["git push --force origin main"]


def test_guard_absent_without_shell(tmp_path):
    cfg = Config(store_dir=tmp_path / "s", workspace_dir=tmp_path / "ws")
    reg = build_registry(cfg)
    assert "run_command" not in reg.names()


def test_interactive_approver_none_without_tty(monkeypatch):
    import sys as _sys

    class _FakeStdin:
        def isatty(self) -> bool:
            return False

    monkeypatch.setattr(_sys, "stdin", _FakeStdin())
    assert interactive_approver() is None
