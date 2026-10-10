"""First-run endpoint setup: the wizard behind `lithe-cli config`.

The CLI still ships no default endpoint — that principle is untouched.
What changes is the failure mode: on an interactive terminal, a missing
endpoint means a short interview and a saved config file instead of a
refusal with a wall of export lines. Non-interactive contexts (pipes,
scripts, CI) never see the wizard; they keep the hard error.

The interview is provider-first (the flat-triple era predates presets):
it asks which vendor family the endpoint belongs to, then defaults
``base_url`` to that preset's official URL — editable, because a
self-built router speaking a provider's format keeps its own URL, key
and model. Precedence stays flag > env > config file, so the wizard
only fills what nothing else provided. All prompting is prompt_toolkit
(see :mod:`lithe_cli.prompts`): inline URL validation, star-echoed API
key, paste-safe input, Tab-completed provider names.
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
    ENV_PROVIDER,
    config_file,
    save_endpoint,
)
from .profiles import DEFAULT_PROFILE, ProfileStore, models_request, valid_name
from .ui import BOLD, CYAN, GREEN, RED, YELLOW, ui

# The wizard's "no preset, hand-written endpoint" sentinel choice.
NO_PROVIDER = "none"


def stdin_is_interactive() -> bool:
    """True only when both ends are TTYs — pipes and captures stay clean."""
    return sys.stdin.isatty() and sys.stdout.isatty()


def _http_like(value: str) -> str | None:
    return (
        None
        if value.startswith(("http://", "https://"))
        else ("Base URL 需以 http:// 或 https:// 开头。")
    )


def _provider_choices() -> list[str]:
    try:
        from lithe.bundles.providers import known_providers
    except ImportError:
        return [NO_PROVIDER]
    return [*known_providers(), NO_PROVIDER]


def _preset(provider: str) -> dict:
    """The preset dict for *provider* ({} for the no-preset sentinel)."""
    if provider == NO_PROVIDER:
        return {}
    try:
        from lithe.bundles.providers import get_preset
    except ImportError:
        return {}
    try:
        return get_preset(provider)
    except ValueError:
        return {}


def probe_endpoint(base_url: str, api_key: str, provider: str | None = None,
                   timeout: float = 10.0) -> bool:
    """One cheap protocol-aware GET ``{base_url}/models`` to catch typos.

    The save already happened: a failed probe warns, it never blocks —
    endpoints legitimately lack ``/models`` or auth differently. A
    ``messages``-transport provider (anthropic) probes with its own
    ``x-api-key`` headers via :func:`lithe_cli.profiles.models_request`.
    """
    request = models_request({"base_url": base_url, "api_key": api_key,
                              "provider": provider})
    if request is None:
        return False
    url, headers = request
    req = Request(url, headers=headers)
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
    """Provider-first endpoint interview, saved into config.json.

    Asks profile name → provider type (Tab-completed presets, ``none``
    keeps a hand-written endpoint) → base_url (defaults to the preset's
    official URL; override it to point the provider's format at your own
    router) → API key → model. Defaults come from the profile being
    edited, then env vars, so re-running the wizard is re-editing, not
    retyping. Returns the saved endpoint dict, or None on cancel.
    """
    if not stdin_is_interactive():
        print(ui.s("交互配置需要终端（stdin/stdout 不是 TTY）。", RED))
        return None
    store = ProfileStore()
    data = store.load()

    print(ui.s("初始设置：配置模型端点", CYAN, BOLD))
    print(
        f"值保存在 {config_file()}（权限 600）；"
        "优先级 flag > 环境变量 > 配置文件。\n"
        "之后可用 `lithe config` 重新配置，`lithe config --list` 查看。"
    )

    name = prompts.ask(
        "档案名（一个 profile，可保存多个端点）",
        data["active"] or DEFAULT_PROFILE,
        validate=lambda v: None if valid_name(v)
        else "档案名仅限字母、数字、点、下划线、连字符。",
    )
    if not name:
        print(ui.s("已取消，未保存任何配置。", YELLOW))
        return None
    existing = data["profiles"].get(name, {})

    env_provider = os.environ.get(ENV_PROVIDER, "").strip().lower()
    default_provider = (existing.get("provider") or env_provider
                        or NO_PROVIDER)
    choices = _provider_choices()
    if default_provider not in choices:
        choices.append(default_provider)  # unknown-but-saved: still editable
    provider = prompts.choose(
        "Provider 类型（定协议/方言；自建路由选厂商后改 base_url）",
        choices, default=default_provider)
    if provider is None:
        print(ui.s("已取消，未保存任何配置。", YELLOW))
        return None
    preset = _preset(provider)
    if preset.get("notes"):
        print(ui.s(f"  · {preset['notes']}", YELLOW))

    env = {
        "base_url": os.environ.get(ENV_BASE_URL, "").strip(),
        "api_key": os.environ.get(ENV_API_KEY, "").strip(),
        "model": os.environ.get(ENV_MODEL, "").strip(),
    }
    base_url = prompts.ask(
        "Base URL（回车用官方端点；自建路由填自己的）",
        existing.get("base_url") or env["base_url"]
        or preset.get("base_url", ""),
        validate=_http_like,
    )
    api_key = prompts.ask(
        "API key", existing.get("api_key") or env["api_key"], password=True)
    model = prompts.ask(
        "模型名（如 glm-4.6 / claude-sonnet-4）",
        existing.get("model") or env["model"])
    if not (base_url and api_key and model):
        print(ui.s("已取消，未保存任何配置。", YELLOW))
        return None

    path = save_endpoint(
        api_key, base_url, model,
        provider=None if provider == NO_PROVIDER else provider,
        name=name)
    if provider == NO_PROVIDER and existing.get("provider"):
        store.set_provider(name, None)  # explicit demotion to hand-written
    print(ui.s(f"✓ 档案 {name} 已保存到 {path}", GREEN))
    if prompts.yes_no("现在测试一下连接吗", True):
        probe_endpoint(base_url, api_key,
                       None if provider == NO_PROVIDER else provider)
    result = {"api_key": api_key, "base_url": base_url, "model": model}
    if provider != NO_PROVIDER:
        result["provider"] = provider
    return result
