"""Offline test fixtures: a scripted LLM transport, after minimal_host.py."""

from __future__ import annotations

import json
from pathlib import Path

from lithe_cli.config import Config


def _tc(name: str, args: dict, cid: str) -> dict:
    return {
        "id": cid,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


def scripted_transport(responses: list[dict]):
    """A tiny LLMTransport replaying canned model turns (tool calls, answers)."""
    it = iter(responses)

    class _Scripted:
        async def complete(self, client, **kw):
            r = next(it)
            return {
                "content": r.get("content", ""),
                "tool_calls": r.get("tool_calls", []),
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 20,
                    "total_tokens": 120,
                },
            }

    return _Scripted()


def make_config(tmp_path: Path, responses: list[dict]) -> Config:
    """A Config bound to temp dirs, driven by the scripted transport."""
    return Config(
        store_dir=tmp_path / "store",
        workspace_dir=tmp_path / "ws",
        transport=scripted_transport(responses),
    )
