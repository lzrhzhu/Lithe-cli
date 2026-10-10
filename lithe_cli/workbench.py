"""The Workbench: one orchestration object behind both frontends.

Owns the mutable endpoint view (``cfg``), the profile store, the session
manager and the running turns. Frontends (the plain REPL and the TUI) never
touch the run store or the kernel directly — they subscribe to events, call
:meth:`dispatch` for every input line, and render what comes back.

Turn model: one turn at a time per session; different sessions may run in
parallel. Switching away from a running session does *not* cancel it — the
task keeps writing into the store, ``busy_ids`` keeps its badge lit, and the
frontend replays the missed feed on switch-back. Model/profile switches edit
the shared ``cfg``; the next turn's ``build_llm`` snapshot picks them up
(turns already running keep their endpoint, like every mainstream CLI).

Event fan-out: subscribers receive ``(conv_id, event)``; kernel events pass
through untouched, plus two synthetic events ``session_busy`` /
``session_idle`` bracketing each turn.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from lithe import replay_messages
from lithe.bundles import JsonlRunStore

from .agent import execute, tool_names, undo
from .commands import ActionResult, help_text
from .config import Config
from .profiles import ProfileStore, fetch_models_for
from .sessions import SessionManager, auto_title, format_session_rows
from .tui import COPY_SCOPE_HELP, parse_copy_scope

# Session-adjustable settings for /set: (user key, cfg attr, label, kind).
# Order is the /set display order. Only per-turn assembly inputs belong
# here — workspace/store/user stay startup-level (session identity), and
# --mcp is process-scoped. `kind` drives parsing: bool accepts
# on/off/开/关 (bare invocation toggles); int/float require a value.
_SETTING_DEFS: list[tuple[str, str, str, str]] = [
    ("shell", "shell", "run_command（宿主 shell，不可撤销）", "bool"),
    ("code", "code", "run_code / run_file（沙箱 Python）", "bool"),
    ("vision", "vision", "image_info / analyze_image（图像理解）", "bool"),
    ("document", "document", "document_info / analyze_document（文档理解）", "bool"),
    ("download", "download", "download_file（网络下载）", "bool"),
    ("subagents", "subagents", "delegate / delegate_parallel（子代理委派）", "bool"),
    ("stream", "stream", "流式输出 token", "bool"),
    ("verbose", "verbose", "显示 usage / reasoning 事件", "bool"),
    ("max-steps", "max_steps", "工具循环步数上限", "int"),
    ("timeout", "timeout", "单次模型调用超时（秒）", "float"),
    ("attempts", "attempts", "模型调用重试次数", "int"),
    ("sleep-429", "sleep_429", "429 重试退避基数（秒）", "float"),
    ("sleep-err", "sleep_err", "网络/5xx 重试退避基数（秒）", "float"),
    ("temperature", "temperature", "采样温度（off = 端点默认）", "float"),
    ("max-output-tokens", "max_tokens", "单次调用输出上限（off = 端点默认）", "int"),
    ("max-cost", "max_cost", "单轮成本预算 $（off = 不限）", "float"),
    ("max-tokens", "max_total_tokens", "单轮 token 预算（off = 不限）", "int"),
    ("reasoning-effort", "reasoning_effort",
     "推理强度（off/minimal/low/medium/high，原样透传）", "enum"),
]
# Settings whose change alters the next turn's tool registry (vs. sampling
# or rendering knobs): flipping them invalidates the /tools cache.
_TOOL_AFFECTING = {"shell", "code", "vision", "document", "download",
                   "subagents"}
# Numeric knobs that accept "off"/"none" to clear back to None (unset),
# and (for temperature) a zero value.
_NULLABLE = {"temperature", "max-output-tokens", "max-cost", "max-tokens"}
_TRUTHY = {"on", "true", "1", "开"}
_FALSY = {"off", "false", "0", "关"}
# Reasoning levels offered by /reasoning; any other typed token passes
# through verbatim (rare per-model values like "none" stay reachable).
REASONING_LEVELS = ("off", "minimal", "low", "medium", "high")


class Workbench:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.profiles = ProfileStore()
        self.store = JsonlRunStore(cfg.store_dir)
        self.sessions = SessionManager(self.store, cfg.user_id)
        self.current: dict | None = None
        # Human-confirmation channel for destructive run_command calls
        # (kernel guard). Line-mode front-ends install a stdin y/n approver;
        # the Textual TUI installs a modal one. None → deny with guidance.
        self.approver = None
        self.turns: dict[int, dict] = {}
        self.subscribers: list[Callable[[int, dict], None]] = []
        self._tool_names: list[str] | None = None

    # -- events ---------------------------------------------------------------

    def subscribe(self, fn: Callable[[int, dict], None]) -> None:
        self.subscribers.append(fn)

    def _emit(self, conv_id: int | None, ev: dict) -> None:
        for fn in self.subscribers:
            try:
                fn(conv_id, ev)
            except Exception:  # noqa: BLE001 — a broken UI must not kill turns
                pass

    def busy_ids(self) -> set[int]:
        return set(self.turns)

    def busy(self, conv_id: int | None = None) -> bool:
        cid = self._cid(conv_id)
        if cid is None:
            return bool(self.turns)
        return cid in self.turns

    def _cid(self, conv_id: int | None = None) -> int | None:
        if conv_id is not None:
            return conv_id
        return self.current["id"] if self.current else None

    # -- session lifecycle ------------------------------------------------------

    def _meta(self) -> dict:
        return {
            "workspace": str(self.cfg.workspace_dir.resolve()),
            "profile": self.cfg.profile,
            "model": self.cfg.model,
        }

    def new_session(self, title: str | None = None) -> dict:
        self.current = self.sessions.new(title=title, meta=self._meta())
        return self.current

    def open(
        self,
        *,
        resume: str | None = None,
        continue_latest: bool = False,
        title: str | None = None,
    ) -> ActionResult:
        """Open the starting session (CLI entry); errors are SystemExit."""
        result = ActionResult()
        if resume is not None:
            row, err = self.sessions.resolve_exact(resume)
            if row is None:
                raise SystemExit(err)
            self.current = row
            self.sync_session_pin()
            result.say("dim", f"已恢复会话 #{row['id']} {row.get('title') or ''}")
        elif continue_latest:
            row = self.sessions.latest()
            if row is None:
                self.new_session(title)
                result.say("dim", "没有历史会话，已开始新会话")
            else:
                self.current = row
                self.sync_session_pin()
                result.say(
                    "dim",
                    f"已继续最近会话 #{row['id']} {row.get('title') or ''}",
                )
        else:
            self.new_session(title)
        return result

    def _apply_endpoint(self, endpoint: dict) -> None:
        """Adopt a stored profile's fields onto ``cfg`` (endpoint truth).

        The pinnable endpoint keys (api_key / base_url / model / provider)
        respect ``pinned_keys``; everything else the profile carries
        (dialect knobs, extra_body, pricing, ...) is copied verbatim and
        reset to None when the profile doesn't set it — switching profiles
        means switching truths, not layering one vendor's fields over
        another's endpoint. A profile without a model keeps the current
        one (upsert always saves one, but hand-edited files may not).
        """
        pinned = self.cfg.pinned_keys
        if "api_key" not in pinned:
            self.cfg.api_key = endpoint.get("api_key")
        if "base_url" not in pinned:
            base = endpoint.get("base_url")
            if not base and endpoint.get("provider"):
                # a preset-only profile stores an empty base_url; the
                # preset's official URL fills it at adopt time, matching
                # load_config's layering (switching must not blank the
                # endpoint the way a raw copy would).
                try:
                    from lithe.bundles.providers import get_preset
                except ImportError:
                    get_preset = None
                if get_preset is not None:
                    try:
                        base = get_preset(endpoint["provider"]).get("base_url")
                    except ValueError:
                        base = None
            self.cfg.base_url = base
        if endpoint.get("model") and "model" not in pinned:
            self.cfg.model = endpoint["model"]
        if "provider" not in pinned:
            self.cfg.provider = endpoint.get("provider")
        self.cfg.document_format = endpoint.get("document_format")
        self.cfg.reasoning_effort = endpoint.get("reasoning_effort")
        self.cfg.context_window = endpoint.get("context_window")
        self.cfg.temperature = endpoint.get("temperature")
        self.cfg.max_tokens = endpoint.get("max_tokens")
        self.cfg.extra_body = endpoint.get("extra_body")
        self.cfg.default_headers = endpoint.get("default_headers")
        self.cfg.pricing = endpoint.get("pricing")

    def sync_session_pin(self) -> None:
        """A resumed session remembers its model/profile unless pinned.

        The session's own model pin (last model used in it) wins over the
        profile's default — switching model inside a session sticks across
        resumes even if the profile default says otherwise.
        """
        if self.current is None:
            return
        meta = self.current.get("meta") or {}
        pinned = self.cfg.pinned_keys
        if meta.get("profile") and "profile" not in pinned:
            endpoint = self.profiles.endpoint(meta["profile"])  # may be gone
            if endpoint:
                self._apply_endpoint(endpoint)
                self.cfg.profile = meta["profile"]
        if meta.get("model") and "model" not in pinned:
            self.cfg.model = meta["model"]

    def switch_session(self, ident: str) -> ActionResult:
        result = ActionResult(changed=True)
        row, err = self.sessions.resolve_exact(ident)
        if row is None:
            result.say("err", err)
            return result
        if self.current and row["id"] == self.current["id"]:
            result.say("dim", f"已在会话 #{row['id']} 中")
            return result
        self.current = row
        self.sync_session_pin()
        result.say(
            "dim",
            f"已切换到会话 #{row['id']} {row.get('title') or ''}"
            f"（{self.cfg.profile or 'env'} · {self.cfg.model}）",
        )
        result.overlay = "rebuild"  # TUI rebuilds the conversation pane
        return result

    def rename_session(self, title: str) -> ActionResult:
        result = ActionResult()
        if self.current is None:
            result.say("err", "当前没有会话")
            return result
        self.sessions.rename(self.current["id"], title)
        self.current["title"] = title
        result.say("dim", f"已重命名会话 #{self.current['id']} → {title}")
        result.changed = True
        return result

    def delete_session(self, ident: str) -> ActionResult:
        result = ActionResult()
        row, err = self.sessions.resolve_exact(ident)
        if row is None:
            result.say("err", err)
            return result
        self.sessions.delete(row["id"])
        if self.current and self.current["id"] == row["id"]:
            self.current = None
        result.say("dim", f"已删除会话 #{row['id']}（runs 保留，可 lithe log 查看）")
        result.changed = True
        return result

    def session_list(self) -> list[dict]:
        return self.sessions.list()

    # -- model / profile --------------------------------------------------------

    def _endpoint_dialect(self, endpoint: dict) -> str:
        """``"zai · chat"``-style label for listings; "" when unknown.

        Provider comes from the profile field; a profile without one gets a
        display-only inference from matching a preset's base_url — shown,
        never written back (an inference is not truth).
        """
        provider, transport = endpoint.get("provider"), None
        try:
            from lithe.bundles.providers import PRESETS, get_preset
        except ImportError:
            return ""
        if provider:
            try:
                transport = get_preset(provider).get("transport")
            except ValueError:
                return f"{provider}（未知 preset）"
        else:
            for name, preset in PRESETS.items():
                if preset.get("base_url") and \
                        preset["base_url"] == endpoint.get("base_url"):
                    provider, transport = name, preset.get("transport")
                    break
        if not provider:
            return ""
        return f"{provider} · {transport}" if transport else provider

    def recent_models(self, limit: int = 5) -> list[tuple[str, str]]:
        """Distinct ``(profile, model)`` pairs by session recency.

        Derived from conversation meta on the fly — zero sidecar state; the
        models a user actually switched through are exactly what they want
        one keystroke away.
        """
        seen: set[tuple[str, str]] = set()
        out: list[tuple[str, str]] = []
        for row in self.sessions.list(limit=50):
            meta = row.get("meta") or {}
            pair = (meta.get("profile"), meta.get("model"))
            if pair[0] and pair[1] and pair not in seen:
                seen.add(pair)
                out.append(pair)
        return out[:limit]

    def model_entries(self, *, fav_only: bool = False) -> list[dict]:
        """Ordered picker entries across every saved profile.

        Sections: ★常用 (favorites, qualified) → 最近 (recents not already
        favorited) → one group per profile (current first, its saved model
        + cached list). Each entry carries ``{profile, model, section,
        group, favorite}`` — ``section`` is the section kind
        (``fav``/``recent``/``profile``), ``group`` the header it renders
        under (profile name). Numbering follows this order, so the
        favorites are always the first few numbers — the whole point of
        the quick lane.
        """
        favorites = set(self.profiles.favorites())
        entries: list[dict] = []
        listed: set[tuple[str, str]] = set()

        def _add(profile: str, model: str, section: str) -> None:
            pair = (profile, model)
            if pair in listed:
                return
            listed.add(pair)
            entries.append({"profile": profile, "model": model,
                            "section": section, "group": profile,
                            "favorite": f"{profile}:{model}" in favorites})

        for ref in favorites:
            profile, _, model = ref.partition(":")
            _add(profile, model, "fav")
        if fav_only:
            return entries
        current_pair = (self.cfg.profile, self.cfg.model)
        for pair in self.recent_models():
            if f"{pair[0]}:{pair[1]}" in favorites:
                continue
            if pair == current_pair:
                # the current model renders under its own profile group (●);
                # duplicating it in 最近 would also swallow that group's
                # only entry — and with it the group header.
                continue
            _add(pair[0], pair[1], "recent")
        current = self.cfg.profile
        names = [current] if current else []
        names += [n for n in self.profiles.names() if n != current]
        for name in names:
            try:
                ep = self.profiles.endpoint(name)
            except SystemExit:
                continue
            if ep.get("model"):
                _add(name, ep["model"], "profile")
            for m in ep.get("cached_models") or []:
                _add(name, m, "profile")
        return entries

    def set_model(self, arg: str, *, save: bool = False) -> ActionResult:
        result = ActionResult(changed=True)
        if self.cfg.pinned_keys and "model" in self.cfg.pinned_keys:
            result.say("warn", "模型由 flag/环境变量钉住，本次运行不切换")
            return result
        name = arg.strip()
        # Qualified ref "profile:model": profile names cannot contain ":"
        # (model ids can — OpenRouter's "vendor/model"), so the first colon
        # splits unambiguously. An unknown prefix falls through as a bare
        # model name (custom endpoints may use colon-y ids).
        if name and ":" in name:
            prefix, _, rest = name.partition(":")
            if prefix in self.profiles.names() and rest:
                switch = self.set_profile(prefix)
                if switch.messages and switch.messages[0][0] == "err":
                    return switch
                result.messages.extend(switch.messages)
                name = rest
        if name.isdigit():
            idx = int(name) - 1
            entries = self.model_entries()
            if 0 <= idx < len(entries):
                entry = entries[idx]
                if entry["profile"] != self.cfg.profile:
                    switch = self.set_profile(entry["profile"])
                    result.messages.extend(switch.messages)
                name = entry["model"]
            else:
                result.say("err", f"序号超出范围（1–{len(entries)}）")
                return result
        if not name:
            result.say("err", "缺少模型名")
            return result
        self.cfg.model = name
        if save and self.cfg.profile:
            self.profiles.set_model(self.cfg.profile, name)
            result.say("ok", f"模型已切换为 {name}（已存为档案 {self.cfg.profile} 默认）")
        else:
            result.say("ok", f"模型已切换为 {name}，下一轮生效")
            if self.cfg.profile:
                result.say("dim", "（加 --save 可同时存为档案默认）")
        if self.current is not None:
            self.sessions.set_meta(self.current["id"], {"model": name})
        return result

    def set_reasoning(self, arg: str, *, save: bool = False) -> ActionResult:
        """``/reasoning`` — view / set the reasoning-effort knob.

        ``arg`` empty → listing + picker overlay; otherwise a level (or any
        verbatim token); ``off`` clears. ``--save`` persists to the profile.
        """
        result = ActionResult()
        arg = arg.strip()
        if not arg:
            current = self.cfg.reasoning_effort or "off"
            result.say("dim", f"当前推理强度：{current}")
            for i, level in enumerate(REASONING_LEVELS, 1):
                mark = "●" if level == current else " "
                result.say("dim", f" {mark} {i}. {level}")
            result.say("dim", "（/reasoning 级别 或 /reasoning 序号；off 表示不发送"
                              "该字段；--save 存为档案默认）")
            result.overlay = "reasoning"
            return result
        if arg.isdigit():
            idx = int(arg) - 1
            if not 0 <= idx < len(REASONING_LEVELS):
                result.say("err", f"序号超出范围（1–{len(REASONING_LEVELS)}）")
                return result
            arg = REASONING_LEVELS[idx]
        r = self.set_setting("reasoning-effort", arg)
        result.messages.extend(r.messages)
        if r.messages and r.messages[0][0] == "ok" and save:
            if self.cfg.profile:
                self.profiles.set_reasoning_effort(
                    self.cfg.profile, self.cfg.reasoning_effort)
                result.say("ok", f"（已存为档案 {self.cfg.profile} 默认）")
            else:
                result.say("dim", "（没有已保存档案，--save 未生效）")
        return result

    def set_profile(self, name: str) -> ActionResult:
        result = ActionResult(changed=True)
        try:
            endpoint = self.profiles.endpoint(name)
        except SystemExit as exc:
            result.say("err", str(exc.code))
            return result
        # Full endpoint truth: credentials + provider/dialect fields switch
        # together — swapping base_url while keeping the old provider's
        # preset would layer one vendor's transport over another's endpoint.
        self._apply_endpoint(endpoint)
        self.cfg.profile = name
        if self.current is not None:
            self.sessions.set_meta(
                self.current["id"], {"profile": name, "model": self.cfg.model}
            )
        dialect = self._endpoint_dialect(endpoint)
        result.say(
            "ok", f"档案已切换为 {name}{f'（{dialect}）' if dialect else ''}"
            f" · {self.cfg.model}，下一轮生效"
        )
        return result

    def save_profile(self, name: str, provider: str, base_url: str,
                     api_key: str, model: str) -> ActionResult:
        """Create or overwrite a stored profile from form values (F7 表单).

        A *new* profile is switched to immediately — creating an endpoint
        in-session means using it now. Overwriting the *current* profile
        re-adopts its fields so provider/base_url edits take effect on
        the next turn; other profiles are saved without switching.
        ``provider == "none"`` (the form's sentinel) stores no preset and
        explicitly clears one the profile previously had.
        """
        result = ActionResult(changed=True)
        clean = (name or "").strip()
        provider = (provider or "").strip()
        provider = "" if provider.lower() in ("", "none", "off") else provider
        if not clean:
            result.say("err", "档案名不能为空")
            return result
        if not ((base_url or "").strip() and (api_key or "").strip()
                and (model or "").strip()):
            result.say("err", "base_url / API key / 模型 均不能为空")
            return result
        existed = clean in self.profiles.names()
        had_provider = None
        if existed:
            try:
                had_provider = self.profiles.endpoint(clean).get("provider")
            except SystemExit:
                had_provider = None
        try:
            self.profiles.upsert(clean, base_url.strip(), api_key.strip(),
                                 model.strip(),
                                 provider=provider or None)
        except SystemExit as exc:
            result.say("err", str(exc.code))
            return result
        if not provider and had_provider:
            try:
                self.profiles.set_provider(clean, None)
            except ValueError:
                pass
        if not existed:
            switch = self.set_profile(clean)
            result.messages.extend(switch.messages)
            return result
        if clean == self.cfg.profile:
            try:
                self._apply_endpoint(self.profiles.endpoint(clean))
            except SystemExit:
                pass
            dialect = self._endpoint_dialect(
                {"provider": provider or None,
                 "base_url": base_url.strip()})
            result.say("ok", f"档案 {clean} 已更新"
                       f"{'（' + dialect + '）' if dialect else ''}，下一轮生效")
        else:
            result.say("ok", f"档案 {clean} 已保存（Enter 可切换）")
        return result

    def change_profile_provider(self, name: str, provider: str) -> ActionResult:
        """Swap a stored profile's provider preset (F7 › p).

        ``none``/``off``/empty clears it. When the profile is the current
        one, its fields are re-adopted so the transport/dialect change
        applies to the next turn.
        """
        result = ActionResult(changed=True)
        if name not in self.profiles.names():
            result.say("err", f"未知档案 {name}")
            return result
        try:
            self.profiles.set_provider(name, provider)
        except ValueError as exc:
            result.say("err", str(exc))
            return result
        raw = (provider or "").strip()
        if raw.lower() in ("", "none", "off"):
            result.say("ok", f"档案 {name} 已清除 provider（手写端点）")
        else:
            result.say("ok", f"档案 {name} provider → {raw.lower()}")
        if name == self.cfg.profile:
            try:
                self._apply_endpoint(self.profiles.endpoint(name))
            except SystemExit:
                pass
            result.say("dim", "下一轮生效")
        return result

    async def fetch_model_list(self, scope: str = "current") -> ActionResult:
        """Fetch ``/models`` per protocol (see ``fetch_models_for``).

        ``current`` hits the effective endpoint; ``all`` fans out to every
        saved profile in parallel, caching each — the collision-safe
        cross-provider view: every model is displayed (and favoritable)
        as a qualified ``profile:model`` ref.
        """
        result = ActionResult(changed=True)
        if scope == "all":
            names = self.profiles.names()
            if not names:
                result.say("warn", "没有已保存档案（lithe-cli config 配置）")
                return result
            pairs = []
            for name in names:
                try:
                    ep = self.profiles.endpoint(name)
                except SystemExit:
                    continue
                pairs.append((name, ep))
            # current profile first, the rest in saved order — the gather
            # list and the report loop must walk the same sequence.
            current = self.cfg.profile
            pairs.sort(key=lambda ne: ne[0] != current)
            fetched = await asyncio.gather(*[
                asyncio.to_thread(fetch_models_for, ep) for _name, ep in pairs
            ])
            for (name, _ep), models in zip(pairs, fetched, strict=True):
                if not models:
                    result.say("warn", f"✗ {name} 拉取失败或为空")
                    continue
                self.profiles.cache_models(name, models)
                preview = "、".join(f"{name}:{m}" for m in models[:6])
                more = f" …（共 {len(models)} 个）" if len(models) > 6 else ""
                result.say("ok", f"{name}：{preview}{more}")
            return result
        base_url, api_key = self.cfg.base_url, self.cfg.api_key
        provider = self.cfg.provider
        if not (base_url and api_key):
            result.say("err", "当前没有可用的 base_url/api_key")
            return result
        endpoint = {"base_url": base_url, "api_key": api_key,
                    "provider": provider}
        models = await asyncio.to_thread(fetch_models_for, endpoint)
        if not models:
            result.say("warn", f"拉取失败或为空（{base_url}/models）")
            return result
        if self.cfg.profile:
            self.profiles.cache_models(self.cfg.profile, models)
        preview = "、".join(models[:8]) + ("…" if len(models) > 8 else "")
        result.say("ok", f"端点返回 {len(models)} 个模型：{preview}")
        return result

    # -- turns ------------------------------------------------------------------

    def history_rows(self, conv_id: int) -> list[dict]:
        return self.sessions.history(conv_id)

    async def _run_turn(self, conv: dict, task: str) -> None:
        cid = conv["id"]
        stop = asyncio.Event()
        # Steering inbox: texts submitted while this turn runs (see steer);
        # the kernel drains it at step boundaries and injects them as user
        # messages. The wrap-up step skips the drain — leftovers are
        # reported below instead of vanishing.
        inbox: asyncio.Queue = asyncio.Queue()
        handle = {"task": asyncio.current_task(), "stop": stop,
                  "inbox": inbox}
        existing = self.turns.get(cid)
        if existing is not None and existing.get("task") not in (None, handle["task"]):
            self._emit(cid, {"type": "error",
                             "message": "该会话已有进行中的轮次"})
            return
        self.turns[cid] = handle
        # auto-title a fresh session from its first task
        if (conv.get("title") or "新会话") == "新会话":
            title = auto_title(task)
            conv["title"] = title
            self.sessions.rename(cid, title)
        self._emit(cid, {"type": "session_busy", "conv_id": cid, "task": task})
        rid: str | None = None
        status: str | None = None
        try:
            rows = self.history_rows(cid)
            history = replay_messages(rows) if rows else None
            rid, done, _ = await execute(
                self.cfg,
                task,
                history=history,
                conversation_id=cid,
                on_event=lambda ev: self._emit(cid, ev),
                stop=stop,
                inbox=inbox,
                approver=self.approver,
            )
            status = done.get("status")
            if self.current and self.current["id"] == cid:
                self.sessions.set_meta(
                    cid, {"model": self.cfg.model, "profile": self.cfg.profile}
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self._emit(cid, {"type": "error", "message": f"运行出错：{exc}"})
            status = "failed"
        finally:
            self.turns.pop(cid, None)
            leftover = 0
            while not inbox.empty():
                try:
                    inbox.get_nowait()
                    leftover += 1
                except asyncio.QueueEmpty:
                    break
            if leftover:
                self._emit(cid, {
                    "type": "command_output", "style": "warn",
                    "text": f"（{leftover} 条运行中输入未能在本轮结束前注入，已丢弃）",
                })
            self._emit(cid, {"type": "session_idle", "conv_id": cid,
                             "run_id": rid, "status": status})

    async def submit(self, task: str) -> asyncio.Task | None:
        """Spawn a turn for the current session (TUI path)."""
        if self.current is None:
            self.new_session()
        cid = self.current["id"]
        if cid in self.turns:
            self._emit(cid, {
                "type": "error", "message": "本轮还在进行中，请等它结束或 Ctrl+C 取消",
            })
            return None
        # reserve the slot immediately; _run_turn fills in task/stop
        self.turns[cid] = {"task": None, "stop": None}
        return asyncio.get_running_loop().create_task(
            self._run_turn(self.current, task)
        )

    async def run_turn(self, task: str) -> None:
        """Run one turn to completion (plain REPL path)."""
        if self.current is None:
            self.new_session()
        cid = self.current["id"]
        if cid in self.turns:
            self._emit(cid, {
                "type": "error", "message": "本轮还在进行中，请等它结束或 Ctrl+C 取消",
            })
            return
        self.turns[cid] = {"task": None, "stop": None}
        try:
            await self._run_turn(self.current, task)
        finally:
            self.turns.pop(cid, None)

    def cancel(self, conv_id: int | None = None) -> bool:
        cid = self._cid(conv_id)
        if cid is None:
            return False
        handle = self.turns.get(cid)
        if handle and handle.get("stop") is not None:
            handle["stop"].set()
            return True
        return False

    def steer(self, text: str, conv_id: int | None = None) -> bool:
        """Queue a user text into the running turn's steering inbox.

        The kernel drains the inbox at step boundaries and injects each
        text as a user message (announced via ``user_injected`` events),
        so the model incorporates it on its next call. False when no turn
        is running for the session (the caller falls back to normal
        submit behavior).
        """
        if not text.strip():
            return False
        cid = self._cid(conv_id)
        if cid is None:
            return False
        handle = self.turns.get(cid)
        inbox = handle.get("inbox") if handle else None
        if inbox is None:
            return False
        inbox.put_nowait(text)
        return True

    # -- undo -------------------------------------------------------------------

    async def _undo(self, run_id: str | None) -> None:
        rid = run_id
        if rid is None:
            if self.current is None:
                self._emit(-1, {"type": "error", "message": "当前没有会话"})
                return
            runs = [
                r for r in self.sessions.runs(self.current["id"])
                if r.status in ("done", "cancelled", "max_steps")
            ]
            if not runs:
                self._emit(-1, {"type": "error",
                                "message": "当前会话还没有可撤销的 run"})
                return
            rid = runs[-1].run_id
        reverted = await undo(self.cfg, rid, quiet=True)
        run = self.store.get_run(rid, self.cfg.user_id)
        cid = (run.conversation_id if run is not None else None) or -1
        self._emit(cid, {"type": "undo_done", "run_id": rid,
                         "reverted": reverted})

    # -- session-adjustable settings (/set) -------------------------------------

    def settings_rows(self) -> list[dict]:
        """Current values of the /set-able knobs, in display order."""
        return [
            {"key": key, "label": label, "kind": kind,
             "value": getattr(self.cfg, attr)}
            for key, attr, label, kind in _SETTING_DEFS
        ]

    def _setting_def(self, key: str) -> tuple[str, str, str, str] | None:
        """Resolve a /set argument (user key, underscore variant, or 序号)."""
        norm = key.strip().lower().replace("_", "-")
        for defn in _SETTING_DEFS:
            if defn[0] == norm:
                return defn
        if key.strip().isdigit():
            idx = int(key.strip()) - 1
            if 0 <= idx < len(_SETTING_DEFS):
                return _SETTING_DEFS[idx]
        return None

    def set_setting(self, key: str, value: str = "") -> ActionResult:
        """Flip one /set-able knob on the shared cfg; applies next turn.

        Booleans accept on/off (bare = toggle); numerics need a value.
        Tool-affecting flips invalidate the /tools cache; turning shell on
        restates its trust warning so the escalation is never silent.
        """
        result = ActionResult()
        defn = self._setting_def(key)
        if defn is None:
            names = "、".join(d[0] for d in _SETTING_DEFS)
            result.say("err", f"未知设置项 {key!r}（可用：{names}）")
            return result
        ukey, attr, label, kind = defn
        current = getattr(self.cfg, attr)
        if kind == "bool":
            token = value.strip().lower()
            if not token:
                new = not current  # bare invocation toggles
            elif token in _TRUTHY:
                new = True
            elif token in _FALSY:
                new = False
            else:
                result.say("err", f"{ukey} 是开关：on / off（不带值则为切换）")
                return result
        elif kind == "enum":
            token = value.strip()
            if not token:
                known = "、".join(REASONING_LEVELS)
                result.say("err", f"用法：/reasoning 级别（{known}；当前 "
                                  f"{current or 'off'}）")
                return result
            # "off" clears the knob (nothing sent); other tokens pass
            # through verbatim — which values the model accepts is the
            # endpoint's call (400 diagnostics catch mistakes).
            new = None if token.lower() == "off" else token
        else:
            token = value.strip().lower()
            if not value.strip():
                result.say("err", f"用法：/set {ukey} 值（当前 {current}）")
                return result
            if token in ("off", "none") and ukey in _NULLABLE:
                new = None  # clear back to "unset / endpoint default"
            else:
                try:
                    new = int(value.strip()) if kind == "int" \
                        else float(value.strip())
                except ValueError:
                    result.say(
                        "err",
                        f"{ukey} 需要一个{'整数' if kind == 'int' else '数值'}"
                        "（off 清除）")
                    return result
                # positivity, except temperature where 0 is a valid setting
                if ukey != "temperature" and (
                        kind == "int" and new < 1 or kind == "float" and new <= 0):
                    result.say("err", f"{ukey} 必须为正数")
                    return result
        if new == current:
            result.say("dim", f"{label}：已是 {new if new is not None else 'off'}")
            return result
        setattr(self.cfg, attr, new)
        if ukey in _TOOL_AFFECTING:
            self._tool_names = None  # /tools must re-derive the registry
            result.changed = True
        shown = "on" if new is True else "off" \
            if new is False or new is None else new
        was = "on" if current is True else "off" \
            if current is False or current is None else current
        result.say("ok", f"{ukey}：{was} → {shown}，下一轮生效")
        if ukey == "shell" and new is True:
            result.say("warn", "run_command 以当前用户权限执行、非沙箱、结果不可撤销")
        elif ukey == "code" and new is True:
            result.say("dim", "（无 bubblewrap 的环境回退为非沙箱直通执行）")
        elif ukey == "subagents" and new is True:
            result.say("warn", "委派子代理会成倍放大 token 花费；并行子任务共享同一预算上限")
        return result

    # -- dispatch -----------------------------------------------------------------

    def dispatch(self, line: str) -> ActionResult:
        text = line.strip()
        if not text:
            return ActionResult()
        if not text.startswith("/"):
            if text in ("exit", "quit"):  # plain-REPL muscle memory
                return ActionResult(action="exit")
            if self.busy():
                out = ActionResult()
                out.say("warn", "本轮还在进行中，请等它结束或 Ctrl+C 取消")
                return out
            return ActionResult(action="submit", text=text)
        cmd, _, arg = text.partition(" ")
        cmd = cmd[1:].lower()
        arg = arg.strip()
        return self._dispatch(cmd, arg)

    def _dispatch(self, cmd: str, arg: str) -> ActionResult:
        result = ActionResult()
        if cmd in ("exit", "quit"):
            return ActionResult(action="exit")
        if cmd == "help":
            result.messages.extend(help_text())
            return result
        if cmd == "tools":
            if self._tool_names is None:
                self._tool_names = tool_names(self.cfg)
            result.say("dim", "已注册工具：" + "、".join(self._tool_names))
            return result
        if cmd == "new":
            row = self.new_session(arg or None)
            result.say("dim", f"已开始新会话 #{row['id']}（/resume 可切回）")
            result.changed = True
            result.overlay = "rebuild"
            return result
        if cmd == "rename":
            if not arg:
                result.say("err", "用法：/rename 标题")
                return result
            return self.rename_session(arg)
        if cmd in ("sessions", "resume") or cmd == "session":
            if arg and cmd == "resume":
                return self.switch_session(arg)
            rows = self.session_list()
            busy = self.busy_ids()
            for line in format_session_rows(rows, self._cid(), busy):
                result.say("dim", line)
            result.overlay = "sessions"
            return result
        if cmd == "model":
            if not arg:
                entries = self.model_entries()
                result.say("dim", f"当前：{self.cfg.profile or 'env'} · {self.cfg.model}")
                section = group = None
                for i, entry in enumerate(entries, 1):
                    # fav/recent heads render once per section; profile
                    # heads once per profile group
                    changed = ((entry["section"] != section)
                               if entry["section"] in ("fav", "recent")
                               else ((entry["section"], entry["group"])
                                     != (section, group)))
                    if changed:
                        section, group = entry["section"], entry["group"]
                        if section == "fav":
                            result.say("dim", "── ★ 常用 ──")
                        elif section == "recent":
                            result.say("dim", "── 最近 ──")
                        else:
                            try:
                                ep = self.profiles.endpoint(group)
                            except SystemExit:
                                ep = {}
                            dialect = self._endpoint_dialect(ep)
                            tag = f"（{dialect}）" if dialect else ""
                            result.say("dim", f"── {group}{tag} ──")
                    active = (entry["profile"] == self.cfg.profile
                              and entry["model"] == self.cfg.model)
                    mark = "●" if active else " "
                    star = "★" if entry["favorite"] else " "
                    label = (f"{entry['profile']}:{entry['model']}"
                             if section in ("fav", "recent")
                             else entry["model"])
                    result.say("dim", f" {mark} {i}. {star}{label}")
                if not any(e["section"] == "fav" for e in entries):
                    result.say("dim", "（/fav 档案:模型 收藏常用，列表顶部直达）")
                result.say("dim", "（/model 名称 或 档案:模型 或 序号；--save 存为档案默认）")
                result.overlay = "model"
                return result
            save = "--save" in arg
            name = arg.replace("--save", "").strip()
            return self.set_model(name, save=save)
        if cmd == "models":
            scope = "all" if arg.strip().lower() == "all" else "current"
            if scope == "all":
                result.say("dim", "正在并行拉取全部档案的模型列表…")
            else:
                result.say("dim", f"正在从 {self.cfg.base_url} 拉取模型列表…")
            result.awaitable = (lambda: self._fetch_and_report(scope))
            return result
        if cmd == "fav":
            return self.fav_command(arg)
        if cmd == "profile":
            if not arg:
                names = self.profiles.names()
                if not names:
                    result.say("dim", "没有已保存档案（lithe-cli config 配置）")
                    return result
                for name in names:
                    mark = "*" if name == self.cfg.profile else " "
                    ep = self.profiles.masked(name)
                    dialect = self._endpoint_dialect(ep)
                    suffix = f"（{dialect} · key {ep.get('api_key')}）" \
                        if dialect else f"（key {ep.get('api_key')}）"
                    result.say(
                        "dim",
                        f"{mark} {name} · {ep.get('model')} @ {ep.get('base_url')}"
                        f"{suffix}",
                    )
                result.say("dim", "（/profile 名称 切换；provider/协议为 preset 推断，仅供识别）")
                return result
            return self.set_profile(arg)
        if cmd == "undo":
            result.awaitable = lambda: self._undo(arg or None)
            return result
        if cmd == "set":
            if not arg:
                for row in self.settings_rows():
                    value = row["value"]
                    shown = "on" if value is True else "off" \
                        if value is False or value is None else value
                    hint = "" if row["kind"] != "enum" \
                        else "（/reasoning 或 F6 调整）"
                    result.say("dim",
                               f"  {row['key']:<10} {str(shown):<6} "
                               f"{row['label']}{hint}")
                result.say("dim", "（/set 名称 on|off 或 /set 序号；数值项 /set 名称 值）")
                result.overlay = "set"
                return result
            key, _, value = arg.partition(" ")
            return self.set_setting(key, value)
        if cmd == "reasoning":
            save = "--save" in arg
            level = arg.replace("--save", "").strip()
            return self.set_reasoning(level, save=save)
        if cmd == "copy":
            scope = parse_copy_scope(arg)
            if scope is None:
                result.say("err", f"用法：/copy [{COPY_SCOPE_HELP}]"
                                  "（不写参数即最后一条回答）")
                return result
            result.copy_scope = scope
            return result
        if cmd == "sidebar":
            result.toggle_sidebar = True
            return result
        result.say("err", f"未知命令 /{cmd}（/help 查看可用命令）")
        return result

    def fav_command(self, arg: str) -> ActionResult:
        """``/fav`` — the quick-switch lane across providers.

        Bare: list favorites as qualified refs (the same strings the ★
        section of ``/model`` numbers first). With a ``profile:model`` ref:
        toggle. The ref must name a saved profile — a typo'd favorite is an
        error, not silent garbage in the config.
        """
        result = ActionResult()
        arg = arg.strip()
        if not arg:
            favs = self.profiles.favorites()
            if not favs:
                result.say("dim", "还没有收藏（/fav 档案:模型 或选择器内 a 键收藏）")
                return result
            for i, ref in enumerate(favs, 1):
                profile, _, model = ref.partition(":")
                active = profile == self.cfg.profile and model == self.cfg.model
                result.say("dim",
                           f" {'●' if active else ' '} {i}. {ref}")
            result.say("dim", "（/model 序号直达；/fav 档案:模型 再执行一次即取消）")
            return result
        state = self.profiles.toggle_favorite(arg)
        if state is None:
            known = "、".join(self.profiles.names()) or "（无）"
            result.say("err", f"{arg!r} 不是可收藏的引用（用 档案:模型；"
                              f"已存档案：{known}）")
            return result
        profile, _, model = arg.partition(":")
        if state:
            result.say("ok", f"已收藏 {profile}:{model.strip()}（/model 列表顶部直达）")
        else:
            result.say("dim", f"已取消收藏 {profile}:{model.strip()}")
        return result

    async def _fetch_and_report(self, scope: str = "current") -> None:
        res = await self.fetch_model_list(scope)
        cid = self._cid() or -1
        for cls, text in res.messages:
            self._emit(cid, {"type": "command_output", "style": cls,
                             "text": text})
