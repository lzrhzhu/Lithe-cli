"""Configuration resolved from CLI flags, the environment, and a saved file.

Endpoint credentials follow the same convention as lithe's ``live_host``
example: ``LITHE_API_KEY`` / ``LITHE_BASE_URL`` / ``LITHE_MODEL``. There
is no default endpoint on purpose: the CLI refuses to run rather than
silently hitting some third-party URL. Resolution order is flag > env >
``$LITHE_HOME/config.json`` (written by the first-run wizard or
``lithe-cli config``; mode 0600 since it holds the key). On an
interactive terminal a missing endpoint triggers the wizard instead of
the refusal — see :mod:`lithe_cli.setup`.

Capability flags (all off unless asked for):

- ``--code``       sandboxed ``run_code`` / ``run_file`` tools (bwrap when
                   available, passthrough otherwise);
- ``--skills DIR`` a markdown skill library (``load_skill`` tool); defaults
                   to ``$LITHE_HOME/skills`` when that directory exists,
                   ``--skills ""`` disables it;
- ``--mcp SPEC``   external MCP servers (JSON array/object, or ``@file``);
                   env ``LITHE_MCP`` holds the same spec;
- ``--vision``     ``image_info`` + ``analyze_image`` (one vision call on
                   the main endpoint, memoized per file hash).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ENV_API_KEY = "LITHE_API_KEY"
ENV_BASE_URL = "LITHE_BASE_URL"
ENV_MODEL = "LITHE_MODEL"
ENV_HOME = "LITHE_HOME"
ENV_MCP = "LITHE_MCP"
ENV_PROFILE = "LITHE_PROFILE"
ENV_PROVIDER = "LITHE_PROVIDER"

DEFAULT_USER = "cli"


@dataclass
class Config:
    """Everything one CLI invocation needs (endpoint, store, workspace)."""

    api_key: str | None = None
    base_url: str | None = None
    model: str | None = None
    # Which saved profile the endpoint came from (display / doctor / TUI);
    # None when the endpoint is env/flag-only or nothing is saved.
    profile: str | None = None
    # Optional vendor preset name (profile field "provider" or LITHE_PROVIDER):
    # fills base_url when unset and contributes LLMConfig defaults (transport,
    # extra_body, ...) under the user's explicit values. See lithe.bundles.providers.
    provider: str | None = None
    store_dir: Path = field(default_factory=lambda: default_store_dir())
    workspace_dir: Path = field(default_factory=lambda: Path.cwd())
    user_id: str = DEFAULT_USER
    stream: bool = False
    max_steps: int = 35
    context_window: int | None = None
    timeout: float = 180.0
    attempts: int = 2
    download: bool = False
    verbose: bool = False
    code: bool = False
    shell: bool = False
    skills_dir: Path | None = None
    mcp_spec: str | None = None
    mcp_servers: list = field(default_factory=list)
    vision: bool = False
    color: bool | None = None
    # Keys fixed by flag/env this invocation: in-session profile switching
    # must not clobber them (CI pinning LITHE_MODEL stays pinned).
    pinned_keys: frozenset = frozenset()
    # Wire protocol ("chat" | "responses") or a custom LLMTransport instance.
    # Not exposed as a flag: the injection point for offline tests.
    transport: Any = "chat"
    # Reasoning intensity (None = send nothing; "off" normalizes to None at
    # load). Passed verbatim to the kernel's reasoning_effort — which values
    # the current model accepts is the endpoint's call.
    reasoning_effort: str | None = None

    @property
    def has_endpoint(self) -> bool:
        if not isinstance(self.transport, str):
            return True  # scripted/custom transport needs no endpoint
        return bool(self.api_key and self.base_url and self.model)


def lithe_home() -> Path:
    return Path(os.environ.get(ENV_HOME, "~/.lithe")).expanduser()


def default_store_dir() -> Path:
    return lithe_home() / "runs"


def default_skills_dir() -> Path:
    return lithe_home() / "skills"


def load_mcp_spec(raw: str) -> list:
    """Parse an MCP spec (raw JSON or ``@file``) into server configs.

    Raises SystemExit with a friendly message on a bad spec — config errors
    should stop the CLI before any run starts.
    """
    from lithe.bundles.mcp import parse_servers

    text = raw
    if raw.startswith("@"):
        path = Path(raw[1:]).expanduser()
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise SystemExit(f"读取 MCP 配置文件失败：{path}（{exc}）") from exc
    try:
        return parse_servers(text)
    except (ValueError, json.JSONDecodeError) as exc:
        raise SystemExit(
            f"MCP 配置解析失败：{exc}\n"
            "spec 应为 JSON 数组（stdio: command/env，http: url/headers），"
            '例如：--mcp \'[{"name":"fs","command":["npx","-y",'
            '"@modelcontextprotocol/server-filesystem","."]}]\''
        ) from exc


_ENDPOINT_KEYS = ("api_key", "base_url", "model")


def config_file() -> Path:
    """Where the wizard-saved profiles live (see :mod:`lithe_cli.profiles`)."""
    return lithe_home() / "config.json"


def load_saved_endpoint() -> dict:
    """Read the active profile's {api_key, base_url, model, provider}; {} if none.

    A corrupt file is treated as absent — a bad edit should cost the user
    one re-prompt, not a broken CLI.
    """
    from .profiles import ProfileStore

    endpoint = ProfileStore().endpoint()
    keys = (*_ENDPOINT_KEYS, "provider")
    return {k: endpoint[k] for k in keys if endpoint.get(k)}


def _restrict_to_owner(path: Path, directory: bool = False) -> None:
    if os.name != "nt":
        try:
            path.chmod(0o700 if directory else 0o600)
        except OSError:
            pass
        return
    icacls = shutil.which("icacls")
    if not icacls:
        raise OSError("icacls is required to protect the saved API key on Windows")
    account = os.environ.get("USERDOMAIN")
    username = os.environ.get("USERNAME")
    owner = f"{account}\\{username}" if account and username else username
    if not owner:
        raise OSError("unable to determine current Windows account for API key ACL")
    permissions = f"{owner}:(OI)(CI)(F)" if directory else f"{owner}:(F)"
    try:
        reset = subprocess.run(
            [icacls, str(path), "/reset"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=10,
        )
        if reset.returncode:
            raise OSError(f"failed to reset access to {path}")
        result = subprocess.run(
            [icacls, str(path), "/inheritance:r", "/grant:r", permissions],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise OSError(f"failed to restrict access to {path}") from exc
    if result.returncode:
        raise OSError(f"failed to restrict access to {path}")


def save_endpoint(api_key: str, base_url: str, model: str) -> Path:
    """Write the endpoint into the active profile (creating ``default`` when
    no profile exists), preserving every other saved profile."""
    from .profiles import DEFAULT_PROFILE, ProfileStore

    store = ProfileStore()
    name = store.active_name() or DEFAULT_PROFILE
    return store.upsert(name, base_url, api_key, model)


def load_config(args: Any) -> Config:
    """Merge parsed argparse flags over environment defaults."""

    def g(name: str, default):
        return getattr(args, name, default)

    store_dir = (
        Path(g("store", None)).expanduser() if g("store", None) else default_store_dir()
    )
    workspace_dir = (
        Path(g("workspace", None)).expanduser() if g("workspace", None) else Path.cwd()
    )
    skills = g("skills", None)
    if skills is not None and skills.strip():
        skills_dir: Path | None = Path(skills).expanduser()
    elif skills is not None:  # explicit "" → disabled
        skills_dir = None
    else:  # auto: $LITHE_HOME/skills when it exists
        cand = default_skills_dir()
        skills_dir = cand if cand.is_dir() else None

    mcp_raw = g("mcp", None) or os.environ.get(ENV_MCP)
    # Endpoint layer: flag > env > selected profile's fields, per key.
    # --profile / LITHE_PROFILE pick the profile (unknown name → hard exit).
    from .profiles import ProfileStore

    selected = g("profile", None) or os.environ.get(ENV_PROFILE)
    store = ProfileStore()
    saved = store.endpoint(selected) if (selected or store.names()) else {}
    active = store.active_name(selected) if selected else store.active_name()
    pinned = frozenset(
        k
        for k, flag, env in (
            ("api_key", g("api_key", None), ENV_API_KEY),
            ("base_url", g("base_url", None), ENV_BASE_URL),
            ("model", g("model", None), ENV_MODEL),
        )
        if flag or os.environ.get(env)
    )
    # Vendor preset (profile "provider" field > LITHE_PROVIDER): fills
    # base_url when nothing explicit set it — the preset's whole point —
    # while every explicit value stays untouched. An unknown name is a
    # config typo: die loudly like an unknown --profile, listing options.
    provider = (os.environ.get(ENV_PROVIDER) or saved.get("provider")
                or g("provider", None))
    base_url = (
        g("base_url", None) or os.environ.get(ENV_BASE_URL)
        or saved.get("base_url")
    )
    if provider and not base_url:
        try:
            from lithe.bundles.providers import get_preset

            base_url = get_preset(provider).get("base_url")
        except ValueError as exc:
            # get_preset's message already lists the known providers.
            raise SystemExit(
                f"{exc}。可手写 base_url，或从上述 preset 中选择。"
            ) from exc
        if not base_url:
            raise SystemExit(
                f"provider {provider!r} 的 preset 未提供 base_url；"
                f"请在档案中手写 base_url。"
            )
    # Reasoning effort: flag > saved profile field; "off" normalizes to None
    # (send nothing). An in-session /reasoning overrides both.
    effort = g("reasoning_effort", None) or saved.get("reasoning_effort")
    if effort is not None and effort.strip().lower() == "off":
        effort = None
    return Config(
        api_key=(
            g("api_key", None) or os.environ.get(ENV_API_KEY) or saved.get("api_key")
        ),
        base_url=base_url,
        model=(g("model", None) or os.environ.get(ENV_MODEL) or saved.get("model")),
        profile=active,
        provider=provider,
        reasoning_effort=(effort.strip() if isinstance(effort, str) and effort.strip()
                          else None),
        store_dir=store_dir,
        workspace_dir=workspace_dir,
        user_id=g("user", None) or DEFAULT_USER,
        stream=g("stream", False),
        max_steps=g("max_steps", 35),
        context_window=g("context_window", None) or saved.get("context_window"),
        timeout=g("timeout", 180.0),
        attempts=g("attempts", 2),
        download=g("download", False),
        verbose=g("verbose", False),
        code=g("code", False),
        shell=g("shell", False),
        skills_dir=skills_dir,
        mcp_spec=mcp_raw,
        mcp_servers=load_mcp_spec(mcp_raw) if mcp_raw else [],
        vision=g("vision", False),
        color=(True if g("color", False) else False if g("no_color", False) else None),
        pinned_keys=pinned,
    )


def require_endpoint(cfg: Config, interactive: bool = False) -> None:
    """Ensure an endpoint, launching the first-run wizard when it helps.

    ``interactive`` asks for the wizard; it still only runs on a TTY (both
    ends), so pipes, captures and CI keep the plain SystemExit.
    """
    if cfg.has_endpoint:
        return
    if interactive:
        from .setup import run_setup_wizard, stdin_is_interactive

        if stdin_is_interactive():
            saved = run_setup_wizard() or {}
            cfg.api_key = cfg.api_key or saved.get("api_key")
            cfg.base_url = cfg.base_url or saved.get("base_url")
            cfg.model = cfg.model or saved.get("model")
            if cfg.has_endpoint:
                return
    raise SystemExit(
        "缺少模型端点配置。两种方式任选：\n"
        "  1) 交互式（推荐）：运行 lithe-cli config，按提示输入后保存到\n"
        f"     {config_file()}\n"
        "     可保存多个档案（provider），用 lithe-cli config --use 切换。\n"
        "  2) 环境变量（或对应flag）：\n"
        f"     export {ENV_API_KEY}=sk-...\n"
        f"     export {ENV_BASE_URL}=https://your-endpoint/api/v1\n"
        f"     export {ENV_MODEL}=your-model\n"
        "本 CLI 不内置任何默认端点。"
    )
