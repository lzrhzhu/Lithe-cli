"""First-run endpoint setup: the wizard behind `lithe-cli config`.

The CLI still ships no default endpoint — that principle is untouched.
What changes is the failure mode: on an interactive terminal, a missing
endpoint means three prompts and a saved config file instead of a
refusal with a wall of export lines. Non-interactive contexts (pipes,
scripts, CI) never see the wizard; they keep the hard error.

Precedence stays flag > env > config file, so the wizard only fills what
nothing else provided. All prompting is prompt_toolkit (see
:mod:`lithe_cli.prompts`): inline URL validation, star-echoed API key,
paste-safe input.
"""

from __future__ import annotations

import os
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from . import prompts
from .config import (
    ENV_API_KEY,
    ENV_BASE_URL,
    ENV_MODEL,
    config_file,
    load_saved_endpoint,
    save_endpoint,
)
from .ui import BOLD, CYAN, GREEN, RED, YELLOW, ui


def stdin_is_interactive() -> bool:
    """True only when both ends are TTYs — pipes and captures stay clean."""
    return sys.stdin.isatty() and sys.stdout.isatty()


def _http_like(value: str) -> str | None:
    return (
        None
        if value.startswith(("http://", "https://"))
        else ("Base URL 需以 http:// 或 https:// 开头。")
    )


def probe_endpoint(base_url: str, api_key: str, timeout: float = 10.0) -> bool:
    """One cheap GET ``{base_url}/models`` to catch typos before chat.

    The save already happened: a failed probe warns, it never blocks —
    endpoints legitimately lack ``/models`` or auth differently.
    """
    url = base_url.rstrip("/") + "/models"
    req = Request(url, headers={"Authorization": f"Bearer {api_key}"})
    try:
        with urlopen(req, timeout=timeout) as resp:
            print(ui.s(f"✓ 端点可达（HTTP {resp.status}）", GREEN))
            return True
    except HTTPError as exc:
        if exc.code in (401, 403):
            print(ui.s(f"~ 端点可达但拒绝了该 key（HTTP {exc.code}）", YELLOW))
        else:
            print(ui.s(f"~ 端点有响应：HTTP {exc.code}（无 /models 也无妨）", YELLOW))
        return True  # reachable is all we wanted to know
    except (URLError, OSError, TimeoutError) as exc:
        print(ui.s(f"✗ 连不上 {url}：{exc}", RED))
        return False


def run_setup_wizard() -> dict | None:
    """Prompt for base_url / api_key / model and save the config file.

    Defaults come from the already-saved file, then env vars, so
    re-running the wizard is re-editing, not retyping. Returns the saved
    endpoint dict, or None when the user cancels.
    """
    if not stdin_is_interactive():
        print(ui.s("交互配置需要终端（stdin/stdout 不是 TTY）。", RED))
        return None
    saved = load_saved_endpoint()
    env = {
        "base_url": os.environ.get(ENV_BASE_URL, "").strip(),
        "api_key": os.environ.get(ENV_API_KEY, "").strip(),
        "model": os.environ.get(ENV_MODEL, "").strip(),
    }
    defaults = {k: saved.get(k, "") or env[k] for k in env}

    print(ui.s("初始设置：配置模型端点", CYAN, BOLD))
    print(
        f"值保存在 {config_file()}（权限 600）；"
        "优先级 flag > 环境变量 > 配置文件。\n"
        "之后可用 `lithe-cli config` 重新配置。"
    )
    base_url = prompts.ask(
        "Base URL（如 https://your-endpoint/api/v1）",
        defaults["base_url"],
        validate=_http_like,
    )
    api_key = prompts.ask("API key", defaults["api_key"], password=True)
    model = prompts.ask("模型名（如 glm-4.6）", defaults["model"])
    if not (base_url and api_key and model):
        print(ui.s("已取消，未保存任何配置。", YELLOW))
        return None

    path = save_endpoint(api_key, base_url, model)
    print(ui.s(f"✓ 已保存到 {path}", GREEN))
    if prompts.yes_no("现在测试一下连接吗", True):
        probe_endpoint(base_url, api_key)
    return {"api_key": api_key, "base_url": base_url, "model": model}
